"""Sends snapshots to the Unraid server (latest-wins queue, never blocks polling).

What happens while the server is unreachable is kept in a local log (spool.py) and replayed in order when it comes
back, so part times, bars, machine messages and alarms from that period aren't lost.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from .config import normalize_url
from .spool import Spool

log = logging.getLogger("hanwha.uploader")


class Uploader:
    REPLAY_BATCH = 100

    def __init__(self, server_url: str, api_key: str = "", on_ack=None, on_response=None, spool: Spool | None = None):
        self.url = normalize_url(server_url)
        self.api_key = api_key
        self.on_ack = on_ack
        self.on_response = on_response   # called with the server's JSON reply to every upload
        self.connected = False
        self.last_ok: float | None = None
        self.last_error = ""
        self._latest: dict | None = None
        self._cv = threading.Condition()
        self._stop = False
        self._fails = 0
        self.spool = spool
        self.pending = 0                  # how much is waiting in the local log
        self._replay_ok = True            # False once we know the server is too old to accept a replay
        self._row_id: int | None = None
        self._last_prune = 0.0
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
        row = self.spool.add(snapshot) if (self.spool and self.enabled) else None
        with self._cv:
            self._latest = snapshot
            if row:
                self._row_id = row
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
                if self.spool and self._replay_ok and self.pending:
                    self._replay()          # catch the server up on what it missed, in order, first
                row_id = self._row_id
                r = self._session.post(f"{self.url}/api/ingest", json=snap, headers=self._headers(), timeout=4)
                if r.status_code == 401:
                    raise RuntimeError("Server rejected API key (401)")
                r.raise_for_status()
                reply = r.json()
                if self.on_response:
                    try:
                        self.on_response(reply)
                    except Exception:
                        log.exception("on_response failed")
                ack = set(reply.get("ack", []))
                if self.on_ack and ack:
                    self.on_ack(ack)
                if not self.connected:
                    log.info("Server connection OK (%s)", self.url)
                self.connected, self.last_ok, self.last_error, self._fails = True, time.time(), "", 0
                if self.spool and row_id:
                    self.spool.mark_sent(row_id)
                self._housekeeping()
            except Exception as e:
                self._fails += 1
                self.last_error = str(e)
                if self.connected or self._fails == 1 or self._fails % 60 == 0:
                    log.warning("Server upload failed: %s", e)
                self.connected = False
                if self.spool:
                    self.pending = self.spool.pending()
                # back off a little while the server is down
                with self._cv:
                    self._cv.wait(timeout=min(10, 2 * self._fails))

    def _housekeeping(self):
        if not self.spool:
            return
        self.pending = self.spool.pending()
        if time.time() - self._last_prune > 600:
            self._last_prune = time.time()
            self.spool.prune()

    def _replay(self):
        """Send everything the server missed, oldest first, before the live snapshot."""
        sent = 0
        while not self._stop:
            batch = self.spool.unsent(self.REPLAY_BATCH)
            if not batch:
                break
            r = self._session.post(f"{self.url}/api/replay", json={"snapshots": [s for _, s in batch]},
                                   headers=self._headers(), timeout=30)
            if r.status_code == 404:        # server too old for replay - don't keep trying
                log.warning("The server doesn't support catching up yet (update the container); dropping %d saved "
                            "updates", self.spool.pending())
                self._replay_ok = False
                self.spool.mark_sent(batch[-1][0])
                while self.spool.pending():
                    rest = self.spool.unsent(1000)
                    if not rest:
                        break
                    self.spool.mark_sent(rest[-1][0])
                break
            r.raise_for_status()
            reply = r.json()
            ack = set(reply.get("ack", []))
            if self.on_ack and ack:
                self.on_ack(ack)
            self.spool.mark_sent(batch[-1][0])
            sent += len(batch)
            if len(batch) < self.REPLAY_BATCH:
                break
        if sent:
            log.info("Caught the server up with %d saved update(s)", sent)
        self._housekeeping()

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
