"""Offline checks for the passive detectors with synthetic signals (no band, no server):
    .venv/bin/python charles/test_passive.py
Sounds: lock / pill rattle / phone set-down, calibrated and not, plus things that must NOT
count (knock, voice, hum). Motion: set-down, pick-up, hand-to-mouth, a twist that is not an
up/down move, walking (band step counter and accelerometer), and standing up to walk away
(must not count as picking the phone up)."""
import random
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


if __name__ == "__main__":
    test_sounds()
    test_motion()
    test_gestures()
    test_walk()
    print("\n%s" % ("ALL PASSED" if not FAILS else "%d FAILED: %s" % (len(FAILS), ", ".join(FAILS))))
    sys.exit(1 if FAILS else 0)
