"""CI step: sign the built HanwhaMonitor.exe for automatic updates.

    UPDATE_SIGNING_KEY=<hex> python sign_update.py dist/HanwhaMonitor.exe   ->  dist/HanwhaMonitor.exe.sig

The PC app only installs an update whose signature matches the public key built into it (hanwha_agent/update_key.py).
"""
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hanwha_agent import ed25519  # noqa: E402
from hanwha_agent.config import VERSION  # noqa: E402
from hanwha_agent.update_key import PUBLIC_KEY_HEX  # noqa: E402

exe = Path(sys.argv[1])
key = bytes.fromhex(os.environ["UPDATE_SIGNING_KEY"].strip())
if ed25519.public_key(key).hex() != PUBLIC_KEY_HEX:
    sys.exit("UPDATE_SIGNING_KEY doesn't match the public key in hanwha_agent/update_key.py")
digest = hashlib.sha256(exe.read_bytes()).hexdigest()
sig = ed25519.sign(key, ed25519.update_message(VERSION, digest)).hex()
Path(str(exe) + ".sig").write_text(f"{sig}\n{VERSION}\n")   # line 1 signature, line 2 version
print(f"Signed {exe.name} {VERSION} (sha256 {digest[:16]}...)")
