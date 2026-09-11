import Foundation
import UIKit

/// Apple push (paid developer account): sends this device's push tokens to the Unraid server so it can
///  * update the Live Activity / Dynamic Island while the app is closed,
///  * start a new Live Activity by itself (push-to-start, iOS 17.2+),
///  * send alarm notifications even when the app isn't running.
@MainActor
final class PushManager: ObservableObject {
    static let shared = PushManager()

    @Published private(set) var deviceToken: String?
    @Published private(set) var serverPushEnabled = false
    @Published private(set) var lastError: String?
    @Published private(set) var lastRegistered: Date?

    private let cacheKey = "pushTokenCache"   // "kind|activityID" -> token

    /// True when the server is sending our alarm notifications, so the app shouldn't post its own.
    var serverHandlesPush: Bool { serverPushEnabled && deviceToken != nil }

    /// Development-signed builds talk to Apple's sandbox push servers; TestFlight/App Store use production.
    static let apsEnvironment: String = {
        guard let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
              let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .isoLatin1),
              let r = text.range(of: "<key>aps-environment</key>") else { return "production" }
        return text[r.upperBound...].prefix(120).contains("development") ? "sandbox" : "production"
    }()

    private var cache: [String: String] {
        get { UserDefaults.standard.dictionary(forKey: cacheKey) as? [String: String] ?? [:] }
        set { UserDefaults.standard.set(newValue, forKey: cacheKey) }
    }

    var prefs: [String: Bool] {
        ["alarms": AppSettings.notifyAlarms, "stopped": AppSettings.notifyStopped, "off": AppSettings.notifyOff]
    }

    // MARK: - from the app delegate

    func didRegister(deviceToken data: Data) {
        let token = data.hexString
        deviceToken = token
        lastError = nil
        EventLog.shared.add("APNs device token received")
        register(kind: "alert", token: token, activityID: nil)
    }

    func didFail(_ error: Error) {
        lastError = "Push not available: \(error.localizedDescription)"
        EventLog.shared.add("APNs registration failed: \(error.localizedDescription)")
    }

    // MARK: - server registration

    func register(kind: String, token: String, activityID: String?) {
        var c = cache
        c["\(kind)|\(activityID ?? "")"] = token
        if kind == "la_start" {   // only the newest push-to-start token matters
            c = c.filter { !$0.key.hasPrefix("la_start|") || $0.value == token }
        }
        cache = c
        Task { await send(kind: kind, token: token, activityID: activityID) }
    }

    func unregister(activityID: String) {
        var c = cache
        let key = "la|\(activityID)"
        c.removeValue(forKey: key)
        cache = c
        Task { try? await APIClient.current.pushUnregister(activityID: activityID) }
    }

    /// Re-send everything (on launch, after changing the server address or notification settings).
    func registerAll() {
        for (key, token) in cache {
            let parts = key.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
            let kind = parts.first ?? ""
            let activity = parts.count > 1 && !parts[1].isEmpty ? parts[1] : nil
            if kind == "la", let activity, !LiveActivityManager.isRunning(id: activity) {
                var c = cache; c.removeValue(forKey: key); cache = c
                continue
            }
            Task { await send(kind: kind, token: token, activityID: activity) }
        }
    }

    private func send(kind: String, token: String, activityID: String?) async {
        do {
            let enabled = try await APIClient.current.pushRegister(kind: kind, token: token, activityID: activityID,
                                                                   env: Self.apsEnvironment,
                                                                   prefs: kind == "alert" ? prefs : nil)
            serverPushEnabled = enabled
            lastRegistered = Date()
            EventLog.shared.add("registered \(kind) token with server (server push \(enabled ? "on" : "OFF"))")
        } catch {
            lastError = "Couldn't register with server: \(error.localizedDescription)"
            EventLog.shared.add("push register (\(kind)) failed: \(error.localizedDescription)")
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        application.registerForRemoteNotifications()
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { @MainActor in PushManager.shared.didRegister(deviceToken: deviceToken) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in PushManager.shared.didFail(error) }
    }
}
