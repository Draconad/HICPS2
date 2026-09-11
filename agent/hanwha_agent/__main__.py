"""Entry point:  python -m hanwha_agent  [--minimized] [--headless] [--demo]"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from .config import APP_NAME, VERSION, Config, data_dir


def setup_logging(console: bool):
    logdir = data_dir() / "logs"
    logdir.mkdir(exist_ok=True)
    root = logging.getLogger("hanwha")
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(logdir / "agent.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console and sys.stderr:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)


def single_instance() -> bool:
    """Prevent two copies polling the machine at once (Windows only)."""
    if os.name != "nt":
        return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}Mutex")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main(argv=None):
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--headless", action="store_true", help="no window, log to console (for debugging)")
    ap.add_argument("--demo", action="store_true", help="simulate a machine")
    args = ap.parse_args(argv)

    setup_logging(console=args.headless)
    log = logging.getLogger("hanwha")
    cfg = Config.load()
    if not Config.path().exists():
        cfg.save()
    if args.demo:
        cfg.demo_mode = True

    if not single_instance():
        if not args.headless:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo(APP_NAME, "Hanwha Monitor is already running — check the system tray.")
        return 1

    log.info("%s %s starting (config: %s)", APP_NAME, VERSION, Config.path())

    if args.headless:
        from .collector import Collector
        from .uploader import Uploader
        from .camera import CameraRelay
        col = Collector(cfg)
        cam = CameraRelay(cfg)
        up = Uploader(cfg.server_url, cfg.api_key, on_ack=col.ack,
                      on_response=lambda j: cam.set_live(j.get("camera_live")))
        col.listeners.append(up.submit)
        col.listeners.append(lambda s: log.info("state=%s parts=%s/%s cycle=%s prog=%s alarms=%d",
                                                s["state"], s.get("parts"), s.get("parts_required"),
                                                s.get("last_cycle_s"), (s.get("program") or {}).get("name"),
                                                len([a for a in s.get("alarms", []) if not a.get("cleared_at")])))
        up.start()
        col.start()
        cam.start()
        from .commands import CommandClient
        cmd = CommandClient(cfg, col, cam)
        cmd.start()
        from .updater import Updater
        upd = Updater(cfg, on_restart=lambda: os._exit(0))
        upd.start()
        signal.signal(signal.SIGINT, lambda *_: (cmd.stop(), cam.stop(), col.stop(), up.stop(), sys.exit(0)))
        while True:
            time.sleep(1)

    from .gui import App
    App(cfg, start_minimized=args.minimized or cfg.start_minimized).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
