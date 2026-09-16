"""Automatic PC-app updates from the Unraid server.

The push script uploads each new signed build to the server; this checks the server every CHECK_EVERY seconds.
An update is only installed if its SHA-256 matches and its Ed25519 signature verifies against the public key
built into this app (update_key.py) - so the server (or anyone with the API key) can't make this PC run
anything that wasn't built and signed by your GitHub Actions.

Windows won't overwrite a running .exe, so the swap is done by a tiny .cmd that waits for this app to exit,
moves the new file into place and starts it again.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from . import ed25519
from .config import VERSION, Config, normalize_url
from .update_key import PUBLIC_KEY_HEX

log = logging.getLogger("hanwha.updater")

CHECK_EVERY = 30 * 60
FIRST_CHECK = 90
NO_WINDOW = 0x08000000 | 0x00000008   # CREATE_NO_WINDOW | DETACHED_PROCESS


def vtuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


class Updater:
    def __init__(self, cfg: Config, on_restart):
        self.cfg = cfg
        self.server = normalize_url(cfg.server_url)
        self.on_restart = on_restart          # called (on any thread) when the app must exit for the swap
        self.status = "Automatic updates on" if cfg.auto_update else "Automatic updates off"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._refused: set[str] = set()      # sha256s already rejected - don't download them again
        self.exe = Path(sys.executable) if getattr(sys, "frozen", False) else None

    def start(self):
        if self.server and self.exe:
            threading.Thread(target=self._loop, name="updater", daemon=True).start()
        elif not self.exe:
            self.status = "Updates only work in the installed HanwhaMonitor.exe"
        self._cleanup()

    def stop(self):
        self._stop.set()

    def _loop(self):
        self._stop.wait(FIRST_CHECK)
        while not self._stop.is_set():
            if self.cfg.auto_update:
                try:
                    self.check(install=True)
                except Exception as e:  # noqa: BLE001
                    log.warning("Update check failed: %s", e)
            self._stop.wait(CHECK_EVERY)

    def _headers(self):
        return {"X-API-Key": self.cfg.api_key} if self.cfg.api_key else {}

    def check(self, install: bool) -> str:
        """Look for a newer version on the server; install it if asked. Returns a status line."""
        with self._lock:
            if not self.exe:
                self.status = "Updates only work in the installed HanwhaMonitor.exe"
                return self.status
            r = requests.get(f"{self.server}/api/agent/update", headers=self._headers(), timeout=10)
            if r.status_code == 404:
                self.status = "The server doesn't support updates yet (update the server container)"
                return self.status
            r.raise_for_status()
            info = r.json()
            ver = info.get("version")
            if not ver or vtuple(ver) <= vtuple(VERSION):
                self.status = f"Up to date (v{VERSION})"
                return self.status
            if not install:
                self.status = f"Update v{ver} available"
                return self.status
            if info.get("sha256") in self._refused:
                return self.status
            self.status = f"Downloading v{ver}…"
            new = self.exe.with_name("HanwhaMonitor.update.exe")
            h = hashlib.sha256()
            size = 0
            with requests.get(f"{self.server}/api/agent/update/download", headers=self._headers(),
                              stream=True, timeout=60) as dl:
                dl.raise_for_status()
                with open(new, "wb") as f:
                    for chunk in dl.iter_content(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
            digest = h.hexdigest()
            problem = None
            if size != info.get("size") or digest != info.get("sha256"):
                problem = "the download doesn't match (size/checksum)"
            elif not PUBLIC_KEY_HEX or not ed25519.verify(bytes.fromhex(PUBLIC_KEY_HEX),
                                                          ed25519.update_message(ver, digest),
                                                          bytes.fromhex(info.get("signature") or "00" * 64)):
                problem = "its signature isn't valid - it wasn't built by your GitHub Actions"
            if problem:
                new.unlink(missing_ok=True)
                self._refused.add(info.get("sha256"))
                self.status = f"Refused update v{ver}: {problem}"
                log.warning(self.status)
                return self.status
            log.info("Update v%s verified (sha256 %s…) - restarting to install it", ver, digest[:12])
            self._swap(new)
            self.status = f"Installing v{ver} - restarting…"
            return self.status

    def _swap(self, new: Path):
        exe = self.exe
        # in a one-file build the bootloader (our parent) holds the .exe until everything has exited
        wait_pid = os.getppid() if getattr(sys, "_MEIPASS", None) else os.getpid()
        args = " ".join(f'"{a}"' for a in sys.argv[1:]) or ("--minimized" if self.cfg.start_minimized else "")
        script = exe.with_name("_update.cmd")
        script.write_text(
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {wait_pid}" 2>nul | find "{wait_pid}" >nul\r\n'
            "if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto wait)\r\n"
            "ping -n 2 127.0.0.1 >nul\r\n"
            f'move /y "{exe}" "{exe.with_name("HanwhaMonitor.old.exe")}" >nul\r\n'
            f'move /y "{new}" "{exe}" >nul\r\n'
            f'start "" "{exe}" {args}\r\n'
            'del "%~f0"\r\n', encoding="ascii", errors="replace")
        subprocess.Popen(["cmd.exe", "/c", str(script)], cwd=str(exe.parent), creationflags=NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        threading.Timer(0.5, self.on_restart).start()

    def _cleanup(self):
        """Remove leftovers from the previous update."""
        if self.exe:
            for name in ("HanwhaMonitor.old.exe", "HanwhaMonitor.update.exe"):
                try:
                    self.exe.with_name(name).unlink(missing_ok=True)
                except OSError:
                    pass
