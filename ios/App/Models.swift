import Foundation
import SwiftUI

struct ProgramInfo: Decodable, Equatable {
    var number: Int?
    var name: String?
    var comment: String?
    var key: String?            // "O3110" - what names and bar counts are stored against
    var customName: String?     // your own name for it ("EMS301"), set in the app or on the dashboard
    var label: String?          // "O3110 - EMS301"
    var avgCycleS: Double?      // average part-to-part time of this program's recent parts
    var avgCycleParts: Int?
    var stickOut: Double?       // setup sheet: sub spindle stick out, in mm
    var hasDoc: Bool?           // there's a job PDF for it

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
    var hasClip: Bool?          // a few seconds of camera video around it were saved

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
    /// Jobs on a schedule - the machine's backup battery to start with
    var maintenance: [MaintenanceItem]?

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
        var nextBarAt: Double?     // when this bar is expected to run out (server clock)
        var nextBarInS: Double?
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
             finishAt, jobComplete, barChanges, runningEndedAt, overProducing, maintenance
    }

    /// The one to warn about: overdue first, then due soon.
    var maintenanceDue: MaintenanceItem? {
        let items = maintenance ?? []
        return items.first { $0.isOverdue } ?? items.first { $0.state == "soon" }
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
    var avgCycleS: Double?
    var avgCycleParts: Int?
    var cyclesRecorded: Int?
    var barsRecorded: Int?
    var firstBarAt: Double?
    var lastBarAt: Double?
    var loaded: Bool?
    var stickOut: Double?           // sub spindle stick out for this job, in mm
    var hasDoc: Bool?               // there's a job PDF (dimensions) stored for it
    var docName: String?
    var docAt: Double?
    var docSize: Int?
    var bars: [BarRecord]?          // detail only

    var id: String { program }
    var ppbInUse: Double? { ppbManual ?? ppbLearnt }
    var stickOutText: String? {
        guard let v = stickOut else { return nil }
        return (v == v.rounded() ? String(Int(v)) : String(format: "%.2f", v).replacingOccurrences(of: "0$", with: "",
                                                                                                  options: .regularExpression)) + " mm"
    }

    struct BarRecord: Decodable, Equatable, Identifiable {
        var id: Int
        var parts: Int
        var startedAt: Double
        var endedAt: Double
    }
}

/// One day's bar changes (the dropdown in the Bar changes card)
struct BarDay: Decodable {
    var date: String?
    var changes: Int?
    var parts: Int?
    var bars: [Change]

    struct Change: Decodable, Identifiable {
        var at: Double                 // when the bar was changed
        var durationS: Double?         // how long the change took
        var parts: Int?                // parts made by the bar that just finished
        var partial: Bool?             // more than one program ran on it: left out of the averages
        var barId: Int?
        var programs: [Part]?

        var id: Double { at }

        struct Part: Decodable, Hashable {
            var program: String?
            var label: String?
            var parts: Int?
        }
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


// MARK: - Operations report

/// Parts, bars and machine time by day, month or year (Report tab).
struct OpsReport: Decodable {
    var period: String
    var buckets: [Bucket]
    var totals: Totals
    var from: String?

    struct Bucket: Decodable, Identifiable {
        var key: String
        var label: String
        var parts: Int
        var bars: Int
        var runS: Double
        var standbyS: Double
        var alarmS: Double
        var offS: Double
        var days: Int?
        var avgCycleS: Double?
        var partsPerHour: Double?
        var programs: [ProgramTotal]

        var id: String { key }
        var onS: Double { runS + standbyS + alarmS }
        var utilisation: Double? { onS > 60 ? runS / onS : nil }
    }

    struct Totals: Decodable {
        var parts: Int
        var bars: Int
        var runS: Double
        var standbyS: Double
        var alarmS: Double
        var offS: Double
        var programs: [ProgramTotal]
    }

    struct ProgramTotal: Decodable, Identifiable {
        var program: String?
        var label: String?
        var parts: Int
        var bars: Int
        var runS: Double
        var avgCycleS: Double?

        var id: String { (program ?? "-") + (label ?? "") }
        var title: String { label ?? program ?? "No program" }
    }
}


/// Something that has to be done every so often - the machine's memory backup battery, to start with.
/// Nothing on the machine warns you before the parameters have already gone, so the date of the last change
/// is kept on the server and counted down from here.
struct MaintenanceItem: Decodable, Equatable, Identifiable {
    var item: String
    var name: String
    var lastAt: Double?
    var everyMonths: Double
    var warnDays: Double?
    var notes: String?
    var history: [Double]?
    var dueAt: Double?
    var daysLeft: Double?
    var state: String            // ok | soon | overdue | unset

    var id: String { item }
    var isOverdue: Bool { state == "overdue" }
    var needsAttention: Bool { state == "overdue" || state == "soon" }
    var last: Date? { lastAt.map { Date(timeIntervalSince1970: $0) } }
    var due: Date? { dueAt.map { Date(timeIntervalSince1970: $0) } }
    var days: Int { Int((daysLeft ?? 0).rounded()) }

    /// "Overdue by 35 days", "Due in 12 days", "365 days left"
    var headline: String {
        switch state {
        case "unset": return "Not set"
        case "overdue": return "Overdue by \(abs(days)) day\(abs(days) == 1 ? "" : "s")"
        case "soon": return "Due in \(days) day\(days == 1 ? "" : "s")"
        default: return "\(days) day\(days == 1 ? "" : "s") left"
        }
    }
}
