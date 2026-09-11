import SwiftUI

/// Remote changes to the machine, sent through the server to the PC app on the machine.
/// Nothing is written unless "Allow remote changes" is ticked in the PC app. Machine buttons (cycle start etc.)
/// are shown but not available yet - they need machine-specific signals and safety interlocks first.
struct ControlsView: View {
    @EnvironmentObject var store: MachineStore
    @State private var requiredText = ""
    @State private var busy = false
    @State private var result: (ok: Bool, text: String)?
    @State private var confirmRequired: Int?
    @State private var confirmCounter: Bool?
    @FocusState private var editing: Bool

    private var s: MachineStatus? { store.status }
    private var controls: MachineStatus.ControlsInfo? { s?.controls }
    private var pcConnected: Bool { controls?.connected == true && s?.agentOnline == true }
    private var remoteAllowed: Bool { controls?.remote == true }
    private var requiredNow: String { s?.partsRequired.map { String($0) } ?? "—" }
    private var canChange: Bool { pcConnected && remoteAllowed && s?.machineConnected == true && !busy }

    var body: some View {
        NavigationStack {
            Form {
                accessSection
                partsSection
                counterSection
                buttonsSection
                if let result {
                    Section {
                        Label(result.text, systemImage: result.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(result.ok ? .green : .red)
                            .font(.subheadline)
                    }
                }
            }
            .navigationTitle("Controls")
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { editing = false }
                }
            }
            .refreshable { await store.refresh() }
            .confirmationDialog(
                "Set the required count to \(confirmRequired ?? 0)?",
                isPresented: Binding(get: { confirmRequired != nil }, set: { if !$0 { confirmRequired = nil } }),
                titleVisibility: .visible
            ) {
                Button("Set to \(confirmRequired ?? 0)") {
                    if let v = confirmRequired { send(["type": "set_required", "value": v]) }
                }
            } message: {
                Text("This changes the required part count on the machine (currently \(requiredNow)).")
            }
            .confirmationDialog(
                confirmCounter == true ? "Turn stop-at-count ON?" : "Turn stop-at-count OFF?",
                isPresented: Binding(get: { confirmCounter != nil }, set: { if !$0 { confirmCounter = nil } }),
                titleVisibility: .visible
            ) {
                Button(confirmCounter == true ? "Turn on" : "Turn off", role: confirmCounter == true ? nil : .destructive) {
                    if let on = confirmCounter { send(["type": "set_work_counter", "on": on]) }
                }
            } message: {
                Text(confirmCounter == true
                     ? "The machine will stop when it reaches the required count."
                     : "The machine will keep running past the required count.")
            }
        }
    }

    // MARK: - sections

    @ViewBuilder private var accessSection: some View {
        if s == nil {
            Section { Text("Not connected to the server.").foregroundStyle(.secondary) }
        } else if !pcConnected {
            Section {
                Label("The PC app on the machine isn't connected, so changes can't be sent.",
                      systemImage: "desktopcomputer.trianglebadge.exclamationmark")
                    .foregroundStyle(.orange)
            }
        } else if !remoteAllowed {
            Section {
                Label("Remote changes are switched off.", systemImage: "lock.fill")
                    .foregroundStyle(.orange)
            } footer: {
                Text("To allow them, tick \"Allow remote changes from the app\" in the PC app's Settings on the machine. "
                     + "This has to be done at the machine on purpose.")
            }
        } else if s?.machineConnected != true {
            Section { Label("The machine is off or not reachable.", systemImage: "powerplug").foregroundStyle(.orange) }
        }
    }

    private var partsSection: some View {
        Section {
            LabeledContent("Made", value: s?.parts.map { String($0) } ?? "—")
            LabeledContent("Required", value: s?.partsRequired.map { String($0) } ?? "—")
            HStack {
                TextField("New required count", text: $requiredText)
                    .keyboardType(.numberPad)
                    .focused($editing)
                Button("Set") {
                    editing = false
                    if let v = Int(requiredText.trimmingCharacters(in: .whitespaces)), v > 0 { confirmRequired = v }
                }
                .disabled(!canChange || Int(requiredText.trimmingCharacters(in: .whitespaces)) == nil)
            }
        } header: {
            Text("Part count")
        }
    }

    @ViewBuilder private var counterSection: some View {
        Section {
            Toggle("Stop when count reached", isOn: Binding(
                get: { s?.workCounter ?? false },
                set: { confirmCounter = $0 }))
                .disabled(!canChange || s?.workCounter == nil || controls?.workCounterSignal != true)
        } footer: {
            if controls?.workCounterSignal != true {
                Text("Needs the work counter signal set up in the PC app (Signal finder).")
            }
        }
    }

    private var buttonsSection: some View {
        Section {
            Label("Cycle start", systemImage: "play.fill").foregroundStyle(.secondary)
            Label("Cycle stop / feed hold", systemImage: "pause.fill").foregroundStyle(.secondary)
            Label("Continuous", systemImage: "repeat").foregroundStyle(.secondary)
        } header: {
            Text("Machine buttons")
        } footer: {
            Text("Not available yet. Starting or stopping the machine remotely needs machine-specific signals and "
                 + "safety interlocks (so it can't start with someone at the machine) - to be set up together first.")
        }
    }

    // MARK: - sending

    private func send(_ body: [String: Any]) {
        busy = true
        result = nil
        Task {
            defer { busy = false }
            do {
                let r = try await APIClient.current.command(body)
                result = (r.ok, r.text)
                if r.ok { requiredText = "" }
            } catch {
                result = (false, error.localizedDescription)
            }
            await store.refresh()
        }
    }
}
