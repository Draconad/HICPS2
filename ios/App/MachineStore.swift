import BackgroundTasks
import Foundation
import UIKit

/// Polls the Unraid server and fans the result out to the UI, the Live Activity and notifications.
@MainActor
final class MachineStore: ObservableObject {
    static let refreshTaskID = "local.hanwhamonitor.app.refresh"

    @Published private(set) var status: MachineStatus?
    @Published private(set) var error: String?
    @Published private(set) var lastSuccess: Date?
    @Published private(set) var isLoading = false

    private var loop: Task<Void, Never>?
    private var previousState: MachineStateKind?
    private var isForeground = true
    /// For the Settings diagnostics: proves the app is still alive while locked.
    private(set) var lastBackgroundPoll: Date?

    var machineName: String { status?.machineName ?? "Hanwha XE35" }
    var reachable: Bool { error == nil && status != nil }

    func startPolling() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if AppSettings.keepAlive { BackgroundKeeper.shared.ensurePlaying() }
                if !self.isForeground { self.lastBackgroundPoll = Date() }
                await self.refresh()
                let wait = self.isForeground ? AppSettings.pollInterval : AppSettings.backgroundInterval
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            }
        }
    }

    func stopPolling() {
        loop?.cancel()
        loop = nil
    }

    func restartPolling() {
        stopPolling()
        startPolling()
    }

    func scenePhaseChanged(active: Bool, background: Bool) {
        EventLog.shared.add(active ? "APP FOREGROUND" : (background ? "APP BACKGROUND (low power: \(ProcessInfo.processInfo.isLowPowerModeEnabled))" : "app inactive"))
        isForeground = active
        // Silent audio must already be playing before iOS suspends us, so start it while we're in front.
        if AppSettings.keepAlive { BackgroundKeeper.shared.start() }
        if active {
            // A fresh poll straight away when the app comes back to the front
            Task { await refresh() }
        }
        if background {
            scheduleAppRefresh()
        }
    }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let s = try await APIClient.current.status()
            handleNotifications(new: s)
            status = s
            error = nil
            lastSuccess = Date()
            if !isForeground {
                EventLog.shared.add("bg poll ok: \(s.state.rawValue) \(s.parts.map { String($0) } ?? "-")/\(s.partsRequired.map { String($0) } ?? "-")")
            }
        } catch {
            EventLog.shared.add("poll FAILED\(isForeground ? "" : " (bg)"): \(error.localizedDescription)")
            self.error = error.localizedDescription
        }
        await LiveActivityManager.shared.sync(status: error == nil ? status : nil, reachable: error == nil,
                                              machineName: machineName)
    }

    // MARK: notifications

    private func handleNotifications(new s: MachineStatus) {
        defer { previousState = s.state }
        if AppSettings.notifyAlarms {
            for a in s.activeAlarms where NotificationManager.shared.markAlarmNotified(a.id) {
                // Don't spam about alarms that were already active long before we looked
                guard Date().timeIntervalSince1970 - (a.startedAt + s.clockOffset) < 15 * 60 else { continue }
                NotificationManager.shared.post(
                    id: "alarm-\(a.id)",
                    title: "⚠️ \(s.machineName) — \(a.displayCode)",
                    body: [a.displayMessage, a.pathName.map { "\($0) path" }].compactMap { $0 }.joined(separator: " · "))
            }
        }
        guard let prev = previousState, prev != s.state else { return }
        if AppSettings.notifyStopped, prev == .running, s.state == .standby {
            NotificationManager.shared.post(id: "stopped-\(Int(s.serverTime))", title: "\(s.machineName) stopped",
                                            body: s.stateDetail ?? "Machine is in standby")
        }
        if AppSettings.notifyOff, s.state == .off, prev != .off {
            NotificationManager.shared.post(id: "off-\(Int(s.serverTime))", title: "\(s.machineName) is off",
                                            body: s.stateDetail ?? "No data from the machine")
        }
    }

    // MARK: background refresh fallback (used when "keep alive" is off or iOS stops the audio)

    func scheduleAppRefresh() {
        let req = BGAppRefreshTaskRequest(identifier: Self.refreshTaskID)
        req.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(req)
    }

    func backgroundRefresh() async {
        EventLog.shared.add("BGAppRefresh task woke the app")
        scheduleAppRefresh()
        await refresh()
    }
}
