"""Memory Band: the bridge between the Vexo band and the Memory Band server.

Passive first (nothing to say or press). The band's mic and motion sensor only:
  door   lock clack (calibrated sound) or key-turn twist + click        -> door locked
  pill   pill bottle rattle + cap twist or hand-to-mouth within 30 s    -> pill taken
  phone  clack + hand lowered and resting = phone set down; then walking away (band steps)
         with no pick-up                                                 -> "Don't forget your phone"
  band link  -> signal strength to this Mac (like band-leash)    -> "left home" (fallback; the phone's
                                                                     Shortcuts automation is the main one)
  bathroom  toilet paper rustle, then no flush         -> "Looks like the toilet wasn't flushed" (to the wearer)
            flushes counted; none by noon               -> gentle note to the family
  off-wrist  band dead still (no pulse/tremor wobble) for 20 min    -> "you're not wearing your band"
  double-tap -> "yes / I did it / I'm fine"     free-fall -> fall check
Optional extra: speech (Deepgram) -> sentences -> the server's tagger.
Calibration: from the setup page (http://127.0.0.1:8001/setup) while this runs, or the CLI below.

Run from the repo root while the server runs (.venv/bin/uvicorn memory_band.main:app --port 8001):
  .venv/bin/python memory_band/band_bridge.py 17                       # your band number, or BAND_NUMBER in .env
  .venv/bin/python memory_band/band_bridge.py --calibrate-sound lock   # your lock's sound 3x (= --calibrate-lock)
  .venv/bin/python memory_band/band_bridge.py --calibrate-sound pill   # shake your pill bottle 3x
  .venv/bin/python memory_band/band_bridge.py --calibrate-sound setdown  # put your phone on the table 3x
  .venv/bin/python memory_band/band_bridge.py --calibrate-sound flush  # optional: flush your toilet 3x
  .venv/bin/python memory_band/band_bridge.py --calibrate-sound paper  # optional: pull toilet paper off the roll 3x
  .venv/bin/python memory_band/band_bridge.py --calibrate-twist        # record your key-turn twist 3x
  .venv/bin/python memory_band/band_bridge.py --debug-sensors          # live sound scores + motion numbers
  .venv/bin/python memory_band/band_bridge.py 17 --sim --script memory_band/examples/demo_band.txt   # pretend band

The band takes one Bluetooth connection at a time: close the kit dashboard first, and
double-tap the band right before starting so it can be found for 60 seconds. While the mic
is on (lock sound, speech) the band's light is solid blue and it uses more battery.
"""
import argparse
import os
import queue
import sys
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

try:
    from dotenv import load_dotenv
    for p in (REPO / ".env", HERE / ".env"):
        if p.exists():
            load_dotenv(p, override=False)
except ImportError:
    pass


def find_kit():
    candidates = [os.environ.get("VEXO_KIT", ""), REPO / "vexo-open-kit",          # the team's layout (gitignored)
                  REPO.parent / "vexo-open-kit", "/workspace/vexokit"]
    for c in candidates:
        if c and (Path(c) / "python" / "vexo_open").is_dir():
            return Path(c)
    sys.exit("Can't find the Vexo Open Kit. In the repo root run: "
             "git clone https://github.com/VexoAI-Org/vexo-open-kit.git   (or set VEXO_KIT=/path/to/vexo-open-kit)")


KIT = find_kit()
sys.path.insert(0, str(KIT / "python"))                      # the library: from vexo_open.sync import Band
sys.path.insert(0, str(KIT / "examples" / "python"))         # the kit's speech helper (Deepgram)
from vexo_open.sync import Band, VexoError  # noqa: E402
import passive  # noqa: E402
import sounds  # noqa: E402

CFG = passive.load_config()


class Poster:
    """Sends to the server from its own thread, so band handlers never wait on the network."""

    def __init__(self, server, quiet=False):
        self.api = server.rstrip("/") + "/api"
        self.q = queue.Queue()
        self.quiet = quiet
        self.enabled = True
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, path, body):
        if self.enabled:
            self.q.put((path, body))

    def call(self, path, body=None, method="post"):
        """A direct call (heartbeat, calibration progress), from the caller's thread."""
        try:
            r = getattr(requests, method)(self.api + path, json=body, timeout=5)
            return r.json() if r.ok else None
        except (requests.RequestException, ValueError):
            return None

    def _run(self):
        s = requests.Session()
        while True:
            path, body = self.q.get()
            for attempt in range(3):
                try:
                    r = s.post(self.api + path, json=body, timeout=30)
                    if r.ok and not self.quiet:
                        data = r.json()
                        if path == "/transcript":
                            kinds = ", ".join(t["kind"] for t in data.get("tags", []))
                            if kinds:
                                print("   -> %s" % kinds)
                            for reply in data.get("replies", []):
                                print("   answer sent to phone: %s" % reply)
                        elif path in ("/tap", "/gesture", "/signal"):
                            print("   -> %s" % (data.get("reply") or data.get("result")))
                    break
                except requests.RequestException:
                    if attempt == 2:
                        print("Server not reachable at %s (is uvicorn running?)" % self.api, file=sys.stderr)
                    time.sleep(1)


class SoundWorker:
    """Decodes the band's Opus packets on its own thread and runs the sound detector.
    Band handlers only enqueue."""

    def __init__(self, detector):
        from vexo_open.audio import PcmDecoder
        print("Loading the sound decoder...")
        self.decoder = PcmDecoder()
        self.detector = detector
        self.q = queue.Queue(maxsize=500)
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, opus, seq):
        try:
            self.q.put_nowait((opus, seq))
        except queue.Full:
            pass

    def put_pcm(self, pcm):
        """Plain 16-bit samples (the pretend band's sounds), on the same thread as the band's sound."""
        self.q.put((None, pcm))

    def _run(self):
        while True:
            opus, seq = self.q.get()
            try:
                self.detector.feed(seq if opus is None else self.decoder.feed(opus, seq))
            except Exception as e:                   # a bad packet never stops the listener
                print("sound decode problem: %s" % e.__class__.__name__, file=sys.stderr)


def read_script(path):
    """Lines 'seconds | words' or 'seconds | @action'. Seconds from the start."""
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        at, _, what = (x.strip() for x in line.partition("|"))
        out.append((float(at), what))
    return out


def parse_number(a):
    raw = a.number or os.environ.get("BAND_NUMBER", "").strip()
    if not raw:
        sys.exit("Which band? Add your band number (written on its bag), or set BAND_NUMBER in .env. "
                 "The pretend band (--sim) uses the same number.")
    digits = "".join(ch for ch in raw if ch.isdigit()) or raw   # "40", "#40" or the full band name
    if not (digits.isdigit() and 1 <= int(digits) <= 999):
        sys.exit('"%s" is not a band number. Type the number on your band\'s bag, e.g. 17.' % raw)
    return int(digits)


def connect(number, a):
    if a.sim:
        from vexo_open.transports.sim import SimTransport
        sim = SimTransport(number, auto_tap=False)        # no taps of its own: the script decides
        band = Band.connect(number, transport=sim)
        band.transport.set_activity("still")              # not the made-up slow walk
        band.transport.play_motion(dict(passive.REST_POSE), seconds=0.1)   # lying still, not the made-up swing
        return band
    print("Looking for band %02d ... (double-tap the band if it isn't found)" % number)
    return Band.connect(number, transport="serial" if a.usb else "ble", auto_reconnect=True)


# ---- what the pretend band can act out ---------------------------------------------------
def sim_sound(worker, action, seed=5, delay=0.0, sim=None):
    """Feed a synthetic sound (lock / pill / setdown / knock / speech) into the sound detector."""
    import lock_sound
    x = {"knock": lambda: lock_sound.synth_other("knock"), "speech": lambda: sounds.synth_speech(seed)}.get(
        action, lambda: sounds.synth(action, seed))()
    pcm = lock_sound.to_pcm(sounds.with_background(x, seconds=0.3, seed=seed))
    if worker is None:
        return
    if delay and sim is not None:
        sim.after(delay, lambda: worker.put_pcm(pcm))
    else:
        worker.put_pcm(pcm)


def sim_act(what, sim, worker):
    """Pretend-band actions shared by --script and calibration. Returns False if unknown."""
    P = passive
    if what == "@twist":
        print("(pretend band: wrist turns like a key)")
        sim.play_motion(P.twist_motion(120, 0.5, "x"), seconds=0.6)
    elif what == "@wave":
        print("(pretend band: a big slow arm wave, not a key turn)")
        sim.play_motion(lambda s: {"gz": 150.0}, seconds=2.5)
    elif what == "@lock-sound":
        print("(pretend lock click fed to the sound detector)")
        sim_sound(worker, "lock")
    elif what == "@knock":
        print("(pretend knock fed to the sound detector: should not count)")
        sim_sound(worker, "knock")
    elif what == "@flush":
        print("(pretend toilet flush fed to the sound detector)")
        sim_sound(worker, "flush")
    elif what == "@paper":
        print("(pretend toilet paper rustle fed to the sound detector)")
        sim_sound(worker, "paper")
    elif what == "@off-wrist":
        print("(pretend band: lying dead still on the table)")
        sim.play_motion(dict(P.REST_POSE), seconds=max(30.0, sim_off_wrist_seconds() + 10))
    elif what == "@on-wrist":
        print("(pretend band: back on the wrist, moving)")
        sim.play_motion(P.twist_motion(90, 0.6, "x"), seconds=0.7)
    elif what == "@cap-twist":
        print("(pretend band: twisting a pill bottle cap)")
        sim.play_motion(P.twist_motion(150, 0.6, "x"), seconds=0.7)
    elif what == "@rattle":
        print("(pretend pill bottle rattle fed to the sound detector)")
        sim_sound(worker, "pill", seed=7)
    elif what == "@to-mouth":
        print("(pretend band: hand to mouth and back)")
        sim.play_motion(P.vertical_motion(0.35, 0.8, 60, "x"), seconds=0.8)
        sim.play_motion(P.hold_pose(60, "x"), seconds=1.0)
        sim.play_motion(P.vertical_motion(-0.35, 0.8, -60, "x"), seconds=0.8)
        sim.play_motion(dict(P.REST_POSE), seconds=0.5)
    elif what == "@pill":
        print("(pretend band: open the bottle, shake out a pill, hand to mouth)")
        sim_act("@cap-twist", sim, worker)
        sim.play_motion(dict(P.REST_POSE), seconds=0.8)
        sim_sound(worker, "pill", seed=7, delay=1.5, sim=sim)
        sim.play_motion(dict(P.REST_POSE), seconds=1.5)
        sim_act("@to-mouth", sim, worker)
    elif what in ("@set-down", "@lower"):
        print("(pretend band: hand goes down to the table and rests%s)" % (" + clack" if what == "@set-down" else ""))
        sim.play_motion(P.vertical_motion(-0.3, 0.6), seconds=0.6)
        sim.play_motion(dict(P.REST_POSE), seconds=1.0)
        if what == "@set-down":
            sim_sound(worker, "setdown", seed=3, delay=0.55, sim=sim)
    elif what == "@clack":
        sim_sound(worker, "setdown", seed=3)
    elif what == "@pickup":
        print("(pretend band: hand lifts from the table)")
        sim.play_motion(P.vertical_motion(0.3, 0.6), seconds=0.6)
        sim.play_motion(dict(P.REST_POSE), seconds=1.0)
    elif what == "@walk":
        print("(pretend band: starts walking; the band counts steps)")
        sim.set_activity("walk")
    elif what == "@stop":
        print("(pretend band: stands still)")
        sim.set_activity("still")
    elif what == "@double-tap":
        sim.tap("double-tap")
    elif what == "@fall":
        sim.tap("free-fall")
    elif what == "@walk-away":
        print("(pretend band: signal fades to -90 dBm)")
        sim.set_rssi(-90)
    elif what == "@back":
        print("(pretend band: signal strong again)")
        sim.set_rssi(-50)
    else:
        return False
    return True


def sim_off_wrist_seconds():
    c = CFG.get("off_wrist", {})
    return float(c.get("still_seconds") or c.get("still_minutes", 20) * 60)


SIM_SAMPLE = {"lock": "@lock-sound", "pill": "@rattle", "setdown": "@clack", "flush": "@flush", "paper": "@paper"}


# ---- calibration (from the setup page or the command line) ----------------------------------
class Calibrator:
    """Records `samples` examples of one action from the live band and saves them.
    sound actions (lock, pill, setdown): each new sound onset is a sample.
    twist: each wrist turn of min_twist_angle degrees or more is a sample.
    While recording, nothing is reported to the server as an action."""

    def __init__(self, pipe, report):
        self.p = pipe
        self.report = report                 # report(state, samples, message)
        c = CFG.get("calibration", {})
        self.need = int(c.get("samples", 3))
        self.gap = float(c.get("sample_gap_seconds", 1.5))
        self.timeout = float(c.get("timeout_seconds", 90))
        self.min_angle = float(c.get("min_twist_angle", 50))
        self.action = None
        self.got = []
        self.last = 0.0
        self.started = 0.0
        self.lock = threading.Lock()

    @property
    def active(self):
        return self.action is not None

    def start(self, action):
        with self.lock:
            self.action, self.got, self.started, self.last = action, [], time.time(), 0.0
        self.report("recording", 0, self.prompt(0))
        self._sim_next()

    def prompt(self, i):
        what = {"lock": "Lock your front door (band on the wrist you lock with)",
                "twist": "Turn the key in your front door, like locking it",
                "pill": "Pick up your pill bottle and shake out a pill",
                "setdown": "Put your phone down on the table",
                "flush": "Flush the toilet (wait for the tank to refill between flushes)",
                "paper": "Pull some toilet paper off the roll and tear it"}[self.action]
        return "(%d/%d) %s now..." % (i + 1, self.need, what)

    def _sim_next(self):
        sim = self.p.sim
        if sim is None or not self.active:
            return
        if self.action == "twist":
            i = len(self.got)
            sim.after(2.0, lambda: sim.play_motion(passive.twist_motion(110 + 10 * i, 0.5, "x"), seconds=0.6))
        else:
            act = SIM_SAMPLE[self.action]
            sim.after(2.0 if self.action != "flush" else 12.0, lambda: sim_act(act, sim, self.p.worker))

    def on_onset(self, desc):
        if self.action in sounds.NOISE_ACTIONS:
            return True
        if self.action not in sounds.ACTIONS:
            return self.active                # recording a twist: still keep sounds quiet
        self._take(desc)
        return True

    def on_noise(self, desc):
        """A whole noise event (flush / toilet paper calibration)."""
        if self.action not in sounds.NOISE_ACTIONS:
            return self.active
        self._take(desc)
        return True

    def on_segment(self, seg):
        if self.action != "twist":
            return self.active
        if seg["angle"] >= self.min_angle:
            self._take(seg)
        elif self.p.debug:
            print("   (twist of %.0f deg: too small for a sample)" % seg["angle"])
        return True

    def _take(self, sample):
        with self.lock:
            now = time.time()
            if not self.active or now - self.last < self.gap:
                return
            self.last = now
            self.got.append(sample)
            n = len(self.got)
        if n < self.need:
            self.report("sample", n, "Got %d of %d. %s" % (n, self.need, self.prompt(n)))
            self._sim_next()
        else:
            self.finish()

    def finish(self):
        action, got = self.action, list(self.got)
        self.action = None
        if action == "twist":
            tpl = passive.make_template(got)
            passive.save_template(tpl)
            self.p.twist.template = tpl
            msg = "Saved your key-turn twist: axis %s, about %.0f degrees." % (tpl["axis"], tpl["angle"])
        else:
            tpls = [sounds.template_of(action, d) for d in got]
            sounds.save_templates(action, tpls)
            self.p.sound.templates[action] = tpls
            msg = "Saved %d samples of %s." % (len(tpls), CAL_LABEL[action])
        self.report("done", len(got), msg)

    def tick(self):
        limit = self.timeout * (4 if self.action == "flush" else 1)      # a tank takes ~1 min to refill
        if self.active and time.time() - self.started > limit:
            n = len(self.got)
            self.action = None
            self.report("failed", n, "Timed out after %d s with %d of %d samples. Try Redo." % (limit, n, self.need))

    def cancel(self):
        self.action = None

    def use_default(self, action):
        self.action = None
        if action == "twist":
            try:
                passive.TEMPLATE_FILE.unlink()
            except OSError:
                pass
            self.p.twist.template = None
        else:
            import lock_sound
            files = [sounds.template_file(action)] + ([lock_sound.TEMPLATE_FILE] if action == "lock" else [])
            for f in files:
                try:
                    f.unlink()
                except OSError:
                    pass
            self.p.sound.templates[action] = []
        self.report("default", 0, "Using the default for %s." % CAL_LABEL[action])


CAL_LABEL = {"lock": "your door lock sound", "twist": "your key-turn twist", "pill": "your pill bottle",
             "setdown": "your phone being set down", "flush": "your toilet flush", "paper": "your toilet paper roll"}


# ---- the detectors, wired up ----------------------------------------------------------------
class Pipeline:
    """Every passive detector, fed by the band's handlers; results go to the server."""

    def __init__(self, a, post, mic):
        self.a, self.post, self.mic = a, post, mic
        self.debug = a.debug_sensors
        self.sim = None
        twist_tpl = passive.load_template()
        tpls = sounds.load_all()
        self.twist = passive.TwistDetector(CFG.get("twist"), template=twist_tpl, on_twist=self.on_twist)
        self.cap = passive.TwistDetector(CFG.get("cap_twist"), on_twist=self.on_cap_twist)
        self.sound = sounds.SoundDetector(CFG.get("sounds"), templates=tpls, on_detect=self.on_sound,
                                          on_debug=self.on_sound_debug if self.debug else None)
        self.walk = passive.WalkDetector(CFG.get("walk"), on_walk=self.on_walk)
        self.gestures = passive.Gestures(CFG.get("gestures"), on_gesture=self.on_gesture, walk=self.walk,
                                         on_debug=(lambda m: print("   %s" % m)) if self.debug else None)
        self.motion = passive.MotionTracker(CFG.get("motion"), on_move=self.on_move)
        self.wear = passive.OffWrist(CFG.get("off_wrist"), on_change=self.on_wear)
        self.presence = passive.Presence(CFG.get("presence"), on_change=self.on_presence)
        self.shake = passive.ShakeDetector(CFG.get("shake"), on_shake=lambda: (print("Pill bottle shake"),
                                           self.post.send("/gesture", {"kind": "pill_shake"})))
        self.door = passive.DoorSound(CFG.get("door_sound"), on_sound=lambda db: self.post.send(
            "/signal", {"kind": "door_sound", "value": db}))
        self.cal = Calibrator(self, self.report_cal)
        self.sound.on_onset = self.cal.on_onset
        self.sound.noise.on_event = self.cal.on_noise
        self.twist.on_segment = self.cal.on_segment
        self.cap.on_segment = lambda seg: self.cal.active
        self.worker = SoundWorker(self.sound) if mic else None
        self.cal_id = None
        self.last_debug = 0.0
        self.last_seen = {}
        if self.debug:
            orig = self.twist.on_segment
            self.twist.on_segment = lambda seg: (print("   twist segment: %(angle).0f deg about %(axis)s in %(seconds).2f s, "
                                                       "dominance %(dominance).2f" % seg), orig(seg))[1]

    def status_lines(self):
        t = self.twist.template
        out = ["Key-turn twist: %s" % ("calibrated (axis %s, ~%.0f deg)" % (t["axis"], t["angle"]) if t else
                                       "default (calibrate on the setup page or --calibrate-twist)")]
        for a in sounds.CAL_SOUNDS:
            n = len(self.sound.templates.get(a) or [])
            out.append("Sound %-8s %s" % (a + ":", "mic off" if not self.mic else ("%d calibrated samples" % n) if n
                                         else "default rule (calibrate: --calibrate-sound %s)" % a))
        return out

    def calibrated(self):
        return {"twist": bool(self.twist.template), **{a: bool(self.sound.templates.get(a)) for a in sounds.CAL_SOUNDS}}

    # ---- results -> server ----
    def on_twist(self, seg):
        print("Key-turn twist felt: %(angle).0f deg about %(axis)s in %(seconds).2f s" % seg)
        self.post.send("/gesture", {"kind": "lock_twist", "angle": seg["angle"], "axis": seg["axis"],
                                    "seconds": seg["seconds"], "mic_on": self.mic})

    def on_cap_twist(self, seg):
        print("Cap twist felt: %(angle).0f deg about %(axis)s" % seg)
        self.post.send("/gesture", {"kind": "cap_twist", "angle": seg["angle"], "axis": seg["axis"], "seconds": seg["seconds"]})

    def on_sound(self, kind, score):
        if kind:
            print("Sound: %s (score %.2f)" % (kind, score))
            self.post.send("/signal", {"kind": kind, "value": score})

    def on_sound_debug(self, info):
        if "noise" in info:
            n = info["noise"]
            print("   noise event: %(seconds).1f s, flat %(flat).2f, bright %(hf).2f, steady cv %(cv).2f, loud %(loud_fraction).2f" % n
                  + " -> %s" % (", ".join(info["kinds"]) or "nothing"))
            return
        sc = info["scores"]
        print("   sound onset: lock %.2f  pill %.2f  setdown %.2f | %d little clicks, bright %.2f, dies away x%.1f -> %s" % (
            sc.get("lock", 0), sc.get("pill", 0), sc.get("setdown", 0), info["clicks"], info["hf"], info["decay"],
            ", ".join(info["kinds"]) or "nothing"))

    def on_move(self, m):
        if self.debug and m["seconds"] >= 0.2:
            print("   move: %s, %+.2f m up, forearm tilted %.0f deg, %.2f s (gyro/gravity mismatch %.0f deg)" % (
                m["kind"], m["dz"], m["tilt"], m["seconds"], m.get("drift", 0)))
        if not self.cal.active:
            self.gestures.move(m)

    def on_gesture(self, kind, data):
        print({"set_down": "Hand lowered and rested (%.2f m down)" % -data["dz"],
               "pickup": "Hand lifted from where it set something down",
               "to_mouth": "Hand to mouth (%.2f m up, %.0f deg tilt)" % (data["dz"], data["tilt"])}.get(kind, kind))
        self.post.send("/gesture", dict(data, kind=kind))

    def on_walk(self, steps, how):
        print("Walking: %d steps (%s)" % (steps, how))
        if not self.cal.active:
            self.post.send("/gesture", {"kind": "walking", "steps": int(steps), "how": how})

    def on_wear(self, state, still_for):
        print("Band %s (dead still for %.0f s)" % ("NOT on the wrist" if state == "not_worn" else "back on the wrist", still_for))
        self.post.send("/signal", {"kind": state, "value": round(still_for)})

    def on_presence(self, state, reason, rssi):
        print("Presence: %s (%s)" % (state, reason))
        self.post.send("/presence", {"state": state, "reason": "band: " + reason, "rssi": rssi})

    # ---- band handlers ----
    def on_motion(self, m):
        t = m.t / 1000.0
        self.twist.feed(t, m.gx, m.gy, m.gz)
        self.cap.feed(t, m.gx, m.gy, m.gz)
        self.motion.feed(t, m.ax, m.ay, m.az, m.gx, m.gy, m.gz)
        self.walk.feed_accel(t, m.ax, m.ay, m.az)
        self.wear.feed(t, m.ax, m.ay, m.az, m.gx, m.gy, m.gz)
        if self.a.pill_shake:
            self.shake.feed(t, m.ax, m.ay, m.az)
        if self.debug and time.time() - self.last_debug >= 0.5:
            self.last_debug = time.time()
            tilt = passive.angle_between(self.motion.pose, (m.ax, m.ay, m.az)) if self.motion.pose and m.ax is not None else 0
            print("   motion: gyro %4.0f dps  |a| %.2f g  %s  tilt from rest %3.0f deg  steps(12s) %d  %s" % (
                self.motion.gyro_mag, self.motion.acc_dev + self.motion.g0, "still " if self.motion.still else "moving",
                tilt, self.walk.steps_recent, "WALKING" if self.walk.walking else (self.walk.state or "-")))
            if self.wear.last:
                print("   wear: gyro spread %.2f dps, accel spread %.4f g, dead still %.0f s (off-wrist after %.0f s)" % (
                    self.wear.last["gyro_spread"], self.wear.last["acc_spread"], self.wear.still_for, self.wear.still_seconds))

    def on_activity(self, e):
        self.walk.feed_activity(e.steps, e.state)

    def tick(self):
        self.presence.tick()
        self.walk.tick()
        self.gestures.tick()
        self.cal.tick()

    # ---- the setup page: heartbeat + calibration requests ----
    def report_cal(self, state, samples, message):
        print("Calibration: %s" % message)
        if self.cal_id:
            self.post.call("/calibrate/progress", {"id": self.cal_id, "state": state, "samples": samples, "message": message})

    def heartbeat(self):
        r = self.post.call("/listener/heartbeat", {"calibrated": self.calibrated(), "mic": self.mic, "sim": self.sim is not None})
        demo_still = (r or {}).get("off_wrist_seconds")
        want = float(demo_still) if demo_still else float(CFG.get("off_wrist", {}).get("still_minutes", 20)) * 60
        if want != self.wear.still_seconds:
            self.wear.still_seconds = want
            print("Off-wrist reminder after %.0f s dead still" % want)
        for act in (r or {}).get("sim_actions") or []:
            if self.sim is not None and not sim_act(act, self.sim, self.worker):
                print("(unknown pretend-band action %s)" % act)
        cal = (r or {}).get("calibration") or {}
        if not cal.get("id"):
            return
        if cal["state"] == "waiting" and cal["id"] != self.cal_id:
            self.cal_id = cal["id"]
            if cal["action"] != "twist" and not self.mic:
                return self.report_cal("failed", 0, "The microphone is off (--no-mic): sounds can't be calibrated.")
            print("\nSetup page: calibrating %s" % CAL_LABEL[cal["action"]])
            self.cal.start(cal["action"])
        elif cal["state"] == "use_default" and cal["id"] != self.cal_id:
            self.cal_id = cal["id"]
            self.cal.use_default(cal["action"])
        elif cal["state"] == "cancelled" and cal["id"] == self.cal_id and self.cal.active:
            self.cal.cancel()
            print("Calibration cancelled from the setup page.")


def heartbeat_loop(pipe, stop):
    every = float(CFG.get("calibration", {}).get("poll_seconds", 1.0))
    while not stop.is_set():
        try:
            pipe.heartbeat()
        except Exception as e:                           # never let the setup link stop the listener
            print("setup link problem: %r" % (e,), file=sys.stderr)
        stop.wait(every)


def setup_streams(b, a, mic):
    if a.pill_shake:
        try:
            b.set_motion(accel_range=8)               # a shake goes beyond the default 2 g
        except VexoError:
            pass
    streams = dict(events=True, motion=CFG.get("motion_rate", 50), audio=mic)
    try:
        streams["activity"] = float(CFG.get("walk", {}).get("activity_interval_seconds", 1))
    except (TypeError, ValueError):
        streams["activity"] = True
    if not a.usb:
        streams["link"] = True                        # signal strength: Bluetooth only
    if a.door_sound and mic:
        streams["mic_level"] = True
    try:
        b.configure(**streams)
    except VexoError as e:                            # e.g. firmware 1.0 without intervals
        print("Band said: %s. Trying without the activity interval." % e)
        streams["activity"] = True
        b.configure(**streams)


# ---- command-line calibration ---------------------------------------------------------------
def calibrate_cli(number, a, action):
    post = Poster(a.server, quiet=True)
    post.enabled = False                              # nothing goes to the server while calibrating
    pipe = Pipeline(a, post, mic=(action != "twist"))
    band = connect(number, a)
    pipe.sim = band.transport if a.sim else None
    print("Connected to %s." % band.name)
    band.on("motion", pipe.on_motion)
    band.on("audio", lambda au: pipe.worker and pipe.worker.put(au.opus, au.seq))
    band.configure(motion=CFG.get("motion_rate", 50), audio=pipe.mic)
    if pipe.mic:
        print("The band's light is solid blue while the microphone is on.")
    done = threading.Event()
    pipe.cal.report = lambda state, n, msg: (print(msg), done.set() if state in ("done", "failed") else None)
    pipe.cal.start(action)
    while not done.is_set():
        band.wait(0.2)
        pipe.cal.tick()
    try:
        band.configure(audio=False)
    except VexoError:
        pass
    band.disconnect()


# ---- the listener --------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("number", nargs="?", help="your band number (written on its bag); default BAND_NUMBER from .env")
    p.add_argument("--sim", action="store_true", help="use the kit's pretend band (no hardware)")
    p.add_argument("--usb", action="store_true", help="use the USB cable instead of Bluetooth (no signal strength)")
    p.add_argument("--server", default=os.environ.get("MEMORY_SERVER", "http://127.0.0.1:8001"))
    p.add_argument("--script", help="timed sentences / band actions instead of real life (see examples/demo_band.txt)")
    p.add_argument("--speech", choices=["auto", "on", "off"], default="auto",
                   help="Deepgram speech-to-text: auto = on when DEEPGRAM_API_KEY is set")
    p.add_argument("--no-mic", action="store_true", help="keep the microphone off (no sounds, no speech)")
    p.add_argument("--pill-shake", action="store_true", help="count shaking the pill bottle as taking the pill")
    p.add_argument("--door-sound", action="store_true", help="note loud door sounds (hint only)")
    p.add_argument("--debug-sensors", action="store_true", help="print live sound scores and motion numbers (for tuning)")
    p.add_argument("--calibrate-sound", choices=sorted(sounds.CAL_SOUNDS), help="record 3 samples of this sound, then exit")
    p.add_argument("--calibrate-lock", action="store_true", help="same as --calibrate-sound lock")
    p.add_argument("--calibrate-twist", action="store_true", help="record your key-turn twist 3 times, then exit")
    p.add_argument("--seconds", type=float, default=None, help="stop after this long (default: until Ctrl+C)")
    a = p.parse_args()
    number = parse_number(a)
    if a.calibrate_lock:
        a.calibrate_sound = "lock"
    if a.calibrate_sound or a.calibrate_twist:
        return calibrate_cli(number, a, "twist" if a.calibrate_twist else a.calibrate_sound)

    post = Poster(a.server)
    script = read_script(a.script) if a.script else None
    mic = not a.no_mic
    use_speech = mic and script is None and (a.speech == "on" or (a.speech == "auto" and os.environ.get("DEEPGRAM_API_KEY")))

    # ---- speech (optional, the kit's Deepgram helper) ----
    ears = None

    def on_text(text, final):
        if text.strip() and not final:
            print("  (hearing) %s" % text)
        if final and text.strip():
            print("Heard: %s" % text)
            post.send("/transcript", {"text": text, "source": "band"})

    def start_ears():
        import speech                                   # vexo-open-kit/examples/python/speech.py
        e = speech.create()                             # Deepgram nova-3, key from DEEPGRAM_API_KEY / .env
        e.start(on_text)
        print("Speech is turned into text by %s." % e.label)
        return e

    if use_speech:
        try:
            ears = start_ears()
        except VexoError as problem:                     # SpeechError is a VexoError: no key, bad key, no internet
            print("No speech-to-text: %s" % problem)

    # ---- passive detectors ----
    pipe = Pipeline(a, post, mic)
    for line in pipe.status_lines():
        print(line)

    # ---- the band ----
    band = connect(number, a)
    pipe.sim = band.transport if a.sim else None
    print("Connected to %s." % band.name)
    post.send("/band", {"connected": True, "name": band.name})

    def on_audio(au):
        if pipe.worker:
            pipe.worker.put(au.opus, au.seq)
        if ears is not None:
            try:
                ears.feed(au.opus, au.seq)
            except Exception as e:                      # a dropped Deepgram link is restarted below
                if not getattr(on_audio, "_warned", False):
                    on_audio._warned = True
                    print("speech feed problem: %r" % (e,), file=sys.stderr)

    def on_double_tap(e):
        print("Double-tap (yes / I did it / I'm fine)")
        post.send("/tap", {"kind": "double-tap", "source": "band"})

    def on_fall(e):
        print("Free-fall detected: asking the wearer if they're OK")
        post.send("/fall", {"source": "band"})

    def on_link(e):
        pipe.presence.rssi(e.rssi)

    def on_lost(e):
        pipe.presence.link_lost()
        post.send("/band", {"connected": False, "name": band.name, "note": "reconnecting"})

    def on_back(e):
        pipe.presence.link_back()
        post.send("/band", {"connected": True, "name": band.name})

    gone = threading.Event()
    handlers = (("audio", on_audio), ("motion", pipe.on_motion), ("activity", pipe.on_activity),
                ("double-tap", on_double_tap), ("free-fall", on_fall), ("link", on_link),
                ("reconnecting", on_lost), ("reconnected", on_back))
    for ev, fn in handlers:
        band.on(ev, fn)
    band.on("disconnect", lambda e: gone.set())
    if a.door_sound:
        band.on("mic-level", lambda e: pipe.door.feed(e.time, e.rms_db))

    if mic:
        print("The microphone stays on (sounds%s): the band's light is solid blue and it uses more battery."
              % (" + speech, sent to Deepgram: tell people nearby" if ears else ""))
    setup_streams(band, a, mic)
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(pipe, stop), daemon=True).start()
    print("Setup page (calibrate from your phone): %s/setup" % a.server.rstrip("/"))

    start = time.time()
    end = None if a.seconds is None else start + a.seconds
    if script and end is None:
        end = start + script[-1][0] + 6
    i = 0
    last_retry = 0.0
    try:
        while end is None or time.time() < end:
            band.wait(0.2)
            pipe.tick()
            now = time.time() - start
            while script and i < len(script) and script[i][0] <= now:
                run_script_step(script[i][1], band, a, post, pipe.worker)
                i += 1
            if gone.is_set() and not a.sim:
                # the band gave up reconnecting (away for more than 2 minutes): keep looking
                pipe.presence.link_lost()
                pipe.presence.tick()
                post.send("/band", {"connected": False, "name": "band %02d" % number, "note": "away"})
                print("Band out of reach. Looking for it every 15 s (double-tap it when you're back).")
                while True:
                    time.sleep(15)
                    pipe.presence.tick()
                    try:
                        band = connect(number, a)
                        for ev, fn in handlers:
                            band.on(ev, fn)
                        band.on("disconnect", lambda e: gone.set())
                        gone.clear()
                        setup_streams(band, a, mic)
                        on_back(None)
                        print("Band is back: %s" % band.name)
                        break
                    except VexoError:
                        pass
            if ears is not None and getattr(ears, "error", None) and time.time() - last_retry > 5:
                last_retry = time.time()
                try:
                    ears = start_ears()
                    print("Speech-to-text reconnected.")
                except VexoError as problem:
                    print("Speech-to-text still down: %s" % problem)
    except KeyboardInterrupt:
        pass
    stop.set()
    try:
        band.configure(audio=False)
    except VexoError:
        pass
    post.send("/band", {"connected": False, "name": band.name, "note": "stopped"})
    time.sleep(1.0)
    band.disconnect()
    if ears is not None:
        try:
            ears.stop()
        except Exception:
            pass


def run_script_step(what, band, a, post, worker):
    """One line of a --script file. Band actions go through the pretend band (sounds go
    straight into the detector: the pretend band can't play a new sound)."""
    if not what.startswith("@"):
        print("Heard (script): %s" % what)
        post.send("/transcript", {"text": what, "source": "script"})
        return
    if what == "@leave":
        print("(phone: left Home)")
        post.send("/presence", {"state": "away", "reason": "phone left Home (script)", "source": "phone"})
        return
    if what == "@arrive":
        post.send("/presence", {"state": "home", "reason": "phone arrived Home (script)", "source": "phone"})
        return
    if not a.sim:
        print("(%s needs --sim; do it on the real band)" % what)
        return
    if not sim_act(what, band.transport, worker):
        print("(unknown action %s)" % what)


if __name__ == "__main__":
    main()
