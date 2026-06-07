import Foundation

struct Config {
    // ── Update these before building ──────────────────────────────────────────
    static let serverURL = "http://34.60.235.98:5001"
    static let apiKey    = Bundle.main.object(forInfoDictionaryKey: "LAB_API_KEY") as? String ?? "CHANGE_ME"
    // ─────────────────────────────────────────────────────────────────────────

    static var baseURL: URL { URL(string: serverURL)! }

    static func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) -> URLRequest {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.setValue(apiKey, forHTTPHeaderField: "X-API-Key")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 15
        if let body {
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        return req
    }
}
