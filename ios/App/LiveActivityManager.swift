import ActivityKit
import Foundation
import UIKit

/// Starts, updates and restarts the lock screen Live Activity / Dynamic Island.
@MainActor
final class LiveActivityManager: ObservableObject {
    static let shared = LiveActivityManager()

    typealias Attrs = MachineActivityAttributes

    @Published private(set) var isActive = false
    @Published private(set) var lastError: String?

    private var activity: Activity<Attrs>?
    private var startedAt: Date?
    private var lastPushed: Attrs.ContentState?
    private var lastPushTime = Date.distantPast
    private var stateTask: Task<Void, Never>?
    private var tokenTask: Task<Void, Never>?
    private var watchers: [Task<Void, Never>] = []
    private var warnedRestart = false

    /// iOS ends a Live Activity after 8 hours; restart a little before that.
    private let maxAge: TimeInterval = 7.5 * 3600

    var systemEnabled: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    static func isRunning(id: String) -> Bool {
        Activity<Attrs>.activities.contains { $0.id == id && ($0.activityState == .active || $0.activityState == .stale) }
    }

    private init() {
        // Activities the SERVER starts (push-to-start) appear here - the app is woken in the background
        // so it can pick them up and send their update token back to the server.
        watchers.append(Task { [weak self] in
            for await a in Activity<Attrs>.activityUpdates {
                guard let self else { return }
                if self.activity?.id != a.id {
                    EventLog.shared.add("LA appeared (started by push?) \(a.id.prefix(8))")
                    if let old = self.activity {   // the server replaced it (8-hour rollover) - end the old card
                        PushManager.shared.unregister(activityID: old.id)
                        Task { await old.end(nil, dismissalPolicy: .immediate) }
                    }
                    self.adopt(a, startedAt: Date())
                }
            }
        })
        if #available(iOS 17.2, *) {
            watchers.append(Task {
                for await data in Activity<Attrs>.pushToStartTokenUpdates {
                    PushManager.shared.register(kind: "la_start", token: data.hexString, activityID: nil)
                }
            })
        }
        // Re-attach to an activity that survived an app relaunch; end any duplicates.
        let existing = Activity<Attrs>.activities
        if let first = existing.first {
            adopt(first, startedAt: nil)
            for extra in existing.dropFirst() {
                Task { await extra.end(nil, dismissalPolicy: .immediate) }
            }
        }
    }

    private func adopt(_ a: Activity<Attrs>, startedAt: Date?) {
        activity = a
        let key = "liveActivityStarted-\(a.id)"
        if let startedAt {
            UserDefaults.standard.set(startedAt.timeIntervalSince1970, forKey: key)
            self.startedAt = startedAt
        } else {
            let saved = UserDefaults.standard.double(forKey: key)
            self.startedAt = saved > 0 ? Date(timeIntervalSince1970: saved) : Date()
        }
        isActive = true
        tokenTask?.cancel()
        tokenTask = Task {
            for await data in a.pushTokenUpdates {
                PushManager.shared.register(kind: "la", token: data.hexString, activityID: a.id)
            }
        }
        stateTask?.cancel()
        stateTask = Task { [weak self] in
            for await st in a.activityStateUpdates {
                EventLog.shared.add("LA state -> \(st)")
                if st == .ended || st == .dismissed {
                    PushManager.shared.unregister(activityID: a.id)
                    // This Task inherits @MainActor from adopt(), so no MainActor.run hop is needed
                    // (and referencing the weak `self` capture from a nested @Sendable closure is an error).
                    if let self, self.activity?.id == a.id {
                        self.activity = nil
                        self.isActive = false
                    }
                    break
                }
            }
        }
    }

    static func content(from s: MachineStatus?, reachable: Bool, previous: Attrs.ContentState?) -> Attrs.ContentState {
        guard let s else {
            return previous.map { p in var c = p; c.reachable = false; return c } ??
                Attrs.ContentState(state: .off, detail: "Waiting for server", parts: nil, required: nil, lastCycle: nil,
                                   cycleStartEpoch: nil, program: "", alarms: [], reachable: false,
                                   updatedEpoch: Date().timeIntervalSince1970)
        }
        var program = s.program?.title ?? ""
        if program == "—" { program = "" }
        return Attrs.ContentState(
            state: s.state,
            detail: s.stateDetail ?? "",
            parts: s.parts,
            required: s.partsRequired,
            lastCycle: s.lastCycleS,
            cycleStartEpoch: s.cycleStart?.timeIntervalSince1970,
            program: program,
            alarms: s.activeAlarms.prefix(3).map(\.summary),
            reachable: reachable,
            updatedEpoch: Date().timeIntervalSince1970,
            barChangeStartEpoch: s.barChangeStart?.timeIntervalSince1970,
            counterStop: s.workCounter,
            message: s.messages?.first?.text,
            finishEpoch: s.finish.map { ($0.timeIntervalSince1970 / 60).rounded() * 60 },
            overProducing: s.overProducing)
    }

    /// One-line summary shown in Settings to help work out why nothing appears.
    var diagnostics: String {
        let idiom = UIDevice.current.userInterfaceIdiom == .pad ? "iPad app"
            : (UIDevice.current.model.hasPrefix("iPad") ? "iPhone app on iPad" : "iPhone")
        let running = Activity<Attrs>.activities.map { "\($0.activityState)" }.joined(separator: ",")
        return "\(idiom) · iOS \(UIDevice.current.systemVersion) · enabled \(systemEnabled) · "
            + "activities [\(running)] · last start: \(lastAttempt)"
    }
    @Published private(set) var lastAttempt = "never"

    func restart(with status: MachineStatus?, machineName: String) async {
        await stop()
        start(with: status, machineName: machineName)
    }

    func start(with status: MachineStatus?, machineName: String) {
        guard AppSettings.liveActivity else {
            lastAttempt = "skipped (switched off in this app)"
            return
        }
        guard systemEnabled else {
            lastAttempt = "blocked by iOS"
            lastError = "iOS isn't allowing Live Activities for this app. Check Settings › HiCPS-2 › Live Activities (and Settings › Face ID & Passcode › Live Activities on the Lock Screen)."
            return
        }
        guard activity == nil else { lastAttempt = "already running"; return }
        let state = Self.content(from: status, reachable: status != nil, previous: nil)
        do {
            let content = ActivityContent(state: state, staleDate: staleDate())
            let a: Activity<Attrs>
            do {
                // .token lets the server update it through Apple push (paid developer account builds)
                a = try Activity.request(attributes: Attrs(machineName: machineName), content: content, pushType: .token)
            } catch {
                EventLog.shared.add("LA push-token request refused (\(error)) - starting without push")
                a = try Activity.request(attributes: Attrs(machineName: machineName), content: content, pushType: nil)
            }
            adopt(a, startedAt: Date())
            EventLog.shared.add("LA started")
            lastPushed = state
            lastPushTime = Date()
            lastError = nil
            warnedRestart = false
            lastAttempt = "started \(Date().formatted(date: .omitted, time: .shortened))"
        } catch {
            lastAttempt = "failed"
            EventLog.shared.add("LA start FAILED: \(error)")
            lastError = "Couldn't start Live Activity: \(error.localizedDescription) [\(String(describing: error))]"
        }
    }

    func stop() async {
        for a in Activity<Attrs>.activities {
            await a.end(nil, dismissalPolicy: .immediate)
        }
        activity = nil
        isActive = false
    }

    private func staleDate() -> Date {
        // If nothing new arrives in this time, the widget greys itself out ("No update since …").
        // Generous, so a brief Wi-Fi blip or a slow poll doesn't grey the card out.
        // With server push, the server's heartbeat (every 10 min, stale-date +15 min) keeps it fresh after the app
        // is suspended, so a local 3-minute stale date would grey the card out between heartbeats.
        Date().addingTimeInterval(max(PushManager.shared.serverHandlesPush ? 15 * 60 : 180,
                                      AppSettings.backgroundInterval * 12))
    }

    /// Called after every poll.
    func sync(status: MachineStatus?, reachable: Bool, machineName: String) async {
        guard AppSettings.liveActivity else {
            if activity != nil { await stop() }
            return
        }
        // "Only while running" (Settings > Notifications): leave the card as it was when the machine stopped,
        // and don't start one until the machine runs again
        if AppSettings.onlyWhileRunning, !(status?.inRunningWindow() ?? false) { return }
        let foreground = UIApplication.shared.applicationState == .active

        if let a = activity, let started = startedAt, Date().timeIntervalSince(started) > maxAge {
            // Try to roll over to a fresh activity before iOS kills this one at 8 h.
            if foreground {
                await a.end(nil, dismissalPolicy: .immediate)
                activity = nil
                isActive = false
                start(with: status, machineName: machineName)
                return
            } else if !warnedRestart && !PushManager.shared.serverHandlesPush {
                warnedRestart = true
                NotificationManager.shared.post(id: "la-restart", title: "Live Activity ending soon",
                                                body: "Open the monitor app to keep the lock screen status going.")
            }
        }

        if activity == nil {
            if foreground { start(with: status, machineName: machineName) }
            return
        }
        guard let a = activity else { return }

        let new = Self.content(from: status, reachable: reachable, previous: lastPushed)
        var cmpNew = new, cmpOld = lastPushed
        cmpNew.updatedEpoch = 0
        cmpOld?.updatedEpoch = 0
        // cycleStart jitters by a few hundred ms between polls; ignore small drift
        if let n = cmpNew.cycleStartEpoch, let o = cmpOld?.cycleStartEpoch, abs(n - o) < 3 {
            cmpNew.cycleStartEpoch = o
        }
        let changed = cmpNew != cmpOld
        let heartbeatDue = Date().timeIntervalSince(lastPushTime) > 30
        guard changed || heartbeatDue else { return }

        // Light up the Dynamic Island on a new alarm - unless a normal notification is already doing that.
        let alert: AlertConfiguration? = (changed && new.state == .alarm && lastPushed?.state != .alarm
                                          && !AppSettings.notifyAlarms)
            ? AlertConfiguration(title: "\(machineName) alarm",
                                 body: "\(new.alarms.first ?? new.detail)",
                                 sound: .default)
            : nil
        await a.update(ActivityContent(state: new, staleDate: staleDate()), alertConfiguration: alert)
        EventLog.shared.add("LA update pushed\(UIApplication.shared.applicationState == .active ? "" : " (bg)"): \(new.state.rawValue) \(new.partsText)\(changed ? "" : " [heartbeat]")")
        lastPushed = new
        lastPushTime = Date()
    }
}


extension Data {
    var hexString: String { map { String(format: "%02x", $0) }.joined() }
}
