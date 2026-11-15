"""Passive signals from the band, no speaking needed. Used by band_bridge.py.

TwistDetector  - a key / deadbolt turn: the wrist rotates ~70-180 degrees about one axis
                 (the forearm) in about a second, then stops. Calibrate with your own door:
                 band_bridge.py N --calibrate-twist (3 twists -> data/lock_twist.json).
ShakeDetector  - shaking a pill bottle (optional): fast up-and-down acceleration, 3+ times.
Presence       - home / away from the band's Bluetooth signal to the home Mac, like the kit's
                 band-leash project: weak signal or lost link for `grace_seconds` = left home.
DoorSound      - optional cue: a sudden loud sound (door closing) from the mic level.

All numbers are starting guesses: tune them with a real band (the kit says the same about
its own projects)."""
import json
import math
import time
from collections import deque
from pathlib import Path

DATA = Path(__file__).resolve().parent / "memory" / "data"
TEMPLATE_FILE = DATA / "lock_twist.json"
AXES = ("x", "y", "z")


def load_config():
    try:
        with open(Path(__file__).resolve().parent / "memory" / "config.json", encoding="utf-8") as f:
            return json.load(f).get("passive", {})
    except (OSError, ValueError):
        return {}


class TwistDetector:
    """Feed gyro samples (deg/s). A 'segment' starts when any axis turns faster than
    start_dps and ends after the wrist is still (below stop_dps) for stop_seconds. Each
    segment's rotation is the integral of the gyro on each axis. It is a lock twist when one
    axis carries most of the rotation, the angle is in range and it was quick."""

    def __init__(self, cfg=None, template=None, on_twist=None):
        c = dict(start_dps=60.0, stop_dps=30.0, stop_seconds=0.15, min_angle=70.0, max_angle=200.0,
                 max_seconds=1.2, dominance=0.75, cooldown_seconds=3.0)
        c.update(cfg or {})
        self.c = c
        self.template = template
        self.on_twist = on_twist
        self.seg = None
        self.last_fire = -1e9
        self.last_t = None
        self.segments = []            # finished segments (for calibration)
        self.on_segment = None        # called with every finished segment; returning True = don't match it

    def feed(self, t, gx, gy, gz):
        """t in seconds (band time), gyro in degrees/second."""
        if gx is None or gy is None or gz is None:
            return None
        dt = 0.02 if self.last_t is None else max(0.0, min(0.1, t - self.last_t))
        self.last_t = t
        g = (gx, gy, gz)
        fast = max(abs(v) for v in g)
        if self.seg is None:
            if fast >= self.c["start_dps"]:
                self.seg = {"start": t, "angle": [0.0, 0.0, 0.0], "still_since": None, "peak": fast}
            else:
                return None
        s = self.seg
        for i in range(3):
            s["angle"][i] += g[i] * dt
        s["peak"] = max(s["peak"], fast)
        if fast < self.c["stop_dps"]:
            s["still_since"] = s["still_since"] if s["still_since"] is not None else t
            if t - s["still_since"] >= self.c["stop_seconds"]:
                self.seg = None
                return self._finish(s, s["still_since"] - s["start"])
        else:
            s["still_since"] = None
        if t - s["start"] > 3.0:          # a long, slow movement: not a twist
            self.seg = None
        return None

    def _finish(self, s, duration):
        a = s["angle"]
        axis = max(range(3), key=lambda i: abs(a[i]))
        angle = abs(a[axis])
        norm = math.sqrt(sum(v * v for v in a)) or 1.0
        seg = {"axis": AXES[axis], "angle": round(angle, 1), "seconds": round(duration, 2),
               "dominance": round(angle / norm, 2), "sign": 1 if a[axis] > 0 else -1}
        self.segments.append(seg)
        del self.segments[:-50]
        if self.on_segment and self.on_segment(seg):
            return None
        if self.matches(seg) and s["start"] - self.last_fire >= self.c["cooldown_seconds"]:
            self.last_fire = s["start"]
            if self.on_twist:
                self.on_twist(seg)
            return seg
        return None

    def matches(self, seg):
        c, tpl = self.c, self.template
        if seg["dominance"] < c["dominance"] or seg["seconds"] > (tpl or {}).get("max_seconds", c["max_seconds"]):
            return False
        if tpl:
            return (seg["axis"] == tpl["axis"]
                    and tpl["angle"] * 0.6 <= seg["angle"] <= tpl["angle"] * 1.6)
        return c["min_angle"] <= seg["angle"] <= c["max_angle"]


def make_template(segs):
    """From 3 recorded twists: the common axis, the average angle and a time limit."""
    axis = max(AXES, key=lambda ax: sum(1 for s in segs if s["axis"] == ax))
    same = [s for s in segs if s["axis"] == axis]
    return {"axis": axis, "angle": round(sum(s["angle"] for s in same) / len(same), 1),
            "max_seconds": round(max(0.5, 1.5 * max(s["seconds"] for s in same)), 2),
            "samples": segs, "made": time.strftime("%Y-%m-%d %H:%M")}


def load_template():
    try:
        return json.loads(TEMPLATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def save_template(tpl):
    DATA.mkdir(parents=True, exist_ok=True)
    TEMPLATE_FILE.write_text(json.dumps(tpl, indent=2))


class ShakeDetector:
    """Shaking a pill bottle: |a| swings by more than `range_g` within half a second,
    `count` times within `window_seconds`. Needs band.set_motion(accel_range=8)."""

    def __init__(self, cfg=None, on_shake=None):
        c = dict(range_g=1.2, count=3, window_seconds=2.0, cooldown_seconds=10.0)
        c.update(cfg or {})
        self.c, self.on_shake = c, on_shake
        self.recent = deque(maxlen=25)
        self.hits = deque()
        self.quiet_until = -1e9
        self.last_fire = -1e9

    def feed(self, t, ax, ay, az):
        if ax is None:
            return False
        self.recent.append(math.sqrt(ax * ax + ay * ay + az * az))
        if t > self.quiet_until and max(self.recent) - min(self.recent) > self.c["range_g"]:
            self.quiet_until = t + 0.25
            self.hits.append(t)
        while self.hits and t - self.hits[0] > self.c["window_seconds"]:
            self.hits.popleft()
        if len(self.hits) >= self.c["count"] and t - self.last_fire > self.c["cooldown_seconds"]:
            self.last_fire = t
            self.hits.clear()
            if self.on_shake:
                self.on_shake()
            return True
        return False


class Presence:
    """home / away from the link to the home Mac. Weak (smoothed rssi below far_dbm) or lost
    for grace_seconds -> away. Strong again (above near_dbm) or link back -> home."""

    def __init__(self, cfg=None, on_change=None, clock=time.time):
        c = dict(far_dbm=-80.0, near_dbm=-65.0, grace_seconds=20.0, smooth_readings=5)
        c.update(cfg or {})
        self.c, self.on_change, self.clock = c, on_change, clock
        self.state = "home"
        self.readings = deque(maxlen=int(c["smooth_readings"]))
        self.weak_since = None
        self.lost_since = None
        self.signal = None

    def rssi(self, dbm):
        if dbm is None:
            return
        self.readings.append(dbm)
        self.signal = sum(self.readings) / len(self.readings)
        now = self.clock()
        if self.signal < self.c["far_dbm"]:
            self.weak_since = self.weak_since or now
        else:
            self.weak_since = None
        if self.state == "away" and self.signal >= self.c["near_dbm"]:
            self._set("home", "signal strong again (%d dBm)" % self.signal)
        self.tick()

    def link_lost(self):
        self.lost_since = self.lost_since or self.clock()

    def link_back(self):
        self.lost_since = None
        self.readings.clear()
        self.weak_since = None
        if self.state == "away":
            self._set("home", "band connected again")

    def tick(self):
        now = self.clock()
        if self.state == "home":
            if self.lost_since and now - self.lost_since >= self.c["grace_seconds"]:
                self._set("away", "lost the band's signal for %ds" % (now - self.lost_since))
            elif self.weak_since and now - self.weak_since >= self.c["grace_seconds"]:
                self._set("away", "weak signal (%d dBm) for %ds" % (self.signal, now - self.weak_since))

    def _set(self, state, reason):
        self.state = state
        if self.on_change:
            self.on_change(state, reason, None if self.signal is None else round(self.signal))


class DoorSound:
    """A sudden loud sound after quiet: rms_db (0 = loudest, 96 = silence) drops below
    loud_db right after being above quiet_db. Only a hint; never an alert by itself."""

    def __init__(self, cfg=None, on_sound=None):
        c = dict(quiet_db=40.0, loud_db=15.0, cooldown_seconds=10.0)
        c.update(cfg or {})
        self.c, self.on_sound = c, on_sound
        self.prev = None
        self.last = -1e9

    def feed(self, t, rms_db):
        if rms_db is None:
            return False
        fired = False
        if self.prev is not None and self.prev >= self.c["quiet_db"] and rms_db <= self.c["loud_db"] \
                and t - self.last > self.c["cooldown_seconds"]:
            self.last = t
            fired = True
            if self.on_sound:
                self.on_sound(rms_db)
        self.prev = rms_db
        return fired


class OffWrist:
    """Is the band on a wrist? A worn band always shakes a little (pulse, tremor, breathing):
    gyro and accelerometer wobble above the sensor's noise floor. On a table or the charger it
    is dead still. Every window_seconds of motion samples: dead still if the gyro's spread is
    under still_gyro_dps and the accelerometer's under still_g. Dead still for still_minutes ->
    on_change("not_worn"); the first window that isn't -> on_change("worn").
    Sound never counts as worn (a TV talks to a band on the table). No motion at all (band out
    of reach) reports nothing: that's presence."""

    def __init__(self, cfg=None, on_change=None):
        c = dict(still_minutes=20, window_seconds=5, still_gyro_dps=0.8, still_g=0.005)
        c.update(cfg or {})
        self.c = c
        self.still_seconds = float(c.get("still_seconds") or c["still_minutes"] * 60)
        self.on_change = on_change
        self.win = []
        self.win_start = None
        self.still_since = None
        self.state = None                         # None (not known yet), "worn", "not_worn"
        self.last = {}

    def feed(self, t, ax, ay, az, gx, gy, gz):
        if ax is None or gx is None:
            return
        if self.win_start is None:
            self.win_start = t
        self.win.append((ax, ay, az, gx, gy, gz))
        if t - self.win_start < self.c["window_seconds"]:
            return
        cols = list(zip(*self.win))

        def spread(v):
            m = sum(v) / len(v)
            return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5
        acc = max(spread(cols[i]) for i in range(3))
        gyr = max(spread(cols[i]) for i in range(3, 6))
        self.last = {"acc_spread": round(acc, 4), "gyro_spread": round(gyr, 2)}
        still = acc < self.c["still_g"] and gyr < self.c["still_gyro_dps"]
        self.win, self.win_start = [], t
        if still:
            if self.still_since is None:
                self.still_since = t - self.c["window_seconds"]
            if t - self.still_since >= self.still_seconds and self.state != "not_worn":
                self._set("not_worn", t - self.still_since)
        else:
            self.still_since = None
            if self.state != "worn":
                self._set("worn", 0)

    def _set(self, state, still_for):
        first = self.state is None
        self.state = state
        if self.on_change and not (first and state == "worn"):     # "worn" at start-up isn't news
            self.on_change(state, still_for)

    @property
    def still_for(self):
        return 0.0 if self.still_since is None or self.win_start is None else self.win_start - self.still_since


def twist_motion(angle=120.0, seconds=0.5, axis="x"):
    """For the pretend band: a function for band.transport.play_motion that turns the wrist
    by `angle` degrees about `axis` in `seconds` (a half-sine speed profile)."""
    peak = angle * math.pi / (2 * seconds)            # integral of peak*sin(pi*s/T) over T = angle
    key = "g" + axis

    def f(s):
        return {key: peak * math.sin(math.pi * min(s, seconds) / seconds)}
    return f


# ---- wrist movements: up / down, resting, walking ------------------------------------
def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n > 0 else [0.0, 0.0, 1.0]


def angle_between(u, v):
    d = sum(a * b for a, b in zip(_norm(u), _norm(v)))
    return math.degrees(math.acos(max(-1.0, min(1.0, d))))


class MotionTracker:
    """Splits the wrist's motion into "moves" between moments of rest, and for each move works
    out how far the wrist went up or down (dz, metres) and how much the forearm tilted.

    At rest the accelerometer reads gravity, which tells which way is up in the band's own
    axes. During a move the gyro keeps that "up" direction turned along with the wrist, the
    acceleration along "up" is integrated twice, and the speed is forced back to zero when the
    wrist rests again (a zero-velocity update), which removes most of the drift for short moves.

        kind "lower": the hand went down and came to rest   -> set something down
        kind "raise": the hand went up and stopped            -> pick up / hand to mouth
    on_move(move) gets a dict: kind, dz, tilt, seconds, start_pose, end_pose, t."""

    def __init__(self, cfg=None, on_move=None):
        c = dict(still_dps=25.0, still_g=0.06, still_seconds=0.25, move_dps=35.0, move_g=0.10,
                 min_seconds=0.2, max_seconds=4.0, min_dz=0.05, max_dz=1.0, max_pose_error_deg=25.0)
        c.update(cfg or {})
        self.c, self.on_move = c, on_move
        self.recent = deque()               # (t, a, gyro_mag, acc_dev) for the still check
        self.pose = None                    # gravity direction at the last rest
        self.g0 = 1.0                       # gravity magnitude the accelerometer reads (scale error)
        self.still = False
        self.seg = None
        self.last_t = None
        self.moves = []                     # finished moves (for --debug-sensors and tests)
        self.gyro_mag = 0.0
        self.acc_dev = 0.0

    def feed(self, t, ax, ay, az, gx, gy, gz):
        if ax is None or gx is None:
            return None
        dt = 0.02 if self.last_t is None else max(0.0, min(0.1, t - self.last_t))
        self.last_t = t
        a = (ax, ay, az)
        mag = math.sqrt(ax * ax + ay * ay + az * az)
        self.gyro_mag = math.sqrt(gx * gx + gy * gy + gz * gz)
        self.acc_dev = abs(mag - self.g0)
        self.recent.append((t, a, self.gyro_mag, self.acc_dev))
        while self.recent and t - self.recent[0][0] > self.c["still_seconds"]:
            self.recent.popleft()
        span = t - self.recent[0][0]
        quiet = all(g < self.c["still_dps"] and d < self.c["still_g"] for _, _, g, d in self.recent)
        now_still = quiet and span >= self.c["still_seconds"] * 0.8
        out = None
        if self.seg is not None:
            s = self.seg
            w = [math.radians(gx), math.radians(gy), math.radians(gz)]
            u = s["up"]                                         # d(up)/dt = -w x up
            cr = (w[1] * u[2] - w[2] * u[1], w[2] * u[0] - w[0] * u[2], w[0] * u[1] - w[1] * u[0])
            s["up"] = _norm([u[i] - cr[i] * dt for i in range(3)])
            av = sum(a[i] * s["up"][i] for i in range(3)) - self.g0
            s["acc"].append((t, av * 9.81, dt))
            if now_still:
                self.seg = None
                out = self._finish(s, t)
            elif t - s["start"] > self.c["max_seconds"]:
                self.seg = None                                 # walking or a long fidget: not one move
        elif now_still:
            pose = [sum(r[1][i] for r in self.recent) / len(self.recent) for i in range(3)]
            self.pose = pose
            self.g0 = 0.9 * self.g0 + 0.1 * math.sqrt(sum(p * p for p in pose)) if self.still else math.sqrt(sum(p * p for p in pose))
        elif self.pose is not None and (self.gyro_mag >= self.c["move_dps"] or self.acc_dev >= self.c["move_g"]):
            # back-date the start to the last still moment so the first push is integrated too
            up = _norm(self.pose)
            pre, prev = [], None
            for tt, aa, _, _ in self.recent:                    # the slow first part of the move, too
                pre.append((tt, (sum(aa[i] * up[i] for i in range(3)) - self.g0) * 9.81,
                            0.02 if prev is None else tt - prev))
                prev = tt
            start = self.recent[0][0] if self.recent else t
            self.seg = {"start": start, "up": up, "start_pose": list(self.pose), "acc": pre}
        self.still = now_still
        return out

    def _finish(self, s, t):
        acc = s["acc"]
        v, vs = 0.0, []
        for _, a, dt in acc:
            v += a * dt
            vs.append(v)
        total = sum(dt for _, _, dt in acc) or 1e-6
        dz, el = 0.0, 0.0
        for (tt, a, dt), vv in zip(acc, vs):
            el += dt
            dz += (vv - vs[-1] * el / total) * dt                # speed forced to 0 at the end
        end_pose = [sum(r[1][i] for r in self.recent) / len(self.recent) for i in range(3)]
        self.pose = end_pose
        seconds = t - s["start"]
        kind = "other"
        # the gyro-tracked "up" must agree with gravity at the end, or the sums can't be trusted
        drift = angle_between(s["up"], end_pose)
        if (seconds >= self.c["min_seconds"] and self.c["min_dz"] <= abs(dz) <= self.c["max_dz"]
                and drift <= self.c["max_pose_error_deg"]):
            kind = "raise" if dz > 0 else "lower"
        move = {"kind": kind, "dz": round(dz, 3), "tilt": round(angle_between(s["start_pose"], end_pose), 1),
                "seconds": round(seconds, 2), "drift": round(drift, 1), "start_pose": s["start_pose"], "end_pose": end_pose, "t": t}
        self.moves.append(move)
        del self.moves[:-50]
        if self.on_move:
            self.on_move(move)
        return move


class WalkDetector:
    """Walking away: the band's own step counter / activity state (the `activity` stream)
    first; a simple step counter from the accelerometer when no activity readings come.
    Fires on_walk(steps, how) once per walking bout: `min_steps` steps within
    `window_seconds`, or the band saying walk/run for `state_seconds`. A bout ends after
    `rest_seconds` with no steps."""

    def __init__(self, cfg=None, on_walk=None, clock=time.time):
        c = dict(min_steps=8, window_seconds=12.0, state_seconds=4.0, rest_seconds=3.0,
                 step_g=0.15, step_min_seconds=0.3, step_max_seconds=1.2)
        c.update(cfg or {})
        self.c, self.on_walk, self.clock = c, on_walk, clock
        self.counts = deque()             # (time, band lifetime steps)
        self.walk_since = None
        self.imu_steps = deque()          # times of steps counted from the accelerometer
        self.last_peak = None
        self.above = False
        self.activity_seen = None
        self.walking = False
        self.last_step = None
        self.state = None
        self.steps_recent = 0

    def feed_activity(self, steps, state):
        now = self.clock()
        self.activity_seen = now
        self.state = state
        if self.counts and steps > self.counts[-1][1]:
            self.last_step = now
        self.counts.append((now, steps))
        while self.counts and now - self.counts[0][0] > self.c["window_seconds"]:
            self.counts.popleft()
        if state in ("walk", "run"):
            self.walk_since = self.walk_since or now
        else:
            self.walk_since = None
        n = self.counts[-1][1] - self.counts[0][1] if self.counts else 0
        self.steps_recent = n
        if n >= self.c["min_steps"]:
            self._fire(n, "band step counter")
        elif self.walk_since and now - self.walk_since >= self.c["state_seconds"]:
            self._fire(n, "band says %s" % state)
        self.tick()

    def feed_accel(self, t, ax, ay, az):
        """Fallback step counter: a peak of |a| more than step_g above 1 g, at a walking rhythm."""
        if ax is None:
            return
        now = self.clock()
        if self.activity_seen and now - self.activity_seen < 3.0:
            return                                    # the band's own counter is better
        dev = math.sqrt(ax * ax + ay * ay + az * az) - 1.0
        if dev > self.c["step_g"] and not self.above:
            self.above = True
            gap = None if self.last_peak is None else t - self.last_peak
            if gap is None or gap >= self.c["step_min_seconds"]:
                if gap is not None and gap > self.c["step_max_seconds"]:
                    self.imu_steps.clear()            # rhythm broken: start again
                self.imu_steps.append(now)
                self.last_peak = t
                self.last_step = now
        elif dev < self.c["step_g"] * 0.5:
            self.above = False
        while self.imu_steps and now - self.imu_steps[0] > self.c["window_seconds"]:
            self.imu_steps.popleft()
        self.steps_recent = len(self.imu_steps)
        if len(self.imu_steps) >= self.c["min_steps"]:
            self._fire(len(self.imu_steps), "steps felt by the motion sensor")
        self.tick()

    def stepped_since(self, t):
        """Any sign of walking since time t (a step counted, or the band saying walk/run)."""
        return bool((self.last_step and self.last_step >= t) or (self.state in ("walk", "run") and self.walk_since))

    def tick(self):
        if self.walking and (self.last_step is None or self.clock() - self.last_step > self.c["rest_seconds"]):
            self.walking = False                      # bout over: count the next one from zero
            self.imu_steps.clear()
            if self.counts:
                last = self.counts[-1]
                self.counts.clear()
                self.counts.append(last)
            self.walk_since = None
            self.steps_recent = 0

    def _fire(self, steps, how):
        if self.walking:
            return
        self.walking = True
        if self.on_walk:
            self.on_walk(steps, how)


class Gestures:
    """Turns moves into the actions the server combines with sounds:
      set_down  a "lower" move ending at rest           (with a clack -> item placed)
      pickup    a "raise" that starts from a pose like the one the hand had when it set
                something down (hand back on the table, palm the same way)  -> item picked up.
                Standing up from the table looks the same, so a pickup only counts when no
                walking starts within pickup_walk_veto_seconds (then it was standing up).
      to_mouth  a "raise" of at least to_mouth_min_rise_m with the forearm tilting
                to_mouth_min_tilt_deg or more                  (with a rattle -> pill taken)"""

    def __init__(self, cfg=None, on_gesture=None, walk=None, clock=time.time, on_debug=None):
        c = dict(setdown_min_drop_m=0.06, setdown_max_seconds=2.5, pickup_min_rise_m=0.06, pickup_max_rise_m=0.5,
                 pickup_pose_tol_deg=30.0, pickup_lookback_seconds=7200.0, pickup_walk_veto_seconds=4.0,
                 to_mouth_min_rise_m=0.15, to_mouth_min_tilt_deg=30.0)
        c.update(cfg or {})
        self.c, self.on_gesture, self.walk, self.clock, self.on_debug = c, on_gesture, walk, clock, on_debug
        self.rest_poses = deque(maxlen=10)            # (time, pose) where something was set down
        self.pending = []                             # pickups waiting out the walking veto

    def _walking(self):
        return bool(self.walk and self.walk.walking)

    def move(self, m):
        c, now = self.c, self.clock()
        if self._walking():
            return []
        out = []
        if m["kind"] == "lower" and -m["dz"] >= c["setdown_min_drop_m"] and m["seconds"] <= c["setdown_max_seconds"]:
            self.rest_poses.append((now, m["end_pose"]))
            out.append(("set_down", {"dz": m["dz"], "tilt": m["tilt"], "seconds": m["seconds"]}))
        if m["kind"] == "raise":
            if m["dz"] >= c["to_mouth_min_rise_m"] and m["tilt"] >= c["to_mouth_min_tilt_deg"]:
                out.append(("to_mouth", {"dz": m["dz"], "tilt": m["tilt"], "seconds": m["seconds"]}))
            if c["pickup_min_rise_m"] <= m["dz"] <= c["pickup_max_rise_m"] and any(
                    now - t0 <= c["pickup_lookback_seconds"] and angle_between(p, m["start_pose"]) <= c["pickup_pose_tol_deg"]
                    for t0, p in self.rest_poses):
                self.pending.append((now, {"dz": m["dz"], "tilt": m["tilt"], "seconds": m["seconds"]}))
                if self.on_debug:
                    self.on_debug("pickup? (waiting %.0f s to be sure it isn't standing up to walk)" % c["pickup_walk_veto_seconds"])
        for kind, data in out:
            if self.on_gesture:
                self.on_gesture(kind, data)
        return out

    def tick(self):
        now, keep = self.clock(), []
        for t0, data in self.pending:
            if self.walk and self.walk.stepped_since(t0):
                if self.on_debug:
                    self.on_debug("pickup ignored: walking started right after (standing up)")
                continue
            if now - t0 >= self.c["pickup_walk_veto_seconds"]:
                if self.on_gesture:
                    self.on_gesture("pickup", data)
            else:
                keep.append((t0, data))
        self.pending = keep


# ---- synthetic motions for the pretend band and tests --------------------------------
def vertical_motion(dz=-0.3, seconds=0.6, tilt_deg=0.0, axis="x"):
    """The wrist (band lying flat, az = 1 g at rest) moves dz metres up (+) or down (-) in
    `seconds`, optionally turning the forearm by tilt_deg about `axis` at the same time.
    Returns a function for SimTransport.play_motion. Smooth start and stop."""
    k = "xyz".index(axis)
    w = math.radians(tilt_deg) / seconds                  # constant turn speed, rad/s

    def f(s):
        s = min(s, seconds)
        a_up = dz * (2 * math.pi / seconds ** 2) * math.sin(2 * math.pi * s / seconds) / 9.81
        th = w * s
        # gravity ("up") in the band's axes after turning th about the axis: d(up)/dt = -w x up
        up = [0.0, 0.0, 1.0]
        if k == 0:
            up = [0.0, math.sin(th), math.cos(th)]
        elif k == 1:
            up = [-math.sin(th), 0.0, math.cos(th)]
        a = [(1.0 + a_up) * u for u in up]
        g = [0.0, 0.0, 0.0]
        g[k] = math.degrees(w) if s < seconds else 0.0
        return {"ax": a[0], "ay": a[1], "az": a[2], "gx": g[0], "gy": g[1], "gz": g[2]}
    return f


def hold_pose(tilt_deg=0.0, axis="x"):
    th = math.radians(tilt_deg)
    up = {"x": [0.0, math.sin(th), math.cos(th)], "y": [-math.sin(th), 0.0, math.cos(th)]}[axis]
    return {"ax": up[0], "ay": up[1], "az": up[2], "gx": 0.0, "gy": 0.0, "gz": 0.0}


def walk_motion(steps=12, cadence=110):
    """Arm swinging while walking: one |a| bump per step (for the accelerometer step counter)."""
    period = 60.0 / cadence

    def f(s):
        ph = (s % period) / period
        bump = 0.35 * math.exp(-((ph - 0.2) / 0.06) ** 2)
        return {"ax": 0.1 * math.sin(2 * math.pi * s / (2 * period)), "ay": 0.0, "az": 1.0 + bump,
                "gx": 0.0, "gy": 60.0 * math.sin(2 * math.pi * s / (2 * period)), "gz": 0.0}
    return f, steps * period


REST_POSE = {"ax": 0.0, "ay": 0.0, "az": 1.0, "gx": 0.0, "gy": 0.0, "gz": 0.0}
