"""Commands from the app/dashboard (camera pan/tilt, Controls tab), delivered through the server.

The PC keeps one long-poll request open to the server, so a button press arrives in well under a second.
Machine writes only happen if "Allow remote changes" is ticked in this app, and only for the few things the
Controls tab offers. Cycle start/stop and other machine buttons are deliberately not implemented.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import requests

from .config import Config, normalize_url
from .focas import parse_signal
from .onvif import OnvifError, OnvifPTZ

log = logging.getLogger("hanwha.commands")

NOT_ALLOWED = "Remote changes are switched off in the PC app on the machine (Settings > Allow remote changes)."


class CommandClient:
    def __init__(self, cfg: Config, collector, camera=None):
        self.cfg, self.collector, self.camera = cfg, collector, camera
        self.server = normalize_url(cfg.server_url)
        self._stop = threading.Event()
        self._session = requests.Session()
        self._ptz: OnvifPTZ | None = None
        self._ptz_lock = threading.Lock()
        self.connected = False

    def _headers(self):
        return {"X-API-Key": self.cfg.api_key} if self.cfg.api_key else {}

    def start(self):
        if self.server:
            threading.Thread(target=self._run, name="commands", daemon=True).start()
        if self.cfg.camera_enabled and self.cfg.camera_ptz and self.cfg.camera_address.strip():
            threading.Thread(target=self._preset_loop, name="presets", daemon=True).start()

    # ------------------------------------------------------------------ camera presets
    def _ptz_client(self) -> OnvifPTZ:
        if self._ptz is None:
            self._ptz = OnvifPTZ(self._camera_host(), self.cfg.camera_user, self.cfg.camera_password)
        return self._ptz

    def refresh_presets(self):
        with self._ptz_lock:
            try:
                presets = self._ptz_client().presets()
            except OnvifError as e:
                self._ptz = None
                log.info("Camera presets: %s", e)
                return
        if presets != getattr(self.collector, "camera_presets", None):
            log.info("Camera presets: %s", ", ".join(p["name"] for p in presets) or "none saved")
        self.collector.camera_presets = presets   # goes to the app/dashboard with the next status update

    def _preset_loop(self):
        """Pick up presets saved in the Tapo app: at start, then every few minutes."""
        self._stop.wait(3)
        while not self._stop.is_set():
            self.refresh_presets()
            self._stop.wait(300)

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------ long-poll loop
    def _run(self):
        fails = 0
        while not self._stop.is_set():
            try:
                r = self._session.get(f"{self.server}/api/agent/commands", params={"wait": 25},
                                      headers=self._headers(), timeout=40)
                if r.status_code == 404:        # older server without the command channel
                    self._stop.wait(60)
                    continue
                r.raise_for_status()
                cmds = r.json().get("commands", [])
                self.connected, fails = True, 0
            except Exception as e:  # noqa: BLE001
                self.connected = False
                fails += 1
                if fails in (1, 10) or fails % 60 == 0:
                    log.warning("Command channel to the server: %s", e)
                self._stop.wait(min(30, 2 * fails))
                continue
            for c in cmds:
                threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c: dict):
        cid, kind = c.get("id"), c.get("type", "")
        try:
            msg = self._dispatch(kind, c) or "Done"
            ok = True
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e) or e.__class__.__name__
        (log.info if ok else log.warning)("Command %s from %s: %s", kind, c.get("from", "app"), msg)
        try:
            self._session.post(f"{self.server}/api/agent/results", json={"id": cid, "ok": ok, "message": msg},
                               headers=self._headers(), timeout=8)
        except Exception as e:  # noqa: BLE001
            log.debug("Couldn't report command result: %s", e)

    # ------------------------------------------------------------------ what each command does
    def _dispatch(self, kind: str, c: dict) -> str | None:
        cfg = self.cfg
        if kind == "ptz":
            if not (cfg.camera_enabled and cfg.camera_ptz):
                raise RuntimeError("Pan/tilt is switched off in the PC app (Camera tab).")
            x, y = float(c.get("x", 0)), float(c.get("y", 0))
            with self._ptz_lock:          # one move at a time
                try:
                    self._ptz_client().move(x, y)
                except OnvifError:
                    self._ptz = None      # reconnect next time (camera restarted, login changed...)
                    raise
            return None

        if kind == "ptz_preset":
            if not (cfg.camera_enabled and cfg.camera_ptz):
                raise RuntimeError("Pan/tilt is switched off in the PC app (Camera tab).")
            token = str(c.get("token", ""))
            names = {p["token"]: p["name"] for p in (getattr(self.collector, "camera_presets", None) or [])}
            with self._ptz_lock:
                try:
                    self._ptz_client().goto_preset(token)
                except OnvifError:
                    self._ptz = None
                    raise
            return f"Moving to {names.get(token, 'preset ' + token)}"

        if kind in ("set_required", "set_work_counter"):
            if not cfg.remote_control:
                raise PermissionError(NOT_ALLOWED)
            if kind == "set_required":
                value = int(c.get("value"))
                if not 1 <= value <= 9_999_999:
                    raise ValueError("The required count must be between 1 and 9,999,999.")
                self.collector.run_on_machine(lambda m: m.write_macro(cfg.count_path, 3902, value))
                return f"Required count set to {value}"
            sig = parse_signal(cfg.work_counter_signal)
            if not sig:
                raise RuntimeError("The work counter signal isn't set up yet (PC app > Signal finder).")
            on = bool(c.get("on"))
            self.collector.run_on_machine(lambda m: m.write_signal(sig, on))
            # the machine's ladder may drive this address from the real setting and put it straight back
            time.sleep(1.5)
            if self.collector.run_on_machine(lambda m: m.read_signal(sig)) != on:
                raise RuntimeError(f"The machine put {cfg.work_counter_signal} straight back - its own logic controls that "
                                   "setting, so it can't be switched from here. Change it on the machine.")
            return f"Stop at required count {'on' if on else 'off'}"

        if kind in ("cycle_start", "cycle_stop", "continuous"):
            raise RuntimeError("Machine buttons aren't available yet - they need machine-specific setup and safety "
                               "interlocks first.")
        raise ValueError(f"Unknown command '{kind}'")

    def _camera_host(self) -> str:
        addr = self.cfg.camera_address.strip()
        if "://" in addr:
            return urlparse(addr).hostname or addr
        return addr.split(":")[0]
