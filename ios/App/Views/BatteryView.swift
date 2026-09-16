import SwiftUI

/// The machine's memory backup battery. Nothing on the machine warns you before the parameters have already
/// gone, so the date of the last change is kept on the server and counted down from here.
struct BatteryBanner: View {
    let item: MaintenanceItem
    @EnvironmentObject var store: MachineStore
    @State private var editing = false

    private var colour: Color { item.isOverdue ? MachineStateKind.alarm.color : .orange }

    var body: some View {
        Button { editing = true } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: item.isOverdue ? "minus.plus.batteryblock.exclamationmark.fill" : "minus.plus.batteryblock.fill")
                    .font(.system(size: 30))
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(item.name) \(item.isOverdue ? "overdue" : "due soon")".uppercased())
                        .font(.system(size: 19, weight: .heavy, design: .rounded))
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                    Text(item.headline)
                        .font(.subheadline.weight(.bold))
                    if let last = item.last {
                        Text("Last changed \(Fmt.date(last)) · every \(Int(item.everyMonths)) months")
                            .font(.caption.weight(.medium))
                            .opacity(0.9)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.footnote.weight(.bold)).opacity(0.7)
            }
            .foregroundStyle(.white)
            .padding(14)
            .frame(maxWidth: .infinity)
            .background(colour, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .shadow(color: colour.opacity(0.35), radius: 8, y: 3)
        }
        .buttonStyle(.plain)
        .sheet(isPresented: $editing) {
            NavigationStack { BatteryView() }
                .environmentObject(store)
                .presentationDetents([.large])
        }
    }
}

/// Set when the battery was last changed and how often it needs doing.
struct BatteryView: View {
    @EnvironmentObject var store: MachineStore
    @Environment(\.dismiss) private var dismiss

    @State private var item: MaintenanceItem?
    @State private var lastChanged = Date()
    @State private var haveDate = false
    @State private var everyMonths = 12
    @State private var warnDays = 30
    @State private var warnChoices = [7, 14, 30, 60]
    @State private var saving = false
    @State private var message: (ok: Bool, text: String)?
    @State private var confirmChanged = false

    private var colour: Color {
        switch item?.state {
        case "overdue": return MachineStateKind.alarm.color
        case "soon": return .orange
        case "ok": return MachineStateKind.running.color
        default: return .secondary
        }
    }

    var body: some View {
        Form {
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    Text(item?.headline ?? "…")
                        .font(.system(size: 30, weight: .bold, design: .rounded))
                        .foregroundStyle(colour)
                        .minimumScaleFactor(0.6)
                        .lineLimit(1)
                    if let due = item?.due {
                        Text("Next due \(Fmt.date(due))").font(.subheadline).foregroundStyle(.secondary)
                    } else {
                        Text("Set the date it was last changed.").font(.subheadline).foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 4)
            }

            Section {
                Button {
                    confirmChanged = true
                } label: {
                    Label("Changed today", systemImage: "checkmark.circle.fill").fontWeight(.semibold)
                }
                .disabled(saving)
            } footer: {
                Text("Records today's date and starts the countdown again.")
            }

            Section {
                Toggle("Know when it was last changed", isOn: $haveDate)
                if haveDate {
                    DatePicker("Last changed", selection: $lastChanged, in: ...Date(), displayedComponents: .date)
                }
                Picker("Change every", selection: $everyMonths) {
                    ForEach([6, 12, 18, 24, 36], id: \.self) { m in
                        Text(m % 12 == 0 ? "\(m / 12) year\(m == 12 ? "" : "s")" : "\(m) months").tag(m)
                    }
                }
                Picker("Warn me", selection: $warnDays) {
                    ForEach(warnChoices, id: \.self) { d in
                        Text(warnLabel(d)).tag(d)
                    }
                }
            } header: {
                Text("Schedule")
            } footer: {
                Text("FANUC controls normally want their memory backup batteries once a year. The dashboard, the wall display and this app all warn from the date you set, and a notification comes through when it's due.")
            }

            Section {
                Button {
                    save()
                } label: {
                    HStack {
                        Text("Save").fontWeight(.semibold)
                        if saving { Spacer(); ProgressView() }
                    }
                }
                .disabled(saving)
                if let message {
                    Text(message.text).font(.footnote).foregroundStyle(message.ok ? .green : .red)
                }
            }

            if let history = item?.history, history.count > 1 {
                Section("Changed before this") {
                    ForEach(history.dropFirst().prefix(8), id: \.self) { at in
                        Text(Fmt.date(Date(timeIntervalSince1970: at)))
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .navigationTitle(item?.name ?? "Backup battery")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        .task { await load() }
        .confirmationDialog("Record the backup battery as changed today?", isPresented: $confirmChanged,
                            titleVisibility: .visible) {
            Button("Changed today") { save(changedNow: true) }
        }
    }

    private func load() async {
        do {
            guard let m = try await APIClient.current.maintenance().first(where: { $0.item == "battery" }) else { return }
            item = m
            if let last = m.last { lastChanged = last; haveDate = true } else { haveDate = false }
            everyMonths = max(1, Int(m.everyMonths.rounded()))
            warnDays = Int((m.warnDays ?? 30).rounded())
            if !warnChoices.contains(warnDays) { warnChoices.append(warnDays); warnChoices.sort() }
            message = nil
        } catch {
            message = (false, "Couldn't load it: \(error.localizedDescription)")
        }
    }

    private func save(changedNow: Bool = false) {
        saving = true
        Task {
            do {
                item = try await APIClient.current.saveMaintenance(
                    lastAt: changedNow ? nil : (haveDate ? noon(lastChanged) : nil),
                    everyMonths: everyMonths, warnDays: warnDays, changed: changedNow,
                    clearDate: !changedNow && !haveDate)
                if let last = item?.last { lastChanged = last; haveDate = true }
                message = (true, changedNow ? "Recorded as changed today" : "Saved")
                await store.refresh()
            } catch {
                message = (false, "Couldn't save it: \(error.localizedDescription)")
            }
            saving = false
        }
    }

    private func warnLabel(_ d: Int) -> String {
        switch d {
        case 7: return "A week before"
        case 14: return "Two weeks before"
        case 30: return "A month before"
        case 60: return "Two months before"
        default: return "\(d) days before"
        }
    }

    /// Midday, so the due date can't slip a day either way when the clocks change.
    private func noon(_ d: Date) -> Date {
        Calendar.current.date(bySettingHour: 12, minute: 0, second: 0, of: d) ?? d
    }
}
