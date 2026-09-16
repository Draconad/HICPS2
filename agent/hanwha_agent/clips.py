"""Saves a few seconds of camera video around each alarm.

ffmpeg keeps a rolling recording of the camera on this PC - short segments, copied straight from the camera
with no re-encoding - and only the couple of minutes just gone are kept. When an alarm comes in, the segments
covering that moment are stitched together and sent to the server, where the app and the dashboard can play it.

The camera picture runs a few seconds behind real life (the camera's own buffering, then the network). So the
moment the alarm happened doesn't reach the recording until `camera_delay_s` later: the clip window is shifted
by that much, and the clip isn't cut until the footage has actually arrived.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

from .camera import NO_WINDOW, camera_url, find_ffmpeg
from .config import Config, data_dir, normalize_url

log = logging.getLogger("hanwha.clips")

SEGMENT = 2.0            # seconds per segment of the rolling recording
KEEP = 180.0             # keep this much of the recording on disk
MAX_CLIP = 60 * 1024 * 1024
MAX_WINDOW = 120.0       # a clip is never longer than this
SETTLE = 3.0             # extra wait after the last wanted moment, so the segment is finished and closed


class ClipRecorder:
    """Rolling recording + "save what happened around this alarm"."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.server = normalize_url(cfg.server_url)
        self.dir = data_dir() / "clipbuf"
        self.enabled = bool(cfg.camera_clips and cfg.camera_enabled and cfg.camera_address.strip())
        self.status = "Off" if not self.enabled else "Starting…"
        self.error = ""
        self.saved = 0
        self.last_saved_at: float | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._pending: list[dict] = []
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ control
    def start(self):
        if not self.enabled:
            return
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.status, self.error = "Can't record", str(e)
            return
        for target, name in ((self._run, "clip-record"), (self._worker, "clip-save")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def info(self) -> dict:
        return {"enabled": self.enabled, "status": self.status, "error": self.error, "saved": self.saved,
                "last_saved_at": self.last_saved_at, "pending": len(self._pending),
                "delay_s": float(self.cfg.camera_delay_s or 0)}

    # ------------------------------------------------------------------ alarms
    def on_snapshot(self, snap: dict):
        """Collector listener: spots alarms that have just started and books a clip for each."""
        if not self.enabled:
            return
        for a in snap.get("alarms") or []:
            aid = str(a.get("id") or "")
            if not aid or a.get("cleared_at") or aid in self._seen:
                continue
            self._seen.add(aid)
            started = float(a.get("started_at") or time.time())
            if time.time() - started > 60:      # an alarm that was already on when this app started
                continue
            self.alarm(aid, started, f"{a.get('code') or ''} {a.get('message') or ''}".strip())
        if len(self._seen) > 500:
            self._seen = set(list(self._seen)[-200:])

    def alarm(self, alarm_id: str, at: float, label: str = ""):
        """An alarm has just come in: save the video around it once the camera has caught up."""
        if not self.enabled or not alarm_id:
            return
        delay = max(0.0, float(self.cfg.camera_delay_s or 0))
        post = max(1.0, float(self.cfg.clip_post_s or 10))
        pre = max(1.0, float(self.cfg.clip_pre_s or 15))
        with self._lock:
            if any(j["id"] == alarm_id for j in self._pending):
                return
            self._pending.append({"id": alarm_id, "at": at, "label": label,
                                  # in recording time, the alarm moment shows up `delay` seconds later
                                  "from": at + delay - pre, "to": at + delay + post,
                                  "ready_at": at + delay + post + SETTLE})
        log.info("Alarm %s: clip due in %.0f s", label or alarm_id, max(0.0, at + delay + post + SETTLE - time.time()))

    # ------------------------------------------------------------------ recording
    def _cmd(self) -> list[str] | None:
        ff = find_ffmpeg(self.cfg)
        if not ff:
            return None
        # segments named by the wall-clock time they start, which is what the clip window is matched against
        return [ff, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
                "-use_wallclock_as_timestamps", "1", "-fflags", "+genpts",
                "-i", camera_url(self.cfg), "-map", "0:v:0", "-an", "-c:v", "copy",
                "-f", "segment", "-segment_time", str(SEGMENT), "-reset_timestamps", "1",
                "-segment_format", "mpegts", "-strftime", "1",
                str(self.dir / "seg-%Y%m%d-%H%M%S.ts")]

    def _run(self):
        backoff = 5.0
        while not self._stop.is_set():
            cmd = self._cmd()
            if not cmd:
                self.status, self.error = "ffmpeg.exe not found", "Put ffmpeg.exe next to HanwhaMonitor.exe."
                self._stop.wait(30)
                continue
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE, creationflags=NO_WINDOW)
            except OSError as e:
                self.status, self.error = "Can't record", str(e)
                self._stop.wait(30)
                continue
            self.status, self.error = "Recording", ""
            started = time.time()
            while not self._stop.is_set() and self._proc.poll() is None:
                self._tidy()
                self._stop.wait(2)
            err = b""
            if self._proc and self._proc.stderr:
                try:
                    err = self._proc.stderr.read() or b""
                except Exception:
                    pass
            if self._stop.is_set():
                return
            self.status = "Reconnecting…"
            self.error = (err.decode(errors="replace").strip().splitlines() or ["The camera stopped sending"])[-1][:200]
            backoff = 5.0 if time.time() - started > 60 else min(60.0, backoff * 1.6)
            log.warning("Clip recording stopped (%s) - trying again in %.0f s", self.error, backoff)
            self._stop.wait(backoff)

    def _tidy(self):
        """Throw away segments older than the rolling window - but keep anything a pending clip still needs."""
        with self._lock:
            keep_from = min([j["from"] for j in self._pending], default=None)
        cutoff = time.time() - KEEP
        if keep_from is not None:
            cutoff = min(cutoff, keep_from - SEGMENT * 2)
        for f, start in self._segments():
            if start < cutoff - SEGMENT:
                try:
                    f.unlink()
                except OSError:
                    pass

    def _segments(self) -> list[tuple[Path, float]]:
        """Every segment on disk with the wall-clock second it started (from its file name)."""
        out = []
        for f in self.dir.glob("seg-*.ts"):
            try:
                t = time.mktime(time.strptime(f.stem[4:], "%Y%m%d-%H%M%S"))
            except ValueError:
                continue
            out.append((f, t))
        return sorted(out, key=lambda x: x[1])

    # ------------------------------------------------------------------ saving
    def _worker(self):
        while not self._stop.is_set():
            self._stop.wait(1)
            now = time.time()
            with self._lock:
                due = [j for j in self._pending if j["ready_at"] <= now]
            for job in due:
                try:
                    self._save(job)
                except Exception as e:      # never let one bad clip stop the rest
                    log.warning("Couldn't save the clip for %s: %s", job["label"] or job["id"], e)
                with self._lock:
                    self._pending = [j for j in self._pending if j["id"] != job["id"]]

    def pick(self, segs: list[tuple[Path, float]], start: float, end: float) -> list[Path]:
        """The segments covering [start, end] - a segment counts if any of it falls inside the window."""
        out = []
        for i, (f, at) in enumerate(segs):
            seg_end = segs[i + 1][1] if i + 1 < len(segs) else at + SEGMENT
            if seg_end > start and at < end:
                out.append(f)
        return out

    def _save(self, job: dict):
        segs = self._segments()
        window = min(MAX_WINDOW, job["to"] - job["from"])
        files = self.pick(segs, job["from"], job["from"] + window)
        if not files:
            log.info("No footage for %s - nothing recorded around then", job["label"] or job["id"])
            return
        ff = find_ffmpeg(self.cfg)
        if not ff:
            return
        listing = self.dir / f"list-{int(time.time())}.txt"
        listing.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
        out = self.dir / f"clip-{int(time.time())}.mp4"
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
               "-c", "copy", "-movflags", "+faststart", str(out)]
        try:
            subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           creationflags=NO_WINDOW, timeout=120)
            if not out.exists() or out.stat().st_size < 1000:
                log.info("Clip for %s came out empty", job["label"] or job["id"])
                return
            if out.stat().st_size > MAX_CLIP:
                log.info("Clip for %s is too big to send (%d MB)", job["label"] or job["id"],
                         out.stat().st_size // (1024 * 1024))
                return
            self._upload(job, out.read_bytes(), window)
        finally:
            listing.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

    def _upload(self, job: dict, blob: bytes, seconds: float):
        headers = {"Content-Type": "video/mp4"}
        if self.cfg.api_key:
            headers["X-API-Key"] = self.cfg.api_key
        url = (f"{self.server}/api/camera/clip?alarm={requests.utils.quote(str(job['id']))}"
               f"&at={job['at']:.0f}&seconds={seconds:.0f}&delay={float(self.cfg.camera_delay_s or 0):.1f}")
        r = self._session.put(url, data=blob, headers=headers, timeout=120)
        if r.status_code >= 300:
            log.warning("The server wouldn't take the clip for %s: HTTP %s", job["label"] or job["id"], r.status_code)
            return
        self.saved += 1
        self.last_saved_at = time.time()
        log.info("Clip saved for %s: %.0f s, %d KB", job["label"] or job["id"], seconds, len(blob) // 1024)
