"""Latest camera frame from the PC agent, and who's watching.

The agent posts JPEG frames; viewers (dashboard MJPEG stream, iPhone app polling) mark themselves as watching,
and the agent switches to live streaming while anyone is (it learns that from the upload/ingest replies).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("hanwha-server.camera")

VIEWER_TIMEOUT = 10      # a viewer that hasn't asked for a frame this long has gone
SAVE_EVERY = 60          # keep a copy of the latest still on disk this often (survives restarts)
MAX_FRAME = 3_000_000


class Camera:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "camera-latest.jpg"
        self.cv = threading.Condition()
        self.jpg: bytes | None = None
        self.frame_time: float | None = None     # when the PC captured it (server clock, drift-corrected)
        self.received: float | None = None
        self.seq = 0
        self._last_viewer = 0.0
        self._saved = 0.0
        try:
            if self.path.is_file():
                self.jpg = self.path.read_bytes()
                self.frame_time = self.received = self.path.stat().st_mtime
                self.seq = 1
        except OSError:
            pass

    # ------------------------------------------------------------------ from the agent
    def put(self, jpg: bytes, frame_time: float | None):
        now = time.time()
        with self.cv:
            first = self.received is None or now - self.received > 120
            self.jpg = jpg
            self.received = now
            # the PC's clock may be off; trust it only if it's close to ours
            self.frame_time = frame_time if frame_time and abs(now - frame_time) < 30 else now
            self.seq += 1
            self.cv.notify_all()
        if first:
            log.info("Camera frames arriving (%d KB)", len(jpg) // 1024)
        if now - self._saved > SAVE_EVERY:
            self._saved = now
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_bytes(jpg)
                os.replace(tmp, self.path)
            except OSError as e:
                log.debug("Couldn't save camera still: %s", e)

    # ------------------------------------------------------------------ viewers
    def touch(self):
        self._last_viewer = time.time()

    @property
    def watched(self) -> bool:
        return time.time() - self._last_viewer < VIEWER_TIMEOUT

    def wait_new(self, seq: int, timeout: float) -> tuple[int, bytes | None, float | None]:
        with self.cv:
            self.cv.wait_for(lambda: self.seq != seq, timeout=timeout)
            return self.seq, self.jpg, self.frame_time

    def info(self) -> dict:
        age = time.time() - self.received if self.received else None
        return {"available": self.jpg is not None, "last_frame_at": self.frame_time,
                "age_s": round(age, 1) if age is not None else None,
                "live": bool(age is not None and age < 5 and self.watched), "watched": self.watched}


# ---------------------------------------------------------------------------------------------------------------
# Live video: the agent pushes HLS (the camera's own H.264, not re-encoded) with ffmpeg's HTTP PUT output.
# Viewers get time-limited token URLs, so the iPhone player and <video> tags need no headers.
# ---------------------------------------------------------------------------------------------------------------
import hashlib as _hashlib
import hmac as _hmac
import re as _re
import secrets as _secrets

HLS_KEEP = 16            # segments kept per session (the playlist lists ~8)
HLS_STALE = 12           # a session with no playlist update for this long has ended
TOKEN_TTL = 3 * 3600


class LiveVideo:
    def __init__(self):
        self.lock = threading.Lock()
        self.session: str | None = None
        self.playlist: bytes | None = None
        self.segments: dict[str, bytes] = {}
        self.order: list[str] = []
        self.updated = 0.0
        self.started = 0.0
        self._arrivals: list[tuple[float, int]] = []   # (time, bytes) of recent segments - for diagnostics
        self._secret = _secrets.token_bytes(32)   # tokens stop working after a restart; clients just ask again

    # ---- from the agent
    def put(self, session: str, name: str, data: bytes):
        now = time.time()
        with self.lock:
            if session != self.session:   # the agent runs one stream at a time: a new session replaces the old
                self.session, self.playlist = session, None
                self.segments.clear()
                self.order.clear()
                self._arrivals.clear()
                self.started = now
                log.info("Live video session %s started", session)
            if name.endswith(".m3u8"):
                self.playlist = self._rewrite(data)
                self.updated = now
            else:
                self.segments[name] = data
                self.order.append(name)
                self._arrivals = (self._arrivals + [(now, len(data))])[-10:]
                while len(self.order) > HLS_KEEP:
                    self.segments.pop(self.order.pop(0), None)

    def delete(self, session: str, name: str):
        pass   # ffmpeg deletes old segments; we already keep only the last few

    @staticmethod
    def _rewrite(data: bytes) -> bytes:
        """Make every segment URI a bare file name, so it resolves under the viewer's token URL."""
        out = []
        for line in data.decode("utf-8", "replace").splitlines():
            if line and not line.startswith("#"):
                line = line.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            out.append(line)
        return ("\n".join(out) + "\n").encode()

    # ---- viewers
    @property
    def ready(self) -> bool:
        return bool(self.playlist) and time.time() - self.updated < HLS_STALE and len(self.order) >= 2

    def token(self) -> str:
        exp = int(time.time()) + TOKEN_TTL
        sig = _hmac.new(self._secret, str(exp).encode(), _hashlib.sha256).hexdigest()[:32]
        return f"{exp}-{sig}"

    def token_ok(self, token: str) -> bool:
        m = _re.fullmatch(r"(\d{9,11})-([0-9a-f]{32})", token or "")
        if not m or int(m.group(1)) < time.time():
            return False
        good = _hmac.new(self._secret, m.group(1).encode(), _hashlib.sha256).hexdigest()[:32]
        return _hmac.compare_digest(good, m.group(2))

    def get(self, session: str, name: str) -> tuple[bytes | None, str]:
        with self.lock:
            if session != self.session:
                return None, ""
            if name.endswith(".m3u8"):
                return self.playlist, "application/vnd.apple.mpegurl"
            return self.segments.get(name), "video/mp2t"

    def info(self) -> dict:
        d = {"session": self.session if self.ready else None, "ready": self.ready,
             "age_s": round(time.time() - self.updated, 1) if self.updated else None}
        with self.lock:
            a = list(self._arrivals)
            pl = (self.playlist or b"").decode("utf-8", "replace")
        durs = [float(x) for x in _re.findall(r"#EXTINF:([\d.]+)", pl)]
        if durs:
            d["segment_s"] = round(sum(durs) / len(durs), 2)            # how long each chunk is
        if len(a) >= 3:
            gaps = [b[0] - x[0] for x, b in zip(a, a[1:])]
            d["arrival_gap_s"] = round(sum(gaps) / len(gaps), 2)        # how often chunks arrive (should ~= segment_s)
            d["arrival_gap_max_s"] = round(max(gaps), 2)                # the worst hiccup recently
            span = a[-1][0] - a[0][0]
            if span > 0:
                d["kbps"] = round(sum(x[1] for x in a[1:]) * 8 / span / 1000)
        return d
