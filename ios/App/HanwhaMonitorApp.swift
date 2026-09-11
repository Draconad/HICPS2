import SwiftUI

@main
struct HanwhaMonitorApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = MachineStore()
    @StateObject private var live = LiveActivityManager.shared
    @Environment(\.scenePhase) private var scenePhase

    init() {
        AppSettings.registerDefaults()
        NotificationManager.shared.setup()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
                .environmentObject(live)
                .environmentObject(PushManager.shared)
                .task {
                    _ = await NotificationManager.shared.requestPermission()
                    PushManager.shared.registerAll()
                    store.startPolling()
                }
        }
        .onChange(of: scenePhase) { phase in
            store.scenePhaseChanged(active: phase == .active, background: phase == .background)
        }
        .backgroundTask(.appRefresh(MachineStore.refreshTaskID)) {
            await store.backgroundRefresh()
        }
    }
}

struct RootView: View {
    @EnvironmentObject var store: MachineStore

    var body: some View {
        TabView {
            StatusView()
                .tabItem { Label("Status", systemImage: "gauge.with.dots.needle.67percent") }
            AlarmsView()
                .tabItem { Label("Alarms", systemImage: "exclamationmark.triangle") }
                .badge(store.status?.activeAlarms.count ?? 0)
            ControlsView()
                .tabItem { Label("Controls", systemImage: "slider.horizontal.3") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .tint(Color(red: 0.96, green: 0.46, blue: 0.13))   // Hanwha orange, to go with the black theme
        .preferredColorScheme(.dark)
    }
}
