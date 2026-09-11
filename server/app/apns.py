"""Apple Push Notification service (APNs) sender.

Needs a paid Apple Developer account:
  APNS_KEY_ID    - Key ID of the APNs auth key (developer.apple.com → Keys)
  APNS_TEAM_ID   - your 10-character Team ID
  APNS_TOPIC     - the app's bundle ID, e.g. com.jtquayle.hicps2
  APNS_KEY_FILE  - path to the AuthKey_XXXXXXXXXX.p8 file (default: /data/AuthKey.p8)

Uses HTTP/2 (httpx[http2]) and ES256 JWTs (cryptography). If anything is missing, push is simply disabled.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("hanwha-server.apns")

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class APNs:
    def __init__(self):
        self.key_id = os.environ.get("APNS_KEY_ID", "").strip()
        self.team_id = os.environ.get("APNS_TEAM_ID", "").strip()
        self.topic = os.environ.get("APNS_TOPIC", "").strip()
        key_file = os.environ.get("APNS_KEY_FILE", "").strip()
        if not key_file:
            data_dir = Path(os.environ.get("DB_PATH", "/data/monitor.db")).parent
            found = sorted(data_dir.glob("AuthKey*.p8"))
            key_file = str(found[0]) if found else str(data_dir / "AuthKey.p8")
        self.key_file = key_file
        self.error = ""
        self._key = None
        self._jwt = ("", 0.0)
        self._lock = threading.Lock()
        self._clients: dict = {}
        self.enabled = self._setup()

    def _setup(self) -> bool:
        missing = [n for n, v in (("APNS_KEY_ID", self.key_id), ("APNS_TEAM_ID", self.team_id),
                                  ("APNS_TOPIC", self.topic)) if not v]
        if missing:
            self.error = "not configured (missing " + ", ".join(missing) + ")"
            return False
        if not Path(self.key_file).is_file():
            self.error = f"key file not found: {self.key_file}"
            return False
        try:
            import httpx  # noqa: F401
            import h2  # noqa: F401
            from cryptography.hazmat.primitives import serialization
            self._key = serialization.load_pem_private_key(Path(self.key_file).read_bytes(), password=None)
        except Exception as e:
            self.error = f"unavailable: {e}"
            return False
        log.info("APNs push enabled (topic %s, key %s)", self.topic, self.key_id)
        return True

    # -------------------------------------------------------------------- auth
    def _token(self) -> str:
        tok, made = self._jwt
        if tok and time.time() - made < 45 * 60:
            return tok
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        header = _b64(json.dumps({"alg": "ES256", "kid": self.key_id}).encode())
        claims = _b64(json.dumps({"iss": self.team_id, "iat": int(time.time())}).encode())
        signing_input = f"{header}.{claims}".encode()
        der = self._key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        tok = f"{header}.{claims}.{sig}"
        self._jwt = (tok, time.time())
        return tok

    def _client(self, env: str):
        import httpx
        c = self._clients.get(env)
        if c is None:
            c = httpx.Client(http2=True, base_url=HOSTS[env], timeout=10)
            self._clients[env] = c
        return c

    # -------------------------------------------------------------------- send
    def send(self, token: str, payload: dict, push_type: str, env: str = "production",
             priority: int = 10, expiration: int | None = None, collapse_id: str | None = None) -> tuple[int, str]:
        """Returns (http status, reason). 200 = delivered to Apple."""
        if not self.enabled:
            return 0, self.error
        topic = self.topic + (".push-type.liveactivity" if push_type == "liveactivity" else "")
        headers = {
            "authorization": f"bearer {self._token()}",
            "apns-topic": topic,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
        }
        if expiration is not None:
            headers["apns-expiration"] = str(expiration)
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        body = json.dumps(payload, separators=(",", ":")).encode()
        with self._lock:
            try:
                r = self._client(env).post(f"/3/device/{token}", content=body, headers=headers)
            except Exception as e:
                self._clients.pop(env, None)
                return -1, str(e)
        reason = ""
        if r.status_code != 200:
            try:
                reason = r.json().get("reason", "")
            except Exception:
                reason = r.text[:200]
        return r.status_code, reason
