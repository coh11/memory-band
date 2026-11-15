"""The proactive part: each rule is "something happened, and the thing that should follow
did not". run_rules() is called every ~30 s by a background loop, and right after every new
event, so alerts go out without anybody asking."""
import logging
import threading
from datetime import datetime

from . import clock, notify, store
from .config import CONFIG

log = logging.getLogger("memory.rules")
LOCK = threading.RLock()
WHO = CONFIG.get("person_name", "Mom")

# alert rule -> the event kind that settles it (spoken, or a double-tap on the band)
RESOLVED_BY = {"pill": "pill_taken", "door": "door_locked", "stove": "stove_off", "flush": "toilet_flush", "water": "drink_noticed"}


def _min(n):
    return float(n) * 60.0


def raise_alert(rule, key, level, audience, title, message, confirm=None, escalate_minutes=None, now=None):
    now = now if now is not None else clock.now()
    esc = now + _min(escalate_minutes) if escalate_minutes else None
    aid = store.add_alert(now, rule, key, level, audience, title, message, confirm, esc)
    if aid:
        log.info("ALERT %s -> %s: %s", rule, audience, message)
        notify.send(audience, title, message, level)
        store.update_alert(aid, notified_at=now)
    return aid


def _ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "%d s" % seconds
    if seconds < 5400:
        return "%d min" % round(seconds / 60.0)
    return "%.1f h" % (seconds / 3600.0)


def follow_up_text(a, how, now):
    """The "fixed" push for an alert: (title, message), or None when no push is wanted."""
    rule, t = a["rule"], clock.hhmm(now)
    took = " (%s after the alert)" % _ago(now - a["ts"])
    if rule == "door":
        return "Front door locked", "Front door locked at %s, thanks!%s" % (t, took)
    if rule == "pill":
        return "Pill taken", "Pill taken at %s, thanks!%s" % (t, took)
    if rule.startswith("item:"):
        item = rule.split(":", 1)[1]
        return "Got your %s" % item, "You picked up your %s at %s. Thanks!%s" % (item, t, took)
    if rule == "stove":
        return "Stove off", "Stove confirmed off at %s, thanks!%s" % (t, took)
    if rule == "water":
        return "Drink noticed", "Drink noticed at %s, thanks! 💧%s" % (t, took)
    if rule == "flush":
        return "Toilet flushed", "Toilet flushed at %s, thanks!%s" % (t, took)
    if rule == "bathroom":
        return "Bathroom visit", "%s's bathroom visit noticed at %s. All good." % (WHO, t)
    if rule in ("fall_check",) or how == "no answer":
        return None
    return "Done", "%s: done at %s.%s" % (a["title"], t, took)


def resolve(rule_prefix, how, now=None, follow_up=True):
    """Close every open alert of this rule (an event after the alert fixed it) and send ONE
    follow-up push: to the wearer, and to the family too when it had reached them."""
    now = now if now is not None else clock.now()
    closed = []
    for a in store.alerts(open_only=True):
        if a["rule"] == rule_prefix or a["rule"].startswith(rule_prefix + ":"):
            store.update_alert(a["id"], resolved=1, resolved_how=how, resolved_at=now)
            closed.append(a)
    if not closed or not follow_up or not CONFIG.get("resolution", {}).get("follow_up_push", True):
        return closed
    main = max(closed, key=lambda a: (a["escalated"], a["level"] == "warn", a["ts"]))
    text = follow_up_text(main, how, now)
    if text:
        family = any(a["escalated"] or a["audience"] in ("family", "both") for a in closed)
        aud = "family" if main["rule"] == "bathroom" else ("both" if family else "user")   # flushes are never pushed to the wearer
        notify.send(aud, text[0], text[1], "ok")
        for a in closed:
            store.update_alert(a["id"], follow_up=text[1])
        log.info("RESOLVED %s (%s): %s", rule_prefix, how, text[1])
    return closed


# ---- pills ---------------------------------------------------------------------------
def dose_windows(day):
    p = CONFIG["pills"]
    out = []
    for d in p["doses"]:
        h, m = (int(x) for x in d["time"].split(":"))
        at = datetime(day.year, day.month, day.day, h, m).timestamp()
        out.append({"name": d["name"], "time": d["time"], "at": at,
                    "start": at - _min(p["window_before_minutes"]), "end": at + _min(p["window_after_minutes"])})
    return out


def current_dose(ts):
    for w in dose_windows(datetime.fromtimestamp(ts).date()):
        if w["start"] <= ts <= w["end"]:
            return w
    return None


def pills_today(now):
    p = CONFIG["pills"]
    out = []
    for w in dose_windows(datetime.fromtimestamp(now).date()):
        taken = store.events(since=w["start"], kinds=["pill_taken"])
        taken = [e for e in taken if e["ts"] <= min(now, w["end"])]
        if taken:
            status = "double" if len(taken) > 1 else "taken"
        elif now < w["start"]:
            status = "later"
        elif now < w["at"] + _min(p["remind_after_minutes"]):
            status = "due"
        elif now <= w["end"]:
            status = "late"
        else:
            status = "missed"
        out.append(dict(w, status=status, taken_at=[e["ts"] for e in reversed(taken)]))
    return out


def check_pills(now):
    p = CONFIG["pills"]
    for w in pills_today(now):
        if w["status"] == "due" and now >= w["at"]:
            day = datetime.fromtimestamp(w["at"]).strftime("%Y-%m-%d")
            raise_alert("pill", "pilldue:%s:%s" % (day, w["name"]), "info", "user", "Time for your %s" % w["name"],
                        "It's %s: time for your %s. Double-tap your band when you've taken it." % (clock.hhmm(w["at"]), w["name"]),
                        confirm="pill_taken", now=now)
        if w["status"] == "late":
            day = datetime.fromtimestamp(w["at"]).strftime("%Y-%m-%d")
            raise_alert("pill", "pill:%s:%s" % (day, w["name"]), "warn", "user",
                        "Pill reminder",
                        "You haven't taken your %s yet (due %s). Take it now, then double-tap your band." % (w["name"], clock.hhmm(w["at"])),
                        confirm="pill_taken",
                        escalate_minutes=max(1, p["escalate_after_minutes"] - p["remind_after_minutes"]), now=now)


def check_double_dose(ts):
    w = current_dose(ts)
    start = w["start"] if w else ts - 4 * 3600
    taken = [e for e in store.events(since=start, kinds=["pill_taken"]) if e["ts"] <= ts]
    if len(taken) >= 2:
        times = ", ".join(clock.hhmm(e["ts"]) for e in reversed(taken))
        name = w["name"] if w else "pill"
        raise_alert("double_dose", "double:%s:%d" % (name, taken[0]["id"]), "urgent", "both",
                    "Possible double dose",
                    "The %s was taken %d times (%s). Don't take another one. Check with the pharmacist if unsure." % (
                        name, len(taken), times), now=ts)


# ---- door ----------------------------------------------------------------------------
def check_door(now):
    d = CONFIG["door"]
    for e in store.events(since=now - _min(d["grace_minutes"] + 120), kinds=["leaving_home"]):
        if now < e["ts"] + _min(d["grace_minutes"]):
            continue
        locked = store.last_event("door_locked", before=now, after=e["ts"] - _min(d["lookback_minutes"]))
        if locked or store.alert_exists("door:%d" % e["id"]):
            continue
        # one door alert per outing: the band's signal and a spoken "heading out" may both report it
        # (a new outing after the last door alert was fixed gets its own alert)
        if any(a["rule"] == "door" and a["ts"] > e["ts"] - 600
               and not (a["resolved"] and (a["resolved_at"] or 0) <= e["ts"]) for a in store.alerts(limit=20)):
            continue
        raise_alert("door", "door:%d" % e["id"], "warn", "user", "You didn't lock the door",
                        "You left home at %s but I didn't hear or feel the door being locked. Please go back and lock the front door "
                        "(or tap \"Got it\" if someone else is home)." % clock.hhmm(e["ts"]),
                        confirm="door_locked", escalate_minutes=d["escalate_after_minutes"], now=now)


# ---- items left behind ---------------------------------------------------------------
def check_items(now):
    lookback = _min(CONFIG["items"]["lookback_minutes"])
    for leave in store.events(since=now - 600, kinds=["leaving_place", "leaving_home"]):
        for placed in store.events(since=leave["ts"] - lookback, kinds=["item_placed"]):
            if placed["ts"] > leave["ts"]:
                continue
            if leave["data"].get("walked") and not placed["data"].get("passive"):
                continue                       # walking around at home: only items the band felt set down
            item = placed["data"].get("item", "item")
            picked = [p for p in store.events(since=placed["ts"], kinds=["item_picked_up"])
                      if p["data"].get("item") == item and p["ts"] <= now]
            if picked:
                continue
            place = placed["data"].get("place") or "somewhere"
            raise_alert("item:%s" % item, "item:%d" % placed["id"], "warn", "user", "Don't forget your %s" % item,
                        "Your %s %s still %s. You put %s there at %s." % (item, "are" if item.endswith("s") else "is", place,
                                                          "them" if item.endswith("s") else "it", clock.hhmm(placed["ts"]))
                        if place not in ("down somewhere", "somewhere") else
                        "Did you forget your %s? You put it down at %s." % (item, clock.hhmm(placed["ts"])),
                        confirm="item_picked_up:%s" % item, now=now)


# ---- stove ---------------------------------------------------------------------------
def check_stove_schedule(now):
    """Passive-first: a daily "is the stove off?" check at set times, confirmed by double-tap."""
    s = CONFIG["stove"]
    day = datetime.fromtimestamp(now)
    for hhmm in s.get("check_times", []):
        h, m = (int(x) for x in hhmm.split(":"))
        at = day.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
        if at <= now <= at + 3600:
            raise_alert("stove", "stovecheck:%s:%s" % (day.strftime("%Y-%m-%d"), hhmm), "warn", "user", "Stove check",
                        "Daily check: is the stove off? Take a look, then double-tap your band.",
                        confirm="stove_off", escalate_minutes=s["escalate_after_minutes"] + 20, now=now)


def check_stove(now):
    s = CONFIG["stove"]
    on = store.last_event("stove_on", before=now)
    if not on:
        return
    off = store.last_event("stove_off", before=now, after=on["ts"])
    if off or now < on["ts"] + _min(s["remind_after_minutes"]):
        return
    if now > on["ts"] + 6 * 3600:
        return
    raise_alert("stove", "stove:%d" % on["id"], "urgent", "user", "Stove still on?",
                "The stove has been on since %s. Please check it and turn it off, then double-tap your band." % clock.hhmm(on["ts"]),
                confirm="stove_off", escalate_minutes=s["escalate_after_minutes"], now=now)


# ---- bathroom ------------------------------------------------------------------------
def check_forgot_flush(now):
    """Toilet paper heard, then (the wearer walks away or paper_max_wait_seconds pass) and no
    flush within flush_wait_seconds after that -> a gentle push to the wearer."""
    b = CONFIG.get("bathroom", {})
    if not b.get("forgot_flush", True):
        return
    wait, grace = float(b.get("paper_max_wait_seconds", 90)), float(b.get("flush_wait_seconds", 60))
    if any(a["rule"] == "flush" for a in store.alerts(open_only=True)):
        return
    last_walk = float(store.kv_get("last_walk") or 0)
    for p in store.events(since=now - 3600, kinds=["toilet_paper"]):
        if p["ts"] > now or store.alert_exists("flush:%d" % p["id"]):
            continue
        if store.events(since=p["ts"], kinds=["toilet_flush"]):
            continue
        later = [e for e in store.events(since=p["ts"] + 0.001, kinds=["toilet_paper"]) if e["ts"] <= now]
        if later:
            continue                                     # same visit, a newer rustle: judge that one
        left = last_walk if p["ts"] < last_walk <= p["ts"] + wait else p["ts"] + wait
        if now < left + grace:
            continue
        raise_alert("flush", "flush:%d" % p["id"], "info", "user", "Toilet not flushed?",
                    "Looks like the toilet wasn't flushed (toilet paper at %s, no flush after). "
                    "Please go back and flush it." % clock.hhmm(p["ts"]), confirm="toilet_flush", now=now)
        return


# ---- hydration -----------------------------------------------------------------------
def _hm(day, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


def drinks_today(now):
    start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return [e for e in store.events(since=start, kinds=["drink_noticed"]) if e["ts"] <= now]


def hydration_interval():
    h = CONFIG.get("hydration", {})
    demo = float(store.kv_get("hydration_demo_seconds") or h.get("demo_interval_seconds") or 0)
    return demo if demo > 0 else float(h.get("interval_hours", 3)) * 3600


def water_reminder(now, since_ts, key):
    h = CONFIG.get("hydration", {})
    return raise_alert("water", key, "info", "user", "Time for a glass of water",
                       "Time for a glass of water 💧 No drink noticed since %s." % clock.hhmm(since_ts),
                       confirm="drink_noticed", escalate_minutes=h.get("family_after_minutes", 60), now=now)


def check_hydration(now):
    """No drink noticed for interval_hours in waking hours -> a push to the wearer (the family
    hears if it stays open family_after_minutes). Counting starts when the server first runs
    that day, so a late start doesn't nag straight away."""
    h = CONFIG.get("hydration", {})
    if not h.get("enabled", True):
        return
    day = datetime.fromtimestamp(now)
    wake, sleep = _hm(day, h.get("waking_start", "08:00")), _hm(day, h.get("waking_end", "21:00"))
    if not (wake <= now <= sleep):
        return
    mon = store.kv_get("hydration_monitor") or {}
    if mon.get("day") != day.strftime("%Y-%m-%d"):
        mon = {"day": day.strftime("%Y-%m-%d"), "since": now}
        store.kv_set("hydration_monitor", mon)
    if any(a["rule"] == "water" for a in store.alerts(open_only=True)):
        return
    drinks = drinks_today(now)
    ref = max([wake, float(mon["since"])] + ([drinks[0]["ts"]] if drinks else []))
    if now >= ref + hydration_interval():
        water_reminder(now, drinks[0]["ts"] if drinks else ref, "water:%d" % int(ref))


def flushes_today(now):
    start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return [e for e in store.events(since=start, kinds=["toilet_flush"]) if e["ts"] <= now]


def check_no_flush(now):
    """No flush heard by no_flush_by (default noon): one gentle note to the family that day.
    Only inside a few hours after that time, so starting the system late doesn't cry wolf."""
    b = CONFIG.get("bathroom", {})
    if not b.get("no_flush_check", True):
        return
    day = datetime.fromtimestamp(now)
    h, m = (int(x) for x in b.get("no_flush_by", "12:00").split(":"))
    at = day.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
    if not (at <= now <= at + 3600 * float(b.get("check_window_hours", 3))) or flushes_today(now):
        return
    raise_alert("bathroom", "bathroom:%s" % day.strftime("%Y-%m-%d"), "info", "family", "No bathroom visit noticed",
                "No bathroom visit noticed this morning (no toilet flush heard by %s). Nothing urgent; a gentle "
                "check-in call with %s may help." % (b.get("no_flush_by", "12:00"), WHO), now=now)


# ---- repeated questions --------------------------------------------------------------
def _similar(a, b):
    if a == b:
        return True
    sa, sb = set(a.split()), set(b.split())
    return bool(sa and sb) and len(sa & sb) / len(sa | sb) >= 0.6


def check_repeats(now):
    r = CONFIG["repeated_questions"]
    qs = list(reversed(store.events(since=now - _min(r["window_minutes"]), kinds=["question"])))
    for i, q in enumerate(qs):
        norm = q["data"].get("norm", "")
        if not norm:
            continue
        same = [x for x in qs[i:] if _similar(norm, x["data"].get("norm", ""))]
        if len(same) >= r["count"]:
            raise_alert("repeat", "repeat:%d" % q["id"], "info", "family", "Repeated question",
                        "%s asked \"%s\" %d times in the last %d minutes (%s). No action needed; a gentle check-in call may help." % (
                            WHO, q["text"].strip(), len(same), r["window_minutes"], ", ".join(clock.hhmm(x["ts"]) for x in same)),
                        now=now)
            return


# ---- reminders -----------------------------------------------------------------------
def check_reminders(now):
    for rem in store.reminders(include_fired=False):
        if rem["due"] <= now:
            store.mark_reminder(rem["id"])
            if rem["kind"] == "appointment":
                msg = "Coming up at %s: %s" % (clock.hhmm(rem["event_ts"]), rem["text"])
                title = "Appointment reminder"
            else:
                msg = "You said you would: %s" % rem["text"]
                title = "Reminder"
            raise_alert("reminder", "reminder:%d" % rem["id"], "info", "user", title, msg, now=now)


# ---- fall ----------------------------------------------------------------------------
def start_fall(now, source="band"):
    secs = CONFIG["fall"]["countdown_seconds"]
    store.add_event(now, "fall", "Band detected a fall", {"countdown": secs}, source)
    store.kv_set("fall", {"start": now, "deadline": now + secs})
    raise_alert("fall_check", "fall_check:%d" % int(now), "urgent", "user", "Are you OK?",
                "Your band felt a fall. Double-tap the band or say \"I'm fine\" within %d seconds, or we'll alert your family." % secs,
                confirm="fall_ok", now=now)


def cancel_fall(now, how):
    f = store.kv_get("fall")
    if not f:
        return False
    store.kv_set("fall", None)
    store.add_event(now, "fall_cancelled", "Fall alarm cancelled (%s)" % how, {}, how)
    resolve("fall_check", how, now, follow_up=False)
    return True


def check_fall(now):
    f = store.kv_get("fall")
    if f and now >= f["deadline"]:
        store.kv_set("fall", None)
        resolve("fall_check", "no answer", now, follow_up=False)
        raise_alert("fall", "fall:%d" % int(f["start"]), "urgent", "both", "FALL ALERT",
                    "%s's band detected a fall at %s and there was no response. Please call or check on them now." % (
                        WHO, clock.hhmm(f["start"])), now=now)


# ---- escalation ----------------------------------------------------------------------
def check_escalations(now):
    for a in store.alerts(open_only=True):
        if a["escalate_at"] and not a["escalated"] and now >= a["escalate_at"]:
            store.update_alert(a["id"], escalated=1, escalated_at=now)
            notify.send("family", "No response: " + a["title"],
                        "%s hasn't responded since %s: %s" % (WHO, clock.hhmm(a["ts"]), a["message"]),
                        "urgent" if a["level"] == "urgent" else "warn")
            log.info("ESCALATED %s to family", a["rule"])


CHECKS = [check_hydration, check_forgot_flush, check_no_flush, check_pills, check_door, check_items, check_stove, check_stove_schedule, check_repeats, check_reminders, check_fall, check_escalations]


def run_rules(now=None):
    now = now if now is not None else clock.now()
    with LOCK:
        for check in CHECKS:
            try:
                check(now)
            except Exception:                      # one broken rule never stops the others
                log.exception("rule %s failed", check.__name__)


def double_tap(now, source="band"):
    """Double-tap = "yes / I did it / I'm fine". Answers the newest open question."""
    with LOCK:
        store.add_event(now, "double_tap", "Double-tap on band", {}, source)
        if cancel_fall(now, "double-tap"):
            return "Fall alarm cancelled. Glad you're OK."
        for a in store.open_confirmable():
            confirm = a["confirm"]
            if confirm == "fall_ok":
                store.update_alert(a["id"], resolved=1, resolved_how="double-tap")
                continue
            if confirm.startswith("item_picked_up:"):
                item = confirm.split(":", 1)[1]
                store.add_event(now, "item_picked_up", "Confirmed by double-tap", {"item": item}, "tap")
            else:
                store.add_event(now, confirm, "Confirmed by double-tap", {}, "tap")
            resolve(a["rule"], "double-tap", now)
            for rule, kind in RESOLVED_BY.items():
                if kind == confirm:
                    resolve(rule, "double-tap", now)
            return "Confirmed: %s" % a["title"]
        return "Double-tap noted (nothing was waiting for an answer)."
