import Foundation
import SwiftUI

struct ProgramInfo: Decodable, Equatable {
    var number: Int?
    var name: String?
    var comment: String?
    var key: String?            // "O3110" - what names and bar counts are stored against
    var customName: String?     // your own name for it ("EMS301"), set in the app or on the dashboard
    var label: String?          // "O3110 - EMS301"

    var title: String {
        if let label, !label.isEmpty { return label }
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
    var barChange: Bool?
    var barChangeSince: Double?
    /// Work counter "stop at required count" switch: nil = not set up in the monitor
    var workCounter: Bool?
    var camera: CameraInfo?
    var controls: ControlsInfo?
    /// Operator messages on the CNC screen (e.g. "work count end in 1 hour") - information, not alarms
    var messages: [OpMessage]?
    var finishAt: Double?               // estimated time the required count is reached (server clock)
    var jobComplete: JobComplete?       // the last time the count reached the required count
    var barChanges: BarChangeStats?
    var runningEndedAt: Double?
    var overProducing: Bool?

    struct JobComplete: Decodable, Equatable {
        var at: Double
        var parts: Int?
        var required: Int?
    }

    struct BarChangeStats: Decodable, Equatable {
        var today: Int?
        var avgTodayS: Double?
        var avgWeekS: Double?
        var lastS: Double?
        var lastAt: Double?
        var alertAfterS: Double?
        var perBar: PerBar?
    }

    /// Parts per bar, learnt per program (so it's known as soon as a program that has run before is loaded)
    struct PerBar: Decodable, Equatable {
        var program: String?
        var label: String?         // "O3110 - EMS301"
        var avg: Double?           // nil = still learning this program
        var bars: Int?             // how many bars the average is over
        var intoBar: Int?          // parts made on the current bar so far
        var partsLeft: Int?
        var leftOnBar: Int?        // roughly how many more the current bar will make
        var moreBars: Int?         // bars still to load after this one
        var barsTotal: Int?        // when it isn't known how far into the current bar it is
    }

    struct OpMessage: Decodable, Equatable, Identifiable {
        var id: Int
        var number: Int?
        var text: String
        var startedAt: Double
    }

    struct CameraInfo: Decodable, Equatable {
        var available: Bool?
        var ptz: Bool?
        var audio: Bool?
        var presets: [Preset]?      // positions saved in the Tapo app
    }

    struct Preset: Decodable, Equatable, Hashable {
        var token: String
        var name: String
    }

    /// What the PC app allows the Controls tab to change.
    struct ControlsInfo: Decodable, Equatable {
        var remote: Bool?               // "Allow remote changes" ticked on the PC
        var workCounterSignal: Bool?    // the stop-at-count address has been set up
        var connected: Bool?            // the PC's command link to the server is up
    }

    /// Seconds to add to server timestamps to get phone time (clock drift between Unraid and the phone).
    var clockOffset: Double = 0

    enum CodingKeys: String, CodingKey {
        case serverTime, machineName, state, stateDetail, stateSince, agentOnline, agentLastSeen, machineConnected,
             demo, parts, partsRequired, partsTotal, lastCycleS, cycleTimerS, cycleStartedAt, etaS, program, paths,
             activeAlarms, alarmsToday, barChange, barChangeSince, workCounter, camera, controls, messages,
             finishAt, jobComplete, barChanges, runningEndedAt, overProducing
    }

    func date(_ serverEpoch: Double?) -> Date? {
        serverEpoch.map { Date(timeIntervalSince1970: $0 + clockOffset) }
    }

    var stateSinceDate: Date? { state == .barChange ? (date(barChangeSince) ?? date(stateSince)) : date(stateSince) }
    var barChangeStart: Date? { state == .barChange ? date(barChangeSince) : nil }
    var cycleStart: Date? { state.isRunning ? date(cycleStartedAt) : nil }
    var progress: Double {
        guard let p = parts, let r = partsRequired, r > 0 else { return 0 }
        return min(1, Double(p) / Double(r))
    }
    var remaining: Int? {
        guard let p = parts, let r = partsRequired, r > 0 else { return nil }
        return max(0, r - p)
    }
    var isOverProducing: Bool { overProducing == true && state == .running }
    var headline: String { state.label }
    var headlineColor: Color { state.color }

    /// When the required count will be reached at the current cycle time (phone clock), while running
    var finish: Date? { date(finishAt) }

    /// For "only while running": running now, or stopped less than `grace` seconds ago
    func inRunningWindow(grace: Double = 15) -> Bool {
        if state.isRunning { return true }
        guard let ended = runningEndedAt else { return false }
        return serverTime - ended <= grace
    }
}

/// Everything stored about a program (Settings > Program info)
struct ProgramRecord: Decodable, Equatable, Identifiable {
    var program: String
    var name: String?
    var label: String?
    var notes: String?
    var ppbManual: Double?          // parts per bar you typed in (used instead of the learnt one)
    var ppbLearnt: Double?
    var ppbLearntBars: Int?
    var barsRecorded: Int?
    var firstBarAt: Double?
    var lastBarAt: Double?
    var loaded: Bool?
    var bars: [BarRecord]?          // detail only

    var id: String { program }
    var ppbInUse: Double? { ppbManual ?? ppbLearnt }

    struct BarRecord: Decodable, Equatable, Identifiable {
        var id: Int
        var parts: Int
        var startedAt: Double
        var endedAt: Double
    }
}

struct ProgramList: Decodable {
    var programs: [ProgramRecord]
    var loaded: String?
}

struct AlarmPage: Decodable {
    var alarms: [AlarmEvent]
    var nextBefore: Double?
}

struct HealthResponse: Decodable {
    var ok: Bool
    var version: String?
}
