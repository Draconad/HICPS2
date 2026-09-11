import Foundation

struct ProgramInfo: Decodable, Equatable {
    var number: Int?
    var name: String?
    var comment: String?

    var title: String {
        if let name, !name.isEmpty { return name }
        if let number { return String(format: "O%04d", number) }
        return "—"
    }
}

struct PathInfo: Decodable, Equatable, Identifiable {
    var path: Int
    var name: String
    var mode: String?
    var run: String?
    var id: Int { path }
}

struct AlarmEvent: Decodable, Equatable, Identifiable, Hashable {
    var id: String
    var path: Int?
    var pathName: String?
    var code: String?
    var type: Int?
    var typeName: String?
    var number: Int?
    var axis: Int?
    var message: String?
    var startedAt: Double
    var clearedAt: Double?
    var durationS: Double?
    var active: Bool?

    var isActive: Bool { active ?? (clearedAt == nil) }
    var started: Date { Date(timeIntervalSince1970: startedAt) }
    var cleared: Date? { clearedAt.map { Date(timeIntervalSince1970: $0) } }
    var displayCode: String { code ?? "ALARM" }
    var displayMessage: String {
        let m = (message ?? "").trimmingCharacters(in: .whitespaces)
        return m.isEmpty ? (typeName ?? "Alarm") : m
    }
    /// Short one-liner for the Live Activity / notifications
    var summary: String { "\(displayCode) \(displayMessage)" }
}

struct MachineStatus: Decodable, Equatable {
    var serverTime: Double
    var machineName: String
    var state: MachineStateKind
    var stateDetail: String?
    var stateSince: Double?
    var agentOnline: Bool
    var agentLastSeen: Double?
    var machineConnected: Bool
    var demo: Bool?
    var parts: Int?
    var partsRequired: Int?
    var partsTotal: Int?
    var lastCycleS: Double?
    var cycleTimerS: Double?
    var cycleStartedAt: Double?
    var etaS: Double?
    var program: ProgramInfo?
    var paths: [PathInfo]?
    var activeAlarms: [AlarmEvent]
    var alarmsToday: Int?

    /// Seconds to add to server timestamps to get phone time (clock drift between Unraid and the phone).
    var clockOffset: Double = 0

    enum CodingKeys: String, CodingKey {
        case serverTime, machineName, state, stateDetail, stateSince, agentOnline, agentLastSeen, machineConnected,
             demo, parts, partsRequired, partsTotal, lastCycleS, cycleTimerS, cycleStartedAt, etaS, program, paths,
             activeAlarms, alarmsToday
    }

    func date(_ serverEpoch: Double?) -> Date? {
        serverEpoch.map { Date(timeIntervalSince1970: $0 + clockOffset) }
    }

    var stateSinceDate: Date? { date(stateSince) }
    var cycleStart: Date? { state == .running ? date(cycleStartedAt) : nil }
    var progress: Double {
        guard let p = parts, let r = partsRequired, r > 0 else { return 0 }
        return min(1, Double(p) / Double(r))
    }
    var remaining: Int? {
        guard let p = parts, let r = partsRequired, r > 0 else { return nil }
        return max(0, r - p)
    }
}

struct AlarmPage: Decodable {
    var alarms: [AlarmEvent]
    var nextBefore: Double?
}

struct HealthResponse: Decodable {
    var ok: Bool
    var version: String?
}
