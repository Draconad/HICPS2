import SwiftUI
import UIKit
import UserNotifications

struct SettingsView: View {
    @EnvironmentObject var store: MachineStore
    @EnvironmentObject var live: LiveActivityManager
    @EnvironmentObject var push: PushManager
    @State private var pushInfo: String?

    @AppStorage(SettingsKey.serverURL) private var serverURL = "http://tower.local:8420"
    @AppStorage(SettingsKey.apiKey) private var apiKey = ""
    @AppStorage(SettingsKey.pollInterval) private var pollInterval = 3.0
    @AppStorage(SettingsKey.backgroundInterval) private var backgroundInterval = 10.0
    @AppStorage(SettingsKey.keepAlive) private var keepAlive = false
    @AppStorage(SettingsKey.liveActivity) private var liveActivity = true
    @AppStorage(SettingsKey.notifyAlarms) private var notifyAlarms = true
    @AppStorage(SettingsKey.notifyStopped) private var notifyStopped = false
    @AppStorage(SettingsKey.notifyOff) private var notifyOff = false

    @State private var testResult: (ok: Bool, text: String)?
    @State private var testing = false
    @State private var notifStatus: UNAuthorizationStatus = .notDetermined
    @State private var confirmClear = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://192.168.1.50:8420", text: $serverURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit { store.restartPolling() }
                    SecureField("API key (optional)", text: $apiKey)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button {
                        Task { await test() }
                    } label: {
                        HStack {
                            Text("Test connection")
                            Spacer()
                            if testing { ProgressView() }
                        }
                    }
                    .disabled(testing)
                    if let r = testResult {
                        Label(r.text, systemImage: r.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(r.ok ? .green : .red)
                            .font(.footnote)
                    }
                } header: {
                    Text("Server")
                } footer: {
                    Text("The address of the Hanwha Monitor container on your Unraid server, e.g. http://192.168.1.50:8420")
                }

                Section {
                    Toggle("Show on Lock Screen & Dynamic Island", isOn: $liveActivity)
                        .onChange(of: liveActivity) { on in
                            push.liveActivitySettingChanged(enabled: on)
                            Task {
                                if on { live.start(with: store.status, machineName: store.machineName) }
                                else { await live.stop() }
                            }
                        }
                    HStack {
                        Text("Status")
                        Spacer()
                        Text(live.isActive ? "Showing" : "Not showing").foregroundStyle(.secondary)
                    }
                    HStack {
                        Text("Allowed by iOS")
                        Spacer()
                        Text(live.systemEnabled ? "Yes" : "No")
                            .foregroundStyle(live.systemEnabled ? Color.secondary : Color.orange)
                    }
                    if live.isActive {
                        Button("Restart Live Activity") {
                            Task { await live.restart(with: store.status, machineName: store.machineName) }
                        }
                    } else {
                        Button("Start Live Activity now") {
                            if !liveActivity { liveActivity = true }   // onChange starts it
                            else { live.start(with: store.status, machineName: store.machineName) }
                        }
                    }
                    if let err = live.lastError {
                        Text(err).font(.footnote).foregroundStyle(.orange)
                    }
                    Text(live.diagnostics)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                } header: {
                    Text("Live Activity")
                } footer: {
                    Text("iOS limits a Live Activity to 8 hours. The app restarts it automatically whenever it's open; if it's been running all day you'll get a reminder to open the app.")
                }

                Section {
                    Toggle("Silent-audio keep-alive (no push)", isOn: $keepAlive)
                        .onChange(of: keepAlive) { on in
                            if on { BackgroundKeeper.shared.start() } else { BackgroundKeeper.shared.stop() }
                        }
                    Stepper(value: $backgroundInterval, in: 5...60, step: 5) {
                        LabeledContent("Background refresh", value: "\(Int(backgroundInterval)) s")
                    }
                    Stepper(value: $pollInterval, in: 1...30, step: 1) {
                        LabeledContent("Refresh while open", value: "\(Int(pollInterval)) s")
                    }
                    Text(keeperDiagnostics)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                } header: {
                    Text("Updates")
                } footer: {
                    Text("With Apple push working (see Push below) the server keeps the Live Activity and alerts up to date on its own, so leave this OFF. It's only a fallback for builds without push: it keeps the app awake by playing silent audio.")
                }

                Section {
                    HStack {
                        Text("Status")
                        Spacer()
                        Text(push.serverHandlesPush ? "Working" : (push.deviceToken == nil ? "Not registered" : "Server not set up"))
                            .foregroundStyle(push.serverHandlesPush ? Color.green : Color.orange)
                    }
                    if let err = push.lastError {
                        Text(err).font(.footnote).foregroundStyle(.orange)
                    }
                    if let info = pushInfo {
                        Text(info).font(.footnote).foregroundStyle(.secondary)
                    }
                    Button("Send test notification") {
                        Task {
                            pushInfo = "Sending…"
                            do { pushInfo = try await APIClient.current.pushTest() }
                            catch { pushInfo = error.localizedDescription }
                        }
                    }
                    Button("Re-register with server") { push.registerAll() }
                } header: {
                    Text("Push (Apple)")
                } footer: {
                    Text("The server sends alarm alerts and keeps the Live Activity current through Apple push, even when the app is closed. Needs the APNs key set up on the server (see README).")
                }

                Section {
                    Toggle("New alarms", isOn: $notifyAlarms)
                    Toggle("Machine stopped (running → standby)", isOn: $notifyStopped)
                    Toggle("Machine switched off / offline", isOn: $notifyOff)
                    if notifStatus == .denied {
                        Button("Notifications are blocked — open iOS Settings") {
                            if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                        }
                        .foregroundStyle(.orange)
                    } else if notifStatus == .notDetermined {
                        Button("Allow notifications") {
                            Task {
                                _ = await NotificationManager.shared.requestPermission()
                                notifStatus = await NotificationManager.shared.authorizationStatus()
                            }
                        }
                    }
                } header: {
                    Text("Notifications")
                }

                Section {
                    Button("Clear alarm history", role: .destructive) { confirmClear = true }
                } header: {
                    Text("Data")
                } footer: {
                    Text("Deletes cleared alarms from the server. Active alarms are kept.")
                }

                Section {
                    NavigationLink("Background log") { BackgroundLogView() }
                } footer: {
                    Text("What the app did while the phone was locked. Share it if the Live Activity falls behind.")
                }

                Section("About") {
                    LabeledContent("App version", value: "\(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?") (build \(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?"))")
                    if let s = store.status {
                        LabeledContent("Machine", value: s.machineName)
                        LabeledContent("Monitor PC", value: s.agentOnline ? "Online" : "Offline")
                    }
                    Text("Installed from TestFlight? Builds expire after 90 days - each new push to GitHub makes a fresh one.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
            .task { notifStatus = await NotificationManager.shared.authorizationStatus() }
            .onChange(of: serverURL) { _ in testResult = nil }
            .onChange(of: notifyAlarms) { _ in push.registerAll() }
            .onChange(of: notifyStopped) { _ in push.registerAll() }
            .onChange(of: notifyOff) { _ in push.registerAll() }
            .onDisappear { store.restartPolling() }
            .confirmationDialog("Clear alarm history?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Clear history", role: .destructive) {
                    Task { try? await APIClient.current.clearHistory() }
                }
            }
        }
    }

    private var keeperDiagnostics: String {
        let k = BackgroundKeeper.shared
        var parts = ["silent audio \(k.isRunning ? (k.isPlaying ? "playing" : "PAUSED") : "off")"]
        if let t = store.lastBackgroundPoll {
            parts.append("last locked poll \(t.formatted(date: .omitted, time: .standard))")
        }
        if k.stopCount > 0, let d = k.lastStopDate {
            parts.append("stopped \(k.stopCount)x, last: \(k.lastStopReason) at \(d.formatted(date: .omitted, time: .shortened))")
        }
        return parts.joined(separator: " · ")
    }

    private func test() async {
        testing = true
        defer { testing = false }
        do {
            let h = try await APIClient.current.health()
            testResult = (true, "Connected to server v\(h.version ?? "?")")
            store.restartPolling()
            push.registerAll()
        } catch {
            testResult = (false, error.localizedDescription)
        }
    }
}


struct BackgroundLogView: View {
    @State private var text = EventLog.shared.text

    var body: some View {
        ScrollView {
            Text(text.isEmpty ? "Nothing logged yet." : text)
                .font(.caption2.monospaced())
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
                .padding()
        }
        .navigationTitle("Background log")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                ShareLink(item: EventLog.shared.text)
                Button("Clear") { EventLog.shared.clear(); text = "" }
            }
        }
        .refreshable { text = EventLog.shared.text }
    }
}
