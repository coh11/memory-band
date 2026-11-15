"""What happens when the band reports something, and the answers to questions."""
import logging
import re
from datetime import datetime

from . import clock, notify, rules, store, tagger
from .config import CONFIG, TOPICS, DRY_RUN

log = logging.getLogger("memory.service")
EVENT_TEXT = {
    "pill_taken": "Took pill", "door_locked": "Locked the door", "leaving_home": "Leaving home",
    "leaving_place": "Leaving", "stove_on": "Stove on", "stove_off": "Stove off", "help": "Asked for help",
    "fine": "Said they're fine", "item_picked_up": "Picked up item",
}


def handle_presence(state_name, reason="", rssi=None, now=None, source="band"):
    """From the band's Bluetooth signal to the home Mac (band_listener.py Presence)."""
    now = now if now is not None else clock.now()
    with rules.LOCK:
        was = (store.kv_get("presence") or {}).get("state", "home")
        store.kv_set("presence", {"state": state_name, "since": now, "reason": reason, "rssi": rssi})
        if state_name == "away" and was != "away":
            store.add_event(now, "leaving_home", "Left home (%s)" % (reason or "band signal"), {"rssi": rssi, "passive": True}, source)
        elif state_name == "home" and was == "away":
            store.add_event(now, "arrived_home", "Back home (%s)" % (reason or "band signal"), {"rssi": rssi}, source)
    rules.run_rules(now)
    return {"state": state_name, "was": was}


def _near(kinds, now, seconds):
    return [e for e in store.events(since=now - seconds, kinds=kinds) if e["ts"] <= now + 1]


def _lock(now, how, data, source):
    store.add_event(now, "door_locked", "Locked the door (%s)" % how, data, source)
    rules.resolve("door", how, now)


# ---- pill taken, from the band alone: rattle + cap twist / hand to mouth ----------------
PILL_CUES = ["cap_twist", "to_mouth"]


def _try_pill(now, trigger):
    """A pill bottle rattle together with a motion cue (cap twist or hand to mouth) within
    pills.passive.window_seconds = the pill was taken. A second one within same_dose_seconds
    is the same bottle handling (closing the cap), not a second dose."""
    p = CONFIG["pills"].get("passive", {})
    win = p.get("window_seconds", 30)
    rattle = _near(["pill_rattle"], now, win)
    cues = {e["kind"] for e in _near(PILL_CUES, now, win)}
    if not rattle or (len(cues) < p.get("need_motion_cues", 1) and not p.get("rattle_alone_counts")):
        return None
    if _near(["pill_taken"], now, p.get("same_dose_seconds", 60)):
        return "same_dose"
    how = "pill bottle rattle + " + (" + ".join(sorted(c.replace("_", " ") for c in cues)) or "no motion")
    store.add_event(now, "pill_taken", "Took pill (%s)" % how, {"passive": True, "cues": sorted(cues)}, "band")
    rules.resolve("pill", how, now)
    rules.check_double_dose(now)
    return "pill_taken"


# ---- a drink of water, from the band alone: hand to mouth that isn't a pill -------------
def _try_drink(now, data, source="band"):
    """Hand to mouth (a sip: the raise took sip_min..sip_max seconds) with no pill bottle
    rattle in the last not_pill_within_seconds = a drink. One per debounce_seconds."""
    h = CONFIG.get("hydration", {})
    if source == "band":
        secs = data.get("seconds")
        if secs is not None and not (h.get("sip_min_seconds", 0.3) <= secs <= h.get("sip_max_seconds", 4.0)):
            return None
        if _near(["pill_rattle", "pill_taken"], now, h.get("not_pill_within_seconds", 30)):
            return None
    if _near(["drink_noticed"], now, h.get("debounce_seconds", 180)):
        return "drink_already_counted"
    clink = bool(_near(["impact", "setdown_clack"], now, h.get("clink_within_seconds", 10)))
    store.add_event(now, "drink_noticed", "Drank water (hand to mouth%s)" % (" + glass clink" if clink else "")
                    if source == "band" else "Drank water (%s)" % source, dict(data, clink=clink), source)
    rules.resolve("water", "drink noticed", now)
    return "drink_noticed"


def handle_drink(now=None, source="demo"):
    now = now if now is not None else clock.now()
    with rules.LOCK:
        result = _try_drink(now, {}, source=source)
    rules.run_rules(now)
    return result


# ---- phone set down / picked up / left behind --------------------------------------------
def _items_cfg():
    return CONFIG["items"].get("passive", {})


def _open_placement(now):
    """The last item the band felt being set down, if it wasn't picked up since."""
    c = _items_cfg()
    for placed in store.events(since=now - c.get("lookback_minutes", 120) * 60, kinds=["item_placed"]):
        if not placed["data"].get("passive"):
            continue
        item = placed["data"].get("item")
        picked = [e for e in store.events(since=placed["ts"], kinds=["item_picked_up"]) if e["data"].get("item") == item]
        return None if picked else placed
    return None


def _try_place(now):
    c = _items_cfg()
    win = c.get("sound_motion_window_seconds", 4)
    sound = _near(c.get("sound_kinds", ["setdown_clack", "impact"]), now, win)
    motion = _near(["set_down"], now, win)
    if not sound or not motion:
        return None
    if _near(["item_placed"], now, win):
        return "already"
    item = c.get("item", "phone")
    old = _open_placement(now)
    if old:                                   # set down again: it must have been picked up in between
        store.add_event(now, "item_picked_up", "Picked up %s (set down again)" % item, {"item": item, "passive": True}, "band")
    store.add_event(now, "item_placed", "%s set down (clack + hand lowered)" % item.capitalize(),
                    {"item": item, "place": "down somewhere", "passive": True}, "band")
    return "item_placed"


def _pickup(now, data):
    placed = _open_placement(now)
    if not placed or now - placed["ts"] < _items_cfg().get("min_seconds_before_pickup", 1):
        return "nothing_down"
    item = placed["data"].get("item", "phone")
    store.add_event(now, "item_picked_up", "Picked up %s (hand lifted from where it was set down)" % item,
                    dict(data, item=item, passive=True), "band")
    rules.resolve("item:%s" % item, "picked up", now)
    return "item_picked_up"


def _walked(now, data):
    placed = _open_placement(now)
    if not placed:
        return "walking"
    if store.events(since=placed["ts"], kinds=["leaving_place"]):
        return "walking"                      # already noted for this placement
    store.add_event(now, "leaving_place", "Walked away (%s steps, %s)" % (data.get("steps", "?"), data.get("how", "band")),
                    dict(data, walked=True, passive=True), "band")
    return "walked_away"


GESTURE_TEXT = {"cap_twist": "Pill bottle cap twist", "to_mouth": "Hand to mouth", "set_down": "Hand lowered and rested",
                "pickup": "Hand lifted from the table", "walking": "Walking"}


def handle_gesture(kind, data=None, now=None, source="band"):
    """lock_twist: the band felt a key turn. It counts as "door locked" together with a
    click-ish sound within a few seconds (or alone when the mic is off, or when the config
    says twist_alone_counts). cap_twist / to_mouth: pill cues (with a rattle). set_down /
    pickup / walking: phone left behind. pill_shake: the old optional shake gesture."""
    now = now if now is not None else clock.now()
    data = data or {}
    d = CONFIG["door"]
    with rules.LOCK:
        if kind == "lock_twist":
            store.add_event(now, "lock_twist", "Key-turn twist (%s deg)" % data.get("angle", "?"), data, "twist")
            if _near(["click", "lock_sound"], now, d.get("twist_click_window_seconds", 5)):
                _lock(now, "key turn + click", data, "twist+sound")
                result = "door_locked"
            elif d.get("twist_alone_counts") or data.get("mic_on") is False:
                _lock(now, "key turn", data, "twist")
                result = "door_locked"
            else:
                result = "twist_only"
        elif kind in PILL_CUES:
            store.add_event(now, kind, GESTURE_TEXT[kind], data, "band")
            result = _try_pill(now, kind) or kind
            if kind == "to_mouth" and result == "to_mouth":
                result = _try_drink(now, data) or result
        elif kind == "set_down":
            store.add_event(now, kind, GESTURE_TEXT[kind], data, "band")
            result = _try_place(now) or kind
        elif kind == "pickup":
            result = _pickup(now, data)
        elif kind == "walking":
            store.kv_set("last_walk", now)
            result = _walked(now, data)
        elif kind == "pill_shake":
            w = rules.current_dose(now)
            since = w["start"] if w else now - 4 * 3600
            if store.events(since=since, kinds=["pill_taken"]):
                store.add_event(now, "pill_bottle", "Pill bottle shaken (already taken)", data, "shake")
                result = "ignored"
            else:
                store.add_event(now, "pill_taken", "Took pill (pill bottle shake)", data, "shake")
                rules.resolve("pill", "pill bottle", now)
                result = "pill_taken"
        else:
            store.add_event(now, "gesture", kind, data, source)
            result = "noted"
    rules.run_rules(now)
    return result


SIGNAL_TEXT = {"door_sound": "Door sound", "pill_rattle": "Pill bottle rattle", "setdown_clack": "Set-down clack (calibrated)",
               "impact": "Set-down clack", "toilet_paper": "Toilet paper sound"}


def handle_signal(kind, data=None, now=None, source="band"):
    """Sounds from the band mic: lock_sound (matches the calibrated lock) = door locked;
    click (click-ish) = door locked only with a key-turn twist nearby; pill_rattle = pill
    taken with a cap twist / hand to mouth nearby; setdown_clack / impact = phone set down
    with the hand lowering nearby; door_sound = a hint."""
    now = now if now is not None else clock.now()
    data = data or {}
    d = CONFIG["door"]
    result = "noted"
    with rules.LOCK:
        if kind == "lock_sound":
            store.add_event(now, "lock_sound", "Lock sound (match %s)" % data.get("value"), data, "mic")
            _lock(now, "lock sound", data, "sound")
            result = "door_locked"
        elif kind == "click":
            store.add_event(now, "click", "Click sound", data, "mic")
            if _near(["lock_twist"], now, d.get("twist_click_window_seconds", 5)):
                _lock(now, "key turn + click", data, "twist+sound")
                result = "door_locked"
        elif kind == "pill_rattle":
            store.add_event(now, kind, SIGNAL_TEXT[kind], data, "mic")
            result = _try_pill(now, kind) or kind
        elif kind == "toilet_flush":
            gap = float(CONFIG.get("bathroom", {}).get("min_flush_gap_seconds", 60))
            if store.events(since=now - gap, kinds=["toilet_flush"]):
                result = "ignored (flush already counted)"
            else:
                store.add_event(now, "toilet_flush", "Toilet flushed", data, "mic" if source == "band" else source)
                rules.resolve("flush", "flush heard", now)
                rules.resolve("bathroom", "flush heard", now)
                result = "toilet_flush"
        elif kind in ("not_worn", "worn"):
            was = (store.kv_get("wear") or {}).get("state")
            if kind != was:
                still = float(data.get("value") or 0)
                since = now - still if kind == "not_worn" else now
                store.kv_set("wear", {"state": kind, "since": since})
                store.add_event(now, "band_off_wrist" if kind == "not_worn" else "band_on_wrist",
                                "Band not on the wrist (dead still since %s)" % clock.hhmm(since) if kind == "not_worn"
                                else "Band back on the wrist", data, source)
                if kind == "worn":
                    rules.resolve("offwrist", "band back on", now)
            result = kind
        elif kind == "toilet_paper":
            store.add_event(now, kind, SIGNAL_TEXT[kind], data, "mic" if source == "band" else source)
            result = kind
        elif kind in ("setdown_clack", "impact"):
            store.add_event(now, kind, SIGNAL_TEXT[kind], data, "mic")
            result = _try_place(now) or kind
        else:
            store.add_event(now, kind, SIGNAL_TEXT.get(kind, kind), data, source)
    rules.run_rules(now)
    return result


def _when(ts):
    d = datetime.fromtimestamp(ts)
    today = clock.now_dt().date()
    if d.date() == today:
        return clock.hhmm(ts)
    return d.strftime("%a %b %-d, ") + clock.hhmm(ts)


def answer_where(item):
    item = tagger.item_name(item)
    f = store.get_fact("location", item)
    if not f:
        return "I don't know where your %s is. Next time, say where you put it." % item
    return "Your %s %s %s (you said so at %s)." % (item, "are" if item.endswith("s") else "is", f["value"], _when(f["ts"]))


def answer_who(name):
    f = store.get_fact("person", name)
    if not f:
        return "I don't know %s yet." % name
    return "%s is your %s (you met at %s)." % (name, f["value"], _when(f["ts"]))


def answer_pill(now):
    w = rules.current_dose(now)
    since = w["start"] if w else now - 12 * 3600
    taken = store.events(since=since, kinds=["pill_taken"])
    if taken:
        return "Yes. You took your %s at %s. Don't take another." % (w["name"] if w else "pill", clock.hhmm(taken[0]["ts"]))
    return "Not yet. You haven't taken your %s." % (w["name"] if w else "pill") + (" It's due at %s." % clock.hhmm(w["at"]) if w else "")


def answer_door(now):
    locked = store.last_event("door_locked", before=now, after=now - 12 * 3600)
    if locked:
        return "Yes, the door was locked at %s." % clock.hhmm(locked["ts"])
    return "I didn't notice the door being locked today. Please go and check it."


def ask(question, now=None):
    """The dashboard's ask box: "where are my keys", "who is Tom", "did I take my pill"."""
    now = now if now is not None else clock.now()
    tags = tagger.keyword_tags(question if question.strip().endswith("?") else question.strip() + "?",
                               datetime.fromtimestamp(now))
    for t in tags:
        if t["kind"] == "where_query":
            return answer_where(t["item"])
        if t["kind"] == "who_query":
            return answer_who(t["name"])
        if t["kind"] == "pill_question":
            return answer_pill(now)
        if t["kind"] == "door_question":
            return answer_door(now)
    low = question.lower()
    for f in store.facts("location"):
        if f["subject"] in low:
            return answer_where(f["subject"])
    for f in store.facts("person"):
        if f["subject"] in low:
            return answer_who(f["subject"].title())
    if re.search(r"\b(pill|medicine|meds)\b", low):
        return answer_pill(now)
    if "door" in low:
        return answer_door(now)
    if re.search(r"\b(appointments?|reminders?|today|plans?|schedule|coming up)\b", low):
        ups = store.reminders(include_fired=False)
        if not ups:
            return "Nothing coming up."
        return "Coming up: " + "; ".join("%s (%s)" % (r["text"], _when(r["event_ts"])) for r in ups[:5])
    return "I couldn't answer that. Try \"where are my keys\" or \"who is Tom\"."


def handle_transcript(text, now=None, source="band"):
    """Optional speech path: one sentence -> tags -> events -> rules."""
    now = now if now is not None else clock.now()
    text = (text or "").strip()
    if not text:
        return {"tags": [], "replies": []}
    tags, used = tagger.tag(text, datetime.fromtimestamp(now))
    replies = []
    with rules.LOCK:
        store.add_event(now, "heard", text, {"tags": [t["kind"] for t in tags], "engine": used}, source)
        for t in tags:
            k = t["kind"]
            data = {x: y for x, y in t.items() if x != "kind"}
            if k == "question":
                store.add_event(now, "question", text, data, source)
            elif k == "pill_question":
                replies.append(answer_pill(now))
            elif k == "door_question":
                replies.append(answer_door(now))
            elif k == "where_query":
                replies.append(answer_where(t["item"]))
            elif k == "who_query":
                replies.append(answer_who(t["name"]))
            elif k == "person":
                store.set_fact("person", t["name"], t.get("relation") or "acquaintance", now)
                store.add_event(now, "person", "Met %s (%s)" % (t["name"], t.get("relation") or "?"), data, source)
            elif k == "item_placed":
                store.set_fact("location", t["item"], t.get("place") or "?", now)
                store.add_event(now, "item_placed", "%s: %s" % (t["item"], t.get("place") or "?"), data, source)
            elif k in ("appointment", "promise"):
                due = float(t["due"])
                lead = CONFIG["reminders"]["appointment_lead_minutes"] * 60 if k == "appointment" else 0
                remind = max(now, due - lead)
                store.add_reminder(now, remind, due, k, t.get("what") or text)
                store.add_event(now, k, "%s (%s)" % (t.get("what") or text, _when(due)), data, source)
            elif k == "leaving_home":
                store.add_event(now, k, "Said they're leaving", data, source)
            else:
                store.add_event(now, k, EVENT_TEXT.get(k, k), data, source)
                if k == "pill_taken":
                    rules.resolve("pill", "said", now)
                    rules.check_double_dose(now)
                elif k == "door_locked":
                    rules.resolve("door", "said", now)
                elif k == "stove_off":
                    rules.resolve("stove", "said", now)
                elif k == "item_picked_up":
                    rules.resolve("item:%s" % t.get("item"), "said", now)
                elif k == "fine":
                    if rules.cancel_fall(now, "said I'm fine"):
                        replies.append("Fall alarm cancelled. Glad you're OK.")
                elif k == "help":
                    rules.raise_alert("help", "help:%d" % int(now), "urgent", "both", "HELP",
                                      "%s asked for help: \"%s\" (%s). Please call them now." % (rules.WHO, text, clock.hhmm(now)),
                                      now=now)
    for r in replies:                                   # questions answered on the wearer's phone
        notify.send("user", "Memory Band", r, "info")
    rules.run_rules(now)
    return {"tags": tags, "engine": used, "replies": replies}


def handle_tap(kind, now=None, source="band"):
    now = now if now is not None else clock.now()
    if kind == "double-tap":
        reply = rules.double_tap(now, source)
        rules.run_rules(now)
        return reply
    store.add_event(now, "tap", "Tap on band", {"kind": kind}, source)
    return "noted"


def handle_fall(now=None, source="band"):
    now = now if now is not None else clock.now()
    with rules.LOCK:
        if not store.kv_get("fall"):
            rules.start_fall(now, source)
    return store.kv_get("fall")


def _alert_view(a):
    v = dict(a, time=_when(a["ts"]), status=store.status(a))
    if a.get("resolved_at"):
        v["resolved_time"] = _when(a["resolved_at"])
        v["time_to_fix"] = rules._ago(a["resolved_at"] - a["ts"])
        v["time_to_fix_seconds"] = round(a["resolved_at"] - a["ts"])
    if a.get("escalated_at"):
        v["escalated_time"] = _when(a["escalated_at"])
    return v


def _phone_state(now):
    placed = _open_placement(now)
    return {"down_at": clock.hhmm(placed["ts"]) if placed else None,
            "item": placed["data"].get("item") if placed else None}


def _water_state(now):
    d = rules.drinks_today(now)
    return {"drinks_today": len(d), "last_at": clock.hhmm(d[0]["ts"]) if d else None,
            "every_minutes": round(rules.hydration_interval() / 60.0, 1)}


def _bathroom_state(now):
    fl = rules.flushes_today(now)
    return {"flushes_today": len(fl), "last_at": clock.hhmm(fl[0]["ts"]) if fl else None,
            "check_by": CONFIG.get("bathroom", {}).get("no_flush_by", "12:00")}


def state():
    now = clock.now()
    today_start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    last_lock = store.last_event("door_locked", before=now, after=today_start)
    presence = store.kv_get("presence", {}) or {}
    last_leave = store.last_event("leaving_home", before=now, after=today_start)
    stove_on = store.last_event("stove_on", before=now, after=today_start)
    stove_off = store.last_event("stove_off", before=now, after=stove_on["ts"]) if stove_on else None
    fall = store.kv_get("fall")
    return {
        "now": now,
        "now_text": clock.now_dt().strftime("%A %b %-d, ") + clock.hhmm(now),
        "demo_clock": clock.is_shifted(),
        "person": rules.WHO,
        "tagger": tagger.engine(),
        "ntfy": {"server": TOPICS["server"], "user_topic": TOPICS["user"], "family_topic": TOPICS["family"], "dry_run": DRY_RUN},
        "band": store.kv_get("band", {}),
        "pills": [{"name": p["name"], "time": p["time"], "status": p["status"],
                   "taken_at": [clock.hhmm(t) for t in p["taken_at"]]} for p in rules.pills_today(now)],
        "presence": {"state": presence.get("state", "home"), "since": clock.hhmm(presence["since"]) if presence.get("since") else None,
                     "rssi": presence.get("rssi")},
        "door": {"locked_at": clock.hhmm(last_lock["ts"]) if last_lock else None,
                 "how": (last_lock["source"] if last_lock else None),
                 "left_at": clock.hhmm(last_leave["ts"]) if last_leave else None,
                 "ok": bool(last_lock) and (not last_leave or last_lock["ts"] >= last_leave["ts"] - 15 * 60)},
        "stove": {"on": bool(stove_on and not stove_off), "since": clock.hhmm(stove_on["ts"]) if stove_on and not stove_off else None},
        "phone": _phone_state(now),
        "bathroom": _bathroom_state(now),
        "water": _water_state(now),
        "wear": {"state": (store.kv_get("wear") or {}).get("state") or "worn",
                 "since": clock.hhmm((store.kv_get("wear") or {})["since"]) if (store.kv_get("wear") or {}).get("since") else None},
        "fall": {"active": bool(fall), "seconds_left": max(0, int(fall["deadline"] - now)) if fall else 0},
        "alerts": [_alert_view(a) for a in store.alerts(limit=50)],
        "calibration": store.kv_get("calib"),
        "listener": store.kv_get("listener"),
        "timeline": [dict(e, time=_when(e["ts"])) for e in store.events(since=today_start, limit=200)
                     if e["kind"] != "question"],
        "reminders": [dict(r, when=_when(r["event_ts"]), remind_at=_when(r["due"])) for r in store.reminders()],
        "people": [{"name": f["subject"].title(), "relation": f["value"], "time": _when(f["ts"])} for f in store.facts("person")],
        "places": [{"item": f["subject"], "place": f["value"], "time": _when(f["ts"])} for f in store.facts("location")],
    }
