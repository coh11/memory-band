"""Hear everyday actions with the band's mic: one detector, any number of calibrated sounds.

Actions (calibrate each with 3 samples: band_listener.py --calibrate-sound lock|pill|setdown):
  lock     your front-door lock / deadbolt clack   -> "lock_sound" (or "click" when only close)
  pill     your pill bottle rattling                -> "pill_rattle"
  setdown  your phone / wallet set down on a table  -> "setdown_clack"

How (numpy only, like lock_sound.py): the band's sound is decoded to 16 kHz. Every 10 ms frame's
energy is compared with the last second of background; a sudden jump is an "onset". After an
onset we take 1 s of sound and describe it two ways:
  transient  250 ms spectrogram (24 log-spaced bands x 25 frames): the shape of one clack.
             Compared with the templates by cosine similarity allowing a small time shift.
             Used for lock and setdown.
  texture    the average spectrum over 1 s + how many small clicks it holds (a rattle is many
             little clicks). Used for pill.
Without calibration each action falls back to a rough rule, reported with the same kind:
  lock     "click"          a short bright transient (counts only with a key-turn twist)
  pill     "pill_rattle"    4+ little clicks in 1 s with bright sound (counts only with a cap twist
                            or hand-to-mouth motion)
  setdown  "impact"         a short transient that dies away fast (counts only with a wrist
                            lowering-and-resting motion)
No sound is stored or sent anywhere; only the scores. Limits: similar sounds (keys jangling vs. a
pill bottle, a cup vs. a phone on the table) can match. Every kind needs a motion cue as well,
except a calibrated lock-sound match."""
import json
import time
from collections import deque
from pathlib import Path

import numpy as np

import lock_sound
from lock_sound import FRAME, RATE, NFFT, features as transient_features, similarity

DATA = Path(__file__).resolve().parent / "memory" / "data"
ACTIONS = {
    "lock": dict(mode="transient", kind="lock_sound", near_kind="click", heuristic="click"),
    "pill": dict(mode="texture", kind="pill_rattle", heuristic="pill_rattle"),
    "setdown": dict(mode="transient", kind="setdown_clack", heuristic="impact"),
}
TEXTURE_FRAMES = 100                          # 1 s
EVAL_FRAMES = TEXTURE_FRAMES + NFFT // FRAME + 2
DEFAULTS = {
    "onset_ratio": 6.0, "min_rms": 0.01, "refractory_seconds": 1.0,
    "lock": {"match_score": 0.80, "click_score": 0.60, "hf_ratio_for_click": 0.35},
    "pill": {"match_score": 0.80, "min_clicks": 3, "heuristic_clicks": 4, "heuristic_hf_ratio": 0.30,
             "uncalibrated_counts": True},
    "setdown": {"match_score": 0.75, "decay_ratio": 4.0, "max_clicks": 2, "min_hf_ratio": 0.15, "uncalibrated_counts": True},
}


def config(cfg=None):
    c = json.loads(json.dumps(DEFAULTS))
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


def template_file(action):
    return DATA / ("sound_%s.json" % action)


# ---- features ------------------------------------------------------------------------
_edges = lock_sound._edges
_window = lock_sound._window


def frame_rms(x):
    n = len(x) // FRAME
    return np.sqrt(np.mean(x[:n * FRAME].reshape(n, FRAME) ** 2, axis=1)) if n else np.zeros(0)


def count_clicks(x, min_rms=0.004, rise=2.0, min_gap_frames=4):
    """Little sharp sounds inside a snippet: a 10 ms frame at least `rise` x louder than the
    quietest of the 3 frames before it."""
    r = frame_rms(x)
    n, last = 0, -99
    for i in range(3, len(r)):
        if r[i] >= min_rms and r[i] >= rise * max(r[i - 3:i].min(), 1e-5) and i - last >= min_gap_frames:
            n += 1
            last = i
    return n


def texture_features(x):
    """1 s -> (normalised mean log spectrum over 24 bands, number of little clicks)."""
    x = np.asarray(x, dtype=np.float32)
    rows = []
    for i in range(0, max(1, len(x) - NFFT), FRAME * 2):
        seg = x[i:i + NFFT]
        if len(seg) < NFFT:
            break
        p = np.abs(np.fft.rfft(seg * _window)) ** 2
        rows.append([p[_edges[b]:_edges[b + 1]].sum() for b in range(len(_edges) - 1)])
    rows = np.asarray(rows) if rows else np.zeros((1, len(_edges) - 1))
    loud = rows.sum(axis=1)
    keep = rows[loud >= np.percentile(loud, 50)]              # the louder half: the rattle, not the gaps
    spec = np.log10(keep.mean(axis=0) + 1e-9)
    spec -= spec.mean()
    n = np.linalg.norm(spec)
    return {"spec": (spec / n if n > 0 else spec).tolist(), "clicks": count_clicks(x)}


def hf_ratio(x):
    spec = np.abs(np.fft.rfft(np.asarray(x[:2048])))
    return float(spec[len(spec) // 4:].sum() / (spec.sum() + 1e-9))


def decay(x):
    """Peak frame energy in the first 60 ms / mean energy 120-250 ms later (big = dies fast)."""
    r = frame_rms(x)
    if len(r) < 26:
        return 0.0
    peak = r[:6].max()
    return float(peak / max(r[12:25].mean(), 1e-6))


def describe(snippet):
    """Everything the detector needs about one onset (1 s starting just before it)."""
    return {"transient": transient_features(snippet[:lock_sound.WIN_FRAMES * FRAME + NFFT]),
            "texture": texture_features(snippet[:TEXTURE_FRAMES * FRAME]),
            "hf": hf_ratio(snippet), "decay": decay(snippet),
            "clicks": count_clicks(snippet[:TEXTURE_FRAMES * FRAME])}


def score(action, desc, templates):
    if not templates:
        return 0.0
    if ACTIONS[action]["mode"] == "transient":
        return max(similarity(desc["transient"], np.asarray(t)) for t in templates)
    spec = np.asarray(desc["texture"]["spec"])
    best = 0.0
    for t in templates:
        s = float(np.dot(spec, np.asarray(t["spec"])))
        need = max(2, int(0.5 * t.get("clicks", 4)))
        if desc["texture"]["clicks"] < need:
            s *= 0.5                                         # one clack is not a rattle
        best = max(best, s)
    return best


def template_of(action, desc):
    if action in NOISE_ACTIONS:
        return {"spec": desc["spec"], "seconds": desc["seconds"]}
    return desc["transient"].tolist() if ACTIONS[action]["mode"] == "transient" else desc["texture"]


# ---- templates -----------------------------------------------------------------------
def load_templates(action):
    try:
        return json.loads(template_file(action).read_text())["templates"]
    except (OSError, ValueError, KeyError):
        pass
    if action == "lock":                                     # made by the older --calibrate-lock
        return [t.tolist() for t in lock_sound.load_templates()]
    return []


def save_templates(action, templates):
    DATA.mkdir(parents=True, exist_ok=True)
    mode = ACTIONS[action]["mode"] if action in ACTIONS else "noise"
    template_file(action).write_text(json.dumps({"action": action, "mode": mode,
                                                 "templates": templates, "made": time.strftime("%Y-%m-%d %H:%M")}))


def load_all():
    return {a: load_templates(a) for a in CAL_SOUNDS}


# ---- the detector --------------------------------------------------------------------
class SoundDetector:
    """feed(pcm_bytes) with 16-bit mono 16 kHz samples. For each onset calls
    on_detect(kind, score) for every kind it reports, and on_debug(info) with all scores."""

    def __init__(self, cfg=None, templates=None, on_detect=None, on_debug=None):
        self.c = config(cfg)
        self.templates = templates if templates is not None else load_all()
        self.on_detect, self.on_debug = on_detect, on_debug
        self.buf = np.zeros(0, dtype=np.float32)
        self.history = deque(maxlen=100)
        self.ring = deque(maxlen=lock_sound.PRE_FRAMES)
        self.pending = None
        self.quiet_until = 0.0
        self.t = 0.0
        self.onsets = []                                     # (t, description), for calibration
        self.on_onset = None                                 # on_onset(desc) -> True: calibrating, report nothing
        self.noise = NoiseTracker(self.c, self.templates, on_detect=lambda k, s: self.on_detect and self.on_detect(k, s),
                                  on_debug=lambda i: self.on_debug and self.on_debug(i))

    def calibrated(self, action):
        return bool(self.templates.get(action))

    def feed(self, pcm):
        if not pcm:
            return
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, x])
        while len(self.buf) >= FRAME:
            frame, self.buf = self.buf[:FRAME], self.buf[FRAME:]
            self._frame(frame)

    def feed_float(self, x):
        self.feed(lock_sound.to_pcm(x))

    def _frame(self, frame):
        self.t += FRAME / RATE
        if self.pending is not None:
            self.pending.append(frame)
            if len(self.pending) >= EVAL_FRAMES:
                self.evaluate(np.concatenate(self.pending))
                self.pending = None
        rms = float(np.sqrt(np.mean(frame ** 2)))
        bg = float(np.median(self.history)) if len(self.history) >= 10 else None
        if (self.pending is None and bg is not None and self.t >= self.quiet_until
                and rms >= self.c["min_rms"] and rms >= self.c["onset_ratio"] * max(bg, 1e-4)):
            self.pending = list(self.ring) + [frame]
            self.quiet_until = self.t + self.c["refractory_seconds"]
        self.history.append(rms)
        self.ring.append(frame)
        self.noise.frame(frame, rms, self.t)

    def classify(self, desc):
        """-> (list of (kind, score), {action: score})"""
        c = self.c
        scores = {a: round(score(a, desc, self.templates.get(a)), 3) for a in ACTIONS}
        out = []
        matched = [a for a in ACTIONS if self.calibrated(a) and scores[a] >= c[a]["match_score"]]
        if matched:
            best = max(matched, key=lambda a: scores[a] - c[a]["match_score"])
            out.append((ACTIONS[best]["kind"], scores[best]))
            return out, scores
        if self.calibrated("lock") and scores["lock"] >= c["lock"]["click_score"]:
            out.append(("click", scores["lock"]))
        rattle = desc["clicks"] >= c["pill"]["heuristic_clicks"] and desc["hf"] >= c["pill"]["heuristic_hf_ratio"]
        if not self.calibrated("pill") and c["pill"].get("uncalibrated_counts", True) and rattle:
            return out + [("pill_rattle", round(min(1.0, desc["clicks"] / 8.0), 3))], scores
        short = desc["decay"] >= c["setdown"]["decay_ratio"] and desc["clicks"] <= c["setdown"]["max_clicks"]
        if not self.calibrated("lock") and short and desc["hf"] >= c["lock"]["hf_ratio_for_click"]:
            out.append(("click", round(desc["hf"], 3)))
        if (not self.calibrated("setdown") and c["setdown"].get("uncalibrated_counts", True) and short
                and desc["hf"] >= c["setdown"]["min_hf_ratio"]):
            out.append(("impact", round(min(1.0, desc["decay"] / 20.0), 3)))
        return out, scores

    def evaluate(self, snippet):
        desc = describe(snippet)
        self.onsets.append((self.t, desc))
        del self.onsets[:-20]
        if self.on_onset and self.on_onset(desc):
            return [], {}
        kinds, scores = self.classify(desc)
        if self.on_debug:
            self.on_debug({"t": round(self.t, 2), "scores": scores, "clicks": desc["clicks"],
                           "hf": round(desc["hf"], 2), "decay": round(desc["decay"], 1),
                           "kinds": [k for k, _ in kinds]})
        for kind, s in kinds:
            if self.on_detect:
                self.on_detect(kind, s)
        return kinds, scores


# ---- synthetic sounds for tests and the pretend band ---------------------------------
def synth_rattle(seed=0, gain=0.5, seconds=0.9, clicks=9):
    """Pills rattling in a plastic bottle: many short bright clicks at uneven times."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    out = np.zeros(n)
    times = np.sort(rng.uniform(0.0, seconds - 0.05, clicks))
    times[0] = 0.0
    for t0 in times:
        i = int(t0 * RATE)
        m = int(0.03 * RATE)
        tt = np.arange(min(m, n - i)) / RATE
        f = rng.uniform(3500, 5500)
        out[i:i + len(tt)] += rng.uniform(0.4, 1.0) * np.exp(-180 * tt) * (
            np.sin(2 * np.pi * f * tt) + 0.6 * rng.standard_normal(len(tt)))
    return gain * out / np.max(np.abs(out))


def synth_setdown(seed=0, gain=0.6):
    """A phone set down on a wooden table: a dull knock plus a short hard tap, dying fast."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(0.25 * RATE)) / RATE
    x = np.exp(-70 * t) * (np.sin(2 * np.pi * 650 * t) + 0.6 * np.sin(2 * np.pi * 1300 * t))
    x += 0.5 * np.exp(-250 * t) * rng.standard_normal(len(t))
    return gain * x / np.max(np.abs(x))


def synth_speech(seed=0, gain=0.4, seconds=1.2):
    """Voice-like: a buzzing 140 Hz tone with harmonics and syllable rhythm (not an action)."""
    t = np.arange(int(seconds * RATE)) / RATE
    x = sum(np.sin(2 * np.pi * 140 * k * t) / k for k in range(1, 12))
    env = np.clip(np.sin(2 * np.pi * 3.5 * t), 0, 1) ** 0.5
    return gain * x * env / np.max(np.abs(x))


def synth(action, seed=0):
    return {"lock": lambda: lock_sound.synth_lock(seed=seed), "pill": lambda: synth_rattle(seed=seed),
            "setdown": lambda: synth_setdown(seed=seed), "flush": lambda: synth_flush(seed=seed),
            "paper": lambda: synth_paper(seed=seed)}[action]()


def with_background(sound, seconds=1.2, tail=1.3, noise=0.003, seed=2):
    """Quiet room noise before and after (long enough after for the 1 s analysis)."""
    rng = np.random.default_rng(seed)
    return np.concatenate([noise * rng.standard_normal(int(seconds * RATE)),
                           sound + noise * rng.standard_normal(len(sound)),
                           noise * rng.standard_normal(int(tail * RATE))])


# ---- longer noises: toilet flush and toilet paper ------------------------------------
# Not single clacks, so they don't go through the onset classifier. NoiseTracker cuts the
# band's sound into "noise events": a stretch clearly louder than the slow room background
# (short dips under gap_seconds are bridged). When one ends it is judged as a whole:
#   flush  "toilet_flush"  2.5-10 s of steady, flat (noise-like, not voice) broadband sound;
#                          the quieter tank refill after it is cut off and ignored
#   paper  "toilet_paper"  0.4-3 s of flat, bright, uneven rustle (roll spinning / tearing)
# Speech is rejected: it has gaps between syllables and a harmonic (not flat) spectrum.
# With 3 calibrated samples (memory/data/sound_flush.json / sound_paper.json) the event's
# average spectrum must match your own toilet / roll instead of the rough rule.
NOISE_ACTIONS = {"flush": "toilet_flush", "paper": "toilet_paper"}
CAL_SOUNDS = list(ACTIONS) + list(NOISE_ACTIONS)
DEFAULTS["noise"] = {"ratio": 4.0, "min_rms": 0.006, "gap_seconds": 0.3, "drop_ratio": 0.3, "bg_seconds": 8.0,
                     "max_store_seconds": 14.0}
DEFAULTS["flush"] = {"min_seconds": 2.5, "max_seconds": 10.0, "min_loud_fraction": 0.85, "min_flatness": 0.15,
                     "max_cv": 0.6, "min_hf": 0.15, "match_score": 0.85, "debounce_seconds": 60}
DEFAULTS["paper"] = {"min_seconds": 0.4, "max_seconds": 3.0, "min_loud_fraction": 0.5, "min_flatness": 0.15,
                     "min_hf": 0.25, "match_score": 0.85, "debounce_seconds": 20}


def flatness(x, n=512):
    """Median spectral flatness (geometric / arithmetic mean power, 100 Hz-7 kHz) over 32 ms
    windows: ~0.5 for hiss / rushing water, under 0.1 for a voice or a hum."""
    lo, hi = int(100 * n / RATE), int(7000 * n / RATE)
    w = np.hanning(n)
    vals = []
    for i in range(0, len(x) - n, n):
        p = np.abs(np.fft.rfft(x[i:i + n] * w))[lo:hi] ** 2 + 1e-12
        vals.append(float(np.exp(np.mean(np.log(p))) / np.mean(p)))
    return float(np.median(vals)) if vals else 0.0


def noise_features(x, rms_list):
    x = np.asarray(x, dtype=np.float32)
    r = np.asarray(rms_list) if len(rms_list) else frame_rms(x)
    spec_rows = []
    for i in range(0, max(1, len(x) - NFFT), NFFT):
        seg = x[i:i + NFFT]
        if len(seg) < NFFT:
            break
        p = np.abs(np.fft.rfft(seg * _window)) ** 2
        spec_rows.append([p[_edges[b]:_edges[b + 1]].sum() for b in range(len(_edges) - 1)])
    spec = np.log10(np.mean(spec_rows, axis=0) + 1e-9) if spec_rows else np.zeros(len(_edges) - 1)
    spec -= spec.mean()
    nrm = np.linalg.norm(spec)
    full = np.abs(np.fft.rfft(x[:min(len(x), RATE * 4)]))
    hf = float(full[len(full) // 8:].sum() / (full.sum() + 1e-9))          # share above 1 kHz
    loud = r[r >= 0.3 * np.median(r)] if len(r) else r
    return {"seconds": round(len(x) / RATE, 2), "flat": round(flatness(x), 3), "hf": round(hf, 3),
            "cv": round(float(np.std(loud) / (np.mean(loud) + 1e-9)), 3) if len(loud) else 9.0,
            "spec": (spec / nrm if nrm > 0 else spec).tolist()}


class NoiseTracker:
    """Fed every 10 ms frame by SoundDetector. Calls on_detect(kind, score) for a flush / paper
    and on_event(desc) -> True while calibrating (then nothing is reported)."""

    def __init__(self, c, templates, on_detect=None, on_debug=None):
        self.c, self.templates = c, templates            # templates: the SoundDetector's dict (shared)
        self.on_detect, self.on_debug, self.on_event = on_detect, on_debug, None
        self.bg = deque(maxlen=int(c["noise"]["bg_seconds"] * 100))
        self.ev = None
        self.wait_quiet = 0
        self.last = {}
        self.t = 0.0

    def frame(self, frame, rms, t):
        self.t = t
        n = self.c["noise"]
        bg = float(np.percentile(self.bg, 20)) if len(self.bg) >= 100 else None
        thr = max(n["min_rms"], n["ratio"] * max(bg or 0.0, 1e-4))
        if self.ev is None:
            if self.wait_quiet:                          # after an event: wait for the room to go quiet again
                self.wait_quiet = self.wait_quiet - 1 if rms < thr else 50
                return
            if bg is not None and rms >= thr:
                self.ev = {"frames": [frame], "rms": [rms], "loud": 1, "gap": 0, "long": False}
            else:
                self.bg.append(rms)
            return
        e = self.ev
        loud = rms >= thr
        if loud and len(e["rms"]) >= 100 and rms < n["drop_ratio"] * float(np.median(e["rms"][:300])):
            loud = False                                 # the quieter tank refill after the whoosh
        if len(e["frames"]) < n["max_store_seconds"] * 100:
            e["frames"].append(frame)
            e["rms"].append(rms)
        else:
            e["long"] = True
        if loud:
            e["loud"] += 1
            e["gap"] = 0
        else:
            e["gap"] += 1
        if e["gap"] >= n["gap_seconds"] * 100:
            self.ev = None
            self.wait_quiet = 50
            k = len(e["frames"]) - e["gap"]
            if not e["long"]:
                self.judge(np.concatenate(e["frames"][:k]), e["rms"][:k], e["loud"] / max(1, k))

    def classify(self, d):
        c = self.c
        out = []
        for action, kind in NOISE_ACTIONS.items():
            a = c[action]
            tpls = self.templates.get(action) or []
            if tpls:
                s = max(float(np.dot(d["spec"], t["spec"])) for t in tpls)
                secs = [t.get("seconds", d["seconds"]) for t in tpls]
                if s >= a["match_score"] and 0.5 * min(secs) <= d["seconds"] <= 2.0 * max(secs) + 1:
                    out.append((kind, round(s, 3)))
                continue
            if not (a["min_seconds"] <= d["seconds"] <= a["max_seconds"]):
                continue
            if d["loud_fraction"] < a["min_loud_fraction"] or d["flat"] < a["min_flatness"] or d["hf"] < a["min_hf"]:
                continue
            if action == "flush" and d["cv"] > a["max_cv"]:
                continue
            conf = min(1.0, 0.4 + d["flat"] + 0.2 * d["loud_fraction"])
            out.append((kind, round(conf, 3)))
        if len(out) > 1:                                 # both: the longer one wins
            out = [o for o in out if o[0] == ("toilet_flush" if d["seconds"] >= 2.5 else "toilet_paper")]
        return out

    def judge(self, x, rms, loud_fraction):
        d = noise_features(x, rms)
        d["loud_fraction"] = round(loud_fraction, 3)
        if self.on_event and self.on_event(d):
            return []
        kinds = self.classify(d)
        if self.on_debug:
            self.on_debug({"noise": {k: v for k, v in d.items() if k != "spec"}, "kinds": [k for k, _ in kinds]})
        out = []
        for kind, s in kinds:
            action = "flush" if kind == "toilet_flush" else "paper"
            if self.t - self.last.get(kind, -1e9) < self.c[action]["debounce_seconds"]:
                continue
            self.last[kind] = self.t
            out.append((kind, s))
            if self.on_detect:
                self.on_detect(kind, s)
        return out


def synth_flush(seed=0, gain=0.35, whoosh=5.0, refill=3.0):
    """A toilet flush: rushing water (broad noise, a bit of low rumble) that swells, holds and
    fades, then a quieter steady refill hiss."""
    rng = np.random.default_rng(seed)
    n = int(whoosh * RATE)
    t = np.arange(n) / RATE
    x = rng.standard_normal(n)
    x = np.convolve(x, np.ones(3) / 3, mode="same")                          # gentle low-pass
    x += 0.3 * np.sin(2 * np.pi * 90 * t) * rng.standard_normal(n) * 0.5
    env = np.minimum(1.0, t / 0.3) * np.minimum(1.0, (whoosh - t) / 0.8) * (1 + 0.1 * np.sin(2 * np.pi * 0.7 * t))
    x = gain * env * x / np.max(np.abs(x))
    r = 0.06 * gain * rng.standard_normal(int(refill * RATE))
    return np.concatenate([x, r])


def synth_paper(seed=0, gain=0.3, seconds=1.4):
    """Toilet paper pulled off the roll: bright uneven rustle with a few tears."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    x = rng.standard_normal(n)
    x = x - np.convolve(x, np.ones(8) / 8, mode="same")                     # brighter (high-pass)
    env = 0.6 + 0.4 * np.abs(np.sin(2 * np.pi * 2.3 * t + rng.uniform(0, 3)))
    env *= np.minimum(1.0, t / 0.05) * np.minimum(1.0, (seconds - t) / 0.1)
    return gain * env * x / np.max(np.abs(x))
