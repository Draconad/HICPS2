import ActivityKit
import SwiftUI

/// Machine state as reported by the server.
enum MachineStateKind: String, Codable, Hashable, CaseIterable {
    case running, standby, alarm, off

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = MachineStateKind(rawValue: raw.lowercased()) ?? .off
    }

    var label: String {
        switch self {
        case .running: return "Running"
        case .standby: return "Standby"
        case .alarm: return "Alarm"
        case .off: return "Off"
        }
    }

    var color: Color {
        switch self {
        case .running: return Color(red: 0.19, green: 0.78, blue: 0.35)
        case .standby: return Color(red: 1.00, green: 0.78, blue: 0.10)
        case .alarm: return Color(red: 0.96, green: 0.23, blue: 0.23)
        case .off: return Color(red: 0.56, green: 0.58, blue: 0.62)
        }
    }

    var symbol: String {
        switch self {
        case .running: return "play.circle.fill"
        case .standby: return "pause.circle.fill"
        case .alarm: return "exclamationmark.triangle.fill"
        case .off: return "power.circle.fill"
        }
    }
}

/// Data shared between the app and the Live Activity / Dynamic Island widget.
struct MachineActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        var state: MachineStateKind
        var detail: String
        var parts: Int?
        var required: Int?
        var lastCycle: Double?
        /// Set while running so the lock screen can count the current cycle up on its own.
        var cycleStart: Date?
        var program: String
        var alarms: [String]
        var reachable: Bool
        var updated: Date

        var progress: Double {
            guard let p = parts, let r = required, r > 0 else { return 0 }
            return min(1, Double(p) / Double(r))
        }

        var partsText: String {
            guard let p = parts else { return "—" }
            if let r = required, r > 0 { return "\(p)/\(r)" }
            return "\(p)"
        }
    }

    var machineName: String
}

enum Fmt {
    /// 83 -> "1:23", 3723 -> "1:02:03"
    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "—" }
        let s = Int(seconds.rounded())
        let h = s / 3600, m = (s % 3600) / 60, sec = s % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
    }

    /// Friendlier long form: "2h 13m", "4m 10s"
    static func span(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "—" }
        let s = Int(seconds.rounded())
        if s >= 86_400 { return "\(s / 86_400)d \((s % 86_400) / 3600)h" }
        if s >= 3600 { return "\(s / 3600)h \((s % 3600) / 60)m" }
        if s >= 60 { return "\(s / 60)m \(s % 60)s" }
        return "\(s)s"
    }
}
