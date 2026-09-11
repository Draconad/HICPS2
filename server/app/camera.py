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
