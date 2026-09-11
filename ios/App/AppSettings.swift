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
}

enum AppSettings {
    static let defaults = UserDefaults.standard

    static func registerDefaults() {
        defaults.register(defaults: [
            SettingsKey.serverURL: "http://tower.local:8420",
            SettingsKey.apiKey: "",
            SettingsKey.pollInterval: 3.0,
            SettingsKey.backgroundInterval: 10.0,
            SettingsKey.keepAlive: false,   // only needed without Apple push (free Apple ID)
            SettingsKey.liveActivity: true,
            SettingsKey.notifyAlarms: true,
            SettingsKey.notifyStopped: false,
            SettingsKey.notifyOff: false,
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
    static var notifyOff: Bool { defaults.bool(forKey: SettingsKey.notifyOff) }
}
