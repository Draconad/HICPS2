import AVFoundation
import Foundation

/// Keeps the app running in the background by playing inaudible silence (UIBackgroundModes = audio).
///
/// A free Apple ID can't use Apple push notifications, so this is what lets the app keep polling the
/// server and updating the Live Activity / Dynamic Island while the phone is locked. It mixes with other
/// audio, so music and podcasts keep playing normally.
final class BackgroundKeeper {
    static let shared = BackgroundKeeper()

    private var player: AVAudioPlayer?
    private var observers: [NSObjectProtocol] = []
    private(set) var isRunning = false

    private init() {
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            guard let self, self.isRunning,
                  let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .ended else { return }
            self.resume()
        })
        observers.append(nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self, self.isRunning else { return }
            self.player = nil
            self.resume()
        })
    }

    func start() {
        guard !isRunning else { return }
        isRunning = true
        resume()
    }

    func stop() {
        isRunning = false
        player?.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func resume() {
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try session.setActive(true)
            if player == nil {
                let p = try AVAudioPlayer(data: Self.silentWAV())
                p.numberOfLoops = -1
                p.volume = 0.01
                p.prepareToPlay()
                player = p
            }
            player?.play()
        } catch {
            print("BackgroundKeeper failed: \(error)")
        }
    }

    /// 1 second of 8 kHz mono 16-bit silence, built in memory.
    private static func silentWAV() -> Data {
        let sampleRate: UInt32 = 8000
        let samples = Int(sampleRate)
        let dataSize = UInt32(samples * 2)
        var d = Data()
        func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
        d.append("RIFF".data(using: .ascii)!); u32(36 + dataSize)
        d.append("WAVE".data(using: .ascii)!)
        d.append("fmt ".data(using: .ascii)!); u32(16); u16(1); u16(1); u32(sampleRate); u32(sampleRate * 2); u16(2); u16(16)
        d.append("data".data(using: .ascii)!); u32(dataSize)
        d.append(Data(count: Int(dataSize)))
        return d
    }
}
