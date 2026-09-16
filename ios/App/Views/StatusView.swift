import SwiftUI
import UIKit

struct StatusView: View {
    @EnvironmentObject var store: MachineStore
    @Environment(\.scenePhase) private var phase
    @StateObject private var camera = CameraModel()
    @State private var cameraFullScreen = false
    @AppStorage(SettingsKey.showCamera) private var showCamera = true
    @AppStorage("cameraCollapsed") private var cameraCollapsed = false
    /// the big status header has scrolled off the top: show the slim status bar there instead
    @State private var pinned = false
    /// ...and once the part count has gone too, the pinned bar shows it as well
    @State private var partsHidden = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 14) {
                    Text(store.status?.machineName ?? "Machine")
                        .font(.largeTitle.weight(.bold))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 4)
                    if let err = store.error {
                        ConnectionBanner(message: err, lastSuccess: store.lastSuccess)
                    }
                    if let s = store.status {
                        // the backup battery, once it's close to (or past) its change date
                        if let due = s.maintenanceDue {
                            BatteryBanner(item: due)
                        }
                        StateHeader(s: s)
                            .background(GeometryReader { g in
                                Color.clear.preference(key: HeaderTopKey.self,
                                                       value: g.frame(in: .named("statusScroll")).minY)
                            })
                            .opacity(pinned ? 0 : 1)     // the pinned bar has taken over
                        if let msgs = s.messages, !msgs.isEmpty {
                            MessagesCard(messages: msgs, offset: s.clockOffset)
                        }
                        if showCamera && s.camera?.available == true {
                            CameraHeader(collapsed: $cameraCollapsed)
                            if !cameraCollapsed {
                                CameraPanel(model: camera, player: camera.player, features: s.camera) {
                                    cameraFullScreen = true
                                }
                                CameraControls(model: camera, features: s.camera)
                            }
                        }
                        HStack(spacing: 14) {
                            PartsCard(s: s)
                            CycleCard(s: s)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                        .background(GeometryReader { g in
                            Color.clear.preference(key: PartsBottomKey.self,
                                                   value: g.frame(in: .named("statusScroll")).maxY)
                        })
                        ProgramCard(s: s)
                        if let bc = s.barChanges, (bc.today ?? 0) > 0 || bc.lastS != nil || bc.perBar != nil {
                            BarChangeCard(stats: bc, s: s)
                        }
                        if !s.activeAlarms.isEmpty {
                            ActiveAlarmsCard(alarms: s.activeAlarms, offset: s.clockOffset)
                        }
                        ControlsCard()
                        Footer(s: s, lastSuccess: store.lastSuccess)
                    } else if store.error == nil {
                        ProgressView("Connecting…").padding(.top, 80)
                    }
                }
                .padding(16)
            }
            .coordinateSpace(name: "statusScroll")
            // switch to the slim bar the moment the status box reaches the status bar / Dynamic Island,
            // so it's never left half-hidden (and unreadable) under it
            .onPreferenceChange(HeaderTopKey.self) { top in
                let hide = top < 1
                if hide != pinned { withAnimation(.easeOut(duration: 0.12)) { pinned = hide } }
            }
            .onPreferenceChange(PartsBottomKey.self) { bottom in
                let gone = bottom < 64          // under the pinned bar
                if gone != partsHidden { withAnimation(.easeOut(duration: 0.18)) { partsHidden = gone } }
            }
            .overlay(alignment: .top) {
                if pinned, let s = store.status {
                    PinnedStatusBar(s: s, showParts: partsHidden)
                        .transition(.opacity)
                } else {
                    // solid strip behind the clock / Dynamic Island, so cards don't show through it as they scroll
                    Color(.systemGroupedBackground)
                        .frame(height: 0)
                        .background(Color(.systemGroupedBackground).ignoresSafeArea(edges: .top))
                }
            }
            .background(Color(.systemGroupedBackground))
            .refreshable { await store.refresh() }
            .toolbar(.hidden, for: .navigationBar)   // the title is part of the page; the status bar pins instead
        }
        // the camera only streams while this screen is showing and the app is in the foreground
        // folding the camera away stops the video too (the PC only streams while someone is watching)
        // one loop only: the full-screen cover used to start a second one on the same model, and closing it
        // stopped the player the first was still using
        .task(id: "\(phase)|\(cameraCollapsed)|\(showCamera)|\(cameraFullScreen)") {
            guard phase == .active, showCamera, !cameraCollapsed || cameraFullScreen else { return }
            await camera.run(store: store)
        }
        .fullScreenCover(isPresented: $cameraFullScreen) {
            CameraFullScreen(model: camera, features: store.status?.camera)
                .environmentObject(store)
        }
    }
}

// MARK: - Cards

/// Operator messages from the CNC (e.g. "work count end in 1 hour") - shown as information, not alarms.
/// "CAMERA ⌄" - tap to fold the video (and its controls) away; remembered.
private struct HeaderTopKey: PreferenceKey {
    static let defaultValue: CGFloat = .infinity
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = min(value, nextValue()) }
}

private struct PartsBottomKey: PreferenceKey {
    static let defaultValue: CGFloat = .infinity
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = min(value, nextValue()) }
}

/// Slim version of the status header, pinned to the top of the screen once the big one has scrolled away.
struct PinnedStatusBar: View {
    let s: MachineStatus
    var showParts = false     // only once the Parts card has scrolled off too

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: s.state.symbol)
                .font(.system(size: 22, weight: .semibold))
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(s.headline.uppercased())
                        .font(.system(size: 17, weight: .heavy, design: .rounded))
                        .lineLimit(1)
                    if s.isOverProducing { OverrunBadge(size: 10) }
                }
                Group {
                    if let since = s.stateSinceDate {
                        Text((s.stateDetail.map { $0 + " · " } ?? "")) + Text(since, style: .relative)
                    } else {
                        Text(s.stateDetail ?? "")
                    }
                }
                .font(.caption.weight(.medium))
                .opacity(0.9)
                .lineLimit(1)
            }
            Spacer(minLength: 8)
            if showParts, let p = s.parts {
                VStack(alignment: .trailing, spacing: 1) {
                    Text(s.partsRequired.map { "\(p)/\($0)" } ?? "\(p)")
                        .font(.system(size: 17, weight: .bold, design: .rounded))
                        .monospacedDigit()
                    if let left = s.remaining, left > 0 {
                        Text("\(left) to go").font(.caption2.weight(.semibold)).opacity(0.85)
                    }
                }
            }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
        .background { s.headlineColor.ignoresSafeArea(edges: .top) }
        .shadow(color: .black.opacity(0.4), radius: 6, y: 3)
    }
}

struct CameraHeader: View {
    @Binding var collapsed: Bool

    var body: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.2)) { collapsed.toggle() }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "video.fill").foregroundStyle(.secondary)
                Text("CAMERA")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if collapsed {
                    Text("hidden – tap to show")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
                Spacer()
                Image(systemName: "chevron.down")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                    .rotationEffect(.degrees(collapsed ? -90 : 0))
            }
            .padding(.horizontal, 4)
            .padding(.top, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(collapsed ? "Show camera" : "Hide camera")
    }
}

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
                Text(s.headline.uppercased())
                    .font(.system(size: 26, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                if let d = s.stateDetail, !d.isEmpty {
                    Text(d)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.white.opacity(0.9))
                        .lineLimit(2)
                }
                if let since = s.stateSinceDate {
                    (Text("Since \(Fmt.clock(since)) · ") + Text(since, style: .relative))
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.8))
                }
            }
            Spacer(minLength: 0)
            if s.isOverProducing {
                OverrunBadge(size: 13)
            }
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
                Label("Done ~" + Fmt.clock(finish), systemImage: "flag.checkered")
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
    @AppStorage("barDayOpen") private var open = false
    @State private var day: BarDay?
    @State private var loadingDay = false

    /// "~3 more bars needed", "Finishes on this bar", or "Learning parts per bar…"
    private func barsNeeded(_ pb: MachineStatus.PerBar) -> String? {
        let prog = pb.label ?? pb.program ?? "This program"
        guard let avg = pb.avg else { return "\(prog): learning parts per bar – shown after its first full bar" }
        if pb.partsLeft != nil {
            if pb.moreBars == 0 { return "\(prog): finishes on this bar" }
            if let more = pb.moreBars {
                var t = "\(prog): ~\(more) more bar\(more == 1 ? "" : "s") needed"
                if let on = pb.leftOnBar, on > 0 { t += " (+ ~\(on) on this one)" }
                return t
            }
            if let total = pb.barsTotal { return "\(prog): ~\(total) bar\(total == 1 ? "" : "s") for the \(pb.partsLeft ?? 0) left" }
        }
        return "\(prog): average of \(pb.bars ?? 0) bar\((pb.bars ?? 0) == 1 ? "" : "s") (~\(Int(avg.rounded())) parts)"
    }

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
                stat("Parts/bar", stats.perBar?.avg.map { "~\(Int($0.rounded()))" } ?? "—")
            }
            if let pb = stats.perBar, let line = barsNeeded(pb) {
                Label(line, systemImage: "cylinder.split.1x2")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(pb.avg == nil ? Color.secondary : Color.primary)
            }
            if let next = s.date(stats.perBar?.nextBarAt), s.state.isRunning {
                let bits = [stats.perBar?.nextBarInS.map { "in " + Fmt.span($0) },
                            stats.perBar?.leftOnBar.map { "~\($0) parts" }].compactMap { $0 }
                Label("Next bar change ~\(Fmt.clock(next))" + (bits.isEmpty ? "" : " (\(bits.joined(separator: ", ")))"),
                      systemImage: "timer")
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
            }
            if let last = s.date(stats.lastAt) {
                Text("Last finished \(Fmt.clock(last))"
                     + (stats.avgWeekS.map { " · 7-day average " + Fmt.span($0) } ?? ""))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            dayDropdown
        }
        .task(id: open) { await loadDay() }
        .onChange(of: stats.today ?? 0) { _ in Task { await loadDay() } }
    }

    /// "Today's changes" - time, parts the bar made and the program(s) that made them
    @ViewBuilder private var dayDropdown: some View {
        Divider().padding(.top, 2)
        Button {
            withAnimation(.easeInOut(duration: 0.2)) { open.toggle() }
        } label: {
            HStack(spacing: 6) {
                Text("TODAY'S CHANGES").font(.caption.weight(.semibold))
                if let d = day, (d.changes ?? 0) > 0 {
                    Text("(\(d.changes ?? 0) · \(d.parts ?? 0) parts)").font(.caption)
                }
                Spacer()
                if loadingDay { ProgressView().controlSize(.mini) }
                Image(systemName: "chevron.down")
                    .font(.caption.weight(.bold))
                    .rotationEffect(.degrees(open ? 180 : 0))
            }
            .foregroundStyle(.secondary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)

        if open {
            if let bars = day?.bars, !bars.isEmpty {
                VStack(spacing: 0) {
                    ForEach(bars) { b in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(Fmt.clock(Date(timeIntervalSince1970: b.at)))
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                            Text(b.parts.map { "\($0) pts" } ?? "—")
                                .font(.subheadline.weight(.semibold).monospacedDigit())
                                .frame(width: 62, alignment: .leading)
                            Text(programs(b))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                            if b.partial == true {
                                Text("PART BAR")
                                    .font(.caption2.weight(.heavy))
                                    .foregroundStyle(Color.orange)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(.vertical, 5)
                        Divider().opacity(0.4)
                    }
                }
            } else if !loadingDay {
                Text("No bar changes today yet.").font(.caption).foregroundStyle(.secondary).padding(.top, 4)
            }
        }
    }

    private func programs(_ b: BarDay.Change) -> String {
        let list = b.programs ?? []
        if list.isEmpty { return "bar before this one wasn't recorded" }
        return list.map { p in
            let name = p.label ?? p.program ?? "?"
            return list.count > 1 ? "\(name) \(p.parts ?? 0)" : name
        }.joined(separator: " + ")
    }

    private func loadDay() async {
        guard open else { return }
        loadingDay = true
        day = try? await APIClient.current.barDay()
        loadingDay = false
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
            if let avg = s.program?.avgCycleS {
                Text("Program avg \(Fmt.duration(avg))")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct ProgramCard: View {
    let s: MachineStatus
    @EnvironmentObject var store: MachineStore
    @State private var naming = false
    @State private var newName = ""
    @State private var saveError: String?

    var body: some View {
        Card(title: "Program") {
            HStack(alignment: .firstTextBaseline) {
                Text(s.program?.title ?? "—")
                    .font(.title2.weight(.bold).monospaced())
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                Spacer()
                if let key = s.program?.key {
                    Button {
                        newName = s.program?.customName ?? ""
                        naming = true
                    } label: {
                        Label(s.program?.customName == nil ? "Name" : "Rename", systemImage: "pencil")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(Color(.tertiarySystemFill), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Name program \(key)")
                }
            }
            if let c = s.program?.comment, !c.isEmpty {
                Text(c)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            // setup sheet bits, so they're to hand when the job is being set: stick out and the job drawing
            if s.program?.stickOut != nil || s.program?.hasDoc == true {
                HStack(spacing: 12) {
                    if let so = s.program?.stickOut {
                        Label("Stick out \(so == so.rounded() ? String(Int(so)) : String(format: "%.2f", so)) mm",
                              systemImage: "ruler")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                    }
                    if s.program?.hasDoc == true, let key = s.program?.key {
                        NavigationLink {
                            JobPDFView(key: key)
                        } label: {
                            Label("Job PDF", systemImage: "doc.richtext")
                                .font(.caption.weight(.semibold))
                                .padding(.horizontal, 10).padding(.vertical, 5)
                                .background(Color(.tertiarySystemFill), in: Capsule())
                        }
                        .buttonStyle(.plain)
                    }
                    Spacer(minLength: 0)
                }
            }
            if let err = saveError {
                Text(err).font(.caption).foregroundStyle(.red)
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
        .alert("Name \(s.program?.key ?? "program")", isPresented: $naming) {
            TextField("e.g. EMS301", text: $newName)
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
            Button("Save") { save(newName) }
            if s.program?.customName != nil {
                Button("Remove name", role: .destructive) { save("") }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Shown after the number, e.g. \(s.program?.key ?? "O3110") - EMS301. Kept with this program's parts-per-bar figures.")
        }
    }

    private func save(_ name: String) {
        guard let key = s.program?.key else { return }
        Task {
            do {
                try await APIClient.current.nameProgram(key, name: name.trimmingCharacters(in: .whitespaces))
                saveError = nil
                await store.refresh()
            } catch {
                saveError = "Couldn't save the name: \(error.localizedDescription)"
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
                        (Text(Fmt.clock(started)) + Text(" · for ") + Text(started, style: .relative))
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
