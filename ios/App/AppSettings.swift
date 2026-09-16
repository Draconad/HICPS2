import Foundation

/// UserDefaults keys shared by @AppStorage in views and by non-view code.
enum SettingsKey {
    static let serverURL = "serverURL"
    static let apiKey = "apiKey"
    static let pollInterval = "pollInterval"
    static let backgroundInterval = "backgroundInterval"
    static let keepAlive = "keepAliveInBackground"
    static let liveActivity = "liveActivityEnabled"
    static let notifyAlarms = "notifyAlarms"
    static let notifyStopped = "notifyStopped"
    static let notifyOff = "notifyOff"
    static let showCamera = "showCameraOnStatus"
    static let notifyMessages = "notifyMessages"
    static let notifyComplete = "notifyJobComplete"
    static let notifyBarChange = "notifyLongBarChange"
    static let onlyWhileRunning = "notifyOnlyWhileRunning"
    static let reportPeriod = "reportPeriod"
    static let notifyBattery = "notifyBatteryDue"
}

enum AppSettings {
    static let defaults = UserDefaults.standard

    static func registerDefaults() {
        defaults.register(defaults: [
            SettingsKey.serverURL: "http://tower.local:8420",
            SettingsKey.apiKey: "",
            SettingsKey.pollInterval: 3.0,
            SettingsKey.backgroundInterval: 10.0,
            SettingsKey.keepAlive: true,    // automatically skipped while Apple push is working
            SettingsKey.liveActivity: true,
            SettingsKey.notifyAlarms: true,
            SettingsKey.notifyStopped: false,
            SettingsKey.notifyOff: false,
            SettingsKey.showCamera: true,
            SettingsKey.notifyMessages: true,
            SettingsKey.notifyComplete: true,
            SettingsKey.notifyBarChange: true,
            SettingsKey.onlyWhileRunning: false,
            SettingsKey.reportPeriod: "day",
            SettingsKey.notifyBattery: true,
        ])
    }

    static var serverURL: String { defaults.string(forKey: SettingsKey.serverURL) ?? "" }
    static var apiKey: String { defaults.string(forKey: SettingsKey.apiKey) ?? "" }
    static var pollInterval: Double { max(1, defaults.double(forKey: SettingsKey.pollInterval)) }
    static var backgroundInterval: Double { max(3, defaults.double(forKey: SettingsKey.backgroundInterval)) }
    static var keepAlive: Bool { defaults.bool(forKey: SettingsKey.keepAlive) }
    static var liveActivity: Bool { defaults.bool(forKey: SettingsKey.liveActivity) }
    static var notifyAlarms: Bool { defaults.bool(forKey: SettingsKey.notifyAlarms) }
    static var notifyStopped: Bool { defaults.bool(forKey: SettingsKey.notifyStopped) }
    static var notifyMessages: Bool { defaults.bool(forKey: SettingsKey.notifyMessages) }
    static var notifyOff: Bool { defaults.bool(forKey: SettingsKey.notifyOff) }
    static var notifyComplete: Bool { defaults.bool(forKey: SettingsKey.notifyComplete) }
    static var notifyBarChange: Bool { defaults.bool(forKey: SettingsKey.notifyBarChange) }
    /// Notifications only while the machine is running (+15 s after it stops). Live Activities are unaffected.
    static var onlyWhileRunning: Bool { defaults.bool(forKey: SettingsKey.onlyWhileRunning) }
    /// Backup battery due / overdue.
    static var notifyBattery: Bool { defaults.bool(forKey: SettingsKey.notifyBattery) }

    /// Which period the Report tab last showed: day, month or year.
    static var reportPeriod: String {
        get { defaults.string(forKey: SettingsKey.reportPeriod) ?? "day" }
        set { defaults.set(newValue, forKey: SettingsKey.reportPeriod) }
    }
}
