"""One clock for the whole server. It can be moved forward for the demo (replay.py), so a
whole day of rules can be shown in two minutes. Everything uses clock.now()."""
import threading
import time
from datetime import datetime

_offset = 0.0
_lock = threading.Lock()


def now() -> float:
    return time.time() + _offset


def now_dt() -> datetime:
    return datetime.fromtimestamp(now())


def set_time(ts: float) -> None:
    global _offset
    with _lock:
        _offset = ts - time.time()


def reset() -> None:
    global _offset
    with _lock:
        _offset = 0.0


def is_shifted() -> bool:
    return abs(_offset) > 1


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%-I:%M %p")
