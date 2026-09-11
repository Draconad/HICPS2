"""Camera relay: reads the Tapo camera's RTSP stream with ffmpeg and posts JPEG frames to the server.

Idle (nobody watching): one JPEG still every IDLE_SNAPSHOT seconds.
Live (the server says someone has the dashboard or app camera open): the camera's own H.264 video is passed through
untouched (no re-encoding - next to no CPU) as HLS, pushed to the server with ffmpeg's HTTP PUT output, plus a
still every LIVE_STILL seconds for the poster image. The server serves it to the browser / iPhone as smooth video.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

from .config import Config, normalize_url

log = logging.getLogger("hanwha.camera")

IDLE_SNAPSHOT = 60      # seconds between stills while nobody is watching
LIVE_LINGER = 10        # keep streaming this long after the last "someone's watching" from the server
MAX_WIDTH = 1280        # stills are scaled down to this width
LIVE_STILL = 3          # seconds between stills while streaming video
HLS_SEGMENT = 2         # seconds per video chunk: players buffer ~3 chunks, so a network hiccup doesn't pause playback
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: no console flashing up


def find_ffmpeg(cfg: Config) -> str | None:
    cands = []
    if cfg.ffmpeg_path.strip():
        p = Path(cfg.ffmpeg_path.strip().strip('"'))
        cands.append(p / "ffmpeg.exe" if p.is_dir() else p)
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    cands += [exe_dir / "ffmpeg.exe", exe_dir / "ffmpeg" / "ffmpeg.exe", exe_dir / "ffmpeg" / "bin" / "ffmpeg.exe"]
    if cfg.dll_path.strip():
        cands.append(Path(cfg.dll_path.strip()) / "ffmpeg.exe")
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which("ffmpeg")


def camera_url(cfg: Config) -> str:
    addr = cfg.camera_address.strip()
    if not addr:
        return ""
    if addr.lower().startswith("rtsp://"):
        return addr
    auth = ""
    if cfg.camera_user:
        auth = quote(cfg.camera_user, safe="") + ":" + quote(cfg.camera_password, safe="") + "@"
    host = addr if ":" in addr else f"{addr}:554"
    return f"rtsp://{auth}{host}/{'stream1' if cfg.camera_hd else 'stream2'}"


def _redact(text: str) -> str:
    """Hide the camera password in log lines."""
    import re
    return re.sub(r"(rtsp://[^:/@\s]+):[^@\s]+@", r"\1:***@", text)


def _explain(err: str) -> str:
    e = err.lower()
    if "[http @" in e or "http error" in e:   # the video upload to the server, not the camera
        if "401" in e:
            return "The server rejected the API key for the video upload."
        return "Couldn't upload the video to the server: " + (err.strip().splitlines() or [""])[-1][:150]
    if "401" in e or "unauthorized" in e:
        return "The camera rejected the username/password (use the Camera Account from the Tapo app, not your Tapo login)."
    if "connection refused" in e or "10061" in e:
        return "The camera refused the connection. Is the Camera Account set up in the Tapo app?"
    if "timed out" in e or "10060" in e or "no route" in e or "unreachable" in e:
        return "Can't reach the camera. Check its IP address."
    if "404" in e or "not found" in e:
        return "The camera doesn't have that stream."
    return err.strip().splitlines()[-1][:200] if err.strip() else "ffmpeg stopped without saying why."


class CameraRelay:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.server = normalize_url(cfg.server_url)
        self.enabled = bool(cfg.camera_enabled and cfg.camera_address.strip())
        self.status = "Off" if not self.enabled else "Starting…"
        self.error = ""
        self.last_frame: bytes | None = None
        self.last_frame_at: float | None = None
        self.last_sent_at: float | None = None
        self.live = False
        self._live_until = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._latest: tuple[bytes, float] | None = None
        self._send_cv = threading.Condition()
        self._session = requests.Session()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ control
    def start(self):
        if not self.enabled:
            return
        for target, name in ((self._run, "camera"), (self._sender, "camera-send")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._send_cv:
            self._send_cv.notify_all()
        self._kill()

    def set_live(self, wanted: bool | None):
        """From the server (ingest response / frame upload response): is anyone watching?"""
        if wanted:
            first = time.time() > self._live_until
            self._live_until = time.time() + LIVE_LINGER
            if first:
                self._wake.set()

    # ------------------------------------------------------------------ capture
    def _ffmpeg_cmd(self, single: bool) -> list[str] | None:
        ff = find_ffmpeg(self.cfg)
        if not ff:
            return None
        fps = max(0.2, min(10.0, float(self.cfg.camera_fps or 4)))
        vf = f"scale='min({MAX_WIDTH},iw)':-2" + ("" if single else f",fps={fps:g}")
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
               "-i", camera_url(self.cfg), "-an", "-vf", vf, "-q:v", "5"]
        cmd += (["-frames:v", "1"] if single else []) + ["-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        return cmd

    def _live_cmd(self, session: str) -> list[str] | None:
        """Video passthrough to the server as HLS (1 s segments), plus stills on stdout."""
        ff = find_ffmpeg(self.cfg)
        if not ff:
            return None
        if self.cfg.camera_transcode:   # H.265 cameras, or cameras whose own frame timing stutters
            kbps = 1500 if self.cfg.camera_hd else 600
            # fps=15 makes the frame timing perfectly even (frames the camera stamps late are evened out)
            video = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                     "-g", str(15 * HLS_SEGMENT), "-keyint_min", str(15 * HLS_SEGMENT), "-sc_threshold", "0",
                     "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                     "-vf", f"fps=15,scale='min({MAX_WIDTH},iw)':-2"]
        else:
            video = ["-c:v", "copy"]
        push = f"{self.server}/api/camera/hls/push/{session}/live.m3u8"
        headers = ["-headers", f"X-API-Key: {self.cfg.api_key}\r\n"] if self.cfg.api_key else []
        # camera timestamps that jump (some Tapo firmware) leave holes players stall on: use arrival time instead
        retime = ["-use_wallclock_as_timestamps", "1", "-fflags", "+genpts"] if self.cfg.camera_retime else []
        return ([ff, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", "8000000"] + retime +
                ["-i", camera_url(self.cfg),
                 "-map", "0:v:0", "-an"] + video +
                ["-f", "hls", "-hls_time", str(HLS_SEGMENT), "-hls_list_size", "8",
                 "-hls_flags", "delete_segments+independent_segments",
                 "-method", "PUT", "-http_persistent", "1"] + headers + [push] +
                ["-map", "0:v:0", "-an", "-vf", f"fps=1/{LIVE_STILL},scale='min({MAX_WIDTH},iw)':-2", "-q:v", "5",
                 "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"])

    def _spawn(self, cmd: list[str]) -> subprocess.Popen:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=NO_WINDOW, bufsize=0)

    def _kill(self):
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

    def _frames(self, proc: subprocess.Popen):
        """Yield JPEG frames from ffmpeg's stdout (split on the JPEG start/end markers)."""
        buf = bytearray()
        while not self._stop.is_set():
            chunk = proc.stdout.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf.clear()
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buf[:start]
                    break
                yield bytes(buf[start:end + 2])
                del buf[:end + 2]

    def _got_frame(self, jpg: bytes):
        now = time.time()
        self.last_frame, self.last_frame_at = jpg, now
        self.error = ""
        with self._send_cv:
            self._latest = (jpg, now)
            self._send_cv.notify_all()

    def _run_ffmpeg(self, single: bool) -> bool:
        """One ffmpeg session. Returns True if at least one frame came through."""
        cmd = self._ffmpeg_cmd(single) if single else self._live_cmd(uuid.uuid4().hex[:10])
        if not self.server and not single:
            self.status, self.error = "Error", "Set the server URL first."
            return False
        if cmd is None:
            self.status, self.error = "ffmpeg.exe not found", "Put ffmpeg.exe next to HanwhaMonitor.exe (it comes with the download)."
            return False
        try:
            self._proc = proc = self._spawn(cmd)
        except OSError as e:
            if getattr(e, "winerror", None) == 216:
                self.error = "This ffmpeg.exe is 64-bit but Windows here is 32-bit - use a 32-bit ffmpeg.exe."
            else:
                self.error = f"Couldn't start ffmpeg: {e}"
            self.status = "Error"
            return False
        errs: list[str] = []
        threading.Thread(target=lambda: errs.extend(proc.stderr.read().decode("utf-8", "replace").splitlines()),
                         daemon=True).start()
        got = stopped_by_us = False
        for jpg in self._frames(proc):
            got = True
            self._got_frame(jpg)
            if single or time.time() > self._live_until:   # one still wanted / nobody watching any more
                stopped_by_us = True
                break
        self._kill()
        if self._stop.is_set():
            return got
        if not got or (not single and not stopped_by_us):   # ffmpeg gave up by itself
            time.sleep(0.3)
            self.error = _explain(_redact("\n".join(errs)))
            return False
        return True

    def _run(self):
        log.info("Camera relay started (%s)", _redact(camera_url(self.cfg)))
        fails = 0
        next_still = 0.0
        while not self._stop.is_set():
            live = time.time() < self._live_until
            if live:
                self.live, self.status = True, "Live"
                if self._run_ffmpeg(single=False):
                    fails = 0
                else:
                    fails += 1
                    self.status = "Error"
                    if fails in (1, 10) or fails % 60 == 0:
                        log.warning("Camera: %s", self.error)
                    self._stop.wait(min(30, 2 * fails))
                continue
            if self.live:   # just stopped streaming
                self.live, self.status = False, "Idle (still every minute)"
            if time.time() >= next_still:
                ok = self._run_ffmpeg(single=True)
                next_still = time.time() + (IDLE_SNAPSHOT if ok else min(60, 5 * (fails + 1)))
                if ok:
                    fails, self.status = 0, "Idle (still every minute)"
                else:
                    fails += 1
                    self.status = "Error"
                    if fails in (1, 10) or fails % 60 == 0:
                        log.warning("Camera: %s", self.error)
            self._wake.wait(1)
            self._wake.clear()

    # ------------------------------------------------------------------ upload
    def _sender(self):
        if not self.server:
            return
        headers = {"Content-Type": "image/jpeg"}
        if self.cfg.api_key:
            headers["X-API-Key"] = self.cfg.api_key
        while not self._stop.is_set():
            with self._send_cv:
                while self._latest is None and not self._stop.is_set():
                    self._send_cv.wait(1)
                if self._stop.is_set():
                    return
                (jpg, at), self._latest = self._latest, None
            try:
                h = dict(headers, **{"X-Frame-Time": f"{at:.3f}"})
                r = self._session.post(f"{self.server}/api/camera/frame", data=jpg, headers=h, timeout=8)
                r.raise_for_status()
                self.last_sent_at = time.time()
                self.set_live(r.json().get("live"))
            except Exception as e:
                log.debug("Camera upload failed: %s", e)
                self._stop.wait(1)

    # ------------------------------------------------------------------ one-off test (GUI button)
    def test_snapshot(self) -> tuple[bytes | None, str]:
        cmd = self._ffmpeg_cmd(single=True)
        if cmd is None:
            return None, "ffmpeg.exe not found - put it next to HanwhaMonitor.exe."
        try:
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=20, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return None, "Timed out - can't reach the camera."
        except OSError as e:
            return None, f"Couldn't start ffmpeg: {e}"
        out = p.stdout
        s, e = out.find(b"\xff\xd8"), out.rfind(b"\xff\xd9")
        if s >= 0 and e > s:
            return out[s:e + 2], ""
        return None, _explain(_redact(p.stderr.decode("utf-8", "replace")))

    # ------------------------------------------------------------------ video timing check (GUI button)
    def check_timing(self, seconds: int = 10) -> str:
        """Record `seconds` of the camera's stream (no re-encoding) and report how evenly the frames are
        timestamped by the camera and how evenly they arrive over the network. Finds the cause of stutter."""
        ff = find_ffmpeg(self.cfg)
        if not ff:
            return "ffmpeg.exe not found - put it next to HanwhaMonitor.exe."
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
               "-i", camera_url(self.cfg), "-t", str(seconds), "-map", "0:v:0", "-c", "copy",
               "-flush_packets", "1", "-f", "framecrc", "pipe:1"]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=NO_WINDOW, bufsize=0)
        except OSError as e:
            return f"Couldn't start ffmpeg: {e}"
        tb = 1 / 90000
        pts: list[float] = []
        arrived: list[float] = []
        keys: list[int] = []
        deadline = time.time() + seconds + 25
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("ascii", "replace").strip()
            if time.time() > deadline:
                break
            if line.startswith("#tb"):
                try:
                    num, den = line.split(":", 1)[1].strip().split("/")
                    tb = int(num) / int(den)
                except ValueError:
                    pass
                continue
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            try:
                pts.append(int(parts[2]) * tb)
            except (IndexError, ValueError):
                continue
            arrived.append(time.time())
            flags = [p for p in parts[6:] if p.startswith("F=")]   # framecrc prints F= only when not a plain keyframe
            if not flags or int(flags[0][2:], 16) & 1:
                keys.append(len(pts) - 1)
        try:
            proc.kill()
        except Exception:
            pass
        err = proc.stderr.read().decode("utf-8", "replace")
        if len(pts) < 10:
            return "Couldn't read the video: " + _explain(_redact(err))
        return summarise_timing(pts, arrived, keys)


def summarise_timing(pts: list[float], arrived: list[float], keys: list[int]) -> str:
    """Plain-English verdict on camera timestamps (content) vs arrival (network)."""
    order = sorted(pts)
    ts_gaps = [b - a for a, b in zip(order, order[1:]) if b > a]
    ar_gaps = [b - a for a, b in zip(arrived, arrived[1:])]
    dur = order[-1] - order[0]
    fps = (len(order) - 1) / dur if dur > 0 else 0
    typ = sorted(ts_gaps)[len(ts_gaps) // 2] if ts_gaps else 0          # median frame interval
    ts_jumps = [g for g in ts_gaps if typ and g > 2.5 * typ]
    ar_late = [g for g in ar_gaps if g > max(0.25, 4 * typ)]
    key_every = (sum(b - a for a, b in zip(keys, keys[1:])) / (len(keys) - 1) / fps) if len(keys) > 1 and fps else None
    lines = [f"{len(order)} frames in {dur:.1f} s = {fps:.1f} fps"
             + (f", keyframe every {key_every:.1f} s" if key_every else "") + ".",
             f"Camera timestamps: frames {typ * 1000:.0f} ms apart, biggest gap {max(ts_gaps) * 1000:.0f} ms"
             f" ({len(ts_jumps)} jumps).",
             f"Network arrival: biggest delay {max(ar_gaps) * 1000:.0f} ms ({len(ar_late)} late bursts)."]
    if ts_jumps and len(ts_jumps) >= max(2, dur / 3):
        lines.append("Verdict: the camera's own timestamps jump - tick 'Fix camera timing' (and if it still isn't "
                     "smooth, also 'Re-encode the video').")
    elif len(ar_late) >= max(2, dur / 3):
        lines.append("Verdict: the video arrives in bursts from the camera (Wi-Fi) - the player buffer should hide "
                     "this; if it still stutters, check the camera's Wi-Fi signal.")
    else:
        lines.append("Verdict: the camera's video looks smooth and arrives evenly.")
    return "\n".join(lines)
