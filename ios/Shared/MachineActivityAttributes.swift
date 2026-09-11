import ActivityKit
import SwiftUI

/// Machine state as reported by the server.
enum MachineStateKind: String, Codable, Hashable, CaseIterable {
    case running, standby, alarm, off
    case barChange = "barchange"

    /// Bar change is part of running a job: the cycle clock keeps going and it isn't a stop.
    var isRunning: Bool { self == .running || self == .barChange }

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
        case .barChange: return "Bar change"
        }
    }

    var color: Color {
        switch self {
        case .running: return Color(red: 0.19, green: 0.78, blue: 0.35)
        case .standby: return Color(red: 1.00, green: 0.78, blue: 0.10)
        case .alarm: return Color(red: 0.96, green: 0.23, blue: 0.23)
        case .off: return Color(red: 0.56, green: 0.58, blue: 0.62)
        case .barChange: return Color(red: 0.20, green: 0.55, blue: 1.00)
        }
    }

    /// Running past the required count: orange, so it stands out from normal running
    static let overProducingColor = Color(red: 0.96, green: 0.46, blue: 0.13)

    var symbol: String {
        switch self {
        case .running: return "play.circle.fill"
        case .standby: return "pause.circle.fill"
        case .alarm: return "exclamationmark.triangle.fill"
        case .off: return "power.circle.fill"
        case .barChange: return "arrow.triangle.2.circlepath.circle.fill"
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
        /// Unix time the current cycle started (while running), so the lock screen can count it up on its own.
        /// Plain numbers rather than Date: the server builds this same JSON for push updates, and Date's
        /// default Codable format (seconds since 2001) is easy to get wrong from outside Swift.
        var cycleStartEpoch: Double?
        var program: String
        var alarms: [String]
        var reachable: Bool
        var updatedEpoch: Double
        /// Server push payloads keep state "running" (so older app builds still understand them) and flag a
        /// bar change separately; `kind` combines the two.
        var barChange: Bool?
        var barChangeStartEpoch: Double?
        /// Work counter "stop at required count": true = on, false = off, nil = not known
        var counterStop: Bool?
        /// The CNC's operator message, if any (e.g. "work count end in 1 hour")
        var message: String?
        /// Estimated time the required count is reached (Unix time, rounded to the minute)
        var finishEpoch: Double?
        /// Still running after reaching the required count (the work counter isn't stopping it)
        var overProducing: Bool?

        var kind: MachineStateKind { barChange == true && state == .running ? .barChange : state }
        var isOverProducing: Bool { overProducing == true && kind == .running }
        /// The state name (overrun stays "Running", in green, with an orange OVERRUN badge beside it)
        var headline: String { kind.label }
        /// Short form for the Dynamic Island
        var shortHeadline: String { kind.label }
        var headlineColor: Color { kind.color }
        var barChangeStart: Date? { barChangeStartEpoch.map { Date(timeIntervalSince1970: $0) } }
        var cycleStart: Date? { cycleStartEpoch.map { Date(timeIntervalSince1970: $0) } }
        var updated: Date { Date(timeIntervalSince1970: updatedEpoch) }

        var progress: Double {
            guard let p = parts, let r = required, r > 0 else { return 0 }
            return min(1, Double(p) / Double(r))
        }

        var remaining: Int? {
            guard let p = parts, let r = required, r > 0 else { return nil }
            return max(0, r - p)
        }

        /// "382 to go · ~2h 20m" (estimate from the last cycle time), "Target reached", or nil with no target
        var remainingText: String? {
            guard let left = remaining else { return nil }
            if left == 0 { return "Target reached" }
            var t = "\(left) to go"
            if let f = finishEpoch, state.isRunning {
                t += " · done ~" + Date(timeIntervalSince1970: f).formatted(date: .omitted, time: .shortened)
            } else if let c = lastCycle, c > 0 {
                t += " · ~" + Fmt.span(Double(left) * c)
            }
            return t
        }

        var partsText: String {
            guard let p = parts else { return "—" }
            if let r = required, r > 0 { return "\(p)/\(r)" }
            return "\(p)"
        }
    }

    var machineName: String
}

/// Orange "OVERRUN" tag shown next to Running when the machine has gone past the required count.
struct OverrunBadge: View {
    var size: CGFloat = 11

    var body: some View {
        Text("OVERRUN")
            .font(.system(size: size, weight: .heavy, design: .rounded))
            .foregroundStyle(Color.black)
            .padding(.horizontal, size * 0.6)
            .padding(.vertical, size * 0.2)
            .background(MachineStateKind.overProducingColor, in: Capsule())
            .padding(size * 0.2)
            .background(Color.black.opacity(0.18), in: Capsule())   // dark ring around it
            .fixedSize()
    }
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
