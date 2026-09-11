import AVFoundation
import SwiftUI
import UIKit

/// Machine camera, relayed by the PC app through the server. While this tab is open the app asks the server for
/// the live video every couple of seconds (which also tells the PC to stream), and plays it with AVPlayer.
/// The latest still is shown until the video starts.
struct CameraView: View {
    @EnvironmentObject var store: MachineStore
    @Environment(\.scenePhase) private var phase
    @StateObject private var player = LivePlayer()

    @State private var still: UIImage?
    @State private var stillEtag: String?
    @State private var stillTime: Date?
    @State private var message: String?
    @State private var noCamera = false

    // pinch to zoom, drag to pan, double-tap to reset
    @State private var scale: CGFloat = 1
    @State private var baseScale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var baseOffset: CGSize = .zero

    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()
                if still != nil || player.hasItem {
                    ZStack {
                        if let still {
                            Image(uiImage: still).resizable().scaledToFit()
                        }
                        PlayerLayerView(player: player.player)
                            .opacity(player.isPlaying ? 1 : 0)
                    }
                    .scaleEffect(scale)
                    .offset(offset)
                    .gesture(SimultaneousGesture(magnify, pan))
                    .onTapGesture(count: 2) { resetZoom() }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .clipped()
                } else {
                    placeholder
                }
            }
            .overlay(alignment: .topLeading) { badge.padding(12) }
            .overlay(alignment: .bottom) {
                if player.isPlaying && message == nil {
                    Text(player.lag.map { "Live video is about \(Int($0.rounded())) seconds behind real time" }
                         ?? "Live video runs a few seconds behind real time")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.8))
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .background(.black.opacity(0.55), in: Capsule())
                        .padding(.bottom, 12)
                } else if let message, still != nil {
                    Text(message)
                        .font(.footnote)
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(.black.opacity(0.7), in: Capsule())
                        .padding(.bottom, 12)
                }
            }
            .navigationTitle(store.status?.machineName ?? "Camera")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(.visible, for: .navigationBar)
        }
        // runs while the tab is visible and the app is in the foreground; cancelled otherwise
        .task(id: phase) {
            guard phase == .active else { player.stop(); return }
            await run()
            player.stop()
        }
        .onDisappear { player.stop() }
    }

    // MARK: - pieces

    @ViewBuilder private var badge: some View {
        if still != nil || player.hasItem {
            HStack(spacing: 6) {
                if player.isPlaying {
                    Circle().fill(Color.red).frame(width: 8, height: 8)
                    Text(player.lag.map { "LIVE · ~\(Int($0.rounded())) s behind" } ?? "LIVE")
                        .font(.caption.weight(.heavy))
                } else if player.hasItem || !noCamera {
                    ProgressView().controlSize(.mini).tint(.white)
                    Text("Starting live video…").font(.caption.weight(.semibold))
                } else if let t = stillTime {
                    Text("Last image \(t.formatted(date: .omitted, time: .standard))").font(.caption.weight(.semibold))
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(.black.opacity(0.65), in: Capsule())
        }
    }

    @ViewBuilder private var placeholder: some View {
        VStack(spacing: 12) {
            if noCamera {
                Image(systemName: "video.slash").font(.system(size: 40)).foregroundStyle(.secondary)
                Text("No camera image yet").font(.headline)
                Text("Set up the camera on the Camera tab of the PC app on the machine.")
                    .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            } else if let message {
                Image(systemName: "wifi.exclamationmark").font(.system(size: 36)).foregroundStyle(.secondary)
                Text(message).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            } else {
                ProgressView()
                Text("Connecting to the camera…").font(.subheadline).foregroundStyle(.secondary)
            }
        }
        .padding(32)
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

    // MARK: - fetching

    private func run() async {
        var tick = 0
        while !Task.isCancelled {
            let api = APIClient.current
            do {
                // live video: the server hands out a link once the PC is streaming
                let live = try await api.cameraLive()
                if live.ready, let path = live.url, let url = api.absoluteURL(path) {
                    player.play(url: url, session: live.session ?? path)
                    player.updateLag(segment: live.segmentS ?? 2)
                    player.restartIfStuck(url: url)
                } else if !live.ready {
                    player.stop()
                }
                // the still: straight away, then every few seconds until the video is playing
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
            tick += 1
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }
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
