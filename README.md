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

### GitHub build minutes
A private repository gets 2,000 free GitHub Actions minutes a month. **iPhone builds run on Macs, which count 10×**, so one iPhone build is about 100 minutes. Windows builds count 2× (about 10 minutes each), and the server image about 2.
- **The iPhone app is only built when you say so.** The push script asks when the iPhone code has changed since its last build, and defaults to No after 20 s. You can also start one from the Actions tab (**iOS app > Run workflow**), or with `gh workflow run ios.yml`.
- **ffmpeg.exe is no longer re-uploaded with every Windows build**, which saves about 100 MB of storage each time. If you ever need it again, go to the Actions tab and run **Windows agent > Run workflow** (tick "Also include ffmpeg.exe").
- Build downloads are deleted after 7 days, and only the last 3 server images are kept.
- Usage is shown on GitHub under **Settings > Billing and plans > Usage**, and the allowance resets monthly. If it runs out, builds just stop until the reset; nothing is charged unless you've set a spending limit. Public repositories get unlimited free minutes.

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

**Check it:** open `http://<unraid-ip>:8420/`. You'll get a login page:
- The first login is **admin / admin**. You then have to choose your own username and password (at least 8 characters).
- If `API_KEY` is set on the container, the login page also asks for it. You don't need `?key=` in the address any more.
- **Remember the API key on this browser** (ticked by default) makes that browser trusted for a year, so later logins only need the username and password. The key itself isn't stored in the browser, only a random token. Changing `API_KEY` on the container forgets every trusted browser. **Log out & forget this browser**, at the bottom of the dashboard, forgets just the one you're using.
- You then see the dashboard showing "Waiting for the monitor PC". **Change login** and **Log out** are at the bottom of the page.
- A browser stays logged in for 30 days. Changing the login signs out every other browser.
- **Forgotten the login?** Set `RESET_LOGIN=true` on the container and restart. The login goes back to admin / admin. Then remove `RESET_LOGIN`, otherwise the login resets on every restart.

The login only protects the dashboard page. The agent and the iPhone app use the API key. Without `API_KEY`, anyone who can reach the server can still read `/api/…`, and the dashboard shows a warning about this. If the server is reachable from the internet, set `API_KEY`.

Environment options:

| Variable | Default | Meaning |
|---|---|---|
| `API_KEY` | *(blank)* | Shared key for the data API. If set, enter the same key in the PC app, the iPhone app and the dashboard login page. Strongly recommended if the server is reachable from the internet. |
| `RESET_LOGIN` | *(blank)* | `true` resets the dashboard login to admin / admin on start. Remove it again afterwards. |
| `AGENT_TIMEOUT` | `30` | Seconds without data from the PC before the machine shows **Off**. |
| `STANDBY_DELAY` | `4` | Running only changes to **Standby** after the machine has been stopped this many seconds (hides the pause between part cycles). |
| `BAR_CHANGE_ALERT` | `180` | A bar change taking longer than this many seconds sends a "bar change taking long" notification (usually a bar that didn't load). |
| `PUSH_RUNNING_GRACE` | `15` | For phones with **Only while the machine is running** switched on: how many seconds after the machine stops notifications still come through. |
| `TZ` | | Your timezone, used for the "alarms today" and "bar changes today" counts. |
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

### Bar change
While the bar-change subprogram **O9002** is running, the status shows **Bar change** (blue) everywhere: the PC app, dashboard, iPhone app and Live Activity, with a timer. The part made across a bar change doesn't count towards the cycle time. The program number is under Settings > **Bar change program**, where 0 turns it off. There's also an optional **Bar change M code** setting, off by default.
The Program box always shows your main part program, not the subprogram it has called.

### Status at a glance
The dashboard opens with a large status banner, readable from across the shop: green **Running**, blue **Bar change**, amber **Standby**, grey **Off**, and a flashing red **Alarm** that shows the alarm code and message. It also shows when that state started and for how long, e.g. "Since 14:02 · 1 hr, 45 min". The browser tab title and the strip along the top of the page change colour too.

If the machine keeps running past the required count, the status stays green **Running** with an orange **OVERRUN** tag next to it, and shows how many parts over it is. This shows on the dashboard, the iPhone app, the Live Activity and the PC app. It usually means **Stop at required count** is off.

### Finish time and job complete
While the machine is running towards a required count, the part count shows the estimated finish time, e.g. "Done ~17:40", based on the last cycle time. This is on the dashboard, the app, the Live Activity and the PC app. When the count reaches the required count, a **✅ Job complete** notification is sent. It can be switched off in the app under Settings > Notifications.

### Bar change statistics
Every bar change is timed. The dashboard and the app's Status screen show today's count, the average and the last one, plus a 7-day average. A bar change that takes longer than `BAR_CHANGE_ALERT` seconds (3 minutes by default) sends a **⏳ Bar change taking long** notification, which usually means the bar didn't load. The history is at `/api/barchanges`.

### Automatic updates of this app
From version 1.8.0 the PC app updates itself, so the exe only has to be copied to the machine PC once.
1. `push-to-github.bat` stores the update-signing key as a GitHub secret the first time it runs (from `update-signing-key.txt`, which it then deletes). GitHub Actions signs every Windows build with it.
2. After the build, `wait-for-builds.ps1` sends the signed exe to your Unraid server. The first time, it asks for the server address and API key and saves them in `update-server.txt`. That file isn't uploaded to GitHub. Type `skip` to never be asked again.
3. The PC app checks the server every 30 minutes. When there's a newer version, it downloads it, checks the signature, closes, swaps in the new exe and starts again, all within a few seconds. Click the version number at the bottom of the PC app to check straight away.

The PC app only installs a build whose signature matches the key built into it, and that key only exists as a GitHub secret. Neither the server nor anyone with the API key can make the machine PC run anything else. To turn updates off, untick **Update automatically from the server** in Settings. The dashboard footer shows the PC app's version and whether an update is waiting.

### Camera (Tapo C210 or any RTSP camera)
The PC app reads the camera on the machine's network and sends it to the server. It shows on the dashboard, below the part count, and on the iPhone app's **Camera** tab. Only the PC app talks to the camera; nothing new is opened up on the network.
1. **In the Tapo app:** tap the camera > ⚙ Settings > **Advanced Settings > Camera Account**. Create a username and password. This is a separate login just for the camera, not your TP-Link account.
2. **ffmpeg.exe:** copy it next to `HanwhaMonitor.exe` on the machine PC. The download script saves it in `build-out` along with the exe. It's a 64-bit program; if the PC runs 32-bit Windows, the Camera tab will say so, and a 32-bit ffmpeg.exe is needed.
3. **In the PC app's Camera tab:**
   1. Enter the camera IP (`192.168.11.15`) and the Camera Account username and password.
   2. Click **Test camera**. You should get "Camera OK" and a preview.
   3. Tick **Send the camera to the dashboard and iPhone app**, then click **Save & apply**.
4. It's worth giving the camera a fixed IP address (a DHCP reservation in the router) so the address doesn't change.

**How it plays:** while the dashboard or the app's Camera tab is open, the PC app passes the camera's own video straight through to the server, without re-encoding, so the PC barely notices. You get smooth, full-frame-rate video in the browser and the app, a few seconds behind real time. It takes 2-3 s to start, and the latest still shows until then. While nobody is watching, one still is sent every minute.

**Data use:** SD video is roughly 0.3-0.6 Mbit/s, HD roughly 1.5-2.5 Mbit/s, and only while someone is watching. It stops about 20 s after the last viewer leaves.

**If the video stutters:** click **Check video timing** in the PC app's Camera tab. It records 10 s straight from the camera and says what's wrong:
- **The camera's own timestamps jump.** Some Tapo firmware does this, and it shows as a pause every second or a picture that goes black. Tick **Fix camera timing**. It re-stamps each frame with its arrival time and uses no extra CPU. If it still isn't perfectly smooth, also tick **Re-encode the video**. That gives perfectly even frames but uses some CPU on the PC.
- **The video arrives in bursts.** This is Wi-Fi, and the player buffer normally hides it. If you still see it, improve the camera's Wi-Fi signal.

If the video never starts but stills work, the camera may be sending H.265. Tick **Re-encode the video**.
The dashboard and the app restart the video by themselves if the picture ever freezes. Hover over the LIVE badge on the dashboard to see how evenly the video is arriving.

**If the camera test fails:**
- **"rejected the username/password":** use the Camera Account from step 1, not your TP-Link account.
- **Refused or can't reach:** check the IP address. Tapo cameras also only run two of these three at once: Tapo Care, SD card recording, and RTSP (which this uses). Turn one of the other two off.

The camera is behind the dashboard login and the API key, like everything else. Set `API_KEY` if the server is reachable from the internet.

### Machine messages (not alarms)
Operator messages on the CNC screen, such as the XE35's "1 hour" and "30 minutes" to required count, are picked up too. They're read with `cnc_rdopmsg`, since they aren't alarms. Each new one sends a notification (💬, not the alarm style) and shows in blue on the dashboard, the app's Status screen, the Live Activity and the PC app's Overview. The machine's state stays Running. Notifications for them can be switched off in the app under Settings > Notifications > **Machine messages**. The history is at `/api/messages`.

### Camera sound, pan/tilt and saved positions
- **Sound:** the camera's microphone plays with the video, converted on the PC to a format phones and browsers can play. It starts muted; tap the speaker button to unmute. Untick **Camera audio** in the Camera tab to leave sound out.
- **Pan/tilt:** use the arrows over the video, in the app or on the dashboard. On the dashboard they appear when the mouse is over the video, or for a few seconds after tapping it on a touch screen. Each tap nudges the camera a little. The picture is a few seconds behind, so moves show up late.
- **Camera mounted upside down:** tick **Reverse pan** and **Reverse tilt** in the PC app's Camera tab. The Tapo app's image flip turns the picture the right way up but not the motors, so the arrows would otherwise move the wrong way.
- **Positions:** positions saved in the Tapo app (camera > pan/tilt > Preset) appear as buttons under the video. In the iPhone app, the arrows and positions are in a **Camera controls** card under the video that you tap to open or close; the app remembers which. New ones show up within about 5 minutes.
- Pan/tilt uses the camera's ONVIF service (port 2020) with the same Camera Account as the video. Commands go through the server to the PC app over a connection the PC keeps open, so they arrive in well under a second.

### Controls tab (iPhone app)
- **Required part count** and **Stop when count reached** can be changed from the phone. This only works if **Allow remote changes from the app** is ticked in the PC app's Settings on the machine, which is off by default, so it has to be switched on at the machine. Every change asks for confirmation and is logged on the PC.
- The required count is written to FANUC system variable #3902. Stop-at-count writes the work counter signal found with the Signal finder. Only K (keep relay), D or # addresses are ever written; anything else is refused.
- **Cycle start, cycle stop and continuous** are shown but not available yet. Starting or stopping a lathe remotely needs machine-specific signals and a safety interlock, so it can't start with someone at the machine. It will be designed together first.

### Work counter "stop at required count"
Whether the machine stops at the required count is a Hanwha setting, not a standard FANUC one, so its address has to be found once:
1. Open the **Signal finder** tab with the machine idle. Click **Take snapshot A**.
2. Change only the work counter on/off setting on the machine, then click **Take snapshot B + compare**.
3. Change it back and click **Take snapshot B + compare** again. The address that flips both times, e.g. `K5.3`, is the one. Addresses in K, R, E and D are listed first, because they're the likely places for a setting.
4. Type it into **Work counter signal** and click **Use & save**. Put `!` in front if it reads the wrong way round.

The part count then shows **Stops at count** or **Won't stop at count** everywhere.

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
- **Only while the machine is running** (Settings > Notifications): notifications and Live Activity updates are only sent while the machine is running, and for 15 seconds after it stops, so the alarm that stopped it still comes through. Nothing arrives while it's sitting idle, e.g. overnight. This is set per phone.
- **The 8-hour limit is handled on iOS/iPadOS 17.2 or later.** Just before iOS ends a Live Activity, the server ends it and starts a fresh one by push. It also starts one on its own when the machine changes state and none is showing, unless you've switched Live Activities off in Settings.
- **The silent-audio trick is no longer needed.** It's off by default now.

*Later, if you want automatic updates:* create the app in App Store Connect (**Apps → + → New App**, bundle ID `com.jtquayle.hicps2`). Then add a repository **variable** (Settings → Secrets and variables → Actions → Variables) `IOS_DISTRIBUTION` = `testflight`. From then on, builds go to TestFlight instead of producing an `.ipa`.

### Paid Apple ID + iLoader (quickest to try)

Keep installing with **iLoader**, but sign in with your **paid** Apple ID. The GitHub secrets aren't needed for this; the build stays the unsigned `.ipa`. Push may or may not work this way, because it depends on whether iLoader asks Apple for the push permission. The app will tell you which.

1. **Server** (you need this for push whichever way you install):
   - On developer.apple.com, go to **Keys → +**, tick **Apple Push Notifications service (APNs)**, download `AuthKey_XXXXXXXXXX.p8` and note its **Key ID**. Also note your **Team ID** (Membership details).
   - Copy the `.p8` into `/mnt/user/appdata/hanwha-monitor/data/`. Add the container variables `APNS_KEY_ID` and `APNS_TEAM_ID`, then rebuild the container. The log should say `push ON`.
   - You don't need `APNS_TOPIC`. The app tells the server its real bundle ID, even if iLoader renames it.
2. Install `HanwhaMonitor-bN.ipa` with iLoader using the paid Apple ID. The app is then signed for a year instead of 7 days.
3. Open HiCPS-2 → Settings → **Push (Apple)**:
   - **Working**: iLoader kept push. Tap *Send test notification* to confirm. The Live Activity and alarms are now updated by the server, and the silent-audio fallback switches itself off.
   - **"Push not available … aps-environment"**: iLoader dropped push. Everything still works as before on the silent-audio fallback. For real push, use the signed `.ipa` route above (GitHub secrets + iMazing/3uTools).

### Free Apple ID: iLoader without push

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
- **Settings:** server, Live Activity, background updates, notifications (new alarm, machine message, job complete, slow bar change, machine stopped, machine off, only while running), and clearing the alarm history.
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
| Operator messages | `cnc_rdopmsg` (Counter path) | Messages 2000-2099, e.g. the time-to-count warnings. Not alarms. |
| Alarms | `cnc_alarm2` + `cnc_rdalmmsg2` per path | Shown as FANUC codes: `EX1051`, `SV0401`, `OT0500`, `DS0300`… |
| Part count / required | macro `#3901` / `#3902` (fallback: parameters 6711 / 6713) | Read from the **Counter path** setting (default 1 = Main). |
| Total parts | parameter 6712 | |
| Cycle time | Time between part-count increments while running (fallback: CNC cycle timer `cnc_rdtimer` type 3) | "Current cycle" is the live CNC cycle timer. |
| Program | `cnc_exeprgname` (fallback `cnc_rdprgnum`) + comment from `cnc_rdprogdir3` | |
| Bar change | `cnc_rdprgnum` (the program executing right now, vs. the main program) | O9002 running = bar change. Optional M code check via `cnc_rdcommand` / `cnc_rdexecprog`. |
| Work counter on/off | `pmc_rdpmcrng` (one PMC bit) or `cnc_rdmacro` | The address comes from the Signal finder. |

If the part count or program looks wrong on the real machine, try **Counter path = 2** first. Then send me the PC app's log.

## What was fixed from the original script

- Fwlib32 functions are `stdcall` returning `short`. The script loaded them as `cdll` with no return type, which is where the odd error numbers `65520` / `74383344` came from (they're really `-16` = socket error).
- **It never reconnected.** Once the machine dropped off, it logged ~4,700 errors and sent ~80 watchdog pushes. The new app frees the handle and reconnects automatically.
- The watchdog crashed on `response.code` (should have been `status_code`), and its message was missing the `f` in its f-string.
- The log file grew without limit.
- The Pushover keys were hard-coded in the old script. They're no longer used, so you may want to regenerate them in Pushover.

## API (for reference)

`GET /api/status` · `GET /api/alarms?limit=100&before=<epoch>&active=1` · `GET /api/states?hours=24` · `POST /api/ingest` (agent) · `DELETE /api/alarms` (clears history) · `GET /api/health`. When `API_KEY` is set, send the key as the `X-API-Key` header or as `?key=`. A logged-in dashboard session also works.
