import SwiftUI
import UIKit

struct StatusView: View {
    @EnvironmentObject var store: MachineStore
    @Environment(\.scenePhase) private var phase
    @StateObject private var camera = CameraModel()
    @State private var cameraFullScreen = false
    @AppStorage(SettingsKey.showCamera) private var showCamera = true

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 14) {
                    if let err = store.error {
                        ConnectionBanner(message: err, lastSuccess: store.lastSuccess)
                    }
                    if let s = store.status {
                        StateHeader(s: s)
                        if let msgs = s.messages, !msgs.isEmpty {
                            MessagesCard(messages: msgs, offset: s.clockOffset)
                        }
                        if showCamera && s.camera?.available == true {
                            CameraPanel(model: camera, player: camera.player, features: s.camera) {
                                cameraFullScreen = true
                            }
                            if s.camera?.ptz == true, let presets = s.camera?.presets, !presets.isEmpty {
                                PresetBar(model: camera, presets: presets)
                            }
                        }
                        HStack(spacing: 14) {
                            PartsCard(s: s)
                            CycleCard(s: s)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                        ProgramCard(s: s)
                        if let bc = s.barChanges, (bc.today ?? 0) > 0 || bc.lastS != nil {
                            BarChangeCard(stats: bc, s: s)
                        }
                        if !s.activeAlarms.isEmpty {
                            ActiveAlarmsCard(alarms: s.activeAlarms, offset: s.clockOffset)
                        }
                        Footer(s: s, lastSuccess: store.lastSuccess)
                    } else if store.error == nil {
                        ProgressView("Connecting…").padding(.top, 80)
                    }
                }
                .padding(16)
            }
            .background(Color(.systemGroupedBackground))
            .refreshable { await store.refresh() }
            .navigationTitle(store.status?.machineName ?? "Machine")
        }
        // the camera only streams while this screen is showing and the app is in the foreground
        .task(id: phase) {
            guard phase == .active, showCamera else { return }
            await camera.run(store: store)
        }
        .fullScreenCover(isPresented: $cameraFullScreen) {
            CameraFullScreen(model: camera, features: store.status?.camera)
                .environmentObject(store)
                .task { await camera.run(store: store) }   // keeps the video going while the cover is up
        }
    }
}

// MARK: - Cards

/// Operator messages from the CNC (e.g. "work count end in 1 hour") - shown as information, not alarms.
struct MessagesCard: View {
    let messages: [MachineStatus.OpMessage]
    let offset: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(messages) { m in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Image(systemName: "text.bubble.fill").foregroundStyle(Color(red: 0.4, green: 0.7, blue: 1.0))
                    Text(m.text).font(.subheadline.weight(.semibold))
                    Spacer(minLength: 4)
                    Text(Date(timeIntervalSince1970: m.startedAt + offset), style: .relative)
                        .font(.caption).foregroundStyle(.secondary)
                        .multilineTextAlignment(.trailing)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color(red: 0.06, green: 0.13, blue: 0.22), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Color(red: 0.12, green: 0.27, blue: 0.44)))
    }
}

struct Card<Content: View>: View {
    var title: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title {
                Text(title.uppercased())
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            content
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(14)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

struct StateHeader: View {
    let s: MachineStatus

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: s.state.symbol)
                .font(.system(size: 38, weight: .semibold))
                .foregroundStyle(.white)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(s.headline.uppercased())
                        .font(.system(size: 26, weight: .heavy, design: .rounded))
                        .foregroundStyle(.white)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                    if s.isOverProducing {
                        OverrunBadge(size: 13)
                    }
                }
                if let d = s.stateDetail, !d.isEmpty {
                    Text(d)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.white.opacity(0.9))
                        .lineLimit(2)
                }
                if let since = s.stateSinceDate {
                    (Text("Since \(since, style: .time) · ") + Text(since, style: .relative))
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.8))
                }
            }
            Spacer(minLength: 0)
        }
        .padding(18)
        .frame(maxWidth: .infinity)
        .background(
            LinearGradient(colors: [s.headlineColor, s.headlineColor.opacity(0.75)], startPoint: .topLeading, endPoint: .bottomTrailing),
            in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: s.headlineColor.opacity(0.35), radius: 10, y: 4)
        .animation(.easeInOut, value: s.state)
    }
}

struct PartsCard: View {
    let s: MachineStatus

    private var remainingText: String? {
        guard let left = s.remaining else { return nil }
        if left == 0 {
            if let p = s.parts, let r = s.partsRequired, p > r { return "\(p - r) over the required count" }
            return "Target reached"
        }
        var t = "\(left) to go"
        if let eta = s.etaS { t += " · ~" + Fmt.span(eta) }
        return t
    }

    var body: some View {
        Card(title: "Parts") {
            HStack(alignment: .lastTextBaseline, spacing: 4) {
                Text(s.parts.map { String($0) } ?? "—")
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                if let r = s.partsRequired, r > 0 {
                    Text("/ \(r)")
                        .font(.system(size: 18, weight: .semibold, design: .rounded))
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
            if s.partsRequired != nil {
                ProgressView(value: s.progress).tint(s.state.color)
            }
            if let text = remainingText {
                Text(text)
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(Color.primary.opacity(0.85))
            }
            if let finish = s.finish, (s.remaining ?? 0) > 0 {
                Label("Done ~" + finish.formatted(date: .omitted, time: .shortened), systemImage: "flag.checkered")
                    .font(.caption.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            if let on = s.workCounter {
                Label(on ? "Stops at count" : "Won't stop at count",
                      systemImage: on ? "stop.circle.fill" : "infinity.circle")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(on ? Color.green : Color.orange)
            }
        }
    }
}

/// How long bar changes take - a slow one usually means the bar didn't load properly.
struct BarChangeCard: View {
    let stats: MachineStatus.BarChangeStats
    let s: MachineStatus

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.subheadline.weight(.semibold)).monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    var body: some View {
        Card(title: "Bar changes") {
            HStack(alignment: .top) {
                stat("Today", "\(stats.today ?? 0)")
                stat("Average", Fmt.span(stats.avgTodayS ?? stats.avgWeekS))
                stat("Last", Fmt.span(stats.lastS))
            }
            if let last = s.date(stats.lastAt) {
                Text("Last finished \(last.formatted(date: .omitted, time: .shortened))"
                     + (stats.avgWeekS.map { " · 7-day average " + Fmt.span($0) } ?? ""))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct CycleCard: View {
    let s: MachineStatus

    var body: some View {
        Card(title: "Cycle time") {
            Text(Fmt.duration(s.lastCycleS))
                .font(.system(size: 34, weight: .bold, design: .rounded))
                .monospacedDigit()
                .minimumScaleFactor(0.6)
                .lineLimit(1)
            if let start = s.cycleStart {
                HStack(spacing: 4) {
                    Image(systemName: "record.circle").foregroundStyle(s.state.color)
                    Text("Current ") + Text(start, style: .timer)
                }
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
            } else {
                Text("Last part")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct ProgramCard: View {
    let s: MachineStatus

    var body: some View {
        Card(title: "Program") {
            HStack(alignment: .firstTextBaseline) {
                Text(s.program?.title ?? "—")
                    .font(.title2.weight(.bold).monospaced())
                if let c = s.program?.comment, !c.isEmpty {
                    Text(c)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer()
            }
            if let paths = s.paths, !paths.isEmpty {
                HStack(spacing: 8) {
                    ForEach(paths) { p in
                        Text("\(p.name): \(p.mode ?? "") \(p.run ?? "")")
                            .font(.caption.weight(.medium))
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(Color(.tertiarySystemFill), in: Capsule())
                    }
                }
            }
            if let total = s.partsTotal {
                Text("Total parts counter: \(total)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct ActiveAlarmsCard: View {
    let alarms: [AlarmEvent]
    let offset: Double

    var body: some View {
        Card(title: "Active alarms") {
            ForEach(alarms) { a in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(MachineStateKind.alarm.color)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(a.displayCode).font(.subheadline.weight(.bold).monospaced())
                            if let p = a.pathName { PathTag(name: p) }
                        }
                        Text(a.displayMessage).font(.subheadline)
                        let started = a.started.addingTimeInterval(offset)
                        (Text(started, style: .time) + Text(" · for ") + Text(started, style: .relative))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                }
                .padding(10)
                .background(MachineStateKind.alarm.color.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
            }
        }
    }
}

struct PathTag: View {
    let name: String
    var body: some View {
        Text(name)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Color(.tertiarySystemFill), in: Capsule())
    }
}

struct ConnectionBanner: View {
    let message: String
    let lastSuccess: Date?

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "wifi.exclamationmark").font(.title3)
            VStack(alignment: .leading, spacing: 2) {
                Text(message).font(.subheadline.weight(.semibold))
                if let t = lastSuccess {
                    (Text("Last update ") + Text(t, style: .relative) + Text(" ago"))
                        .font(.caption)
                } else {
                    Text("Check the server address in Settings, and that you're on the workshop Wi-Fi.")
                        .font(.caption)
                }
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(.white)
        .padding(12)
        .background(Color.orange, in: RoundedRectangle(cornerRadius: 12))
    }
}

struct Footer: View {
    let s: MachineStatus
    let lastSuccess: Date?

    var body: some View {
        VStack(spacing: 4) {
            if s.demo == true {
                Label("Demo data — the PC app is in demo mode", systemImage: "flask")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.orange)
            }
            HStack(spacing: 6) {
                Circle().fill(s.agentOnline ? Color.green : Color.red).frame(width: 7, height: 7)
                Text(s.agentOnline ? "Monitor PC online" : "Monitor PC offline")
                if let today = s.alarmsToday { Text("· \(today) alarm\(today == 1 ? "" : "s") today") }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.top, 4)
    }
}
