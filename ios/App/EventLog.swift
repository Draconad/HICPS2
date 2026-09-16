import Foundation

/// A small rolling log of what the app did while in the background, viewable and shareable from
/// Settings → Background log. Kept on disk so it survives the app being suspended or relaunched.
final class EventLog {
    static let shared = EventLog()

    private let queue = DispatchQueue(label: "EventLog")
    private var lines: [String] = []
    private let maxLines = 800
    private let url: URL
    private var dirty = false
    private let fmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    private init() {
        url = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("background-log.txt")
        if let s = try? String(contentsOf: url, encoding: .utf8) {
            lines = s.split(separator: "\n").map(String.init).suffix(maxLines).map { $0 }
        }
    }

    func add(_ message: String) {
        let line = "\(fmt.string(from: Date()))  \(message)"
        queue.async {
            self.lines.append(line)
            if self.lines.count > self.maxLines { self.lines.removeFirst(self.lines.count - self.maxLines) }
            if !self.dirty {
                self.dirty = true
                self.queue.asyncAfter(deadline: .now() + 2) { self.flush() }
            }
        }
    }

    private func flush() {
        dirty = false
        try? lines.joined(separator: "\n").write(to: url, atomically: true, encoding: .utf8)
    }

    var text: String { queue.sync { lines.reversed().joined(separator: "\n") } }

    func clear() {
        queue.async {
            self.lines.removeAll()
            self.flush()
        }
    }
}
