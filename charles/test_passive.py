"""Offline checks for the passive detectors with synthetic signals (no band, no server):
    .venv/bin/python charles/test_passive.py
Sounds: lock / pill rattle / phone set-down, calibrated and not, plus things that must NOT
count (knock, voice, hum). Motion: set-down, pick-up, hand-to-mouth, a twist that is not an
up/down move, walking (band step counter and accelerometer), and standing up to walk away
(must not count as picking the phone up)."""
import random

import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock_sound  # noqa: E402
import passive as P  # noqa: E402
import sounds  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print("%s  %s %s" % ("ok  " if ok else "FAIL", name, detail))
    if not ok:
        FAILS.append(name)


def kinds_for(x, templates, seed=7):
    got = []
    d = sounds.SoundDetector({}, templates=templates, on_detect=lambda k, s: got.append(k))
    d.feed_float(sounds.with_background(x, seed=seed))
    return got


def calibrate(action):
    out = []
    for i in range(3):
        d = sounds.SoundDetector({}, templates={})
        d.feed_float(sounds.with_background(sounds.synth(action, seed=i), seed=20 + i))
        out.append(sounds.template_of(action, d.onsets[0][1]))
    return out


def test_sounds():
    lock, rattle, setdown = lock_sound.synth_lock(seed=9), sounds.synth_rattle(seed=9), sounds.synth_setdown(seed=9)
    nots = {"knock": lock_sound.synth_other("knock"), "voice": sounds.synth_speech(), "hum": lock_sound.synth_other("hum")}
    # not calibrated: rough rules
    check("uncalibrated rattle -> pill_rattle", kinds_for(rattle, {}) == ["pill_rattle"], kinds_for(rattle, {}))
    check("uncalibrated lock -> click", "click" in kinds_for(lock, {}))
    check("uncalibrated set-down -> impact", "impact" in kinds_for(setdown, {}))
    check("uncalibrated rattle is not a click", "click" not in kinds_for(rattle, {}))
    for n, x in nots.items():
        k = kinds_for(x, {})
        check("uncalibrated %s -> no click / rattle" % n, not ({"click", "pill_rattle"} & set(k)), k)
    # calibrated
    tpl = {a: calibrate(a) for a in sounds.ACTIONS}
    check("calibrated lock -> lock_sound", kinds_for(lock, tpl) == ["lock_sound"], kinds_for(lock, tpl))
    check("calibrated rattle -> pill_rattle", kinds_for(rattle, tpl) == ["pill_rattle"], kinds_for(rattle, tpl))
    check("calibrated set-down -> setdown_clack", kinds_for(setdown, tpl) == ["setdown_clack"], kinds_for(setdown, tpl))
    for n, x in nots.items():
        check("calibrated %s -> nothing" % n, kinds_for(x, tpl) == [], kinds_for(x, tpl))


def play(segs, rate=50, noise=0.01, gnoise=2.0, seed=1):
    rnd = random.Random(seed)
    t, out = 0.0, []
    for src, secs in segs:
        for i in range(int(secs * rate)):
            v = dict(P.REST_POSE, **(src(i / rate) if callable(src) else src))
            out.append((t, v["ax"] + rnd.gauss(0, noise), v["ay"] + rnd.gauss(0, noise), v["az"] + rnd.gauss(0, noise),
                        v["gx"] + rnd.gauss(0, gnoise), v["gy"] + rnd.gauss(0, gnoise), v["gz"] + rnd.gauss(0, gnoise)))
            t += 1.0 / rate
    return out


def moves(segs):
    tr = P.MotionTracker()
    for s in play(segs):
        tr.feed(*s)
    return [(m["kind"], m["dz"], m["tilt"]) for m in tr.moves]


def test_motion():
    rest = P.REST_POSE
    m = moves([(rest, 1), (P.vertical_motion(-0.3, 0.6), 0.6), (rest, 1)])
    check("set-down = lower ~0.3 m", len(m) == 1 and m[0][0] == "lower" and abs(m[0][1] + 0.3) < 0.08, m)
    m = moves([(rest, 1), (P.vertical_motion(-0.15, 0.9), 0.9), (rest, 1)])
    check("slow small set-down = lower ~0.15 m", len(m) == 1 and m[0][0] == "lower" and abs(m[0][1] + 0.15) < 0.06, m)
    m = moves([(rest, 1), (P.vertical_motion(0.3, 0.6), 0.6), (rest, 1)])
    check("pick-up = raise ~0.3 m", len(m) == 1 and m[0][0] == "raise", m)
    m = moves([(rest, 1), (P.vertical_motion(0.35, 0.8, 70, "x"), 0.8), (P.hold_pose(70, "x"), 1)])
    check("hand to mouth = raise + 70 deg tilt", len(m) == 1 and m[0][0] == "raise" and m[0][2] > 50, m)
    m = moves([(rest, 1), (P.twist_motion(120, 0.5, "y"), 0.5), (rest, 1)])
    check("gyro-only twist is not an up/down move", all(k == "other" for k, _, _ in m), m)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def run_gestures(segs, walk_after=None):
    """Feed motion; optionally start walking (band step counter) walk_after seconds in."""
    clk = Clock()
    got = []
    walk = P.WalkDetector({}, clock=clk)
    g = P.Gestures({}, on_gesture=lambda k, d: got.append(k), walk=walk, clock=clk)
    tr = P.MotionTracker(on_move=g.move)
    steps = 0
    start = clk.t
    for s in play(segs):
        clk.t = start + s[0]
        tr.feed(*s)
        if walk_after is not None and s[0] >= walk_after and int(s[0] * 50) % 50 == 0:
            steps += 2
            walk.feed_activity(steps, "walk")
        g.tick()
    for _ in range(100):                      # 5 more seconds standing still
        clk.t += 0.05
        g.tick()
    return got


def test_gestures():
    rest = P.REST_POSE
    down = [(rest, 1), (P.vertical_motion(-0.3, 0.6), 0.6), (rest, 2)]
    up = [(P.vertical_motion(0.3, 0.6), 0.6), (rest, 1)]
    got = run_gestures(down + up)
    check("set down then lift = set_down + pickup", got == ["set_down", "pickup"], got)
    got = run_gestures(down + up + [(rest, 3)], walk_after=4.0)
    check("set down, stand up and walk = NO pickup", got == ["set_down"], got)
    got = run_gestures([(rest, 1), (P.vertical_motion(0.35, 0.8, 60, "x"), 0.8), (P.hold_pose(60, "x"), 1)])
    check("hand to mouth = to_mouth", "to_mouth" in got, got)


def test_walk():
    clk = Clock()
    fired = []
    w = P.WalkDetector({}, on_walk=lambda n, how: fired.append((round(clk.t - 1000, 1), n, how)), clock=clk)
    steps = 500
    for i in range(10):                       # band activity once a second at ~110 steps/min
        clk.t += 1
        steps += 2
        w.feed_activity(steps, "walk")
    check("band step counter -> walking within ~5 s", fired and fired[0][0] <= 5, fired)
    clk2 = Clock()
    fired2 = []
    w2 = P.WalkDetector({}, on_walk=lambda n, how: fired2.append(round(clk2.t - 1000, 1)), clock=clk2)
    f, secs = P.walk_motion(14)
    for s in play([(P.REST_POSE, 1), (f, secs)], noise=0.0, gnoise=0.0):
        clk2.t = 1000 + s[0]
        w2.feed_accel(s[0], s[1], s[2], s[3])
    check("accelerometer steps -> walking within ~6 s", fired2 and fired2[0] <= 6.5, fired2)
    clk3 = Clock()
    fired3 = []
    w3 = P.WalkDetector({}, on_walk=lambda n, how: fired3.append(n), clock=clk3)
    for s in play([(P.REST_POSE, 1), (P.vertical_motion(0.3, 0.6), 0.6), (P.REST_POSE, 1),
                   (P.vertical_motion(-0.3, 0.6), 0.6), (P.REST_POSE, 2)]):
        clk3.t = 1000 + s[0]
        w3.feed_accel(s[0], s[1], s[2], s[3])
    check("arm moves at the table are not walking", not fired3, fired3)


def bathroom_kinds(x, templates=None, seed=3):
    got = []
    d = sounds.SoundDetector({}, templates=templates if templates is not None else {}, on_detect=lambda k, s: got.append(k))
    d.feed_float(sounds.with_background(x, seed=seed))
    return got


def test_bathroom_sounds():
    flush, paper = sounds.synth_flush(seed=4), sounds.synth_paper(seed=4)
    check("flush-like noise burst -> toilet_flush", "toilet_flush" in bathroom_kinds(flush), bathroom_kinds(flush))
    check("flush is not toilet paper", "toilet_paper" not in bathroom_kinds(flush))
    check("paper rustle -> toilet_paper", "toilet_paper" in bathroom_kinds(paper), bathroom_kinds(paper))
    check("paper rustle is not a flush", "toilet_flush" not in bathroom_kinds(paper))
    nots = {"voice (3 s)": sounds.synth_speech(seconds=3.0), "short click": lock_sound.synth_lock(seed=3),
            "knock": lock_sound.synth_other("knock"), "pill rattle": sounds.synth_rattle(seed=3),
            "faucet running 20 s": sounds.synth_flush(seed=5, whoosh=20.0, refill=0.0)}
    for n, x in nots.items():
        k = bathroom_kinds(x)
        check("%s -> no flush / paper" % n, not ({"toilet_flush", "toilet_paper"} & set(k)), k)
    two = np.concatenate([flush, np.zeros(sounds.RATE * 2), flush])
    check("two flushes within 60 s count once", bathroom_kinds(two).count("toilet_flush") == 1, bathroom_kinds(two))
    tpl = {"flush": [], "paper": []}
    for a, f in (("flush", sounds.synth_flush), ("paper", sounds.synth_paper)):
        for i in range(3):
            d = sounds.SoundDetector({}, templates={})
            got = []
            d.noise.on_event = lambda desc: got.append(desc) or True
            d.feed_float(sounds.with_background(f(seed=10 + i), seed=30 + i))
            tpl[a].append(sounds.template_of(a, got[0]))
    check("calibrated flush -> toilet_flush", "toilet_flush" in bathroom_kinds(flush, tpl), bathroom_kinds(flush, tpl))
    check("calibrated paper -> toilet_paper", "toilet_paper" in bathroom_kinds(paper, tpl), bathroom_kinds(paper, tpl))
    check("calibrated: voice -> nothing", not bathroom_kinds(sounds.synth_speech(seconds=3.0), tpl))


def test_bathroom_rules():
    """Server rules on a throwaway in-memory database, pushes printed (dry run)."""
    import os
    import sqlite3
    os.environ["NTFY_DRY_RUN"] = "1"
    real = sqlite3.connect
    sqlite3.connect = lambda *a, **k: real(":memory:", check_same_thread=False)
    try:
        from memory import notify, rules, service, store
    finally:
        sqlite3.connect = real
    if not str(store._db.execute("PRAGMA database_list").fetchone()[2]) == "":
        check("bathroom rules use a throwaway database", False)
        return
    from datetime import datetime
    t0 = datetime.now().replace(hour=16, minute=0, second=0, microsecond=0).timestamp()
    b = rules.CONFIG["bathroom"]
    wait, grace = b["paper_max_wait_seconds"], b["flush_wait_seconds"]
    service.handle_signal("toilet_paper", {"value": 0.9}, now=t0)
    rules.run_rules(t0 + wait + grace - 2)
    check("no push before the grace time", not store.alerts(open_only=True))
    rules.run_rules(t0 + wait + grace + 1)
    op = [a for a in store.alerts(open_only=True) if a["rule"] == "flush"]
    check("toilet paper, no flush -> push to the wearer", len(op) == 1 and op[0]["audience"] == "user", op)
    rules.run_rules(t0 + wait + grace + 30)
    check("only one forgot-flush push", len([a for a in store.alerts() if a["rule"] == "flush"]) == 1)
    service.handle_signal("toilet_flush", {"value": 0.9}, now=t0 + wait + grace + 40)
    a = [a for a in store.alerts() if a["rule"] == "flush"][0]
    check("flush resolves it with a follow-up", a["resolved"] and "flushed" in (a.get("follow_up") or "").lower(), a.get("follow_up"))
    check("flush counted today", service._bathroom_state(t0 + 100)["flushes_today"] == 1)
    t1 = t0 + 600
    service.handle_signal("toilet_paper", {}, now=t1)
    service.handle_signal("toilet_flush", {}, now=t1 + 20)
    rules.run_rules(t1 + wait + grace + 60)
    check("paper then flush -> no push", len([a for a in store.alerts() if a["rule"] == "flush"]) == 1)
    noon = datetime.fromtimestamp(t0).replace(hour=12, minute=5).timestamp()
    store._db.execute("DELETE FROM events")
    rules.run_rules(noon)
    fam = [a for a in store.alerts(open_only=True) if a["rule"] == "bathroom"]
    check("no flush by noon -> one family note", len(fam) == 1 and fam[0]["audience"] == "family", fam)
    n = len(notify.sent)
    service.handle_signal("toilet_flush", {}, now=noon + 60)
    check("a later flush resolves the family note (family only)", not [a for a in store.alerts(open_only=True) if a["rule"] == "bathroom"]
          and all(m["topic"] != rules.notify.TOPICS["user"] for m in notify.sent[n:]), notify.sent[n:])


def test_hydration():
    """Drinks from the band's hand-to-mouth, and the water reminder (in-memory, dry run)."""
    from memory import rules, service, store         # already on the throwaway database (test_bathroom_rules)
    from datetime import datetime
    store._db.execute("DELETE FROM events")
    store._db.execute("DELETE FROM alerts")
    store.kv_set("hydration_demo_seconds", 0)
    day = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    t0 = day.timestamp()
    store.kv_set("hydration_monitor", {"day": day.strftime("%Y-%m-%d"), "since": t0})
    sip = {"dz": 0.3, "tilt": 60.0, "seconds": 0.8}
    r = service.handle_gesture("to_mouth", sip, now=t0 + 10)
    check("hand to mouth (sip) -> drink", r == "drink_noticed", r)
    r = service.handle_gesture("to_mouth", sip, now=t0 + 60)
    check("second sip within 3 min counts once", r == "drink_already_counted" and len(rules.drinks_today(t0 + 61)) == 1, r)
    service.handle_signal("pill_rattle", {"value": 0.9}, now=t0 + 400)
    r = service.handle_gesture("to_mouth", sip, now=t0 + 410)
    check("hand to mouth right after a pill rattle -> pill, not a drink", r != "drink_noticed" and len(rules.drinks_today(t0 + 411)) == 1, r)
    r = service.handle_gesture("to_mouth", dict(sip, seconds=9.0), now=t0 + 1000)
    check("a 9 s slow arm raise is not a sip", r != "drink_noticed", r)
    every = rules.hydration_interval()
    rules.run_rules(t0 + 10 + every - 5)
    check("no water reminder before the interval", not [a for a in store.alerts(open_only=True) if a["rule"] == "water"])
    rules.run_rules(t0 + 10 + every + 5)
    w = [a for a in store.alerts(open_only=True) if a["rule"] == "water"]
    check("no drink for the interval -> water reminder to the wearer", len(w) == 1 and w[0]["audience"] == "user", w)
    late = datetime.now().replace(hour=22, minute=30).timestamp()
    check("no reminder at night", rules.check_hydration(late) is None and len([a for a in store.alerts() if a["rule"] == "water"]) == 1)
    service.handle_gesture("to_mouth", sip, now=t0 + 10 + every + 60)
    a = [a for a in store.alerts() if a["rule"] == "water"][0]
    check("a drink resolves it with a follow-up", a["resolved"] and "drink noticed" in (a.get("follow_up") or "").lower(), a.get("follow_up"))
    store.kv_set("hydration_demo_seconds", 30)
    t2 = t0 + 10 + every + 60
    rules.run_rules(t2 + 25)
    check("demo interval 30 s: nothing at 25 s", not [a for a in store.alerts(open_only=True) if a["rule"] == "water"])
    rules.run_rules(t2 + 35)
    check("demo interval 30 s: reminder at 35 s", len([a for a in store.alerts(open_only=True) if a["rule"] == "water"]) == 1)
    service.handle_tap("double-tap", now=t2 + 40)
    check("double-tap = I drank", not [a for a in store.alerts(open_only=True) if a["rule"] == "water"])
    store.kv_set("hydration_demo_seconds", 0)


if __name__ == "__main__":
    test_sounds()
    test_motion()
    test_gestures()
    test_walk()
    test_bathroom_sounds()
    test_bathroom_rules()
    test_hydration()
    print("\n%s" % ("ALL PASSED" if not FAILS else "%d FAILED: %s" % (len(FAILS), ", ".join(FAILS))))
    sys.exit(1 if FAILS else 0)
