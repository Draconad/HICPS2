import SwiftUI
import UIKit

/// Machine camera, relayed by the PC app through the server. While this tab is open the app fetches the
/// latest frame about 4 times a second; the server sees that and tells the PC to stream live.
struct CameraView: View {
    @EnvironmentObject var store: MachineStore
    @Environment(\.scenePhase) private var phase

    @State private var image: UIImage?
    @State private var etag: String?
    @State private var frameTime: Date?
    @State private var lastNewFrame = Date.distantPast
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
                if let image {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
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
                if let message, image != nil {
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
            guard phase == .active else { return }
            await poll()
        }
    }

    // MARK: - pieces

    private var isLive: Bool { Date().timeIntervalSince(lastNewFrame) < 3 }

    @ViewBuilder private var badge: some View {
        if image != nil {
            TimelineView(.periodic(from: .now, by: 1)) { _ in
                HStack(spacing: 6) {
                    if isLive {
                        Circle().fill(Color.red).frame(width: 8, height: 8)
                        Text("LIVE").font(.caption.weight(.heavy))
                    } else if let t = frameTime {
                        Text("Last image \(t.formatted(date: .omitted, time: .standard))")
                            .font(.caption.weight(.semibold))
                    }
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(.black.opacity(0.65), in: Capsule())
            }
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

    private func poll() async {
        while !Task.isCancelled {
            var delay: UInt64 = 250_000_000
            do {
                switch try await APIClient.current.cameraFrame(etag: etag) {
                case let .new(data, newTag, ft):
                    if let img = UIImage(data: data) {
                        image = img
                        etag = newTag
                        lastNewFrame = Date()
                        frameTime = ft.flatMap { store.status?.date($0) ?? Date(timeIntervalSince1970: $0) }
                    }
                    noCamera = false
                    message = nil
                case .unchanged:
                    message = nil
                case .noImage:
                    noCamera = true
                    delay = 3_000_000_000
                }
            } catch {
                message = error.localizedDescription
                delay = 3_000_000_000
            }
            try? await Task.sleep(nanoseconds: delay)
        }
    }
}
