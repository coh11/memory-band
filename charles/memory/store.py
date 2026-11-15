"""SQLite store: events (what was heard / tapped), alerts (what we told people),
reminders (appointments and promises), facts (people and where things are)."""
import json
import sqlite3
import threading

from .config import DATA

_lock = threading.RLock()
_db = sqlite3.connect(str(DATA / "memory.db"), check_same_thread=False)
_db.row_factory = sqlite3.Row

_db.executescript("""
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, text TEXT, data TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, rule TEXT, key TEXT UNIQUE, level TEXT,
  audience TEXT, title TEXT, message TEXT, confirm TEXT, escalate_at REAL,
  escalated INTEGER DEFAULT 0, acked INTEGER DEFAULT 0, resolved INTEGER DEFAULT 0,
  resolved_how TEXT);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, due REAL, event_ts REAL, kind TEXT,
  text TEXT, fired INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS facts (
  kind TEXT, subject TEXT, value TEXT, ts REAL, PRIMARY KEY (kind, subject));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
""")
# alert lifecycle (added later: older databases get the columns here)
_have = {r[1] for r in _db.execute("PRAGMA table_info(alerts)").fetchall()}
for _col, _type in (("notified_at", "REAL"), ("escalated_at", "REAL"), ("resolved_at", "REAL"),
                    ("follow_up", "TEXT")):
    if _col not in _have:
        _db.execute("ALTER TABLE alerts ADD COLUMN %s %s" % (_col, _type))
_db.commit()


def _rows(sql, args=()):
    with _lock:
        return [dict(r) for r in _db.execute(sql, args).fetchall()]


def _exec(sql, args=()):
    with _lock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur.lastrowid


def _event(r):
    r["data"] = json.loads(r["data"] or "{}")
    return r


# ---- events -------------------------------------------------------------------------
def add_event(ts, kind, text="", data=None, source="band"):
    return _exec("INSERT INTO events (ts, kind, text, data, source) VALUES (?,?,?,?,?)",
                 (ts, kind, text, json.dumps(data or {}), source))


def events(since=None, kinds=None, limit=500):
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if since is not None:
        sql += " AND ts >= ?"
        args.append(since)
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args += list(kinds)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    return [_event(r) for r in _rows(sql, args)]


def last_event(kind, before=None, after=None):
    sql, args = "SELECT * FROM events WHERE kind = ?", [kind]
    if before is not None:
        sql += " AND ts <= ?"
        args.append(before)
    if after is not None:
        sql += " AND ts >= ?"
        args.append(after)
    r = _rows(sql + " ORDER BY ts DESC, id DESC LIMIT 1", args)
    return _event(r[0]) if r else None


# ---- alerts -------------------------------------------------------------------------
def alert_exists(key):
    return bool(_rows("SELECT id FROM alerts WHERE key = ?", (key,)))


def add_alert(ts, rule, key, level, audience, title, message, confirm=None, escalate_at=None):
    """Returns the new alert id, or None when an alert with this key already exists."""
    with _lock:
        if alert_exists(key):
            return None
        return _exec("INSERT INTO alerts (ts, rule, key, level, audience, title, message, confirm, escalate_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?)",
                     (ts, rule, key, level, audience, title, message, confirm, escalate_at))


def alerts(limit=200, open_only=False):
    sql = "SELECT * FROM alerts"
    if open_only:
        sql += " WHERE acked = 0 AND resolved = 0"
    return _rows(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (limit,))


def alert(alert_id):
    r = _rows("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    return r[0] if r else None


def update_alert(alert_id, **fields):
    if not fields:
        return
    sets = ", ".join("%s = ?" % k for k in fields)
    _exec("UPDATE alerts SET %s WHERE id = ?" % sets, list(fields.values()) + [alert_id])


def status(a):
    """open -> notified (push sent) -> resolved / escalated (and acked = "Got it" on the dashboard)."""
    if a.get("resolved"):
        return "resolved"
    if a.get("acked"):
        return "acked"
    if a.get("escalated"):
        return "escalated"
    return "notified" if a.get("notified_at") else "open"


def open_confirmable():
    """Open alerts a double-tap can answer, newest first."""
    return _rows("SELECT * FROM alerts WHERE resolved = 0 AND confirm IS NOT NULL ORDER BY ts DESC, id DESC")


# ---- reminders ----------------------------------------------------------------------
def add_reminder(created, due, event_ts, kind, text):
    return _exec("INSERT INTO reminders (created, due, event_ts, kind, text) VALUES (?,?,?,?,?)",
                 (created, due, event_ts, kind, text))


def reminders(include_fired=True):
    sql = "SELECT * FROM reminders" + ("" if include_fired else " WHERE fired = 0")
    return _rows(sql + " ORDER BY due ASC")


def mark_reminder(rid):
    _exec("UPDATE reminders SET fired = 1 WHERE id = ?", (rid,))


# ---- facts --------------------------------------------------------------------------
def set_fact(kind, subject, value, ts):
    _exec("INSERT OR REPLACE INTO facts (kind, subject, value, ts) VALUES (?,?,?,?)",
          (kind, subject.lower(), value, ts))


def facts(kind=None):
    if kind:
        return _rows("SELECT * FROM facts WHERE kind = ? ORDER BY ts DESC", (kind,))
    return _rows("SELECT * FROM facts ORDER BY ts DESC")


def get_fact(kind, subject):
    r = _rows("SELECT * FROM facts WHERE kind = ? AND subject = ?", (kind, subject.lower()))
    return r[0] if r else None


# ---- kv -----------------------------------------------------------------------------
def kv_get(k, default=None):
    r = _rows("SELECT v FROM kv WHERE k = ?", (k,))
    return json.loads(r[0]["v"]) if r else default


def kv_set(k, v):
    _exec("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (k, json.dumps(v)))


def reset_all():
    with _lock:
        for t in ("events", "alerts", "reminders", "facts"):
            _db.execute("DELETE FROM %s" % t)
        # keep what describes the hardware (band link, listener, calibration request), not the day
        _db.execute("DELETE FROM kv WHERE k NOT IN ('band', 'listener', 'calib')")
        _db.commit()
