import SwiftUI

/// Remote changes to the machine, sent through the server to the PC app on the machine.
/// Nothing is written unless "Allow remote changes" is ticked in the PC app. Machine buttons (cycle start etc.)
/// are shown but not available yet - they need machine-specific signals and safety interlocks first.
///
/// This lives as a folded-away card on the dashboard rather than its own tab: it's rarely needed, and the
/// tab is more useful for the program/setup information.
struct ControlsCard: View {
    @EnvironmentObject var store: MachineStore
    @AppStorage("controlsOpen") private var open = false
    @State private var requiredText = ""
    @State private var busy = false
    @State private var result: (ok: Bool, text: String)?
    @State private var confirmRequired: Int?
    @State private var confirmCounter: Bool?
    @State private var showButtonsNote = false
    @FocusState private var editing: Bool

    private var s: MachineStatus? { store.status }
    private var controls: MachineStatus.ControlsInfo? { s?.controls }
    private var pcConnected: Bool { controls?.connected == true && s?.agentOnline == true }
    private var remoteAllowed: Bool { controls?.remote == true }
    private var requiredNow: String { s?.partsRequired.map { String($0) } ?? "—" }
    private var canChange: Bool { pcConnected && remoteAllowed && s?.machineConnected == true && !busy }

    /// what the header says when it's folded away
    private var summary: String {
        if s == nil { return "not connected" }
        if !pcConnected { return "PC app offline" }
        if !remoteAllowed { return "switched off at the machine" }
        return "required \(requiredNow)" + (s?.workCounter == true ? " · stops at count" : "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            if open {
                access
                requiredRow
                counterRow
                buttonsRow
                if let result {
                    Label(result.text, systemImage: result.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(result.ok ? .green : .red)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .toolbar {   // the number pad has no return key
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { editing = false }
            }
        }
        // `presenting:` hands the value to the buttons; reading the @State there would find it already
        // cleared by the dismissal, and the command would silently not be sent
        .confirmationDialog("Set the required count?", isPresented: Binding(
            get: { confirmRequired != nil }, set: { if !$0 { confirmRequired = nil } }),
            titleVisibility: .visible, presenting: confirmRequired) { v in
            Button("Set to \(v)") { send(["type": "set_required", "value": v]) }
            Button("Cancel", role: .cancel) {}
        } message: { v in
            Text("This changes the required part count on the machine from \(requiredNow) to \(v).")
        }
        .confirmationDialog("Stop at count", isPresented: Binding(
            get: { confirmCounter != nil }, set: { if !$0 { confirmCounter = nil } }),
            titleVisibility: .visible, presenting: confirmCounter) { on in
            Button(on ? "Turn on" : "Turn off", role: on ? nil : .destructive) {
                send(["type": "set_work_counter", "on": on])
            }
            Button("Cancel", role: .cancel) {}
        } message: { on in
            Text(on ? "The machine will stop when it reaches the required count."
                    : "The machine will keep running past the required count.")
        }
        .alert("Machine buttons", isPresented: $showButtonsNote) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("Cycle start, cycle stop and continuous aren't available yet. Starting or stopping the machine "
                 + "remotely needs machine-specific signals and safety interlocks (so it can't start with someone "
                 + "at the machine) - to be set up together first.")
        }
    }

    // MARK: - parts

    private var header: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.2)) { open.toggle() }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "slider.horizontal.3").foregroundStyle(.secondary)
                Text("CONTROLS")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if !open {
                    Text(summary)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                Spacer(minLength: 4)
                if !remoteAllowed && s != nil {
                    Image(systemName: "lock.fill").font(.caption).foregroundStyle(.secondary)
                }
                Image(systemName: "chevron.down")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                    .rotationEffect(.degrees(open ? 0 : -90))
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(open ? "Hide controls" : "Show controls")
    }

    @ViewBuilder private var access: some View {
        if s == nil {
            note("Not connected to the server.", "wifi.exclamationmark", .secondary)
        } else if !pcConnected {
            note("The PC app on the machine isn't connected, so changes can't be sent.",
                 "desktopcomputer.trianglebadge.exclamationmark", .orange)
        } else if !remoteAllowed {
            note("Remote changes are switched off. To allow them, tick \u{201C}Allow remote changes from the app\u{201D} "
                 + "in the PC app's Settings at the machine.", "lock.fill", .orange)
        } else if s?.machineConnected != true {
            note("The machine is off or not reachable.", "powerplug", .orange)
        }
    }

    private func note(_ text: String, _ symbol: String, _ colour: Color) -> some View {
        Label(text, systemImage: symbol)
            .font(.caption.weight(.medium))
            .foregroundStyle(colour)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var requiredRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("Required count").font(.subheadline.weight(.semibold))
                Spacer()
                Text(requiredNow).font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                TextField("New count", text: $requiredText)
                    .keyboardType(.numberPad)
                    .focused($editing)
                    .textFieldStyle(.roundedBorder)
                Button("Set") {
                    editing = false
                    if let v = Int(requiredText.trimmingCharacters(in: .whitespaces)), v > 0 { confirmRequired = v }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canChange || Int(requiredText.trimmingCharacters(in: .whitespaces)) == nil)
            }
        }
    }

    private var counterRow: some View {
        VStack(alignment: .leading, spacing: 2) {
            Toggle("Stop when count reached", isOn: Binding(
                get: { s?.workCounter ?? false },
                set: { confirmCounter = $0 }))
                .font(.subheadline.weight(.semibold))
                .disabled(!canChange || s?.workCounter == nil || controls?.workCounterSignal != true)
            if controls?.workCounterSignal != true {
                Text("Needs the work counter signal set up in the PC app (Signal finder).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var buttonsRow: some View {
        Button { showButtonsNote = true } label: {
            HStack(spacing: 10) {
                ForEach([("Cycle start", "play.fill"), ("Stop", "pause.fill"), ("Continuous", "repeat")], id: \.0) { item in
                    Label(item.0, systemImage: item.1)
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 8).padding(.vertical, 5)
                        .background(Color(.tertiarySystemFill), in: Capsule())
                }
                Spacer(minLength: 0)
                Image(systemName: "info.circle").font(.caption)
            }
            .foregroundStyle(.secondary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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
