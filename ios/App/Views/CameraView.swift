import AVFoundation
import SwiftUI
import UIKit

/// The machine camera, relayed by the PC app through the server - shown on the Status screen (and full screen).
/// While it's on screen the app asks the server for the live video every couple of seconds (which also tells the
/// PC to stream) and plays it with AVPlayer; the latest still is shown until the video starts.
@MainActor
final class CameraModel: ObservableObject {
    let player = LivePlayer()
    @Published var still: UIImage?
    @Published var stillTime: Date?
    @Published var message: String?
    @Published var noCamera = false
    @Published var muted = true { didSet { player.setMuted(muted) } }
    @Published var ptzError: String?
    @Published var ptzBusy = false
    private var stillEtag: String?

    func run(store: MachineStore) async {
        var tick = 0
        while !Task.isCancelled {
            if store.status?.camera?.available == true {
                await pollOnce(store: store, tick: tick)
            } else {
                player.stop()
            }
            tick += 1
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }
        player.stop()
    }

    private func pollOnce(store: MachineStore, tick: Int) async {
        let api = APIClient.current
        do {
            let live = try await api.cameraLive()
            if live.ready, let path = live.url, let url = api.absoluteURL(path) {
                player.play(url: url, session: live.session ?? path)
                player.updateLag(segment: live.segmentS ?? 2)
                player.restartIfStuck(url: url)
            } else if !live.ready {
                player.stop()
            }
            if !player.isPlaying && tick % 2 == 0 {
                switch try await api.cameraFrame(etag: stillEtag) {
                case let .new(data, tag, ft):
                    if let img = UIImage(data: data) {
                        still = img
                        stillEtag = tag
                        stillTime = ft.flatMap { store.status?.date($0) ?? Date(timeIntervalSince1970: $0) }
                    }
                    noCamera = false
                case .unchanged:
                    break
                case .noImage:
                    noCamera = true
                }
            }
            message = nil
        } catch {
            message = error.localizedDescription
        }
    }

    /// Move to a position saved in the Tapo app.
    func goTo(_ preset: MachineStatus.Preset) {
        guard !ptzBusy else { return }
        ptzBusy = true
        Task {
            defer { ptzBusy = false }
            do {
                let r = try await APIClient.current.command(["type": "ptz_preset", "token": preset.token])
                ptzError = r.ok ? nil : r.text
            } catch {
                ptzError = error.localizedDescription
            }
        }
    }

    /// Nudge the camera (x: -1 left ... 1 right, y: -1 down ... 1 up).
    func ptz(x: Double, y: Double) {
        guard !ptzBusy else { return }
        ptzBusy = true
        Task {
            defer { ptzBusy = false }
            do {
                let r = try await APIClient.current.command(["type": "ptz", "x": x, "y": y])
                ptzError = r.ok ? nil : r.text
            } catch {
                ptzError = error.localizedDescription
            }
        }
    }
}

struct CameraPanel: View {
    @ObservedObject var model: CameraModel
    @ObservedObject var player: LivePlayer
    var features: MachineStatus.CameraInfo?
    var fullScreen = false
    var onToggleFullScreen: () -> Void

    // pinch to zoom, drag to pan, double-tap to reset
    @State private var scale: CGFloat = 1
    @State private var baseScale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var baseOffset: CGSize = .zero

    private var hasPicture: Bool { model.still != nil || player.hasItem }

    var body: some View {
        ZStack {
            Color.black
            if hasPicture {
                ZStack {
                    if let still = model.still {
                        Image(uiImage: still).resizable().scaledToFit()
                    }
                    PlayerLayerView(player: player.player)
                        .opacity(player.isPlaying ? 1 : 0)
                }
                .scaleEffect(scale)
                .offset(offset)
                .gesture(SimultaneousGesture(magnify, pan))
                .onTapGesture(count: 2) { resetZoom() }
            } else {
                placeholder
            }
        }
        .aspectRatio(fullScreen ? nil : 16 / 9, contentMode: .fit)
        .frame(maxWidth: .infinity, maxHeight: fullScreen ? .infinity : nil)
        .clipShape(RoundedRectangle(cornerRadius: fullScreen ? 0 : 16, style: .continuous))
        .overlay(alignment: .topLeading) { badge.padding(10) }
        .overlay(alignment: .topTrailing) { topButtons.padding(8) }
        .overlay(alignment: .bottomTrailing) {
            if features?.ptz == true && hasPicture { PTZPad(model: model).padding(8) }
        }
        .overlay(alignment: .bottomLeading) { footnote.padding(10) }
    }

    // MARK: pieces

    @ViewBuilder private var badge: some View {
        if hasPicture {
            HStack(spacing: 6) {
                if player.isPlaying {
                    Circle().fill(Color.red).frame(width: 8, height: 8)
                    Text(player.lag.map { "LIVE · ~\(Int($0.rounded())) s behind" } ?? "LIVE")
                        .font(.caption.weight(.heavy))
                } else if player.hasItem || !model.noCamera {
                    ProgressView().controlSize(.mini).tint(.white)
                    Text("Starting live video…").font(.caption.weight(.semibold))
                } else if let t = model.stillTime {
                    Text("Last image \(t.formatted(date: .omitted, time: .standard))").font(.caption.weight(.semibold))
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(.black.opacity(0.6), in: Capsule())
        }
    }

    private var topButtons: some View {
        HStack(spacing: 8) {
            if features?.audio != false {
                Button {
                    model.muted.toggle()
                } label: {
                    Image(systemName: model.muted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                        .frame(width: 34, height: 34)
                }
                .accessibilityLabel(model.muted ? "Unmute camera" : "Mute camera")
            }
            Button(action: onToggleFullScreen) {
                Image(systemName: fullScreen ? "xmark" : "arrow.up.left.and.arrow.down.right")
                    .frame(width: 34, height: 34)
            }
            .accessibilityLabel(fullScreen ? "Close full screen" : "Full screen")
        }
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(.white)
        .buttonStyle(.plain)
        .background(.black.opacity(0.55), in: Capsule())
    }

    @ViewBuilder private var footnote: some View {
        if let err = model.ptzError ?? (hasPicture ? model.message : nil) {
            Text(err)
                .font(.caption2)
                .lineLimit(2)
                .foregroundStyle(.white)
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 8))
                .frame(maxWidth: 220, alignment: .leading)
        }
    }

    @ViewBuilder private var placeholder: some View {
        VStack(spacing: 10) {
            if model.noCamera {
                Image(systemName: "video.slash").font(.system(size: 30)).foregroundStyle(.secondary)
                Text("No camera image yet").font(.subheadline.weight(.semibold))
                Text("Set it up on the Camera tab of the PC app on the machine.")
                    .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
            } else if let message = model.message {
                Image(systemName: "wifi.exclamationmark").font(.system(size: 28)).foregroundStyle(.secondary)
                Text(message).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
            } else {
                ProgressView()
                Text("Connecting to the camera…").font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(20)
    }

    private var magnify: some Gesture {
        MagnificationGesture()
            .onChanged { v in scale = min(max(baseScale * v, 1), 6) }
            .onEnded { _ in
                baseScale = scale
                if scale <= 1.01 { resetZoom() }
            }
    }

    private var pan: some Gesture {
        DragGesture()
            .onChanged { v in
                guard scale > 1 else { return }
                offset = CGSize(width: baseOffset.width + v.translation.width,
                                height: baseOffset.height + v.translation.height)
            }
            .onEnded { _ in baseOffset = offset }
    }

    private func resetZoom() {
        withAnimation(.easeOut(duration: 0.2)) {
            scale = 1; baseScale = 1; offset = .zero; baseOffset = .zero
        }
    }
}

/// Pan/tilt arrows. Each tap nudges the camera a little; the picture is a few seconds behind, so moves show late.
struct PTZPad: View {
    @ObservedObject var model: CameraModel
    private let step = 0.1

    var body: some View {
        Grid(horizontalSpacing: 2, verticalSpacing: 2) {
            GridRow { Color.clear.frame(width: 1, height: 1); arrow("chevron.up", 0, step); Color.clear.frame(width: 1, height: 1) }
            GridRow {
                arrow("chevron.left", -step, 0)
                Image(systemName: model.ptzBusy ? "circle.dotted" : "circle.fill")
                    .font(.system(size: 6)).foregroundStyle(.white.opacity(0.6))
                arrow("chevron.right", step, 0)
            }
            GridRow { Color.clear.frame(width: 1, height: 1); arrow("chevron.down", 0, -step); Color.clear.frame(width: 1, height: 1) }
        }
        .padding(4)
        .background(.black.opacity(0.5), in: Circle())
    }

    private func arrow(_ symbol: String, _ x: Double, _ y: Double) -> some View {
        Button { model.ptz(x: x, y: y) } label: {
            Image(systemName: symbol)
                .font(.system(size: 15, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 32, height: 32)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(["chevron.up": "Tilt up", "chevron.down": "Tilt down",
                             "chevron.left": "Pan left", "chevron.right": "Pan right"][symbol] ?? symbol)
    }
}

/// The camera's saved positions (from the Tapo app) as a row of buttons under the video.
struct PresetBar: View {
    @ObservedObject var model: CameraModel
    let presets: [MachineStatus.Preset]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                Image(systemName: "scope").foregroundStyle(.secondary)
                ForEach(presets, id: \.self) { p in
                    Button { model.goTo(p) } label: {
                        Text(p.name)
                            .font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 12).padding(.vertical, 7)
                            .background(Color(.tertiarySystemFill), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(model.ptzBusy)
                }
            }
            .padding(.horizontal, 2)
        }
    }
}

/// Full-screen camera (same model and player as the Status screen's panel).
struct CameraFullScreen: View {
    @ObservedObject var model: CameraModel
    var features: MachineStatus.CameraInfo?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 8) {
            CameraPanel(model: model, player: model.player, features: features, fullScreen: true) { dismiss() }
                .ignoresSafeArea(edges: .horizontal)
            if features?.ptz == true, let presets = features?.presets, !presets.isEmpty {
                PresetBar(model: model, presets: presets).padding(.horizontal, 12).padding(.bottom, 8)
            }
        }
        .background(Color.black.ignoresSafeArea())
    }
}

// MARK: - player

/// Owns the AVPlayer for the live HLS stream and reports whether video is actually playing.
@MainActor
final class LivePlayer: ObservableObject {
    let player = AVPlayer()
    @Published private(set) var isPlaying = false
    @Published private(set) var hasItem = false
    /// Roughly how far behind real time the picture is (seconds), while playing.
    @Published private(set) var lag: Double?
    private var session: String?
    private var observation: NSKeyValueObservation?
    private var attachedAt = Date()
    private var lastProgress = Date()
    private var lastTime: Double = -1

    init() {
        player.isMuted = true
        player.automaticallyWaitsToMinimizeStalling = true
        observation = player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] p, _ in
            let playing = p.timeControlStatus == .playing
            Task { @MainActor in self?.isPlaying = playing }
        }
    }

    func play(url: URL, session: String) {
        guard session != self.session else {
            if player.timeControlStatus == .paused { player.play() }
            return
        }
        self.session = session
        attachedAt = Date(); lastProgress = Date(); lastTime = -1
        let item = AVPlayerItem(url: url)
        // stay ~3 chunks (about 6 s) behind live, like the dashboard: a late chunk then doesn't pause the picture
        item.automaticallyPreservesTimeOffsetFromLive = true
        item.configuredTimeOffsetFromLive = CMTime(seconds: 6, preferredTimescale: 600)
        player.replaceCurrentItem(with: item)
        hasItem = true
        player.play()
    }

    /// Distance from the newest video the player has, plus one chunk (a chunk reaches the server once complete).
    func updateLag(segment: Double) {
        guard isPlaying, let item = player.currentItem,
              let range = item.seekableTimeRanges.last?.timeRangeValue else { lag = nil; return }
        let edge = CMTimeGetSeconds(CMTimeRangeGetEnd(range))
        let now = CMTimeGetSeconds(item.currentTime())
        let value = edge - now + segment
        lag = (value.isFinite && value > 0 && value < 120) ? value : nil
    }

    /// Watchdog: if the picture hasn't moved for a while (or the item failed), load the video again from scratch.
    func restartIfStuck(url: URL) {
        guard hasItem, let item = player.currentItem, let current = session else { return }
        let t = CMTimeGetSeconds(item.currentTime())
        if t.isFinite && t != lastTime { lastTime = t; lastProgress = Date() }
        let stuck = Date().timeIntervalSince(lastProgress)
        let age = Date().timeIntervalSince(attachedAt)
        if item.status == .failed || (age > 12 && stuck > 6) {
            self.session = nil
            play(url: url, session: current)
        }
    }

    /// Sound on/off. Unmuting switches the audio session to playback so it's heard even with the ring switch on silent.
    func setMuted(_ muted: Bool) {
        player.isMuted = muted
        if !muted {
            let s = AVAudioSession.sharedInstance()
            try? s.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try? s.setActive(true)
        }
    }

    func stop() {
        guard session != nil || player.currentItem != nil else { return }
        session = nil
        player.pause()
        player.replaceCurrentItem(with: nil)
        hasItem = false
        isPlaying = false
        lag = nil
    }
}

/// AVPlayerLayer in SwiftUI, without playback controls.
struct PlayerLayerView: UIViewRepresentable {
    let player: AVPlayer

    final class View: UIView {
        override static var layerClass: AnyClass { AVPlayerLayer.self }
        var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
    }

    func makeUIView(context: Context) -> View {
        let v = View()
        v.backgroundColor = .clear
        v.playerLayer.videoGravity = .resizeAspect
        v.playerLayer.player = player
        return v
    }

    func updateUIView(_ uiView: View, context: Context) {
        if uiView.playerLayer.player !== player { uiView.playerLayer.player = player }
    }
}
