import Foundation

enum APIError: LocalizedError {
    case badURL
    case unauthorized
    case http(Int)
    case unreachable(String)

    var errorDescription: String? {
        switch self {
        case .badURL: return "The server address isn't valid. Check Settings."
        case .unauthorized: return "The server rejected the API key."
        case .http(let code): return "Server error (HTTP \(code))."
        case .unreachable(let why): return "Can't reach the server. \(why)"
        }
    }
}

struct APIClient {
    var baseURL: String
    var apiKey: String

    static var current: APIClient {
        APIClient(baseURL: AppSettings.serverURL, apiKey: AppSettings.apiKey)
    }

    private static let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 6
        cfg.timeoutIntervalForResource = 10
        cfg.waitsForConnectivity = false
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: cfg)
    }()

    /// Separate session for the job PDFs: the shared one is tuned for 6-second status polls, which would
    /// abandon a 20 MB drawing part way through.
    private static let files: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 60
        cfg.timeoutIntervalForResource = 300
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: cfg)
    }()

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    private func request(_ path: String, query: [URLQueryItem] = [], method: String = "GET") throws -> URLRequest {
        var base = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if !base.lowercased().hasPrefix("http") { base = "http://" + base }
        while base.hasSuffix("/") { base.removeLast() }
        guard var comps = URLComponents(string: base + path) else { throw APIError.badURL }
        if !query.isEmpty { comps.queryItems = query }
        guard let url = comps.url, url.host != nil else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = method
        if !apiKey.isEmpty { req.setValue(apiKey, forHTTPHeaderField: "X-API-Key") }
        req.setValue("ios-app", forHTTPHeaderField: "X-Client")   // lets the server know the app is open
        return req
    }

    private func send<T: Decodable>(_ req: URLRequest, as type: T.Type) async throws -> T {
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await Self.session.data(for: req)
        } catch {
            throw APIError.unreachable((error as NSError).localizedDescription)
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw APIError.unauthorized }
        guard (200..<300).contains(code) else { throw APIError.http(code) }
        return try Self.decoder.decode(T.self, from: data)
    }

    func status() async throws -> MachineStatus {
        var s = try await send(try request("/api/status"), as: MachineStatus.self)
        if s.barChange == true && s.state == .running { s.state = .barChange }
        s.clockOffset = Date().timeIntervalSince1970 - s.serverTime
        if abs(s.clockOffset) < 2 { s.clockOffset = 0 }
        return s
    }

    func alarms(limit: Int = 100, before: Double? = nil, activeOnly: Bool = false) async throws -> AlarmPage {
        var q = [URLQueryItem(name: "limit", value: String(limit))]
        if let before { q.append(URLQueryItem(name: "before", value: String(before))) }
        if activeOnly { q.append(URLQueryItem(name: "active", value: "1")) }
        return try await send(try request("/api/alarms", query: q), as: AlarmPage.self)
    }

    func health() async throws -> HealthResponse {
        try await send(try request("/api/health"), as: HealthResponse.self)
    }

    struct PushStatus: Decodable {
        var enabled: Bool
        var error: String?
        var tokens: [String: Int]?
        var sent: Int?
    }

    private func post<T: Decodable>(_ path: String, _ body: [String: Any], as type: T.Type) async throws -> T {
        var req = try request(path, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(req, as: T.self)
    }

    /// Returns whether the server has Apple push configured.
    func pushRegister(kind: String, token: String, activityID: String?, env: String, prefs: [String: Bool]?) async throws -> Bool {
        struct R: Decodable { var ok: Bool; var pushEnabled: Bool? }
        var body: [String: Any] = ["kind": kind, "token": token, "env": env,
                                   "bundle_id": Bundle.main.bundleIdentifier ?? ""]
        if let activityID { body["activity_id"] = activityID }
        if let prefs { body["prefs"] = prefs }
        return try await post("/api/push/register", body, as: R.self).pushEnabled ?? false
    }

    func pushUnregister(activityID: String) async throws {
        struct R: Decodable { var ok: Bool }
        _ = try await post("/api/push/unregister", ["activity_id": activityID], as: R.self)
    }

    func pushUnregister(token: String) async throws {
        struct R: Decodable { var ok: Bool }
        _ = try await post("/api/push/unregister", ["token": token], as: R.self)
    }

    func pushStatus() async throws -> PushStatus {
        try await send(try request("/api/push/status"), as: PushStatus.self)
    }

    func pushTest() async throws -> String {
        struct R: Decodable { var ok: Bool; var sent: Int?; var devices: Int?; var error: String? }
        let r = try await post("/api/push/test", [:], as: R.self)
        return r.ok ? "Test sent to \(r.sent ?? 0) device(s)" : "Not sent: \(r.error?.isEmpty == false ? r.error! : "no devices registered")"
    }

    struct CameraLive: Decodable {
        var ready: Bool
        var session: String?
        var url: String?
        var segmentS: Double?     // length of each video chunk
    }

    /// Live video info. Asking also tells the server someone is watching, so the PC starts streaming.
    func cameraLive() async throws -> CameraLive {
        try await send(try request("/api/camera/live"), as: CameraLive.self)
    }

    /// Absolute URL for a server-relative path (the live video link is relative, e.g. "api/camera/hls/...").
    func absoluteURL(_ path: String) -> URL? {
        var base = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if !base.lowercased().hasPrefix("http") { base = "http://" + base }
        while base.hasSuffix("/") { base.removeLast() }
        return URL(string: base + "/" + path.drop(while: { $0 == "/" }))
    }

    struct CommandResult: Decodable {
        var ok: Bool
        var message: String?
        var error: String?
        var text: String { (ok ? message : (error ?? message)) ?? (ok ? "Done" : "Failed") }
    }

    /// Send a command to the machine's PC through the server (camera pan/tilt, Controls tab) and wait for its answer.
    func command(_ body: [String: Any]) async throws -> CommandResult {
        var req = try request("/api/commands", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 10
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await Self.session.data(for: req)
        } catch {
            throw APIError.unreachable((error as NSError).localizedDescription)
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw APIError.unauthorized }
        if let r = try? Self.decoder.decode(CommandResult.self, from: data) { return r }   // errors come back as JSON too
        throw APIError.http(code)
    }

    enum CameraFrame {
        case new(Data, etag: String?, frameTime: Double?)
        case unchanged
        case noImage
    }

    /// Latest camera still. Pass the previous ETag to get `.unchanged` instead of the same image again.
    func cameraFrame(etag: String?) async throws -> CameraFrame {
        var req = try request("/api/camera/frame.jpg")
        if let etag { req.setValue(etag, forHTTPHeaderField: "If-None-Match") }
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await Self.session.data(for: req)
        } catch {
            throw APIError.unreachable((error as NSError).localizedDescription)
        }
        let http = resp as? HTTPURLResponse
        switch http?.statusCode ?? 0 {
        case 200:
            let ft = (http?.value(forHTTPHeaderField: "X-Frame-Time")).flatMap(Double.init)
            return .new(data, etag: http?.value(forHTTPHeaderField: "ETag"), frameTime: ft)
        case 304: return .unchanged
        case 404: return .noImage
        case 401: throw APIError.unauthorized
        case let code: throw APIError.http(code)
        }
    }

    /// The day's bar changes: when, how many parts the bar made, and which program(s) made them.
    func barDay(date: String? = nil) async throws -> BarDay {
        let q = date.map { [URLQueryItem(name: "date", value: $0)] } ?? []
        return try await send(try request("/api/bars/day", query: q), as: BarDay.self)
    }

    // MARK: Program info

    func programs() async throws -> ProgramList {
        try await send(try request("/api/programs"), as: ProgramList.self)
    }

    func programDetail(_ key: String) async throws -> ProgramRecord {
        try await send(try request("/api/programs/detail", query: [URLQueryItem(name: "program", value: key)]),
                       as: ProgramRecord.self)
    }

    /// Saves the setup sheet for a program: name, sub stick out, manual parts per bar (nil = use the learnt
    /// figure) and notes.
    func saveProgram(_ key: String, name: String, ppbManual: Double?, notes: String, stickOut: Double?) async throws {
        struct R: Decodable { var ok: Bool; var error: String? }
        let body: [String: Any] = ["program": key, "name": name, "notes": notes,
                                   "ppb_manual": ppbManual.map { $0 as Any } ?? NSNull(),
                                   "stick_out": stickOut.map { $0 as Any } ?? NSNull()]
        let r = try await post("/api/programs", body, as: R.self)
        if !r.ok { throw NSError(domain: "HiCPS", code: 1, userInfo: [NSLocalizedDescriptionKey: r.error ?? "Not saved"]) }
    }

    // MARK: Job PDF

    /// Where a program's job PDF lives (used by the PDF viewer, which downloads it itself).
    func docRequest(_ key: String) throws -> URLRequest {
        try request("/api/programs/doc", query: [URLQueryItem(name: "program", value: key)])
    }

    /// Downloads the job PDF to a file the viewer (and Share) can open.
    func downloadDoc(_ key: String, name: String?) async throws -> URL {
        let req = try docRequest(key)
        let (data, resp) = try await Self.files.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw APIError.unauthorized }
        guard (200..<300).contains(code) else { throw APIError.http(code) }
        var file = (name ?? "\(key).pdf").replacingOccurrences(of: "/", with: "-")
        if !file.lowercased().hasSuffix(".pdf") { file += ".pdf" }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(file)
        try data.write(to: url, options: .atomic)
        return url
    }

    func uploadDoc(_ key: String, data: Data, filename: String) async throws {
        struct R: Decodable { var ok: Bool; var error: String? }
        var req = try request("/api/programs/doc", query: [URLQueryItem(name: "program", value: key)], method: "PUT")
        req.setValue("application/pdf", forHTTPHeaderField: "Content-Type")
        req.setValue(filename.filter { $0.isASCII && !$0.isNewline }, forHTTPHeaderField: "X-Filename")
        req.timeoutInterval = 300
        let (out, resp) = try await Self.files.upload(for: req, from: data)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw APIError.unauthorized }
        let r = try? Self.decoder.decode(R.self, from: out)
        if !(200..<300).contains(code) || r?.ok != true {
            throw NSError(domain: "HiCPS", code: code,
                          userInfo: [NSLocalizedDescriptionKey: r?.error ?? "Couldn't upload it (HTTP \(code))"])
        }
    }

    func deleteDoc(_ key: String) async throws {
        struct R: Decodable { var ok: Bool }
        _ = try await send(try request("/api/programs/doc", query: [URLQueryItem(name: "program", value: key)],
                                       method: "DELETE"), as: R.self)
    }

    /// Playable URL for the camera clip saved around an alarm (the key goes in the URL - AVPlayer can't
    /// send headers of its own).
    func clipURL(alarmID: String) -> URL? {
        guard var req = try? request("/api/camera/clip",
                                     query: [URLQueryItem(name: "alarm", value: alarmID)]
                                         + (apiKey.isEmpty ? [] : [URLQueryItem(name: "key", value: apiKey)])),
              let url = req.url else { return nil }
        req.httpMethod = "GET"
        return url
    }

    // MARK: Maintenance (backup battery)

    func maintenance() async throws -> [MaintenanceItem] {
        struct R: Decodable { var items: [MaintenanceItem] }
        return try await send(try request("/api/maintenance"), as: R.self).items
    }

    /// Records when it was last done and how often it needs doing. `changed: true` means "done just now".
    @discardableResult
    func saveMaintenance(_ item: String = "battery", lastAt: Date? = nil, everyMonths: Int? = nil,
                         warnDays: Int? = nil, notes: String? = nil, changed: Bool = false,
                         clearDate: Bool = false) async throws -> MaintenanceItem {
        var body: [String: Any] = ["item": item]
        if changed { body["changed"] = true }
        if clearDate { body["last_at"] = NSNull() }      // "I don't know when it was last done"
        if let lastAt { body["last_at"] = lastAt.timeIntervalSince1970 }
        if let everyMonths { body["every_months"] = everyMonths }
        if let warnDays { body["warn_days"] = warnDays }
        if let notes { body["notes"] = notes }
        var req = try request("/api/maintenance", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        // the server answers with the saved item (and a 400 with the reason if something was wrong)
        return try await send(req, as: MaintenanceItem.self)
    }

    // MARK: Operations report

    /// Parts, bars and machine time by day, month or year.
    func report(period: String, limit: Int) async throws -> OpsReport {
        try await send(try request("/api/report", query: [URLQueryItem(name: "period", value: period),
                                                          URLQueryItem(name: "limit", value: String(limit))]),
                       as: OpsReport.self)
    }

    /// Leave one recorded bar out (a bad one), or pass a program to forget all of its bars.
    func deleteBars(id: Int? = nil, program: String? = nil) async throws {
        struct R: Decodable { var ok: Bool }
        var q: [URLQueryItem] = []
        if let id { q.append(URLQueryItem(name: "id", value: String(id))) }
        if let program { q.append(URLQueryItem(name: "program", value: program)) }
        _ = try await send(try request("/api/bars", query: q, method: "DELETE"), as: R.self)
    }

    /// Your own name for a program (empty removes it). Stored on the server with the program's bar counts.
    func nameProgram(_ key: String, name: String) async throws {
        struct R: Decodable { var ok: Bool; var error: String? }
        let r = try await post("/api/programs", ["program": key, "name": name], as: R.self)
        if !r.ok { throw NSError(domain: "HiCPS", code: 1, userInfo: [NSLocalizedDescriptionKey: r.error ?? "Not saved"]) }
    }

    func clearHistory() async throws {
        struct OK: Decodable { var ok: Bool }
        _ = try await send(try request("/api/alarms", method: "DELETE"), as: OK.self)
    }
}
