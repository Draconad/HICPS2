import SwiftUI

/// Settings > Program info: every program the server knows, with your name for it and its parts per bar.
struct ProgramListView: View {
    @State private var list: [ProgramRecord] = []
    @State private var loading = true
    @State private var error: String?
    @State private var adding = false
    @State private var newKey = ""
    @State private var openKey: String?

    var body: some View {
        List {
            if let error {
                Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
            }
            Section {
                ForEach(list) { p in
                    NavigationLink(value: p.program) { ProgramRow(p: p) }
                }
                if list.isEmpty && !loading && error == nil {
                    Text("No programs yet – they appear once one has been loaded on the machine.")
                        .foregroundStyle(.secondary)
                }
            } footer: {
                Text("Parts per bar is learnt from the machine (from one bar change to the next) and stored against the program number, together with your name and notes.")
            }
        }
        .navigationTitle("Program info")
        .navigationDestination(for: String.self) { key in
            ProgramDetailView(key: key) { Task { await load() } }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { newKey = ""; adding = true } label: { Image(systemName: "plus") }
                    .accessibilityLabel("Add a program")
            }
        }
        .alert("Add a program", isPresented: $adding) {
            TextField("e.g. O3110", text: $newKey)
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
            Button("Add") { add() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Give a program a name or its parts per bar before it has run.")
        }
        .overlay { if loading && list.isEmpty { ProgressView() } }
        .refreshable { await load() }
        .task { await load() }
        .navigationDestination(item: $openKey) { key in
            ProgramDetailView(key: key) { Task { await load() } }
        }
    }

    private func load() async {
        do {
            list = try await APIClient.current.programs().programs
            error = nil
        } catch {
            self.error = "Couldn't load programs: \(error.localizedDescription)"
        }
        loading = false
    }

    private func add() {
        var k = newKey.trimmingCharacters(in: .whitespaces).uppercased()
        if let n = Int(k), (0...99999).contains(n) { k = String(format: "O%04d", n) }
        guard k.range(of: #"^O\d{4,5}$"#, options: .regularExpression) != nil else {
            error = "Enter a program number like O3110"
            return
        }
        openKey = k
    }
}

private struct ProgramRow: View {
    let p: ProgramRecord

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(p.program).font(.body.weight(.bold).monospaced())
                    if let name = p.name { Text("- \(name)").font(.body.weight(.semibold)) }
                    if p.loaded == true {
                        Text("LOADED")
                            .font(.caption2.weight(.heavy))
                            .foregroundStyle(.black)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(MachineStateKind.running.color, in: Capsule())
                    }
                }
                Text(p.lastBarAt.map { "\(p.barsRecorded ?? 0) bars · last " + Fmt.dateTime(Date(timeIntervalSince1970: $0)) }
                     ?? "No bars recorded yet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 1) {
                Text(p.ppbInUse.map { "~\(Int($0.rounded()))" } ?? "—")
                    .font(.headline.monospacedDigit())
                Text(p.ppbManual != nil ? "set" : "parts/bar")
                    .font(.caption2)
                    .foregroundStyle(p.ppbManual != nil ? Color.orange : Color.secondary)
            }
        }
    }
}

/// One program: edit its name, parts per bar and notes; see and tidy up its recorded bars.
struct ProgramDetailView: View {
    let key: String
    var onChange: () -> Void = {}

    @State private var rec: ProgramRecord?
    @State private var name = ""
    @State private var manualOn = false
    @State private var manualText = ""
    @State private var notes = ""
    @State private var saving = false
    @State private var message: (ok: Bool, text: String)?
    @State private var confirmForget = false

    var body: some View {
        Form {
            Section {
                LabeledContent("Program number") { Text(key).monospaced().fontWeight(.bold) }
                TextField("Name, e.g. EMS301", text: $name)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
            } header: {
                Text("Name")
            } footer: {
                Text("Shown after the number: \(key) - \(name.isEmpty ? "EMS301" : name)")
            }

            Section {
                LabeledContent("Learnt") {
                    Text(rec?.ppbLearnt.map { "~\(Int($0.rounded())) (last \(rec?.ppbLearntBars ?? 0) bars)" } ?? "Not yet")
                }
                Toggle("Set it myself", isOn: $manualOn)
                if manualOn {
                    TextField("Parts per bar", text: $manualText)
                        .keyboardType(.decimalPad)
                }
            } header: {
                Text("Parts per bar")
            } footer: {
                Text(manualOn ? "Your figure is used for the bars-needed estimate instead of the learnt one."
                              : "Learnt from the machine, from one bar change to the next. Odd bars are left out.")
            }

            Section("Notes") {
                TextField("Bar size, material, setup notes…", text: $notes, axis: .vertical)
                    .lineLimit(2...6)
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
                    Text(message.text).font(.footnote).foregroundStyle(message.ok ? Color.green : Color.red)
                }
            }

            Section {
                if let bars = rec?.bars, !bars.isEmpty {
                    ForEach(bars) { b in
                        HStack {
                            Text(Fmt.dateTime(Date(timeIntervalSince1970: b.endedAt)))
                                .font(.subheadline.monospacedDigit())
                            Spacer()
                            Text("\(b.parts) parts").font(.subheadline.weight(.semibold).monospacedDigit())
                        }
                    }
                    .onDelete { idx in
                        let ids = idx.compactMap { rec?.bars?[$0].id }
                        Task {
                            for id in ids { try? await APIClient.current.deleteBars(id: id) }
                            await load(); onChange()
                        }
                    }
                } else {
                    Text("None yet – bars are counted from one bar change to the next while this program runs.")
                        .foregroundStyle(.secondary)
                }
            } header: {
                Text("Recorded bars")
            } footer: {
                Text("Swipe left on a bar to leave it out (e.g. a bad one).")
            }

            if (rec?.barsRecorded ?? 0) > 0 {
                Section {
                    Button("Forget all learnt bars", role: .destructive) { confirmForget = true }
                } footer: {
                    Text("E.g. after changing the bar length. Name, notes and your own parts per bar are kept.")
                }
            }
        }
        .navigationTitle(rec?.label ?? key)
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .confirmationDialog("Forget all the bars recorded for \(key)?", isPresented: $confirmForget, titleVisibility: .visible) {
            Button("Forget bars", role: .destructive) {
                Task { try? await APIClient.current.deleteBars(program: key); await load(); onChange() }
            }
        }
    }

    private func load() async {
        do {
            let r = try await APIClient.current.programDetail(key)
            let first = rec == nil
            rec = r
            if first {      // don't overwrite what's being typed on later reloads
                name = r.name ?? ""
                notes = r.notes ?? ""
                manualOn = r.ppbManual != nil
                manualText = r.ppbManual.map { $0 == $0.rounded() ? String(Int($0)) : String($0) } ?? ""
            }
        } catch {
            message = (false, "Couldn't load: \(error.localizedDescription)")
        }
    }

    private func save() {
        var ppb: Double?
        if manualOn {
            guard let v = Double(manualText.replacingOccurrences(of: ",", with: ".")), v >= 1, v <= 20000 else {
                message = (false, "Enter parts per bar between 1 and 20000, or switch off \u{201C}Set it myself\u{201D}.")
                return
            }
            ppb = v
        }
        saving = true
        Task {
            do {
                try await APIClient.current.saveProgram(key, name: name.trimmingCharacters(in: .whitespaces),
                                                        ppbManual: ppb, notes: notes)
                message = (true, "Saved")
                await load()
                onChange()
            } catch {
                message = (false, "Couldn't save: \(error.localizedDescription)")
            }
            saving = false
        }
    }
}
