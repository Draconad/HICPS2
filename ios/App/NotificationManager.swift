import Foundation
import UserNotifications

/// Local notifications (no Apple push needed, works with a free Apple ID while the app is alive).
final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationManager()

    private let center = UNUserNotificationCenter.current()
    private let notifiedKey = "notifiedAlarmIDs"

    func setup() {
        center.delegate = self
    }

    func requestPermission() async -> Bool {
        (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
    }

    func authorizationStatus() async -> UNAuthorizationStatus {
        await center.notificationSettings().authorizationStatus
    }

    func post(id: String, title: String, body: String, sound: Bool = true) {
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = body
        if sound { c.sound = .default }
        c.threadIdentifier = "machine"
        center.add(UNNotificationRequest(identifier: id, content: c, trigger: nil))
    }

    /// Returns true the first time an alarm id is seen (persisted, so relaunches don't re-notify).
    func markAlarmNotified(_ id: String) -> Bool {
        var ids = UserDefaults.standard.stringArray(forKey: notifiedKey) ?? []
        if ids.contains(id) { return false }
        ids.append(id)
        if ids.count > 300 { ids.removeFirst(ids.count - 300) }
        UserDefaults.standard.set(ids, forKey: notifiedKey)
        return true
    }

    // Show banners even while the app is open
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound, .list])
    }
}
