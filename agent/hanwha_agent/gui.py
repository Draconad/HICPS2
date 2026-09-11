"""Tkinter desktop UI for the Hanwha monitor agent."""
from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from datetime import datetime
from tkinter import messagebox, ttk

from .camera import CameraRelay
from .collector import Collector
from . import signals
from .focas import FocasMachine, MockMachine, parse_signal
from .config import VERSION, Config, data_dir, normalize_url, set_autostart
from .uploader import Uploader

log = logging.getLogger("hanwha.gui")

STATE_COLORS = {"running": "#2fb344", "barchange": "#2f8cff", "standby": "#f2b90c", "alarm": "#e5383b", "off": "#8a8f98"}
STATE_LABELS = {"running": "RUNNING", "barchange": "BAR CHANGE", "standby": "STANDBY", "alarm": "ALARM", "off": "OFF"}
BG = "#000000"
CARD = "#141414"
FIELD = "#1f1f1f"      # inputs, buttons, tabs
LINE = "#2c2c2c"
INK = "#f2f2f2"
MUTED = "#8e8e93"
HEADER = "#000000"
ACCENT = "#f47521"     # Hanwha orange

try:  # camera preview
    import io
    from PIL import Image as PILImage, ImageTk
except Exception:  # pragma: no cover
    ImageTk = None

try:  # optional system tray support
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover
    pystray = None


def fmt_duration(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 60:
        return f"{int(d)}s ago"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M")


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass


class App:
    def __init__(self, cfg: Config, start_minimized: bool = False):
        self.cfg = cfg
        self.collector: Collector | None = None
        self.uploader: Uploader | None = None
        self.camera: CameraRelay | None = None
        self.log_q: queue.Queue = queue.Queue(maxsize=2000)
        h = QueueLogHandler(self.log_q)
        h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
        logging.getLogger("hanwha").addHandler(h)

        self.root = tk.Tk()
        self.root.title(f"Hanwha Monitor — {cfg.machine_name}")
        self.root.geometry("620x800")
        self.root.minsize(540, 640)
        self.root.configure(bg=BG)
        self._dark_title_bar()
        self._set_window_icon()
        self._styles()
        self._build()
        self.tray = None
        self._tray_state = None
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.start_services()
        self.root.after(300, self.refresh)
        self.root.after(200, self.drain_log)
        if pystray:
            self._start_tray()
        if start_minimized:
            self.root.after(50, self.hide)

    # ------------------------------------------------------------------ styling
    def _font(self, size: int, bold: bool = False, mono: bool = False):
        key = (size, bold, mono)
        if key not in self._fonts:
            fams = set(tkfont.families(self.root))
            pref = ("Consolas", "Menlo", "DejaVu Sans Mono") if mono else ("Segoe UI", "Helvetica Neue", "DejaVu Sans", "Helvetica")
            fam = next((f for f in pref if f in fams), "TkDefaultFont")
            self._fonts[key] = tkfont.Font(root=self.root, family=fam, size=size, weight="bold" if bold else "normal")
        return self._fonts[key]

    def _styles(self):
        self._fonts = {}
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        base = self._font(10, False)
        s.configure(".", font=base, background=BG, foreground=INK, fieldbackground=FIELD, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, troughcolor=FIELD, selectbackground=ACCENT,
                    selectforeground="#000000", insertcolor=INK, focuscolor=ACCENT)
        s.map(".", foreground=[("disabled", "#5a5a5e")])
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=INK)
        s.configure("TButton", background=FIELD, foreground=INK, bordercolor=LINE, padding=(10, 4))
        s.map("TButton", background=[("disabled", CARD), ("pressed", "#333333"), ("active", "#2a2a2a")],
              bordercolor=[("focus", ACCENT)])
        s.configure("Accent.TButton", background=ACCENT, foreground="#000000", bordercolor=ACCENT)
        s.map("Accent.TButton", background=[("pressed", "#d9621a"), ("active", "#ff8a3d")],
              foreground=[("active", "#000000")])
        s.configure("TEntry", fieldbackground=FIELD, foreground=INK, insertcolor=INK, bordercolor=LINE)
        s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        s.configure("TCheckbutton", background=BG, foreground=INK, indicatorbackground=FIELD,
                    indicatorforeground=ACCENT)
        s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", FIELD)])
        for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            s.configure(sb, background="#3a3a3a", troughcolor="#0f0f0f", bordercolor="#0f0f0f", lightcolor="#3a3a3a",
                        darkcolor="#3a3a3a", arrowcolor=MUTED, gripcount=0, relief="flat")
            s.map(sb, background=[("active", "#4a4a4a"), ("disabled", "#1a1a1a")])
        s.configure("Card.TFrame", background=CARD, relief="flat")
        s.configure("Card.TLabel", background=CARD, foreground=INK)
        s.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=self._font(9, False))
        s.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=self._font(9, True))
        s.configure("Value.TLabel", background=CARD, foreground=INK, font=self._font(20, True))
        s.configure("Small.TLabel", background=CARD, foreground=INK, font=self._font(11, False))
        s.configure("TNotebook", background=BG, borderwidth=0, bordercolor=LINE)
        s.configure("TNotebook.Tab", padding=(14, 6), font=self._font(10, False), background=CARD,
                    foreground=MUTED, bordercolor=LINE)
        s.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", INK)],
              lightcolor=[("selected", ACCENT)])
        s.configure("Form.TLabel", background=BG, foreground=INK)
        s.configure("Hint.TLabel", background=BG, foreground=MUTED, font=self._font(9, False))
        s.configure("Accent.TButton", font=self._font(10, True))
        s.configure("Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT, bordercolor=FIELD,
                    lightcolor=ACCENT, darkcolor=ACCENT)
        s.configure("Parts.Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT, thickness=8,
                    borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
        s.configure("Treeview", rowheight=24, font=self._font(9, False), background=CARD, fieldbackground=CARD,
                    foreground=INK, bordercolor=LINE)
        s.map("Treeview", background=[("selected", "#3a2414")], foreground=[("selected", INK)])
        s.configure("Treeview.Heading", font=self._font(9, True), background=FIELD, foreground=MUTED,
                    bordercolor=LINE, relief="flat")
        s.map("Treeview.Heading", background=[("active", "#2a2a2a")])

    def _scrolled_text(self, parent, **kw) -> tk.Text:
        """Text box with a dark ttk scrollbar (tk's own scrollbar can't be recoloured on Windows).
        Pack/grid the returned widget's .master."""
        frame = tk.Frame(parent, bg=BG)
        t = tk.Text(frame, highlightthickness=0, borderwidth=0, selectbackground=ACCENT, selectforeground="#000000",
                    **kw)
        sb = ttk.Scrollbar(frame, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        return t

    def _dark_title_bar(self):
        """Dark Windows title bar (Windows 10 20H1+ / 11)."""
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new id, then the pre-20H1 one)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                    break
        except Exception:
            pass

    def _set_window_icon(self):
        try:
            img = tk.PhotoImage(width=32, height=32)
            img.put("#000000", to=(0, 0, 32, 32))
            img.put(ACCENT, to=(6, 6, 26, 26))
            img.put("#000000", to=(11, 11, 21, 21))
            self.root.iconphoto(True, img)
            self._icon_img = img
        except Exception:
            pass

    # ------------------------------------------------------------------ layout
    def _build(self):
        header = tk.Frame(self.root, bg=HEADER, padx=18, pady=14)
        header.pack(fill="x")
        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x")
        left = tk.Frame(header, bg=HEADER)
        left.pack(side="left")
        self.title_lbl = tk.Label(left, text=self.cfg.machine_name, bg=HEADER, fg="white",
                                  font=self._font(15, True))
        self.title_lbl.pack(anchor="w")
        self.detail_lbl = tk.Label(left, text="Starting…", bg=HEADER, fg="#aab0ba", font=self._font(9, False))
        self.detail_lbl.pack(anchor="w")
        self.pill = tk.Canvas(header, width=132, height=38, bg=HEADER, highlightthickness=0)
        self.pill.pack(side="right")

        conn = tk.Frame(self.root, bg=BG, padx=14, pady=10)
        conn.pack(fill="x")
        conn.columnconfigure((0, 1), weight=1, uniform="c")
        self.machine_card = self._conn_card(conn, "MACHINE (FOCAS)", 0)
        self.server_card = self._conn_card(conn, "SERVER (UNRAID)", 1)

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self.nb = nb
        nb.add(self._build_overview(nb), text="Overview")
        nb.add(self._build_settings(nb), text="Settings")
        nb.add(self._build_log(nb), text="Log")
        nb.add(self._build_camera(nb), text="Camera")
        nb.add(self._build_signals(nb), text="Signal finder")

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=16, pady=(0, 8))
        self.demo_lbl = tk.Label(foot, text="", bg=BG, fg="#f59e0b", font=self._font(9, True))
        self.demo_lbl.pack(side="left")
        tk.Label(foot, text=f"v{VERSION}", bg=BG, fg=MUTED, font=self._font(8, False)).pack(side="right")

    def _conn_card(self, parent, title, col):
        f = ttk.Frame(parent, style="Card.TFrame", padding=(12, 8))
        f.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 6 if col == 0 else 0))
        ttk.Label(f, text=title, style="CardTitle.TLabel").pack(anchor="w")
        row = ttk.Frame(f, style="Card.TFrame")
        row.pack(anchor="w", fill="x", pady=(2, 0))
        dot = tk.Canvas(row, width=12, height=12, bg=CARD, highlightthickness=0)
        dot.pack(side="left", padx=(0, 6))
        oval = dot.create_oval(1, 1, 11, 11, fill="#8a8f98", outline="")
        status = ttk.Label(row, text="…", style="Card.TLabel", font=self._font(10, True))
        status.pack(side="left")
        sub = ttk.Label(f, text="", style="Muted.TLabel")
        sub.pack(anchor="w")
        return {"dot": dot, "oval": oval, "status": status, "sub": sub}

    def _set_card(self, card, ok: bool | None, text: str, sub: str):
        color = "#8a8f98" if ok is None else ("#2fb344" if ok else "#e5383b")
        card["dot"].itemconfigure(card["oval"], fill=color)
        card["status"].configure(text=text)
        card["sub"].configure(text=sub)

    def _value_card(self, parent, title, r, c, colspan=1):
        f = ttk.Frame(parent, style="Card.TFrame", padding=(12, 8))
        f.grid(row=r, column=c, columnspan=colspan, sticky="nsew", padx=4, pady=4)
        ttk.Label(f, text=title, style="CardTitle.TLabel").pack(anchor="w")
        v = ttk.Label(f, text="—", style="Value.TLabel")
        v.pack(anchor="w")
        sub = ttk.Label(f, text="", style="Muted.TLabel")
        sub.pack(anchor="w")
        return f, v, sub

    def _build_overview(self, nb):
        page = tk.Frame(nb, bg=BG, padx=6, pady=8)
        page.columnconfigure((0, 1), weight=1, uniform="v")
        f, self.parts_v, self.parts_sub = self._value_card(page, "PART COUNT", 0, 0)
        self.parts_sub.configure(justify="left")
        self.parts_bar = ttk.Progressbar(f, style="Parts.Horizontal.TProgressbar", maximum=100)
        self.parts_bar.pack(fill="x", pady=(4, 0))
        _, self.cycle_v, self.cycle_sub = self._value_card(page, "CYCLE TIME", 0, 1)
        _, self.prog_v, self.prog_sub = self._value_card(page, "PROGRAM", 1, 0, colspan=2)
        self.prog_v.configure(style="Small.TLabel", font=self._font(13, True))

        af = ttk.Frame(page, style="Card.TFrame", padding=(12, 8))
        af.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        page.rowconfigure(2, weight=1)
        ttk.Label(af, text="ACTIVE ALARMS", style="CardTitle.TLabel").pack(anchor="w")
        cols = ("since", "path", "code", "message")
        self.alarm_tree = ttk.Treeview(af, columns=cols, show="headings", height=5)
        for c, w, t in (("since", 70, "Since"), ("path", 50, "Path"), ("code", 70, "Code"), ("message", 300, "Message")):
            self.alarm_tree.heading(c, text=t, anchor="w")
            self.alarm_tree.column(c, width=w, anchor="w", stretch=(c == "message"))
        self.alarm_tree.tag_configure("alarm", foreground="#ff6b6b")
        self.alarm_tree.pack(fill="both", expand=True, pady=(4, 0))
        self.no_alarm_lbl = ttk.Label(af, text="No active alarms", style="Muted.TLabel")
        self.no_alarm_lbl.pack(anchor="w")
        return page

    def _build_settings(self, nb):
        page = tk.Frame(nb, bg=BG, padx=12, pady=10)
        page.columnconfigure(1, weight=1)
        self.vars: dict[str, tk.Variable] = {}
        rows = [
            ("machine_name", "Machine name", "str", ""),
            ("machine_ip", "Machine IP address", "str", "FOCAS Ethernet address of the 0i-F control"),
            ("machine_port", "FOCAS port", "int", "Default 8193"),
            ("poll_interval", "Poll every (seconds)", "float", ""),
            ("count_path", "Counter path", "int", "Path used for part count, cycle time & program (1 = Main)"),
            ("bar_change_program", "Bar change program", "int", "Bar change while this subprogram runs: 9002 = O9002 (0 = off)"),
            ("bar_change_mcode", "Bar change M code", "int", "Optional extra: also while this M code is the active block (0 = off)"),
            ("work_counter_signal", "Work counter signal", "str", "Address that is on when 'stop at required count' is enabled, e.g. K5.3 (find it with Signal finder; ! in front inverts)"),
            ("server_url", "Server URL", "str", "Unraid address + port, e.g. http://100.x.y.z:8420 (Tailscale) or http://192.168.1.50:8420"),
            ("api_key", "API key", "str", "Only if you set API_KEY on the Docker container"),
            ("dll_path", "Fwlib32.dll folder", "str", "Leave blank to use the folder the .exe is in"),
        ]
        r = 0
        for key, label, kind, hint in rows:
            ttk.Label(page, text=label, style="Form.TLabel").grid(row=r, column=0, sticky="w", pady=(6, 0), padx=(0, 10))
            val = getattr(self.cfg, key)
            var = tk.StringVar(value=str(val))
            self.vars[key] = var
            e = ttk.Entry(page, textvariable=var, show="•" if key == "api_key" else "")
            e.grid(row=r, column=1, sticky="ew", pady=(6, 0))
            r += 1
            if hint:
                ttk.Label(page, text=hint, style="Hint.TLabel").grid(row=r, column=1, sticky="w")
                r += 1
        for key, label in (("autostart", "Start automatically when Windows starts"),
                           ("start_minimized", "Start minimised to the tray"),
                           ("demo_mode", "Demo mode (simulated machine — for testing the app)")):
            var = tk.BooleanVar(value=bool(getattr(self.cfg, key)))
            self.vars[key] = var
            ttk.Checkbutton(page, text=label, variable=var).grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
            r += 1
        btns = tk.Frame(page, bg=BG)
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(btns, text="Save & apply", style="Accent.TButton", command=self.apply_settings).pack(side="left")
        ttk.Button(btns, text="Test server", command=self.test_server).pack(side="left", padx=8)
        ttk.Button(btns, text="Open dashboard", command=self.open_dashboard).pack(side="left")
        ttk.Button(btns, text="Open log folder", command=self.open_logs).pack(side="right")
        return page

    def _build_log(self, nb):
        page = tk.Frame(nb, bg=BG, padx=6, pady=6)
        self.log_text = self._scrolled_text(page, height=10, font=self._font(9, False, mono=True), bg="#0a0a0a", fg="#d6dae0",
                                     insertbackground="white", relief="flat", wrap="word")
        self.log_text.master.pack(fill="both", expand=True)
        self.log_text.tag_configure("WARNING", foreground="#fbbf24")
        self.log_text.tag_configure("ERROR", foreground="#f87171")
        self.log_text.configure(state="disabled")
        return page

    def _build_camera(self, nb):
        page = tk.Frame(nb, bg=BG, padx=12, pady=10)
        page.columnconfigure(1, weight=1)
        r = 0
        var = tk.BooleanVar(value=self.cfg.camera_enabled)
        self.vars["camera_enabled"] = var
        ttk.Checkbutton(page, text="Send the camera to the dashboard and iPhone app", variable=var).grid(
            row=r, column=0, columnspan=2, sticky="w")
        r += 1
        for key, label, hint in (
                ("camera_address", "Camera IP address", "Tapo app: camera > Settings > Device Info, e.g. 192.168.11.15"),
                ("camera_user", "Camera username", "Tapo app: camera > Settings > Advanced Settings > Camera Account"),
                ("camera_password", "Camera password", ""),
                ("ffmpeg_path", "ffmpeg.exe", "Leave blank if ffmpeg.exe is next to HanwhaMonitor.exe")):
            ttk.Label(page, text=label, style="Form.TLabel").grid(row=r, column=0, sticky="w", pady=(6, 0), padx=(0, 10))
            v = tk.StringVar(value=str(getattr(self.cfg, key)))
            self.vars[key] = v
            ttk.Entry(page, textvariable=v, show="•" if key == "camera_password" else "").grid(
                row=r, column=1, sticky="ew", pady=(6, 0))
            r += 1
            if hint:
                ttk.Label(page, text=hint, style="Hint.TLabel").grid(row=r, column=1, sticky="w")
                r += 1
        var = tk.BooleanVar(value=self.cfg.camera_hd)
        self.vars["camera_hd"] = var
        ttk.Checkbutton(page, text="HD stream (sharper, about 4x the data - SD is fine for a glance)",
                        variable=var).grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        r += 1
        var = tk.BooleanVar(value=self.cfg.camera_retime)
        self.vars["camera_retime"] = var
        ttk.Checkbutton(page, text="Fix camera timing (if the video stutters - see Check video timing)",
                        variable=var).grid(row=r, column=0, columnspan=2, sticky="w", pady=(4, 0))
        r += 1
        var = tk.BooleanVar(value=self.cfg.camera_transcode)
        self.vars["camera_transcode"] = var
        ttk.Checkbutton(page, text="Re-encode the video (smoothest / H.265 cameras - uses more CPU on this PC)",
                        variable=var).grid(row=r, column=0, columnspan=2, sticky="w", pady=(4, 0))
        r += 1
        ttk.Label(page, text="Live video streams only while someone is watching; otherwise a still is sent every minute.",
                  style="Hint.TLabel").grid(row=r, column=0, columnspan=2, sticky="w", pady=(4, 0))
        r += 1
        btns = tk.Frame(page, bg=BG)
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(12, 6))
        ttk.Button(btns, text="Save & apply", style="Accent.TButton", command=self.apply_settings).pack(side="left")
        self.cam_test_btn = ttk.Button(btns, text="Test camera", command=self._test_camera)
        self.cam_test_btn.pack(side="left", padx=8)
        self.cam_timing_btn = ttk.Button(btns, text="Check video timing", command=self._check_timing)
        self.cam_timing_btn.pack(side="left")
        r += 1
        self.cam_status = ttk.Label(page, text="", style="Hint.TLabel", wraplength=540, justify="left")
        self.cam_status.grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        self.cam_preview = tk.Label(page, bg="#0a0a0a", fg=MUTED, text="No image yet", height=12)
        self.cam_preview.grid(row=r, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        page.rowconfigure(r, weight=1)
        self._cam_img = None
        self._cam_shown_at = None
        self._cam_msg_until = 0.0   # keep a test result on screen for a while
        return page

    def _show_preview(self, jpg: bytes):
        if not ImageTk:
            self.cam_preview.configure(text=f"Image received ({len(jpg) // 1024} KB) - preview unavailable")
            return
        try:
            im = PILImage.open(io.BytesIO(jpg))
            w = max(200, self.cam_preview.winfo_width() - 4)
            h = max(120, self.cam_preview.winfo_height() - 4)
            im.thumbnail((w, h))
            self._cam_img = ImageTk.PhotoImage(im)
            self.cam_preview.configure(image=self._cam_img, text="", height=0)
        except Exception as e:
            self.cam_preview.configure(text=f"Can't show the image: {e}")

    def _test_camera(self):
        from dataclasses import replace
        form = {}
        for key, var in self.vars.items():
            cur = getattr(self.cfg, key)
            v = var.get()
            try:
                form[key] = bool(v) if isinstance(cur, bool) else int(v) if isinstance(cur, int) else \
                    float(v) if isinstance(cur, float) else str(v).strip()
            except ValueError:
                pass
        test_cfg = replace(self.cfg, **form)
        self.cam_test_btn.configure(state="disabled")
        self.cam_status.configure(text="Connecting to the camera…")

        def run():
            jpg, err = CameraRelay(test_cfg).test_snapshot()
            def done():
                self.cam_test_btn.configure(state="normal")
                self._cam_msg_until = time.time() + 30
                if jpg:
                    self.cam_status.configure(text="Camera OK. Click Save & apply to start sending it.")
                    self._show_preview(jpg)
                else:
                    self.cam_status.configure(text=f"Camera test failed: {err}")
            self.root.after(0, done)
        threading.Thread(target=run, daemon=True).start()

    def _check_timing(self):
        from dataclasses import replace
        form = {}
        for key, var in self.vars.items():
            cur = getattr(self.cfg, key)
            try:
                v = var.get()
                form[key] = bool(v) if isinstance(cur, bool) else int(v) if isinstance(cur, int) else \
                    float(v) if isinstance(cur, float) else str(v).strip()
            except (ValueError, tk.TclError):
                pass
        test_cfg = replace(self.cfg, **form)
        self.cam_timing_btn.configure(state="disabled")
        self.cam_status.configure(text="Recording 10 seconds of video from the camera…")
        self._cam_msg_until = time.time() + 60

        def run():
            result = CameraRelay(test_cfg).check_timing(10)
            log.info("Camera video timing:\n%s", result)
            def done():
                self.cam_timing_btn.configure(state="normal")
                self.cam_status.configure(text=result)
                self._cam_msg_until = time.time() + 120
            self.root.after(0, done)
        threading.Thread(target=run, daemon=True).start()

    def _refresh_camera(self):
        cam = self.camera
        if not cam:
            return
        if not cam.enabled:
            text = "Camera is off." if not self.cfg.camera_enabled else "Enter the camera's IP address."
        else:
            text = f"Camera: {cam.status}"
            if cam.last_sent_at:
                text += f" · last sent {ago(cam.last_sent_at)}"
            if cam.error:
                text += f"\n{cam.error}"
        if str(self.cam_test_btn["state"]) != "disabled" and time.time() > self._cam_msg_until:
            self.cam_status.configure(text=text)
        if cam.last_frame and cam.last_frame_at != self._cam_shown_at and self.nb.index("current") == 3:
            self._cam_shown_at = cam.last_frame_at
            self._show_preview(cam.last_frame)

    def _build_signals(self, nb):
        page = tk.Frame(nb, bg=BG, padx=10, pady=8)
        ttk.Label(page, style="Form.TLabel", wraplength=560, justify="left", text=(
            "Finds the machine's internal address for a switch, e.g. the work counter 'stop at required count' "
            "setting.  1) Take snapshot A.  2) Change ONLY that setting on the machine (machine idle is best).  "
            "3) Take snapshot B.  Repeat with the setting changed back - the address that flips both times is "
            "the one.  Each snapshot takes about 10-30 seconds.")).pack(anchor="w")
        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(8, 4))
        self.snap_a_btn = ttk.Button(row, text="Take snapshot A", command=lambda: self._take_snapshot("A"))
        self.snap_a_btn.pack(side="left")
        self.snap_b_btn = ttk.Button(row, text="Take snapshot B + compare", command=lambda: self._take_snapshot("B"),
                                     state="disabled")
        self.snap_b_btn.pack(side="left", padx=8)
        self.snap_prog = ttk.Progressbar(row, maximum=1.0, length=140)
        self.snap_prog.pack(side="left", padx=(8, 0))
        self.snap_lbl = ttk.Label(page, text="", style="Hint.TLabel")
        self.snap_lbl.pack(anchor="w")
        self.snap_text = self._scrolled_text(page, height=8, font=self._font(9, False, mono=True), bg="#0a0a0a", fg="#d6dae0",
                                      insertbackground="white", relief="flat", wrap="none")
        self.snap_text.master.pack(fill="both", expand=True, pady=(4, 6))
        use = tk.Frame(page, bg=BG)
        use.pack(fill="x")
        ttk.Label(use, text="Work counter signal:", style="Form.TLabel").pack(side="left")
        self.wc_pick = tk.StringVar(value=self.cfg.work_counter_signal)
        ttk.Entry(use, textvariable=self.wc_pick, width=12).pack(side="left", padx=6)
        ttk.Button(use, text="Use & save", command=self._use_wc_signal).pack(side="left")
        self._snaps: dict[str, dict] = {}
        return page

    def _take_snapshot(self, which: str):
        self.snap_a_btn.configure(state="disabled")
        self.snap_b_btn.configure(state="disabled")
        self.snap_lbl.configure(text=f"Taking snapshot {which}…")
        cfg = self.cfg

        def progress(frac, text):
            self.root.after(0, lambda: (self.snap_prog.configure(value=frac), self.snap_lbl.configure(text=text)))

        def run():
            m = MockMachine() if cfg.demo_mode else FocasMachine(cfg.machine_ip, cfg.machine_port,
                                                                  cfg.connect_timeout, cfg.dll_path or None)
            try:
                m.connect()
                snap = signals.snapshot(m, progress)
                err = None
            except Exception as e:  # noqa: BLE001
                snap, err = None, str(e)
            finally:
                try:
                    m.close()
                except Exception:
                    pass
            self.root.after(0, lambda: self._snapshot_done(which, snap, err))
        threading.Thread(target=run, daemon=True).start()

    def _snapshot_done(self, which: str, snap, err):
        self.snap_a_btn.configure(state="normal")
        self.snap_prog.configure(value=0)
        t = self.snap_text
        t.configure(state="normal")
        if err or snap is None:
            self.snap_lbl.configure(text=f"Snapshot failed: {err}")
            self.snap_b_btn.configure(state="normal" if "A" in self._snaps else "disabled")
            return
        self._snaps[which] = snap
        size = sum(len(v) for v in snap["pmc"].values())
        if which == "A":
            self.snap_lbl.configure(text=f"Snapshot A taken ({size} PMC bytes, {len(snap['macro'])} variables). "
                                         "Now change the setting on the machine, then take snapshot B.")
            t.delete("1.0", "end")
        else:
            diff = signals.compare(self._snaps["A"], snap)
            self.snap_lbl.configure(text=f"{sum(1 for d in diff if d and not d.startswith('Also'))} change(s). "
                                         "B is now the new A - change the setting back and take B again to confirm.")
            t.delete("1.0", "end")
            t.insert("end", "\n".join(diff) if diff else "Nothing changed.")
            log.info("Signal finder: %d difference(s)%s", len(diff), (": " + "; ".join(diff[:8])) if diff else "")
            self._snaps["A"] = snap
        self.snap_b_btn.configure(state="normal")

    def _use_wc_signal(self):
        text = self.wc_pick.get().strip()
        if text and not parse_signal(text):
            messagebox.showerror("Work counter signal", "Use an address like K5.3, R120.1, !K5.3 or #512.")
            return
        self.vars["work_counter_signal"].set(text)
        self.apply_settings()

    # ------------------------------------------------------------------ services
    def start_services(self):
        self.collector = Collector(self.cfg)
        self.camera = CameraRelay(self.cfg)
        cam = self.camera
        self.uploader = Uploader(self.cfg.server_url, self.cfg.api_key, on_ack=self.collector.ack,
                                 on_response=lambda j: cam.set_live(j.get("camera_live")))
        self.camera.start()
        self.collector.listeners.append(self.uploader.submit)
        self.uploader.start()
        self.collector.start()
        log.info("Monitoring %s at %s:%s%s", self.cfg.machine_name, self.cfg.machine_ip, self.cfg.machine_port,
                 " (DEMO MODE)" if self.cfg.demo_mode else "")

    def stop_services(self):
        if self.camera:
            self.camera.stop()
        if self.uploader:
            self.uploader.stop()
        if self.collector:
            c = self.collector
            threading.Thread(target=c.stop, daemon=True).start()

    def apply_settings(self):
        try:
            for key, var in self.vars.items():
                cur = getattr(self.cfg, key)
                v = var.get()
                if isinstance(cur, bool):
                    setattr(self.cfg, key, bool(v))
                elif isinstance(cur, int):
                    setattr(self.cfg, key, int(str(v).strip()))
                elif isinstance(cur, float):
                    setattr(self.cfg, key, max(0.5, float(str(v).strip())))
                else:
                    setattr(self.cfg, key, str(v).strip())
            self.cfg.server_url = normalize_url(self.cfg.server_url)
            self.vars["server_url"].set(self.cfg.server_url)
        except ValueError as e:
            messagebox.showerror("Invalid setting", str(e))
            return
        self.cfg.save()
        err = set_autostart(self.cfg.autostart, minimized=True)
        if err and self.cfg.autostart:
            messagebox.showwarning("Autostart", f"Could not set autostart: {err}")
        self.title_lbl.configure(text=self.cfg.machine_name)
        self.root.title(f"Hanwha Monitor — {self.cfg.machine_name}")
        log.info("Settings saved - restarting monitor")
        self.stop_services()
        self.root.after(300, self.start_services)

    def test_server(self):
        url, key = self.vars["server_url"].get().strip(), self.vars["api_key"].get().strip()

        def run():
            ok, msg = Uploader.test(url, key)
            self.root.after(0, lambda: (messagebox.showinfo if ok else messagebox.showerror)("Server test", msg))
        threading.Thread(target=run, daemon=True).start()

    def open_dashboard(self):
        webbrowser.open(normalize_url(self.cfg.server_url) + "/")

    def open_logs(self):
        webbrowser.open(str(data_dir() / "logs"))

    # ------------------------------------------------------------------ refresh
    def _draw_pill(self, state: str):
        c = self.pill
        c.delete("all")
        color = STATE_COLORS.get(state, "#8a8f98")
        w, h, r = 132, 38, 19
        c.create_oval(0, 0, h, h, fill=color, outline="")
        c.create_oval(w - h, 0, w, h, fill=color, outline="")
        c.create_rectangle(r, 0, w - r, h, fill=color, outline="")
        c.create_text(w / 2, h / 2, text=STATE_LABELS.get(state, state.upper()), fill="white",
                      font=self._font(12, True))

    def refresh(self):
        try:
            self._refresh()
        except Exception:
            log.exception("UI refresh failed")
        self.root.after(1000, self.refresh)

    def _refresh(self):
        col, up, cfg = self.collector, self.uploader, self.cfg
        snap = col.state.snapshot if col else {}
        self._refresh_camera()
        state = snap.get("state", "off")
        if state == "running" and snap.get("bar_change"):
            state = "barchange"
        self._draw_pill(state)
        self.detail_lbl.configure(text=snap.get("state_detail") or "")
        self.demo_lbl.configure(text="DEMO MODE — simulated data" if cfg.demo_mode else "")

        if col and col.state.machine_connected:
            self._set_card(self.machine_card, True, "Connected", f"{cfg.machine_ip}:{cfg.machine_port} · polled {ago(col.state.last_poll_ok)}")
        elif col and col.state.last_error:
            self._set_card(self.machine_card, False, "Not connected", col.state.last_error[:70])
        else:
            self._set_card(self.machine_card, None, "Connecting…", f"{cfg.machine_ip}:{cfg.machine_port}")

        if up and not up.enabled:
            self._set_card(self.server_card, None, "Not configured", "Set the server URL in Settings")
        elif up and up.connected:
            self._set_card(self.server_card, True, "Connected", f"{up.url} · sent {ago(up.last_ok)}")
        elif up and up.last_error:
            self._set_card(self.server_card, False, "Can't reach server", up.last_error[:70])
        else:
            self._set_card(self.server_card, None, "Connecting…", up.url if up else "")

        parts, req = snap.get("parts"), snap.get("parts_required")
        if parts is None:
            self.parts_v.configure(text="—")
            self.parts_bar["value"] = 0
            self.parts_sub.configure(text="")
        else:
            self.parts_v.configure(text=f"{parts} / {req}" if req else f"{parts}")
            self.parts_bar["value"] = min(100, parts * 100 / req) if req else 0
            left = (req - parts) if req else None
            sub = []
            if left and left > 0 and snap.get("last_cycle_s"):
                sub.append(f"{left} to go · done in ~{fmt_duration(left * snap['last_cycle_s'])}")
            if snap.get("parts_total") is not None:
                sub.append(f"Total {snap['parts_total']}")
            text = "   ".join(sub)
            if snap.get("work_counter") is not None:
                text += "\n" + ("■ Stops at count" if snap["work_counter"] else "∞ Won't stop at count")
            self.parts_sub.configure(text=text)

        self.cycle_v.configure(text=fmt_duration(snap.get("last_cycle_s")))
        cur = snap.get("cycle_timer_s")
        self.cycle_sub.configure(text=f"Current cycle {fmt_duration(cur)}" if cur else "Last completed part")

        prog = snap.get("program") or {}
        name = prog.get("name") or ("O%04d" % prog["number"] if prog.get("number") else "—")
        self.prog_v.configure(text=f"{name}  {prog.get('comment') or ''}".strip())
        paths = snap.get("paths") or []
        self.prog_sub.configure(text="   ".join(f"{p['name']}: {p['mode']} {p['run']}" for p in paths))

        # alarms
        self.alarm_tree.delete(*self.alarm_tree.get_children())
        active = [a for a in (snap.get("alarms") or []) if not a.get("cleared_at")]
        for a in active:
            self.alarm_tree.insert("", "end", tags=("alarm",), values=(
                datetime.fromtimestamp(a["started_at"]).strftime("%H:%M:%S"), a["path_name"], a["code"], a["message"]))
        if active:
            self.no_alarm_lbl.pack_forget()
        else:
            self.no_alarm_lbl.pack(anchor="w")

        if self.tray and state != self._tray_state:
            self._tray_state = state
            try:
                self.tray.icon = self._tray_image(state)
                self.tray.title = f"Hanwha Monitor — {STATE_LABELS.get(state, state)}"
            except Exception:
                pass

    def drain_log(self):
        lines = []
        try:
            while True:
                lines.append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            t = self.log_text
            t.configure(state="normal")
            for ln in lines:
                tag = "ERROR" if " ERROR " in ln else ("WARNING" if " WARNING " in ln else None)
                t.insert("end", ln + "\n", tag)
            if int(t.index("end-1c").split(".")[0]) > 3000:
                t.delete("1.0", "1000.0")
            t.see("end")
            t.configure(state="disabled")
        self.root.after(300, self.drain_log)

    # ------------------------------------------------------------------ tray / window
    def _tray_image(self, state):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=STATE_COLORS.get(state, "#8a8f98"))
        d.ellipse((22, 22, 42, 42), fill="white")
        return img

    def _start_tray(self):
        menu = pystray.Menu(pystray.MenuItem("Open", lambda: self.root.after(0, self.show), default=True),
                            pystray.MenuItem("Quit", lambda: self.root.after(0, self.quit)))
        self.tray = pystray.Icon("HanwhaMonitor", self._tray_image("off"), "Hanwha Monitor", menu)
        try:
            self.tray.run_detached()
        except Exception:
            threading.Thread(target=self.tray.run, daemon=True).start()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def on_close(self):
        if self.tray:
            self.hide()   # keep monitoring in the tray
        else:
            if messagebox.askyesno("Quit", "Stop monitoring the machine and quit?"):
                self.quit()

    def quit(self):
        log.info("Shutting down")
        self.stop_services()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.after(200, self.root.destroy)

    def run(self):
        self.root.mainloop()
