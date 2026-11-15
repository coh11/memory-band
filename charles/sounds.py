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
    template_file(action).write_text(json.dumps({"action": action, "mode": ACTIONS[action]["mode"],
                                                 "templates": templates, "made": time.strftime("%Y-%m-%d %H:%M")}))


def load_all():
    return {a: load_templates(a) for a in ACTIONS}


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
            "setdown": lambda: synth_setdown(seed=seed)}[action]()


def with_background(sound, seconds=1.2, tail=1.3, noise=0.003, seed=2):
    """Quiet room noise before and after (long enough after for the 1 s analysis)."""
    rng = np.random.default_rng(seed)
    return np.concatenate([noise * rng.standard_normal(int(seconds * RATE)),
                           sound + noise * rng.standard_normal(len(sound)),
                           noise * rng.standard_normal(int(tail * RATE))])
