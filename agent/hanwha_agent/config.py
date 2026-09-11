from __future__ import annotations

import json
import re
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "HanwhaMonitor"
VERSION = "1.4.0"


def normalize_url(url: str) -> str:
    """Tidy a typed server address: fixes wrong/missing slashes, backslashes, stray spaces, missing http://."""
    u = (url or "").strip().replace("\\", "/").replace(" ", "")
    if not u:
        return ""
    m = re.match(r"^([a-zA-Z]+):?/*(.*)$", u)
    if m and m.group(1).lower() in ("http", "https") and (":" in u.split("/")[0] or u.lower().startswith(("http/", "https/"))):
        scheme, rest = m.group(1).lower(), m.group(2)
    else:
        scheme, rest = "http", u.lstrip("/")
    return f"{scheme}://{rest}".rstrip("/")


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    machine_name: str = "Hanwha XE35"
    machine_ip: str = "192.168.11.11"
    machine_port: int = 8193
    connect_timeout: int = 5
    poll_interval: float = 2.0
    # FOCAS paths: XE35 = path 1 (Main), path 2 (Sub)
    path_names: dict = field(default_factory=lambda: {"1": "Main", "2": "Sub"})
    count_path: int = 1               # path whose part counter / cycle timer / program is shown
    bar_change_program: int = 9002    # subprogram that does the bar change (O9002 on the XE35; 0 = off)
    bar_change_mcode: int = 0         # optional: also while this M code is the active block (0 = off)
    work_counter_signal: str = ""
    # camera (Tapo C210 etc. over RTSP, relayed to the server)
    camera_enabled: bool = False
    camera_address: str = ""          # camera IP (e.g. 192.168.11.15) or a full rtsp:// URL
    camera_user: str = ""             # Tapo app: camera > Settings > Advanced Settings > Camera Account
    camera_password: str = ""
    camera_hd: bool = False           # False = stream2 (low res, light on the network), True = stream1 (HD)
    camera_fps: float = 4.0           # (no longer used - live video runs at the camera's own frame rate)
    camera_transcode: bool = False    # re-encode to H.264 (only if the camera's own video won't play)
    ffmpeg_path: str = ""             # blank = ffmpeg.exe next to the .exe, or on PATH     # PMC bit / macro var that is on when "stop at required count" is enabled, e.g. K5.3
    server_url: str = "http://tower.local:8420"
    api_key: str = ""
    dll_path: str = ""                # blank = look next to the exe
    demo_mode: bool = False
    start_minimized: bool = False
    autostart: bool = False

    @property
    def paths(self) -> list[tuple[int, str]]:
        return sorted((int(k), v) for k, v in self.path_names.items())

    # -------------------------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        return data_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        p = cls.path()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                known = {f.name for f in fields(cls)}
                for k, v in raw.items():
                    if k in known:
                        setattr(cfg, k, v)
                if "bar_change_program" not in raw:   # before 1.4.0 the default was an M92 guess
                    cfg.bar_change_mcode = 0
            except Exception:
                pass
        cfg.server_url = normalize_url(cfg.server_url)
        return cfg

    def save(self):
        self.path().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def set_autostart(enabled: bool, minimized: bool = True) -> str | None:
    """Add/remove the app from HKCU\\...\\Run. Returns an error string or None."""
    if os.name != "nt":
        return "Autostart is only supported on Windows"
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                             winreg.KEY_SET_VALUE)
        if enabled:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                pyw = Path(sys.executable).with_name("pythonw.exe")
                cmd = f'"{pyw if pyw.exists() else sys.executable}" "{Path(sys.argv[0]).resolve()}"'
            if minimized:
                cmd += " --minimized"
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return None
    except Exception as e:  # pragma: no cover
        return str(e)
