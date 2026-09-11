"""Minimal ONVIF client for camera pan/tilt (Tapo C200/C210 and most IP cameras), using the camera account.

Only what's needed: find the PTZ service and a media profile, then RelativeMove (falls back to ContinuousMove +
Stop for cameras without relative moves). WS-Security UsernameToken digest auth, corrected for camera clock drift.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
import re
import secrets
import time
from xml.sax.saxutils import escape

import requests

log = logging.getLogger("hanwha.onvif")

SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS = {"tds": "http://www.onvif.org/ver10/device/wsdl", "trt": "http://www.onvif.org/ver10/media/wsdl",
      "tptz": "http://www.onvif.org/ver20/ptz/wsdl", "tt": "http://www.onvif.org/ver10/schema"}


class OnvifError(Exception):
    pass


class OnvifPTZ:
    def __init__(self, host: str, user: str, password: str, port: int = 2020, timeout: float = 5):
        self.host, self.user, self.password, self.port, self.timeout = host, user, password, port, timeout
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.ptz_url: str | None = None
        self.media_url: str | None = None
        self.profile: str | None = None
        self.clock_offset = 0.0          # camera clock - our clock
        self.relative_ok = True
        self._session = requests.Session()

    # ------------------------------------------------------------------ SOAP plumbing
    def _security(self) -> str:
        if not self.user:
            return ""
        nonce = secrets.token_bytes(16)
        created = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.clock_offset)) \
            .strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
            f"<Username>{escape(self.user)}</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0'
            f'#PasswordDigest">{digest}</Password>'
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0'
            f'#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f"{created}</Created></UsernameToken></Security>")

    def _call(self, url: str, body: str, auth: bool = True) -> str:
        env = (f'<s:Envelope xmlns:s="{SOAP}" ' + " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items()) + ">"
               f"<s:Header>{self._security() if auth else ''}</s:Header><s:Body>{body}</s:Body></s:Envelope>")
        try:
            r = self._session.post(url, data=env.encode(), timeout=self.timeout,
                                   headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        except requests.RequestException as e:
            raise OnvifError(f"Can't reach the camera's ONVIF service ({self.host}:{self.port}): {e}") from None
        if r.status_code >= 400 or "Fault" in r.text[:2000]:
            reason = re.search(r"<[^>]*Text[^>]*>([^<]+)<", r.text)
            msg = reason.group(1) if reason else f"HTTP {r.status_code}"
            if r.status_code in (400, 401) or "NotAuthorized" in r.text or "authoriz" in msg.lower():
                raise OnvifError(f"The camera rejected the login for pan/tilt ({msg}) - check the Camera Account.")
            raise OnvifError(msg)
        return r.text

    # ------------------------------------------------------------------ setup
    def connect(self):
        # the camera's clock: digest auth fails if ours and the camera's differ by more than a few seconds
        try:
            xml = self._call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
            utc = xml.split("UTCDateTime", 1)[1] if "UTCDateTime" in xml else ""
            parts = {k: int(v) for k, v in re.findall(r"<(?:\w+:)?(Year|Month|Day|Hour|Minute|Second)>(\d+)<", utc)}
            if len(parts) == 6:
                cam = dt.datetime(parts["Year"], parts["Month"], parts["Day"], parts["Hour"], parts["Minute"],
                                  parts["Second"], tzinfo=dt.timezone.utc).timestamp()
                self.clock_offset = cam - time.time()
        except (OnvifError, IndexError, ValueError):
            pass
        xml = self._call(self.device_url, '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>')
        self.ptz_url = self._xaddr(xml, "PTZ") or self.device_url.replace("device_service", "service")
        self.media_url = self._xaddr(xml, "Media") or self.device_url.replace("device_service", "service")
        xml = self._call(self.media_url, "<trt:GetProfiles/>")
        m = re.search(r'Profiles[^>]*\btoken="([^"]+)"', xml)
        if not m:
            raise OnvifError("The camera didn't list any media profiles.")
        self.profile = m.group(1)
        log.info("Camera pan/tilt ready (profile %s)", self.profile)

    @staticmethod
    def _xaddr(xml: str, section: str) -> str | None:
        m = re.search(rf"<(?:\w+:)?{section}>.*?<(?:\w+:)?XAddr>([^<]+)<", xml, re.S)
        return m.group(1).strip() if m else None

    # ------------------------------------------------------------------ presets (the Tapo app's saved positions)
    def presets(self) -> list[dict]:
        """[{token, name}] of the positions saved on the camera (Tapo app: camera > pan/tilt > Preset)."""
        if not self.profile:
            self.connect()
        xml = self._call(self.ptz_url, f"<tptz:GetPresets><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>"
                                       "</tptz:GetPresets>")
        out = []
        for token, inner in re.findall(r'<(?:\w+:)?Preset\b[^>]*\btoken="([^"]+)"[^>]*>(.*?)</(?:\w+:)?Preset>', xml, re.S):
            name = re.search(r"<(?:\w+:)?Name>([^<]*)<", inner)
            out.append({"token": token, "name": (name.group(1).strip() if name else "") or f"Preset {token}"})
        return out

    def goto_preset(self, token: str):
        if not self.profile:
            self.connect()
        self._call(self.ptz_url, f"<tptz:GotoPreset><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>"
                                 f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:GotoPreset>")

    # ------------------------------------------------------------------ moves
    def move(self, x: float, y: float):
        """Nudge the camera. x: -1 (left) .. 1 (right), y: -1 (down) .. 1 (up); small steps like 0.1."""
        if not self.profile:
            self.connect()
        x, y = max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))
        if self.relative_ok:
            try:
                self._call(self.ptz_url, f"<tptz:RelativeMove><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>"
                                         f'<tptz:Translation><tt:PanTilt x="{x:.3f}" y="{y:.3f}"/></tptz:Translation>'
                                         "</tptz:RelativeMove>")
                return
            except OnvifError as e:
                if "rejected the login" in str(e):
                    raise
                log.info("Camera: RelativeMove not supported (%s) - using ContinuousMove", e)
                self.relative_ok = False
        # continuous move for a short burst, then stop
        speed = 0.5
        vx = speed if x > 0 else -speed if x < 0 else 0
        vy = speed if y > 0 else -speed if y < 0 else 0
        self._call(self.ptz_url, f"<tptz:ContinuousMove><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>"
                                 f'<tptz:Velocity><tt:PanTilt x="{vx}" y="{vy}"/></tptz:Velocity></tptz:ContinuousMove>')
        time.sleep(min(1.0, 0.25 + 2 * max(abs(x), abs(y))))
        self._call(self.ptz_url, f"<tptz:Stop><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>"
                                 "<tptz:PanTilt>true</tptz:PanTilt></tptz:Stop>")
