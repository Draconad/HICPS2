"""Web dashboard login: one user, PBKDF2-hashed password, server-side sessions (cookie).

First start (or RESET_LOGIN=true): admin / admin, and the first login has to choose new credentials.
The agent and the iPhone app keep using the API key; a logged-in browser doesn't need it.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time

log = logging.getLogger("hanwha-server.auth")

COOKIE = "hm_session"
DEVICE_COOKIE = "hm_device"   # "this browser already proved it knows the API key"
SESSION_DAYS = 30
DEVICE_DAYS = 365
ITERATIONS = 240_000
MAX_FAILS = 5            # per client address...
FAIL_WINDOW = 5 * 60     # ...within this many seconds, then locked out for the rest of the window


def _hash(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex()


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db, lock, reset: bool = False):
        self.db, self.lock = db, lock
        self._fails: dict[str, list[float]] = {}
        self._fails_lock = threading.Lock()
        with self.lock:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS auth_user (id INTEGER PRIMARY KEY CHECK (id = 1), username TEXT NOT NULL,
                    pw_hash TEXT NOT NULL, salt TEXT NOT NULL, iterations INTEGER NOT NULL, must_change INTEGER NOT NULL,
                    updated REAL);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, username TEXT NOT NULL, created REAL NOT NULL,
                    expires REAL NOT NULL, ip TEXT);
                CREATE TABLE IF NOT EXISTS trusted_devices (id TEXT PRIMARY KEY, key_fp TEXT NOT NULL, created REAL NOT NULL,
                    expires REAL NOT NULL, last_used REAL, ip TEXT);
            """)
            row = db.execute("SELECT id FROM auth_user WHERE id=1").fetchone()
            if row is None or reset:
                self._set_locked("admin", "admin", must_change=True)
                db.execute("DELETE FROM sessions")
                log.warning("Web login %s: admin / admin (you'll be asked to change it on first login)",
                            "reset" if row is not None else "created")
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("DELETE FROM trusted_devices WHERE expires < ?", (time.time(),))

    # ------------------------------------------------------------------ user
    def _set_locked(self, username: str, password: str, must_change: bool):
        salt = secrets.token_bytes(16)
        self.db.execute("""INSERT INTO auth_user(id,username,pw_hash,salt,iterations,must_change,updated)
                           VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,
                           pw_hash=excluded.pw_hash, salt=excluded.salt, iterations=excluded.iterations,
                           must_change=excluded.must_change, updated=excluded.updated""",
                        (username, _hash(password, salt), salt.hex(), ITERATIONS, int(must_change), time.time()))

    def _user(self):
        with self.lock:
            return self.db.execute("SELECT * FROM auth_user WHERE id=1").fetchone()

    def _check_password(self, username: str, password: str) -> bool:
        u = self._user()
        if u is None:
            return False
        ok_pw = hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"]), u["iterations"]), u["pw_hash"])
        ok_user = hmac.compare_digest(username.strip().lower().encode(), u["username"].lower().encode())
        return ok_pw and ok_user

    # ------------------------------------------------------------------ rate limit
    def locked_out(self, ip: str) -> float:
        """Seconds until this address may try again (0 = allowed)."""
        now = time.time()
        with self._fails_lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
            self._fails[ip] = recent
            return (recent[0] + FAIL_WINDOW - now) if len(recent) >= MAX_FAILS else 0.0

    def _failed(self, ip: str):
        with self._fails_lock:
            self._fails.setdefault(ip, []).append(time.time())

    # ------------------------------------------------------------------ trusted browsers
    @staticmethod
    def key_fingerprint(api_key: str) -> str:
        """Ties a trusted browser to the current API key: changing API_KEY forgets every browser."""
        return hashlib.sha256(b"hm-device:" + api_key.encode()).hexdigest()

    def trust_device(self, key_fp: str, ip: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.db.execute("INSERT INTO trusted_devices(id,key_fp,created,expires,last_used,ip) VALUES(?,?,?,?,?,?)",
                            (_token_id(token), key_fp, now, now + DEVICE_DAYS * 86400, now, ip))
        return token

    def device_ok(self, token: str | None, key_fp: str) -> bool:
        if not token:
            return False
        with self.lock:
            d = self.db.execute("SELECT * FROM trusted_devices WHERE id=?", (_token_id(token),)).fetchone()
            if d is None:
                return False
            if d["expires"] < time.time() or not hmac.compare_digest(d["key_fp"], key_fp):
                self.db.execute("DELETE FROM trusted_devices WHERE id=?", (d["id"],))
                return False
            self.db.execute("UPDATE trusted_devices SET last_used=? WHERE id=?", (time.time(), d["id"]))
        return True

    def forget_device(self, token: str | None):
        if token:
            with self.lock:
                self.db.execute("DELETE FROM trusted_devices WHERE id=?", (_token_id(token),))

    # ------------------------------------------------------------------ sessions
    def login(self, username: str, password: str, ip: str, extra_ok: bool = True) -> tuple[str | None, str]:
        """Returns (session token, "") or (None, error message)."""
        wait = self.locked_out(ip)
        if wait:
            return None, f"Too many attempts - try again in {int(wait // 60) + 1} min."
        if not (self._check_password(username or "", password or "") and extra_ok):
            self._failed(ip)
            log.warning("Failed web login from %s", ip)
            return None, "Wrong username, password or API key."
        with self._fails_lock:
            self._fails.pop(ip, None)
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.db.execute("INSERT INTO sessions(id,username,created,expires,ip) VALUES(?,?,?,?,?)",
                            (_token_id(token), self._user()["username"], now, now + SESSION_DAYS * 86400, ip))
        log.info("Web login from %s", ip)
        return token, ""

    def session(self, token: str | None) -> dict | None:
        """{'username', 'must_change'} for a valid session token, else None."""
        if not token:
            return None
        with self.lock:
            s = self.db.execute("SELECT * FROM sessions WHERE id=?", (_token_id(token),)).fetchone()
            if s is None:
                return None
            if s["expires"] < time.time():
                self.db.execute("DELETE FROM sessions WHERE id=?", (s["id"],))
                return None
            u = self.db.execute("SELECT username, must_change FROM auth_user WHERE id=1").fetchone()
        return {"username": u["username"], "must_change": bool(u["must_change"])}

    def logout(self, token: str | None):
        if token:
            with self.lock:
                self.db.execute("DELETE FROM sessions WHERE id=?", (_token_id(token),))

    def change(self, token: str, current_password: str, new_username: str, new_password: str) -> str:
        """Change the login. Returns "" on success or an error message. Other sessions are signed out."""
        u = self._user()
        new_username = (new_username or "").strip()
        if not self._check_password(u["username"], current_password or ""):
            return "Current password is wrong."
        if not new_username or len(new_username) > 64:
            return "Enter a username."
        if len(new_password or "") < 8:
            return "Use at least 8 characters for the password."
        if new_password.lower() in ("admin", "password", "12345678", new_username.lower()):
            return "Pick a less guessable password."
        with self.lock:
            self._set_locked(new_username, new_password, must_change=False)
            self.db.execute("DELETE FROM sessions WHERE id<>?", (_token_id(token),))
            self.db.execute("UPDATE sessions SET username=?", (new_username,))
        log.info("Web login changed (user %s)", new_username)
        return ""
