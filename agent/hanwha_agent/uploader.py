"""Sends snapshots to the Unraid server (latest-wins queue, never blocks polling)."""
from __future__ import annotations

import logging
import threading
import time

import requests

from .config import normalize_url

log = logging.getLogger("hanwha.uploader")


class Uploader:
    def __init__(self, server_url: str, api_key: str = "", on_ack=None):
        self.url = normalize_url(server_url)
        self.api_key = api_key
        self.on_ack = on_ack
        self.connected = False
        self.last_ok: float | None = None
        self.last_error = ""
        self._latest: dict | None = None
        self._cv = threading.Condition()
        self._stop = False
        self._fails = 0
        self._session = requests.Session()
        self._thread = threading.Thread(target=self._run, name="uploader", daemon=True)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def start(self):
        if self.enabled:
            self._thread.start()

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def submit(self, snapshot: dict):
        with self._cv:
            self._latest = snapshot
            self._cv.notify_all()

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def _run(self):
        while True:
            with self._cv:
                while self._latest is None and not self._stop:
                    self._cv.wait(timeout=1)
                if self._stop:
                    return
                snap, self._latest = self._latest, None
            try:
                r = self._session.post(f"{self.url}/api/ingest", json=snap, headers=self._headers(), timeout=4)
                if r.status_code == 401:
                    raise RuntimeError("Server rejected API key (401)")
                r.raise_for_status()
                ack = set(r.json().get("ack", []))
                if self.on_ack and ack:
                    self.on_ack(ack)
                if not self.connected:
                    log.info("Server connection OK (%s)", self.url)
                self.connected, self.last_ok, self.last_error, self._fails = True, time.time(), "", 0
            except Exception as e:
                self._fails += 1
                self.last_error = str(e)
                if self.connected or self._fails == 1 or self._fails % 60 == 0:
                    log.warning("Server upload failed: %s", e)
                self.connected = False
                # back off a little while the server is down
                with self._cv:
                    self._cv.wait(timeout=min(10, 2 * self._fails))

    @staticmethod
    def test(server_url: str, api_key: str = "") -> tuple[bool, str]:
        try:
            server_url = normalize_url(server_url)
            if not server_url:
                return False, "Enter the server address first, e.g. http://100.x.y.z:8420"
            h = {"X-API-Key": api_key} if api_key else {}
            r = requests.get(server_url + "/api/health", headers=h, timeout=4)
            if r.status_code == 401:
                return False, "Reached the server, but the API key is wrong."
            r.raise_for_status()
            return True, f"Connected to {server_url} - server version {r.json().get('version', '?')}"
        except Exception as e:
            return False, f"Could not reach server: {e}"
