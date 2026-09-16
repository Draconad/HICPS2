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
            let color = context.isStale ? MachineStateKind.off.color : s.headlineColor
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 6) {
                        Circle().fill(color).frame(width: 12, height: 12)
                        Text(s.shortHeadline)
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
                    VStack(alignment: .trailing, spacing: 0) {
                        Text(s.partsText)
                            .font(.headline.monospacedDigit())
                            .foregroundStyle(.white)
                        if let left = s.remaining {
                            Text(s.isOverProducing ? "overrun" : (left == 0 ? "done" : "\(left) to go"))
                                .font(.caption2.weight(.semibold).monospacedDigit())
                                .foregroundStyle(s.isOverProducing ? MachineStateKind.overProducingColor
                                                                   : Color.white.opacity(0.6))
                        }
                    }
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
                    .foregroundStyle(s.isOverProducing && !context.isStale ? MachineStateKind.overProducingColor : color)
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

/// The lock screen Live Activity. On iOS 18 and later it also knows when it's being shown full screen in
/// StandBy (the phone on its side while charging) and switches to a much bigger layout for that.
struct LockScreenView: View {
    let name: String
    let s: MachineActivityAttributes.ContentState
    let stale: Bool

    var body: some View {
        if #available(iOS 18.0, *) {
            StandByAware(name: name, s: s, stale: stale)
        } else {
            CompactLockScreen(name: name, s: s, stale: stale)
        }
    }
}

@available(iOS 18.0, *)
private struct StandByAware: View {
    @Environment(\.isActivityFullscreen) private var fullscreen
    let name: String
    let s: MachineActivityAttributes.ContentState
    let stale: Bool

    var body: some View {
        if fullscreen {
            StandByLockScreen(name: name, s: s, stale: stale)
        } else {
            CompactLockScreen(name: name, s: s, stale: stale)
        }
    }
}

struct CompactLockScreen: View {
    let name: String
    let s: MachineActivityAttributes.ContentState
    let stale: Bool

    var color: Color { stale ? MachineStateKind.off.color : s.headlineColor }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 3)
                .fill(color)
                .frame(width: 6)

            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: s.kind.symbol).foregroundStyle(color)
                    Text(s.headline.uppercased())
                        .font(.subheadline.weight(.heavy))
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                        .foregroundStyle(color)
                    if s.isOverProducing && !stale {
                        OverrunBadge(size: 10)
                    }
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
                    VStack(alignment: .leading, spacing: 3) {
                        ProgressView(value: s.progress)
                            .tint(color)
                        if let text = s.remainingText {
                            Text(text)
                                .font(.caption.weight(.semibold).monospacedDigit())
                                .foregroundStyle(.white.opacity(0.75))
                                .lineLimit(1)
                        }
                    }
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

                if let msg = s.message, !msg.isEmpty, s.alarms.isEmpty, !stale {
                    Label(msg, systemImage: "text.bubble.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color(red: 0.4, green: 0.7, blue: 1.0))
                        .lineLimit(1)
                }

                if stale || !s.reachable {
                    HStack(spacing: 4) {
                        Image(systemName: "wifi.exclamationmark")
                        Text("No update since \(Fmt.clock(s.updated))")
                    }
                    .font(.caption2)
                    .foregroundStyle(.white.opacity(0.6))
                }
            }
        }
        .padding(14)
    }
}


/// StandBy: the phone sat on its side on the charger by the machine. Everything is big enough to read from
/// across the shop, and there's nothing on it that needs a closer look.
struct StandByLockScreen: View {
    let name: String
    let s: MachineActivityAttributes.ContentState
    let stale: Bool

    private var color: Color { stale ? MachineStateKind.off.color : s.headlineColor }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Image(systemName: s.kind.symbol)
                    .font(.system(size: 30, weight: .semibold))
                Text(s.headline.uppercased())
                    .font(.system(size: 42, weight: .black, design: .rounded))
                    .lineLimit(1)
                    .minimumScaleFactor(0.5)
                if s.isOverProducing && !stale { OverrunBadge(size: 14) }
                Spacer(minLength: 0)
            }
            .foregroundStyle(color)

            HStack(alignment: .lastTextBaseline, spacing: 6) {
                Text(s.parts.map { String($0) } ?? "—")
                    .font(.system(size: 72, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .minimumScaleFactor(0.5)
                    .lineLimit(1)
                if let r = s.required, r > 0 {
                    Text("/ \(r)")
                        .font(.system(size: 34, weight: .semibold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.white.opacity(0.6))
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 0) {
                    Text(Fmt.duration(s.lastCycle))
                        .font(.system(size: 30, weight: .semibold, design: .rounded))
                        .monospacedDigit()
                    Text("CYCLE").font(.caption2.weight(.semibold)).foregroundStyle(.white.opacity(0.55))
                }
            }
            .foregroundStyle(.white)

            if s.required != nil {
                ProgressView(value: s.progress).tint(color).scaleEffect(x: 1, y: 1.6, anchor: .center)
            }

            if let text = s.remainingText {
                Text(text)
                    .font(.system(size: 20, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.white.opacity(0.8))
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }

            if let first = s.alarms.first, !stale {
                Label(first + (s.alarms.count > 1 ? "  +\(s.alarms.count - 1)" : ""),
                      systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 22, weight: .bold))
                    .foregroundStyle(MachineStateKind.alarm.color)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            } else {
                HStack(spacing: 10) {
                    Text(s.program.isEmpty ? name : s.program).lineLimit(1)
                    Spacer(minLength: 0)
                    if stale || !s.reachable {
                        Label("no update since " + Fmt.clock(s.updated), systemImage: "wifi.exclamationmark")
                    }
                }
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(.white.opacity(0.6))
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}
