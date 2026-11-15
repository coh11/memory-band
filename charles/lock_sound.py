"""Hear the front door being locked: a lock / deadbolt "clack" picked up by the band's mic.

How: the band's Opus sound is decoded to 16 kHz samples (the kit's PcmDecoder). Every 10 ms
frame's energy is compared with the background; a sudden jump is an "onset". Around each
onset we take 250 ms of sound and turn it into a small spectrogram (24 log-spaced frequency
bands x 25 frames). Calibration stores 3 of these from YOUR lock; afterwards each onset is
compared with them (cosine similarity, allowing a small time shift).
  score >= match_score       -> "lock_sound" (counts as the door being locked)
  score >= click_score       -> "click" (click-ish; counts only together with a wrist twist)
Numpy only. Limits: other sharp clicks (cabinet doors, a dropped spoon) can look similar;
a lock far from the wrist (other hand) is quieter; noisy streets mask it. Calibrate at your
own door with the band on the wrist you lock with."""
import json
import time
from collections import deque
from pathlib import Path

import numpy as np

RATE = 16000
FRAME = 160                      # 10 ms
WIN_FRAMES = 25                  # 250 ms per snippet
PRE_FRAMES = 2                   # start 20 ms before the onset
NFFT = 512
BANDS = 24
DATA = Path(__file__).resolve().parent / "memory" / "data"
TEMPLATE_FILE = DATA / "lock_sound.json"

_edges = np.unique(np.geomspace(4, NFFT // 2, BANDS + 1).astype(int))   # 125 Hz .. 8 kHz, log-spaced
_window = np.hanning(NFFT)


def features(snippet):
    """250 ms of float samples -> normalised (frames x bands) log spectrogram."""
    x = np.asarray(snippet, dtype=np.float32)
    need = WIN_FRAMES * FRAME + NFFT
    if len(x) < need:
        x = np.pad(x, (0, need - len(x)))
    rows = []
    for i in range(WIN_FRAMES):
        seg = x[i * FRAME:i * FRAME + NFFT] * _window
        p = np.abs(np.fft.rfft(seg)) ** 2
        rows.append([p[_edges[b]:_edges[b + 1]].sum() for b in range(len(_edges) - 1)])
    f = np.log10(np.asarray(rows) + 1e-9)
    f -= f.mean()
    n = np.linalg.norm(f)
    return f / n if n > 0 else f


def similarity(a, b, max_shift=3):
    """Best cosine similarity of two feature matrices, shifting one by up to max_shift frames."""
    best = -1.0
    for s in range(-max_shift, max_shift + 1):
        if s >= 0:
            x, y = a[s:], b[:len(b) - s]
        else:
            x, y = a[:len(a) + s], b[-s:]
        nx, ny = np.linalg.norm(x), np.linalg.norm(y)
        if nx > 0 and ny > 0:
            best = max(best, float((x * y).sum() / (nx * ny)))
    return best


def load_templates():
    try:
        d = json.loads(TEMPLATE_FILE.read_text())
        return [np.asarray(t) for t in d["templates"]]
    except (OSError, ValueError, KeyError):
        return []


def save_templates(feats):
    DATA.mkdir(parents=True, exist_ok=True)
    TEMPLATE_FILE.write_text(json.dumps({"templates": [f.tolist() for f in feats],
                                         "made": time.strftime("%Y-%m-%d %H:%M")}))


class LockSoundDetector:
    """feed(pcm_bytes) with 16-bit mono 16 kHz samples. Calls on_onset(snippet_features, score,
    kind) where kind is "lock_sound", "click" or None (an onset that matched nothing)."""

    def __init__(self, cfg=None, templates=None, on_detect=None):
        c = dict(onset_ratio=6.0, min_rms=0.01, match_score=0.80, click_score=0.60,
                 refractory_seconds=1.0, hf_ratio_for_click=0.35)
        c.update(cfg or {})
        self.c = c
        self.templates = templates if templates is not None else load_templates()
        self.on_detect = on_detect
        self.buf = np.zeros(0, dtype=np.float32)
        self.history = deque(maxlen=100)          # last 1 s of frame energies (background)
        self.pending = None                       # onset waiting for the rest of its 250 ms
        self.ring = deque(maxlen=PRE_FRAMES)      # frames just before the onset
        self.quiet_until = 0.0
        self.t = 0.0                              # seconds of sound seen
        self.onsets = []                          # (t, features) of every onset, for calibration

    def feed(self, pcm):
        if not pcm:
            return
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, x])
        while len(self.buf) >= FRAME:
            frame, self.buf = self.buf[:FRAME], self.buf[FRAME:]
            self._frame(frame)

    def feed_float(self, x):
        self.feed(to_pcm(x))

    def _frame(self, frame):
        self.t += FRAME / RATE
        if self.pending is not None:
            self.pending.append(frame)
            if len(self.pending) >= WIN_FRAMES + NFFT // FRAME + 1:
                self._evaluate(np.concatenate(self.pending))
                self.pending = None
        rms = float(np.sqrt(np.mean(frame ** 2)))
        bg = float(np.median(self.history)) if len(self.history) >= 10 else None
        if (self.pending is None and bg is not None and self.t >= self.quiet_until
                and rms >= self.c["min_rms"] and rms >= self.c["onset_ratio"] * max(bg, 1e-4)):
            self.pending = list(self.ring) + [frame]
            self.quiet_until = self.t + self.c["refractory_seconds"]
        self.history.append(rms)
        self.ring.append(frame)

    def _evaluate(self, snippet):
        f = features(snippet)
        self.onsets.append((self.t, f))
        del self.onsets[:-20]
        score = max((similarity(f, tpl) for tpl in self.templates), default=0.0)
        if self.templates:
            kind = "lock_sound" if score >= self.c["match_score"] else "click" if score >= self.c["click_score"] else None
        else:
            # Not calibrated: only "click-ish" (short, bright transient) can be reported.
            spec = np.abs(np.fft.rfft(snippet[:2048]))
            hf = float(spec[len(spec) // 4:].sum() / (spec.sum() + 1e-9))
            kind, score = ("click", hf) if hf >= self.c["hf_ratio_for_click"] else (None, hf)
        if self.on_detect:
            self.on_detect(kind, round(score, 3))
        return kind, score


def to_pcm(x):
    """Float samples (-1..1) -> 16-bit little-endian bytes."""
    return (np.clip(np.asarray(x), -1, 1) * 32767).astype("<i2").tobytes()


# ---- synthetic sounds for tests and the pretend band ---------------------------------
def synth_lock(seed=0, gain=0.6):
    """A two-part metallic clack (bolt slide + latch), 0.3 s."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(0.3 * RATE)) / RATE
    out = np.zeros_like(t)
    for start, freq, decay in ((0.0, 3200, 60), (0.08, 2100, 45)):
        m = t >= start
        tt = t[m] - start
        out[m] += np.exp(-decay * tt) * (np.sin(2 * np.pi * freq * tt) + 0.5 * np.sin(2 * np.pi * 1.7 * freq * tt))
        out[m] += 0.3 * np.exp(-200 * tt) * rng.standard_normal(m.sum())
    return gain * out / np.max(np.abs(out))


def synth_other(kind="knock", seed=1, gain=0.6):
    """Not a lock: a low knock or a voice-like hum, for testing that it is not matched."""
    t = np.arange(int(0.3 * RATE)) / RATE
    if kind == "knock":
        x = np.exp(-25 * t) * np.sin(2 * np.pi * 180 * t)
    else:
        x = np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t))
    return gain * x / np.max(np.abs(x))


def with_background(sound, seconds=1.5, noise=0.003, seed=2):
    rng = np.random.default_rng(seed)
    pre = noise * rng.standard_normal(int(seconds * RATE))
    post = noise * rng.standard_normal(int(0.6 * RATE))
    return np.concatenate([pre, sound + noise * rng.standard_normal(len(sound)), post])
