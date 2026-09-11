import SwiftUI
import UIKit

@MainActor
final class AlarmHistoryModel: ObservableObject {
    @Published var alarms: [AlarmEvent] = []
    @Published var error: String?
    @Published var loading = false
    @Published var canLoadMore = false
    private var nextBefore: Double?

    func reload(activeOnly: Bool) async {
        loading = true
        defer { loading = false }
        do {
            let page = try await APIClient.current.alarms(limit: 100, activeOnly: activeOnly)
            alarms = page.alarms
            nextBefore = page.nextBefore
            canLoadMore = page.nextBefore != nil
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    func loadMore(activeOnly: Bool) async {
        guard let before = nextBefore, !loading else { return }
        loading = true
        defer { loading = false }
        if let page = try? await APIClient.current.alarms(limit: 100, before: before, activeOnly: activeOnly) {
            let known = Set(alarms.map(\.id))
            alarms += page.alarms.filter { !known.contains($0.id) }
            nextBefore = page.nextBefore
            canLoadMore = page.nextBefore != nil
        }
    }

    /// Alarms grouped by calendar day, newest first.
    var sections: [(day: Date, items: [AlarmEvent])] {
        let cal = Calendar.current
        let groups = Dictionary(grouping: alarms) { cal.startOfDay(for: $0.started) }
        return groups.keys.sorted(by: >).map { ($0, groups[$0]!.sorted { $0.startedAt > $1.startedAt }) }
    }
}

struct AlarmsView: View {
    @EnvironmentObject var store: MachineStore
    @StateObject private var model = AlarmHistoryModel()
    @State private var filter = 1   // opens on Active alarms
    @State private var selected: AlarmEvent?

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Picker("Show", selection: $filter) {
                        Text("All").tag(0)
                        Text("Active").tag(1)
                    }
                    .pickerStyle(.segmented)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets())
                }

                if let err = model.error, model.alarms.isEmpty {
                    Section { Label(err, systemImage: "wifi.exclamationmark").foregroundStyle(.orange) }
                }

                ForEach(model.sections, id: \.day) { section in
                    Section(header: Text(dayTitle(section.day))) {
                        ForEach(section.items) { a in
                            Button { selected = a } label: { AlarmRow(a: a) }
                                .buttonStyle(.plain)
                        }
                    }
                }

                if model.canLoadMore {
                    HStack { Spacer(); ProgressView(); Spacer() }
                        .task { await model.loadMore(activeOnly: filter == 1) }
                }
            }
            .listStyle(.insetGrouped)
            .overlay {
                if model.alarms.isEmpty && model.error == nil && !model.loading {
                    VStack(spacing: 8) {
                        Image(systemName: "checkmark.seal").font(.system(size: 44)).foregroundStyle(.green)
                        Text(filter == 1 ? "No active alarms" : "No alarms recorded yet").font(.headline)
                        Text("Alarms from the machine will appear here with the time they happened.")
                            .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    .padding(40)
                }
            }
            .refreshable { await model.reload(activeOnly: filter == 1) }
            .navigationTitle("Alarms")
            .task { await model.reload(activeOnly: filter == 1) }
            .onChange(of: filter) { f in Task { await model.reload(activeOnly: f == 1) } }
            // Reload when the set of active alarms changes (new alarm or one cleared)
            .onChange(of: store.status?.activeAlarms.map(\.id) ?? []) { _ in
                Task { await model.reload(activeOnly: filter == 1) }
            }
            .sheet(item: $selected) { a in AlarmDetail(a: a).presentationDetents([.medium, .large]) }
        }
    }

    private func dayTitle(_ d: Date) -> String {
        let cal = Calendar.current
        if cal.isDateInToday(d) { return "Today" }
        if cal.isDateInYesterday(d) { return "Yesterday" }
        return d.formatted(.dateTime.weekday(.wide).day().month(.wide).year())
    }
}

struct AlarmRow: View {
    let a: AlarmEvent

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: a.isActive ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                .font(.title3)
                .foregroundStyle(a.isActive ? MachineStateKind.alarm.color : Color.secondary.opacity(0.6))
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(a.displayCode)
                        .font(.subheadline.weight(.bold).monospaced())
                        .foregroundStyle(a.isActive ? MachineStateKind.alarm.color : .primary)
                    if let p = a.pathName { PathTag(name: p) }
                    Spacer()
                    Text(a.started, format: .dateTime.hour().minute().second())
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Text(a.displayMessage)
                    .font(.subheadline)
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
                Group {
                    if a.isActive {
                        Text("Active now").foregroundStyle(MachineStateKind.alarm.color).fontWeight(.semibold)
                    } else {
                        Text("Cleared after \(Fmt.span(a.durationS))").foregroundStyle(.secondary)
                    }
                }
                .font(.caption)
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
    }
}

struct AlarmDetail: View {
    let a: AlarmEvent

    var body: some View {
        NavigationStack {
            List {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(a.displayCode).font(.title.weight(.bold).monospaced())
                            .foregroundStyle(a.isActive ? MachineStateKind.alarm.color : .primary)
                        Text(a.displayMessage).font(.title3)
                    }
                    .padding(.vertical, 4)
                }
                Section("Details") {
                    row("Type", a.typeName ?? "—")
                    row("Path", a.pathName ?? "—")
                    if let axis = a.axis, axis > 0 { row("Axis", "\(axis)") }
                    row("Started", a.started.formatted(date: .abbreviated, time: .standard))
                    row("Cleared", a.cleared?.formatted(date: .abbreviated, time: .standard) ?? "Still active")
                    row("Duration", Fmt.span(a.durationS))
                }
            }
            .navigationTitle("Alarm")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack { Text(k).foregroundStyle(.secondary); Spacer(); Text(v).multilineTextAlignment(.trailing) }
    }
}
