"""Integration tests for the Hanwha Monitor server. Runs against a live server on a throwaway database."""
import json, os, subprocess, sys, time, urllib.error, urllib.request

BASE = os.environ.get("BASE", "http://localhost:8450")
KEY = os.environ.get("KEY", "testkey123")
PASS, FAIL = [], []


def call(method, path, body=None, raw=None, headers=None, key=True, expect=None):
    url = BASE + path
    h = {"X-API-Key": KEY} if key else {}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out, code = r.read(), r.status
            hdrs = dict(r.headers)
    except urllib.error.HTTPError as e:
        out, code, hdrs = e.read(), e.code, dict(e.headers)
    try:
        parsed = json.loads(out)
    except ValueError:
        parsed = out
    return code, parsed, hdrs


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ok   " if ok else "  FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


def no500(name, method, path, **kw):
    code, body, _ = call(method, path, **kw)
    check(name, code != 500, f"HTTP {code} {str(body)[:120]}")
    return code, body


def snap(**kw):
    s = {"sent_at": time.time(), "machine_connected": True, "state": "running", "demo": False}
    s.update(kw)
    return s


print("\n=== rubbish input: nothing should 500 ===")
no500("alarms as strings", "POST", "/api/ingest", body=snap(alarms=["x"]))
no500("alarm fields as lists", "POST", "/api/ingest",
      body=snap(alarms=[{"id": "a", "key": "k", "started_at": time.time(), "code": ["x"], "message": {"a": 1}}]))
no500("program as a string", "POST", "/api/ingest", body=snap(program="O3110"))
no500("camera_features as a list", "POST", "/api/ingest", body=snap(camera_features=[1, 2]))
no500("controls as a string", "POST", "/api/ingest", body=snap(controls="x"))
no500("parts as a bool", "POST", "/api/ingest", body=snap(parts=True, parts_total=True))
no500("parts as a string", "POST", "/api/ingest", body=snap(parts="lots"))
no500("state as a number", "POST", "/api/ingest", body=snap(state=7))
no500("messages as a dict", "POST", "/api/ingest", body=snap(messages={"a": 1}))
no500("nothing at all", "POST", "/api/ingest", body={})
no500("a list instead of an object", "POST", "/api/ingest", body=[1, 2, 3])
no500("status still works after all that", "GET", "/api/status")
no500("push register, list env", "POST", "/api/push/register", body={"kind": "alert", "token": "aa", "env": ["p"]})
no500("push register, list prefs", "POST", "/api/push/register", body={"kind": "alert", "token": "bb", "prefs": [1, 2]})
no500("push status after bad rows", "GET", "/api/push/status")
no500("maintenance, absurd date", "POST", "/api/maintenance", body={"item": "battery", "last_at": 1e18})
no500("maintenance, text date", "POST", "/api/maintenance", body={"item": "battery", "last_at": "yesterday"})
no500("maintenance, months as text", "POST", "/api/maintenance", body={"item": "battery", "every_months": "lots"})
no500("maintenance still readable", "GET", "/api/maintenance")
no500("status still works", "GET", "/api/status")
no500("report, silly period", "GET", "/api/report?period=fortnight")
no500("report, silly limit", "GET", "/api/report?period=day&limit=-5")
no500("report, text dates", "GET", "/api/report?period=day&from=soon&to=later")
no500("bars/day, bad date", "GET", "/api/bars/day?date=31st")
no500("programs detail, no program", "GET", "/api/programs/detail")
no500("programs, bad number", "POST", "/api/programs", body={"program": "../../etc/passwd"})
no500("programs, dots for a name", "POST", "/api/programs", body={"program": ".."})
no500("programs, silly ppb", "POST", "/api/programs", body={"program": "O1000", "ppb_manual": "many"})
no500("programs, silly stick out", "POST", "/api/programs", body={"program": "O1000", "stick_out": -5})
no500("doc, no program", "GET", "/api/programs/doc")
no500("doc, not a PDF", "PUT", "/api/programs/doc?program=O1000", raw=b"hello")
no500("clip, no alarm id", "GET", "/api/camera/clip")
no500("clip, unknown alarm", "GET", "/api/camera/clip?alarm=nope")
no500("alarms, text limit", "GET", "/api/alarms?limit=abc")
no500("states, text hours", "GET", "/api/states?hours=abc")
no500("replay, not a list", "POST", "/api/replay", body={"snapshots": "x"})
no500("replay, junk inside", "POST", "/api/replay", body={"snapshots": [1, "two", None]})
no500("unicode everywhere", "POST", "/api/ingest", body=snap(machine_name="機械 🙂", state_detail=chr(0) + "ok"))

print("\n=== the machine's day ===")
t0 = time.time() - 3600
call("POST", "/api/ingest", body={"sent_at": t0, "machine_connected": True, "state": "running", "parts": 0,
                                  "parts_total": 1000, "parts_required": 10, "program": {"number": 3110},
                                  "last_cycle_s": 30.0, "demo": False})
for i in range(1, 11):
    call("POST", "/api/ingest", body={"sent_at": t0 + i * 30, "machine_connected": True, "state": "running",
                                      "parts": i, "parts_total": 1000 + i, "parts_required": 10,
                                      "program": {"number": 3110}, "last_cycle_s": 30.0, "demo": False})
code, st, _ = call("GET", "/api/status")
check("part count followed", st.get("parts") == 10, str(st.get("parts")))
check("job complete recorded", bool(st.get("job_complete")), str(st.get("job_complete")))
code, rep, _ = call("GET", "/api/report?period=day&limit=5")
check("report counted the parts", rep["totals"]["parts"] >= 10, str(rep["totals"]))
check("report has a program breakdown", any(p["parts"] for p in rep["totals"]["programs"]), str(rep["totals"]["programs"]))

print("\n=== an alarm through a dropout ===")
now = time.time()
al = {"id": "1-1051-t", "key": "1-1051", "path": 1, "path_name": "Main", "code": "EX1051", "type": 1,
      "type_name": "Alarm", "number": 1051, "axis": 0, "message": "BARFEEDER EMERGENCY STOP", "started_at": now}
call("POST", "/api/ingest", body=snap(sent_at=now, state="alarm", alarms=[al]))
_, st, _ = call("GET", "/api/status")
check("alarm shows", [a["code"] for a in st["active_alarms"]] == ["EX1051"])
call("POST", "/api/ingest", body={"sent_at": now + 2, "machine_connected": False, "state": "off", "alarms": []})
call("POST", "/api/ingest", body=snap(sent_at=now + 4, state="alarm", alarms=[al]))
_, st, _ = call("GET", "/api/status")
check("alarm survives a missed poll", [a["code"] for a in st["active_alarms"]] == ["EX1051"],
      str(st["active_alarms"]))
call("POST", "/api/ingest", body=snap(sent_at=now + 6, state="running", alarms=[dict(al, cleared_at=now + 6)]))
_, st, _ = call("GET", "/api/status")
check("alarm clears when it really clears", st["active_alarms"] == [], str(st["active_alarms"]))

print("\n=== store and forward ===")
_, before, _ = call("GET", "/api/status")
old = [{"sent_at": time.time() - 900 + i * 30, "machine_connected": True, "state": "running", "parts": 50 + i,
        "parts_total": 2000 + i, "program": {"number": 1234}, "last_cycle_s": 30.0, "demo": False} for i in range(5)]
code, res, _ = call("POST", "/api/replay", body={"snapshots": old})
check("replay accepted", code == 200 and res.get("accepted") == 5, f"{code} {res}")
_, after, _ = call("GET", "/api/status")
check("replay didn't rewind the live state", after["state"] == before["state"] and after["agent_online"],
      f"{before['state']} -> {after['state']}, online={after['agent_online']}")
check("replay didn't overwrite the part count", after["parts"] == before["parts"],
      f"{before['parts']} -> {after['parts']}")

print("\n=== job PDF ===")
pdf = b"%PDF-1.4\n" + b"x" * 4000
code, body, _ = call("PUT", "/api/programs/doc?program=O3110", raw=pdf, headers={"X-Filename": "dims.pdf"})
check("PDF uploaded", code == 200 and body.get("has_doc"), f"{code} {body}")
code, got, hdr = call("GET", "/api/programs/doc?program=O3110")
check("PDF comes back whole", got == pdf, f"{code} {len(got) if isinstance(got, bytes) else got}")
code, body, _ = call("PUT", "/api/programs/doc?program=O3110", raw=b"not a pdf at all" * 100)
check("non-PDF refused", code == 400, str(code))
code, got, _ = call("GET", "/api/programs/doc?program=O3110")
check("the good PDF survived the bad upload", got == pdf, "overwritten")
code, _, _ = call("DELETE", "/api/programs/doc?program=O3110")
code, body, _ = call("GET", "/api/programs/doc?program=O3110")
check("deleted PDF is gone", code == 404, str(code))

print("\n=== alarm clips ===")
now = time.time()
al2 = dict(al, id="1-2000-c", key="1-2000", code="SV0401", started_at=now)
call("POST", "/api/ingest", body=snap(sent_at=now, state="alarm", alarms=[al2]))
mp4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 20
code, body, _ = call("PUT", f"/api/camera/clip?alarm=1-2000-c&at={now}&seconds=25&delay=12", raw=mp4)
check("clip uploaded", code == 200, f"{code} {body}")
code, got, hdr = call("GET", "/api/camera/clip?alarm=1-2000-c")
check("clip plays back whole", got == mp4, str(code))
code, part, hdr = call("GET", "/api/camera/clip?alarm=1-2000-c", headers={"Range": "bytes=10-19"})
check("byte range works", code == 206 and part == mp4[10:20] and "bytes 10-19/" in hdr.get("Content-Range", ""),
      f"{code} {hdr.get('Content-Range')}")
code, _, hdr = call("GET", "/api/camera/clip?alarm=1-2000-c", headers={"Range": "bytes=999999-"})
check("silly range refused properly", code == 416, str(code))
_, alarms, _ = call("GET", "/api/alarms?limit=20")
check("alarm says it has a clip", any(a["id"] == "1-2000-c" and a["has_clip"] for a in alarms["alarms"]))

print("\n=== backup battery ===")
_, m, _ = call("POST", "/api/maintenance", body={"item": "battery", "last_at": time.time() - 400 * 86400,
                                                 "every_months": 12})
check("overdue worked out", m.get("state") == "overdue" and m["days_left"] < 0, str(m.get("state")))
_, m, _ = call("POST", "/api/maintenance", body={"item": "battery", "last_at": time.time() - 340 * 86400})
check("due soon worked out", m.get("state") == "soon", str(m.get("state")))
_, m, _ = call("POST", "/api/maintenance", body={"item": "battery", "changed": True})
check("marking it changed resets it", m["state"] == "ok" and m["days_left"] > 360, str(m["days_left"]))
_, st, _ = call("GET", "/api/status")
check("status carries the battery", any(i["item"] == "battery" for i in st.get("maintenance", [])))

print("\n=== keys and logins ===")
code, _, _ = call("GET", "/api/status", key=False)
check("no key = no data", code == 401, str(code))
code, _, _ = call("GET", "/api/status", key=False, headers={"X-API-Key": "wrong"})
check("wrong key = no data", code == 401, str(code))
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
opener = urllib.request.build_opener(NoRedirect)
try:
    opener.open(BASE + "/", timeout=10)
    check("dashboard redirects to the login", False, "no redirect")
except urllib.error.HTTPError as e:
    check("dashboard redirects to the login", e.code in (302, 303, 401), str(e.code))
code, body, _ = call("POST", "/auth/login", body={"username": "admin", "password": "nope"}, key=False)
check("bad password refused", body.get("ok") is False, str(body))

print("\n=== HEAD and odd requests ===")
req = urllib.request.Request(BASE + "/api/status", method="HEAD", headers={"X-API-Key": KEY})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        check("HEAD sends no body", r.read() == b"", "body came back")
except urllib.error.HTTPError as e:
    check("HEAD sends no body", e.read() == b"")
code, _, _ = call("GET", "/api/does-not-exist")
check("unknown route is a 404", code == 404, str(code))
code, _, _ = call("POST", "/api/commands", body={"type": "cycle_start"})
check("unknown command refused", code == 400, str(code))
code, _, _ = call("POST", "/api/commands", body={"type": "set_required", "value": 5})
check("command with no PC gives a clear error", code == 503, str(code))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failed:")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
