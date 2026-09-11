import ActivityKit
import SwiftUI
import WidgetKit

@main
struct MachineWidgetBundle: WidgetBundle {
    var body: some Widget {
        MachineLiveActivity()
    }
}

struct MachineLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: MachineActivityAttributes.self) { context in
            LockScreenView(name: context.attributes.machineName, s: context.state, stale: context.isStale)
                .activityBackgroundTint(Color.black)
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            let s = context.state
            let color = context.isStale ? MachineStateKind.off.color : s.kind.color
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 6) {
                        Circle().fill(color).frame(width: 12, height: 12)
                        Text(s.kind.label)
                            .font(.headline)
                            .foregroundStyle(color)
                    }
                    .padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(context.attributes.machineName)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.8))
                        .lineLimit(1)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(s.partsText)
                        .font(.headline.monospacedDigit())
                        .foregroundStyle(.white)
                        .padding(.trailing, 4)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 6) {
                        if s.required != nil {
                            ProgressView(value: s.progress).tint(color)
                        }
                        HStack {
                            Label(Fmt.duration(s.lastCycle), systemImage: "timer")
                            Spacer()
                            if let start = s.cycleStart, s.kind.isRunning, !context.isStale {
                                Text(start, style: .timer)
                                    .multilineTextAlignment(.trailing)
                                    .frame(maxWidth: 70, alignment: .trailing)
                            }
                            Text(s.program).lineLimit(1)
                        }
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        if let first = s.alarms.first {
                            Label(first, systemImage: "exclamationmark.triangle.fill")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(MachineStateKind.alarm.color)
                                .lineLimit(1)
                        }
                    }
                    .padding(.horizontal, 4)
                }
            } compactLeading: {
                Circle()
                    .fill(color)
                    .frame(width: 12, height: 12)
                    .padding(.leading, 2)
            } compactTrailing: {
                Text(s.partsText)
                    .font(.caption.weight(.semibold).monospacedDigit())
                    .foregroundStyle(color)
                    .minimumScaleFactor(0.7)
            } minimal: {
                ZStack {
                    Circle().stroke(color.opacity(0.3), lineWidth: 3)
                    Circle()
                        .trim(from: 0, to: s.required == nil ? 1 : s.progress)
                        .stroke(color, style: StrokeStyle(lineWidth: 3, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .padding(2)
            }
            .keylineTint(color)
        }
    }
}

struct LockScreenView: View {
    let name: String
    let s: MachineActivityAttributes.ContentState
    let stale: Bool

    var color: Color { stale ? MachineStateKind.off.color : s.kind.color }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 3)
                .fill(color)
                .frame(width: 6)

            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: s.kind.symbol).foregroundStyle(color)
                    Text(s.kind.label.uppercased())
                        .font(.subheadline.weight(.heavy))
                        .foregroundStyle(color)
                    if !s.program.isEmpty {
                        Text(s.program)
                            .font(.caption)
                            .foregroundStyle(.white.opacity(0.6))
                            .lineLimit(1)
                    }
                    Spacer()
                    Text(name)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.9))
                        .lineLimit(1)
                }

                HStack(alignment: .lastTextBaseline) {
                    VStack(alignment: .leading, spacing: 0) {
                        HStack(spacing: 4) {
                            Text("PARTS")
                            if let on = s.counterStop {
                                Image(systemName: on ? "stop.circle.fill" : "infinity")
                                Text(on ? "AUTO STOP" : "NO STOP")
                            }
                        }
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.55))
                        HStack(alignment: .lastTextBaseline, spacing: 3) {
                            Text(s.parts.map { String($0) } ?? "—")
                                .font(.system(size: 30, weight: .bold, design: .rounded))
                                .monospacedDigit()
                                .foregroundStyle(.white)
                            if let r = s.required, r > 0 {
                                Text("/ \(r)")
                                    .font(.system(size: 17, weight: .semibold, design: .rounded))
                                    .monospacedDigit()
                                    .foregroundStyle(.white.opacity(0.6))
                            }
                        }
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 0) {
                        Text("CYCLE").font(.caption2.weight(.semibold)).foregroundStyle(.white.opacity(0.55))
                        Text(Fmt.duration(s.lastCycle))
                            .font(.system(size: 22, weight: .semibold, design: .rounded))
                            .monospacedDigit()
                            .foregroundStyle(.white)
                        if let start = s.cycleStart, s.kind.isRunning, !stale {
                            Text(start, style: .timer)
                                .font(.caption2.monospacedDigit())
                                .foregroundStyle(.white.opacity(0.55))
                                .multilineTextAlignment(.trailing)
                        }
                    }
                }

                if s.required != nil {
                    ProgressView(value: s.progress)
                        .tint(color)
                }

                if !s.alarms.isEmpty && !stale {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(s.alarms.prefix(2).enumerated()), id: \.offset) { _, a in
                            Label(a, systemImage: "exclamationmark.triangle.fill")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(MachineStateKind.alarm.color)
                                .lineLimit(1)
                        }
                        if s.alarms.count > 2 {
                            Text("+\(s.alarms.count - 2) more")
                                .font(.caption2)
                                .foregroundStyle(.white.opacity(0.6))
                        }
                    }
                } else if s.kind == .barChange {
                    HStack(spacing: 4) {
                        Text("Changing bar")
                        if let start = s.barChangeStart, !stale {
                            Text("·")
                            Text(start, style: .timer)
                        }
                    }
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.75))
                    .lineLimit(1)
                } else if s.kind == .off || s.kind == .standby {
                    Text(s.detail)
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.65))
                        .lineLimit(1)
                }

                if stale || !s.reachable {
                    HStack(spacing: 4) {
                        Image(systemName: "wifi.exclamationmark")
                        Text("No update since \(s.updated, style: .time)")
                    }
                    .font(.caption2)
                    .foregroundStyle(.white.opacity(0.6))
                }
            }
        }
        .padding(14)
    }
}
