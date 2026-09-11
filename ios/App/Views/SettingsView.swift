import SwiftUI
import UIKit
import UserNotifications

struct SettingsView: View {
    @EnvironmentObject var store: MachineStore
    @EnvironmentObject var live: LiveActivityManager

    @AppStorage(SettingsKey.serverURL) private var serverURL = "http://tower.local:8420"
    @AppStorage(SettingsKey.apiKey) private var apiKey = ""
    @AppStorage(SettingsKey.pollInterval) private var pollInterval = 3.0
    @AppStorage(SettingsKey.backgroundInterval) private var backgroundInterval = 10.0
    @AppStorage(SettingsKey.keepAlive) private var keepAlive = true
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
                    if liveActivity && !live.isActive {
                        Button("Start Live Activity now") {
                            live.start(with: store.status, machineName: store.machineName)
                        }
                    }
                    if let err = live.lastError {
                        Text(err).font(.footnote).foregroundStyle(.orange)
                    }
                } header: {
                    Text("Live Activity")
                } footer: {
                    Text("iOS limits a Live Activity to 8 hours. The app restarts it automatically whenever it's open; if it's been running all day you'll get a reminder to open the app.")
                }

                Section {
                    Toggle("Keep updating in background", isOn: $keepAlive)
                        .onChange(of: keepAlive) { on in
                            if on { BackgroundKeeper.shared.start() } else { BackgroundKeeper.shared.stop() }
                        }
                    Stepper(value: $backgroundInterval, in: 5...60, step: 5) {
                        LabeledContent("Background refresh", value: "\(Int(backgroundInterval)) s")
                    }
                    Stepper(value: $pollInterval, in: 1...30, step: 1) {
                        LabeledContent("Refresh while open", value: "\(Int(pollInterval)) s")
                    }
                } header: {
                    Text("Updates")
                } footer: {
                    Text("Without paid Apple push notifications, the app keeps itself awake in the background by playing silent audio (it won't interrupt music). This is what keeps the Live Activity and alarm alerts live. Turn it off to save battery — the Live Activity will then only update when you open the app.")
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

                Section("About") {
                    LabeledContent("App version", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "—")
                    if let s = store.status {
                        LabeledContent("Machine", value: s.machineName)
                        LabeledContent("Monitor PC", value: s.agentOnline ? "Online" : "Offline")
                    }
                    Text("Sideloaded with a free Apple ID? The app stops opening after 7 days until Sideloadly re-signs it — turn on Sideloadly's automatic refresh to avoid that.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
            .task { notifStatus = await NotificationManager.shared.authorizationStatus() }
            .onChange(of: serverURL) { _ in testResult = nil }
            .onDisappear { store.restartPolling() }
            .confirmationDialog("Clear alarm history?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Clear history", role: .destructive) {
                    Task { try? await APIClient.current.clearHistory() }
                }
            }
        }
    }

    private func test() async {
        testing = true
        defer { testing = false }
        do {
            let h = try await APIClient.current.health()
            testResult = (true, "Connected to server v\(h.version ?? "?")")
            store.restartPolling()
        } catch {
            testResult = (false, error.localizedDescription)
        }
    }
}
