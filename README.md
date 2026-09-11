# Hanwha XE35 Monitor

Remote status and alarm monitoring for the Hanwha XE35 (FANUC 0i-F), as a replacement for the broken HiCPS app link.

```
 XE35 (FANUC 0i-F)  ──FOCAS──►  Windows PC: HanwhaMonitor.exe  ──HTTP──►  Unraid: hanwha-monitor container  ◄──HTTP──  iPhone app
 192.168.11.11:8193             (agent: reads status/alarms)             (stores history, port 8420)                (Status / Alarms / Live Activity)
```

| Folder | What it is |
|---|---|
| `agent/`  | Windows desktop app. Has a status window, settings, a log and a tray icon, and starts with Windows. |
| `server/` | Docker container for Unraid. Python, with SQLite alarm history, a web dashboard on `:8420`, and Apple push for the iPhone. |
| `ios/`    | SwiftUI iPhone app with a Live Activity and the Dynamic Island. It's built and signed in the cloud and installed with iMazing/3uTools (or iLoader without a paid account). |
| `.github/workflows/` | Free cloud builds for the `.ipa` (on a Mac), the `.exe` (on Windows) and the Docker image. |

---

## 1. Push to GitHub and get the builds (double-click)

GitHub builds everything in the cloud, including the iPhone app (no Mac needed). The repository is **Draconad/HICPS2**, set in `github-repo.txt`.

1. **First time on a PC:** double-click **`github-auth.bat`**. It installs Git and GitHub CLI if they're missing, then signs you in through the browser with a one-time code. You only do this once per PC. If it installs something, close the window and run it again.
2. Double-click **`push-to-github.bat`**. It:
   - commits whatever changed in this folder and pushes it to GitHub;
   - waits for the cloud builds of that exact commit, showing live progress;
   - downloads the results into **`build-out\`**, e.g. `HanwhaMonitor-b1.ipa` (the iPhone app) and `HanwhaMonitor-1.0.0.exe` (the Windows app).
3. If a build fails, its error log is saved in `build-out\` instead. Send me that file.

Details:
- Each build only runs when its own folder changed (`ios/`, `agent/`, `server/`). When a part wasn't rebuilt, the script downloads its last good build, so `build-out\` always has both files.
- If the push worked but the download didn't, run `wait-for-builds.ps1` on its own (right-click → Run with PowerShell). It doesn't push again.
- The push is a force push: this folder is treated as the master copy, and it overwrites anything edited directly on GitHub.
- The FANUC DLLs are never uploaded (`.gitignore` excludes `*.dll`).
- The server image is published to `ghcr.io/draconad/hanwha-monitor-server:latest`.

> Private repos get 2,000 free Action minutes a month. Mac minutes count ×10, so that's roughly 25 iPhone builds a month.

---

## 2. Server on Unraid

**Option A: build it on Unraid (no GitHub needed)**

Copy the `server` folder to `/mnt/user/appdata/hanwha-monitor/src`, then in the Unraid terminal:

```bash
cd /mnt/user/appdata/hanwha-monitor/src
docker build -t hanwha-monitor-server .
docker run -d --name hanwha-monitor --restart unless-stopped \
  -p 8420:8420 -e TZ=Europe/London \
  -v /mnt/user/appdata/hanwha-monitor/data:/data \
  hanwha-monitor-server
```

With the Compose Manager plugin you can instead use `server/docker-compose.yml`. Change `build: .` to `build: /mnt/user/appdata/hanwha-monitor/src`.

**Option B: use the GitHub-built image (stays private)**

The image at `ghcr.io/draconad/hanwha-monitor-server` is private, like the repo. Give Unraid a read-only key to pull it:

1. On GitHub, go to **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)**. Tick only **`read:packages`**, choose an expiry, and copy the token. It has to be a *classic* token, because GitHub's container registry doesn't accept fine-grained ones.
2. In the Unraid terminal, log in once and keep a copy of the login on the flash drive:
   ```bash
   echo "PASTE_TOKEN_HERE" | docker login ghcr.io -u Draconad --password-stdin
   mkdir -p /boot/config/ghcr && cp /root/.docker/config.json /boot/config/ghcr/config.json
   ```
3. Unraid wipes `/root` at every reboot, and that's where Docker keeps the login. To restore it at boot, add this line to the end of `/boot/config/go`:
   ```bash
   mkdir -p /root/.docker && cp /boot/config/ghcr/config.json /root/.docker/config.json
   ```
4. **Docker → Add Container**: set the Repository to `ghcr.io/draconad/hanwha-monitor-server:latest` and fill in the rest from `server/unraid-template.xml`.

To update after a new build, use **Force update** on the container. Unraid's "update available" check often can't see private images, but the pull itself works.

**Check it:** open `http://<unraid-ip>:8420/`. You should see the dashboard showing "Waiting for the monitor PC".

Environment options:

| Variable | Default | Meaning |
|---|---|---|
| `API_KEY` | *(blank)* | Optional shared key. If set, enter the same key in the PC app and the iPhone app. |
| `AGENT_TIMEOUT` | `30` | Seconds without data from the PC before the machine shows **Off**. |
| `TZ` | | Your timezone, used for the "alarms today" count. |
| `APNS_KEY_ID` / `APNS_TEAM_ID` / `APNS_TOPIC` | *(blank)* | Apple push. See section 4. Put the `AuthKey_….p8` in the data folder. |

---

## 2b. Machine PC on a different network from Unraid: use Tailscale

The PC at the machine has to be able to reach Unraid. If they're on different networks (workshop vs home), link them with **Tailscale**. It's a free private VPN: there's no port forwarding, and nothing is exposed to the internet.

1. **Unraid:** go to Apps → search **Tailscale** → install the plugin. Then go to Settings → Tailscale → **Log in** and sign in with a Google or Microsoft account.
2. **Machine PC:** install Tailscale from tailscale.com/download and sign in with the **same account**. Leave it set to start with Windows (the default).
3. At login.tailscale.com → **Machines**, note Unraid's Tailscale address (`100.x.y.z`) or its name (e.g. `tower`).
4. In the PC app, set the **Server URL** to `http://100.x.y.z:8420` (or `http://tower:8420`) and click **Test server**.
5. *(Optional)* Install Tailscale on the iPhone too. The app can then use the same `100.x.y.z` address from anywhere: home, the workshop or 4G.

Tailscale doesn't touch the PC's connection to the lathe (192.168.11.x). If the internet link drops, the PC app keeps polling the machine. Alarms that start and clear while the link is down are held and sent when it comes back.

---

## 3. Windows app on the machine PC

1. Unzip `HanwhaMonitor-exe` into a folder, e.g. `C:\HanwhaMonitor\`.
2. Copy **`Fwlib32.dll` and `fwlibe1.dll`** from `C:\Users\Hanwha\focas\` into the same folder. The app also finds them in that old folder automatically.
3. **Stop the old VS Code script** so the two don't both poll the machine.
4. Run `HanwhaMonitor.exe`. If Windows SmartScreen complains, click **More info → Run anyway**, since the exe isn't code-signed.
5. On the **Settings** tab:
   - **Machine IP**: `192.168.11.11`, port `8193`. These are already filled in from the old script.
   - **Server URL**: `http://<unraid-ip>:8420`. Click **Test server**.
   - Tick **Start automatically when Windows starts** and **Start minimised to the tray**.
   - Click **Save & apply**.
6. The two cards at the top show **Machine: Connected** and **Server: Connected**.

Closing the window hides it to the tray. It keeps monitoring, and the tray icon colour shows the machine state. To exit, right-click the tray icon → **Quit**.
Logs are kept in `%APPDATA%\HanwhaMonitor\logs` and roll over at 1 MB × 5 files. The old script once wrote a 33 MB log.

**Build the exe on the PC yourself (optional):** run `agent\build.bat`. It needs `uv` or 32-bit Python.
**Run from source:** `pythonw -m hanwha_agent`. Add `--headless` to log to the console instead of opening the window, or `--demo` to use a simulated machine.

---

## 4. iPhone app

### With the paid developer account: signed .ipa + Apple push (recommended)

With the paid account, GitHub **signs** the app for your registered iPhone and iPad. You install the `.ipa` it produces with **iMazing** or **3uTools** (no sideloading or re-signing). The signed app lasts **a year** and doesn't count towards any 3-app limit. The server uses **Apple push** to keep the Live Activity, the Dynamic Island and alarm alerts up to date while the app is closed.

You only do this setup once. You **don't** need to create an app in App Store Connect or use TestFlight.

**A. Apple Developer website** (developer.apple.com → Account)
0. **Devices → +**: register your iPhone and iPad by UDID. To find a UDID, plug the device into the PC and look under Device info in iMazing or 3uTools. Only registered devices can install the app.
1. **Identifiers → +**, App IDs → App. Register:
   - `HiCPS-2`, Bundle ID (explicit) **`com.jtquayle.hicps2`**. Tick **Push Notifications** and **Time Sensitive Notifications**.
   - `HiCPS-2 Widget`, **`com.jtquayle.hicps2.widget`**, with no capabilities.
   - To use different IDs, change them in `ios/project.yml`, then set `APNS_TOPIC` on the server to match.
2. **Keys → +**: name it `HiCPS push` and tick **Apple Push Notifications service (APNs)**. Download **`AuthKey_XXXXXXXXXX.p8`** (you can only download it once) and note its **Key ID**. The server uses this key.
3. Note your **Team ID**. It's under Membership details.

**B. An API key so GitHub can sign builds** (appstoreconnect.apple.com → Users and Access → Integrations → App Store Connect API → Team Keys → +)
- Name it `GitHub` with access **Admin**, which lets the build use an Apple-managed signing certificate so you never handle certificates yourself. Download the `.p8`, then note its **Key ID** and the **Issuer ID** shown above the list. You don't need to create an app.

**C. GitHub**: in the HICPS2 repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `ASC_KEY_ID` | API Key ID (B) |
| `ASC_ISSUER_ID` | Issuer ID (B) |
| `ASC_KEY_P8` | open the B `.p8` in Notepad and paste **all** of it, including the BEGIN/END lines |
| `APPLE_TEAM_ID` | Team ID (A3) |

**D. Unraid server** (for push)
1. Copy the APNs key from A2 (`AuthKey_XXXXXXXXXX.p8`) into `/mnt/user/appdata/hanwha-monitor/data/`.
2. Edit the container and add these variables: `APNS_KEY_ID` = the A2 Key ID, `APNS_TEAM_ID` = your Team ID, `APNS_TOPIC` = `com.jtquayle.hicps2`.
3. Rebuild or update the container (this version installs two small Python packages for push). The log should say `push ON`.

**E. Build and install**
1. Run `push-to-github.bat`. It downloads `build-out\HanwhaMonitor-bN.ipa`, now signed by Apple for your devices.
2. Plug the iPhone into the PC. In **iMazing**: select the device → **Manage Apps → Device → Install .ipa**. In **3uTools**: **Apps → Install**. Choose the `.ipa`, then do the same for the iPad.
3. Open HiCPS-2 → Settings: enter the server URL and allow notifications. **Push (Apple)** should say **Working**. Tap **Send test notification** to check.
4. Delete the old sideloaded copy. The new one has a different bundle ID, so the two install side by side.

Updates: push to GitHub, then install the new `.ipa` the same way. It installs over the old one and keeps your settings.
New device: register its UDID (A0), push again so the signing includes it, then install.

What push changes:
- **The Live Activity stays current while the app is closed.** The server sends an update whenever the status, parts, cycle or alarms change, and a refresh every 10 minutes.
- **Alarm notifications arrive even when the app is closed.** They're *Time Sensitive*, so they get through Focus modes. The stopped and off notifications follow your Settings toggles.
- **The 8-hour limit is handled on iOS/iPadOS 17.2 or later.** Just before iOS ends a Live Activity, the server ends it and starts a fresh one by push. It also starts one on its own when the machine changes state and none is showing, unless you've switched Live Activities off in Settings.
- **The silent-audio trick is no longer needed.** It's off by default now.

*Later, if you want automatic updates:* create the app in App Store Connect (**Apps → + → New App**, bundle ID `com.jtquayle.hicps2`). Then add a repository **variable** (Settings → Secrets and variables → Actions → Variables) `IOS_DISTRIBUTION` = `testflight`. From then on, builds go to TestFlight instead of producing an `.ipa`.

### Without push: iLoader (free Apple ID)

If the GitHub secrets aren't set, the build makes an unsigned `HanwhaMonitor-bN.ipa` instead:

1. On the iPhone, turn on **Settings → Privacy & Security → Developer Mode**. The phone restarts.
2. Install the `.ipa` with **iLoader** and your Apple ID.
   > **Don't use Sideloadly for this app.** Sideloadly signs the Live Activity extension in a way iOS rejects (`AMFI: … has entitlements but is not a main binary`). The app still runs, but the Live Activity and the Dynamic Island never appear. iLoader signs it correctly. RED-TOK has the same problem and the same fix.
3. Go to **Settings → General → VPN & Device Management** and trust your Apple ID.
4. Open **HiCPS-2** → Settings: set the server URL, and turn on **Silent-audio keep-alive**. Without push, that's the only way to keep the Live Activity updating.

**What you get**

- **Status:** a big colour header (Running = green, Standby = yellow, Alarm = red, Off = grey) showing how long it's been in that state.
  - Part count / required, with a progress bar and estimated finish time.
  - Last cycle time, plus a live timer for the current cycle.
  - Program number, program comment, and the Main/Sub path modes.
  - Active alarms.
- **Alarms:** full history grouped by day. Each entry shows the FANUC-style code (e.g. `EX1051`), the message, Main/Sub path, the time it happened and how long it lasted. Tap one for details, or filter to active alarms only.
- **Settings:** server, Live Activity, background updates, notifications (new alarm, machine stopped, machine off), and clearing the alarm history.
- **Dynamic Island:**
  - Compact: status-colour dot with `118/500`.
  - Minimal: a status-coloured progress ring.
  - Long-press: status, parts, cycle times, program and the current alarm.
- **Lock screen:** status colour bar, parts/required with progress, cycle time with a live current-cycle timer, and up to two active alarms. It greys out with "No update since…" if the phone loses contact.

### Limits without push (free Apple ID)

- The app has to keep itself awake with silent audio, and iOS still suspends it sometimes, e.g. when a video app takes over the audio. The Live Activity then falls behind until you open the app.
- iOS ends a Live Activity after 8 hours. Opening the app starts a new one.
- Free-signed apps stop launching after 7 days unless iLoader refreshes them.
- The phone needs to reach Unraid: either be on the home Wi-Fi, or run Tailscale (section 2b).

---

## Testing without the machine

Tick **Demo mode** in the PC app's Settings. It simulates cycles, part counts, standby and random alarms such as `EX1051 BARFEEDER EMERGENCY STOP`. This lets you test the server, the iPhone app, notifications and the Live Activity at home. The app shows a "Demo data" label while it's on.

---

## What's read from the machine (FOCAS)

| Data | FOCAS call | Notes |
|---|---|---|
| Run state / mode / e-stop | `cnc_statinfo` per path | Running = START on any path; Alarm = any alarm or E-stop; otherwise Standby. Off = can't connect 3 times in a row. |
| Alarms | `cnc_alarm2` + `cnc_rdalmmsg2` per path | Shown as FANUC codes: `EX1051`, `SV0401`, `OT0500`, `DS0300`… |
| Part count / required | macro `#3901` / `#3902` (fallback: parameters 6711 / 6713) | Read from the **Counter path** setting (default 1 = Main). |
| Total parts | parameter 6712 | |
| Cycle time | Time between part-count increments while running (fallback: CNC cycle timer `cnc_rdtimer` type 3) | "Current cycle" is the live CNC cycle timer. |
| Program | `cnc_exeprgname` (fallback `cnc_rdprgnum`) + comment from `cnc_rdprogdir3` | |

If the part count or program looks wrong on the real machine, try **Counter path = 2** first. Then send me the PC app's log.

## What was fixed from the original script

- Fwlib32 functions are `stdcall` returning `short`. The script loaded them as `cdll` with no return type, which is where the odd error numbers `65520` / `74383344` came from (they're really `-16` = socket error).
- **It never reconnected.** Once the machine dropped off, it logged ~4,700 errors and sent ~80 watchdog pushes. The new app frees the handle and reconnects automatically.
- The watchdog crashed on `response.code` (should have been `status_code`), and its message was missing the `f` in its f-string.
- The log file grew without limit.
- The Pushover keys were hard-coded in the old script. They're no longer used, so you may want to regenerate them in Pushover.

## API (for reference)

`GET /api/status` · `GET /api/alarms?limit=100&before=<epoch>&active=1` · `GET /api/states?hours=24` · `POST /api/ingest` (agent) · `DELETE /api/alarms` (clears history) · `GET /api/health`. When `API_KEY` is set, send the key as the `X-API-Key` header or as `?key=`.
