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
    private var warnedRestart = false

    /// iOS ends a Live Activity after 8 hours; restart a little before that.
    private let maxAge: TimeInterval = 7.5 * 3600

    var systemEnabled: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    private init() {
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
        stateTask?.cancel()
        stateTask = Task { [weak self] in
            for await st in a.activityStateUpdates {
                if st == .ended || st == .dismissed {
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
                                   cycleStart: nil, program: "", alarms: [], reachable: false, updated: Date())
        }
        var program = s.program?.title ?? ""
        if program == "—" { program = "" }
        return Attrs.ContentState(
            state: s.state,
            detail: s.stateDetail ?? "",
            parts: s.parts,
            required: s.partsRequired,
            lastCycle: s.lastCycleS,
            cycleStart: s.cycleStart,
            program: program,
            alarms: s.activeAlarms.prefix(3).map(\.summary),
            reachable: reachable,
            updated: Date())
    }

    func start(with status: MachineStatus?, machineName: String) {
        guard AppSettings.liveActivity else { return }
        guard systemEnabled else {
            lastError = "Live Activities are turned off for this app in iOS Settings."
            return
        }
        guard activity == nil else { return }
        let state = Self.content(from: status, reachable: status != nil, previous: nil)
        do {
            let a = try Activity.request(attributes: Attrs(machineName: machineName),
                                         content: ActivityContent(state: state, staleDate: staleDate()),
                                         pushType: nil)
            adopt(a, startedAt: Date())
            lastPushed = state
            lastPushTime = Date()
            lastError = nil
            warnedRestart = false
        } catch {
            lastError = "Couldn't start Live Activity: \(error.localizedDescription)"
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
        Date().addingTimeInterval(max(60, AppSettings.backgroundInterval * 6))
    }

    /// Called after every poll.
    func sync(status: MachineStatus?, reachable: Bool, machineName: String) async {
        guard AppSettings.liveActivity else {
            if activity != nil { await stop() }
            return
        }
        let foreground = UIApplication.shared.applicationState == .active

        if let a = activity, let started = startedAt, Date().timeIntervalSince(started) > maxAge {
            // Try to roll over to a fresh activity before iOS kills this one at 8 h.
            if foreground {
                await a.end(nil, dismissalPolicy: .immediate)
                activity = nil
                isActive = false
                start(with: status, machineName: machineName)
                return
            } else if !warnedRestart {
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
        cmpNew.updated = .distantPast
        cmpOld?.updated = .distantPast
        // cycleStart jitters by a few hundred ms between polls; ignore small drift
        if let n = cmpNew.cycleStart, let o = cmpOld?.cycleStart, abs(n.timeIntervalSince(o)) < 3 {
            cmpNew.cycleStart = o
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
        lastPushed = new
        lastPushTime = Date()
    }
}
