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

    func clearHistory() async throws {
        struct OK: Decodable { var ok: Bool }
        _ = try await send(try request("/api/alarms", method: "DELETE"), as: OK.self)
    }
}
