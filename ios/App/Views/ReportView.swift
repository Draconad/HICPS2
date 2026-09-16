import SwiftUI

/// What the machine has actually made: parts, bars and running time by day, month or year, with the
/// breakdown by program. The figures come from the server's daily rollup.
struct ReportView: View {
    @State private var period = AppSettings.reportPeriod
    @State private var report: OpsReport?
    @State private var loading = true
    @State private var error: String?
    @State private var open: Set<String> = []

    private var limit: Int { period == "day" ? 60 : period == "month" ? 24 : 10 }

    var body: some View {
        NavigationStack {
            List {
                if let error {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                    }
                }
                if let r = report {
                    Section { totals(r.totals) }
                    Section {
                        if r.buckets.isEmpty {
                            Text("Nothing recorded yet. The figures build up as the machine runs.")
                                .foregroundStyle(.secondary)
                        }
                        ForEach(r.buckets) { b in BucketRow(b: b, open: open.contains(b.key)) { toggle(b.key) } }
                    } header: {
                        Text(period == "day" ? "By day" : period == "month" ? "By month" : "By year")
                    } footer: {
                        Text(r.from.map { "Recorded since \($0). Tap a row for the programs that made them." }
                             ?? "Tap a row for the programs that made them.")
                    }
                    if !r.totals.programs.isEmpty {
                        Section("Parts by program") {
                            ForEach(r.totals.programs) { p in
                                HStack {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(p.title).font(.subheadline.weight(.semibold))
                                        Text("\(p.bars) bar\(p.bars == 1 ? "" : "s") · \(Fmt.span(p.runS)) running")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Text(p.parts.formatted())
                                        .font(.headline.monospacedDigit())
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Report")
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Picker("Period", selection: $period) {
                        Text("Day").tag("day")
                        Text("Month").tag("month")
                        Text("Year").tag("year")
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 260)
                }
            }
            .overlay { if loading && report == nil { ProgressView() } }
            .refreshable { await load() }
            .task(id: period) {
                AppSettings.reportPeriod = period
                open.removeAll()
                await load()
            }
        }
    }

    private func toggle(_ key: String) {
        withAnimation(.easeInOut(duration: 0.18)) {
            if open.contains(key) { open.remove(key) } else { open.insert(key) }
        }
    }

    @ViewBuilder private func totals(_ t: OpsReport.Totals) -> some View {
        let on = t.runS + t.standbyS + t.alarmS
        HStack(alignment: .top, spacing: 12) {
            figure("Parts", t.parts.formatted(), t.bars > 0 && t.parts > 0 ? "~\(t.parts / max(1, t.bars)) a bar" : nil)
            figure("Bars", t.bars.formatted(), nil)
            figure("Running", Fmt.span(t.runS),
                   on > 60 ? "\(Int((t.runS / on * 100).rounded()))% of the time on" : nil)
        }
        .padding(.vertical, 2)
        if t.alarmS > 60 || t.standbyS > 60 {
            Label("\(Fmt.span(t.standbyS)) stopped"
                  + (t.alarmS > 60 ? " · \(Fmt.span(t.alarmS)) in alarm" : ""), systemImage: "pause.circle")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
        }
    }

    private func figure(_ label: String, _ value: String, _ note: String?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 24, weight: .bold, design: .rounded))
                .monospacedDigit()
                .minimumScaleFactor(0.6)
                .lineLimit(1)
            if let note { Text(note).font(.caption2).foregroundStyle(.secondary).lineLimit(2) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func load() async {
        loading = true
        do {
            report = try await APIClient.current.report(period: period, limit: limit)
            error = nil
        } catch {
            self.error = "Couldn't load the report: \(error.localizedDescription)"
        }
        loading = false
    }
}

private struct BucketRow: View {
    let b: OpsReport.Bucket
    let open: Bool
    let tap: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button(action: tap) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.secondary)
                        .rotationEffect(.degrees(open ? 90 : 0))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(b.label).font(.subheadline.weight(.semibold))
                        Text(line).font(.caption).foregroundStyle(.secondary).monospacedDigit()
                    }
                    Spacer(minLength: 6)
                    Text(b.parts.formatted())
                        .font(.headline.monospacedDigit())
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open {
                ForEach(b.programs) { p in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(p.title).font(.caption.weight(.semibold).monospaced()).lineLimit(1)
                        Spacer(minLength: 4)
                        if let c = p.avgCycleS {
                            Text(Fmt.duration(c)).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        Text("\(p.parts)").font(.caption.weight(.semibold).monospacedDigit())
                    }
                    .padding(.leading, 20)
                }
                if b.programs.isEmpty {
                    Text("No parts recorded against a program").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private var line: String {
        var bits = ["\(b.bars) bar\(b.bars == 1 ? "" : "s")", Fmt.span(b.runS) + " running"]
        if let c = b.avgCycleS { bits.append(Fmt.duration(c) + " cycle") }
        if let u = b.utilisation { bits.append("\(Int((u * 100).rounded()))% busy") }
        return bits.joined(separator: " · ")
    }
}
