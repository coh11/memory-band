"""Play a scripted day (demo_day.txt) into the Memory Band server: no band needed.

    .venv/bin/python charles/replay.py                     # whole day, 1.5 s between lines
    .venv/bin/python charles/replay.py --pause 0           # as fast as possible (tests)
    .venv/bin/python charles/replay.py --pause 4 --keep    # slower, keep earlier data
    .venv/bin/python charles/replay.py --real-clock        # don't move the demo clock

Each line moves the server's demo clock to its time (POST /api/clock), then sends the words
(POST /api/transcript), a double-tap (POST /api/tap) or a fall (POST /api/fall). At the end it
prints every alert and resets the clock."""
import argparse
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent

# What band_listener.py and the phone send for each passive action.
PASSIVE = {
    "@leave": ("/leaving", {}),
    "@arrive": ("/arrived", {}),
    "@walk-away": ("/presence", {"state": "away", "reason": "band: weak signal (-86 dBm) for 20s", "rssi": -86}),
    "@back": ("/presence", {"state": "home", "reason": "band: signal strong again", "rssi": -55}),
    "@lock-sound": ("/signal", {"kind": "lock_sound", "value": 0.91, "source": "replay"}),
    "@click": ("/signal", {"kind": "click", "value": 0.7, "source": "replay"}),
    "@twist": ("/gesture", {"kind": "lock_twist", "angle": 118, "axis": "x", "seconds": 0.5, "mic_on": True, "source": "replay"}),
    "@lock": None,  # filled below: key-turn twist + click
    "@shake": ("/gesture", {"kind": "pill_shake", "source": "replay"}),
    # pill taken, passively: rattle + cap twist / hand to mouth
    "@rattle": ("/signal", {"kind": "pill_rattle", "value": 0.9, "source": "replay"}),
    "@cap-twist": ("/gesture", {"kind": "cap_twist", "angle": 150, "axis": "x", "seconds": 0.6, "source": "replay"}),
    "@to-mouth": ("/gesture", {"kind": "to_mouth", "dz": 0.35, "tilt": 60, "seconds": 0.8, "source": "replay"}),
    # phone set down / picked up / walking away
    "@lower": ("/gesture", {"kind": "set_down", "dz": -0.3, "tilt": 1, "seconds": 0.6, "source": "replay"}),
    "@clack": ("/signal", {"kind": "setdown_clack", "value": 0.9, "source": "replay"}),
    "@pickup": ("/gesture", {"kind": "pickup", "dz": 0.3, "tilt": 1, "seconds": 0.6, "source": "replay"}),
    "@walk": ("/gesture", {"kind": "walking", "steps": 10, "how": "band step counter", "source": "replay"}),
}
# Combined actions: several of the above in a row
COMBO = {"@pill": ["@cap-twist", "@rattle", "@to-mouth"], "@set-down": ["@lower", "@clack"], "@lock": ["@twist", "@click"]}
del PASSIVE["@lock"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("script", nargs="?", default=str(HERE / "demo_day.txt"))
    p.add_argument("--server", default="http://127.0.0.1:8001")
    p.add_argument("--token", default=os.environ.get("MEMORY_TOKEN", ""), help="MEMORY_TOKEN, if the server uses one")
    p.add_argument("--pause", type=float, default=1.5, help="real seconds between lines")
    p.add_argument("--keep", action="store_true", help="don't wipe earlier data first")
    p.add_argument("--real-clock", action="store_true", help="don't move the server's clock")
    p.add_argument("--leave-clock", action="store_true", help="don't reset the clock at the end")
    a = p.parse_args()
    api = a.server.rstrip("/") + "/api"
    s = requests.Session()
    try:
        s.get(api + "/state", timeout=5).raise_for_status()
    except requests.RequestException:
        sys.exit("The server is not running at %s. Start it first: .venv/bin/uvicorn charles.main:app --port 8001" % a.server)
    if not a.keep:
        s.post(api + "/reset", timeout=5)

    for raw in Path(a.script).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        at, _, what = (x.strip() for x in line.partition("|"))
        if not a.real_clock and at:
            s.post(api + "/clock", json={"time": at}, timeout=10)
        if what == "@double-tap":
            r = s.post(api + "/tap", json={"kind": "double-tap", "source": "replay"}, timeout=10).json()
            print("%s  [double-tap] %s" % (at, r.get("reply")))
        elif what == "@fall":
            s.post(api + "/fall", json={"source": "replay"}, timeout=10)
            print("%s  [fall detected]" % at)
        elif what in PASSIVE or what in COMBO:
            for step in COMBO.get(what, [what]):
                path, body = PASSIVE[step]
                if path in ("/leaving", "/arrived") and a.token:
                    path += "?token=" + a.token
                r = s.post(api + path, json=body, timeout=10).json()
                print("%s  [%s] %s" % (at, step[1:], r.get("result") or r.get("state") or ""))
        elif what == "@tick":
            print("%s  ..." % at)
        else:
            r = s.post(api + "/transcript", json={"text": what, "source": "replay"}, timeout=30).json()
            kinds = ", ".join(t["kind"] for t in r.get("tags", [])) or "-"
            print("%s  \"%s\"  -> %s" % (at, what, kinds))
            for reply in r.get("replies", []):
                print("        answer: %s" % reply)
        if a.pause:
            time.sleep(a.pause)

    print("\nAlerts sent:")
    for al in reversed(s.get(api + "/alerts", timeout=10).json()):
        print("  [%s -> %s] %s: %s%s%s" % (al["level"], al["audience"], al["title"], al["message"],
                                           "  (escalated to family)" if al["escalated"] else "",
                                           "\n      RESOLVED (%s): %s" % (al["resolved_how"], al["follow_up"]) if al.get("follow_up") else ""))
    if not a.real_clock and not a.leave_clock:
        s.post(api + "/clock", json={"reset": True}, timeout=10)


if __name__ == "__main__":
    main()
