"""Turn one sentence the wearer said into events ("tags") the rules understand.

Order: Gemini (GEMINI_API_KEY) -> OpenAI (OPENAI_API_KEY) -> keyword rules (offline).
MEMORY_TAGGER=keywords|gemini|openai|auto forces one. Any API problem falls back to the
keyword rules; this module never raises.

A tag is a dict with "kind" plus fields:
  pill_taken | pill_question | door_locked | door_question | leaving_home | leaving_place
  item_placed {item, place} | item_picked_up {item} | stove_on | stove_off
  question {norm} | where_query {item} | who_query {name}
  person {name, relation} | appointment {what, due} | promise {what, due}
  help | fine
(due is a unix timestamp)
"""
import json
import logging
import re
from datetime import datetime, timedelta

import requests

from .config import CONFIG, env

log = logging.getLogger("memory.tagger")

KINDS = {"pill_taken", "pill_question", "door_locked", "door_question", "leaving_home", "leaving_place",
         "item_placed", "item_picked_up", "stove_on", "stove_off", "question", "where_query", "who_query",
         "person", "appointment", "promise", "help", "fine"}

ITEMS = CONFIG["items"]["tracked"]
_ITEM_RE = "|".join(sorted((re.escape(i) for i in ITEMS), key=len, reverse=True))
_ITEM_ALIASES = {"cellphone": "phone", "cell phone": "phone", "iphone": "phone", "key": "keys",
                 "car keys": "keys", "house keys": "keys", "reading glasses": "glasses", "handbag": "purse"}
_ITEM_RE = r"(?:car keys|house keys|cell phone|cellphone|iphone|reading glasses|handbag|%s|key)" % _ITEM_RE

RELATIONS = (r"neighbou?r|friend|daughter|son|granddaughter|grandson|grandkid|nurse|doctor|sister|brother|niece|"
             r"nephew|wife|husband|caregiver|aide|cousin|pastor|helper|mailman|niece|daughter-in-law|son-in-law")

WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def item_name(raw):
    raw = raw.lower().strip()
    return _ITEM_ALIASES.get(raw, raw)


def norm_question(text):
    t = re.sub(r"[^a-z0-9' ]", " ", text.lower())
    t = re.sub(r"\b(um+|uh+|so|hey|honey|dear|again|please|just|oh|well|today)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---- time words -> a datetime --------------------------------------------------------
def parse_when(text, now, default_hour=9):
    """'Tuesday at 3', 'tomorrow', 'at 1:30 today', 'in 20 minutes', 'at noon'. None if no time words."""
    t = text.lower()
    for w, n in WORD_NUM.items():
        t = re.sub(r"\bat %s\b" % w, "at %d" % n, t)
        t = re.sub(r"\b%s (o'?clock|thirty|pm|am|p\.m\.|a\.m\.)" % w, lambda m: "%d %s" % (n, m.group(1)), t)
    t = t.replace("thirty", ":30").replace(" :30", ":30")

    m = re.search(r"\bin (\d+|an?|half an) (minute|minutes|min|hour|hours)\b", t)
    if m:
        q = m.group(1)
        n = 1 if q in ("a", "an") else 0.5 if q == "half an" else int(q)
        return now + (timedelta(minutes=n) if m.group(2).startswith("min") else timedelta(hours=n))

    day = None
    if re.search(r"\b(today|this (morning|afternoon|evening))\b", t):
        day = now.date()
    if "tonight" in t:
        day = now.date()
    if "tomorrow" in t:
        day = (now + timedelta(days=1)).date()
    for i, wd in enumerate(WEEKDAYS):
        if re.search(r"\b%s\b" % wd, t):
            ahead = (i - now.weekday()) % 7
            day = (now + timedelta(days=ahead)).date()
            break

    hour = minute = None
    m = (re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)", t)
         or re.search(r"\b(?:at|by|around|for) (\d{1,2})(?::(\d{2}))?\b(?!\s*(?:minutes|hours|days|years))", t)
         or re.search(r"\b(\d{1,2})(?::(\d{2}))? o'?clock", t))
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) if m.lastindex and m.lastindex >= 3 else None) or ""
        ampm = ampm.replace(".", "").replace(" ", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif not ampm and 1 <= hour <= 7:
            hour += 12
        if hour > 23 or minute > 59:
            hour = minute = None
    elif "noon" in t or "lunchtime" in t:
        hour, minute = 12, 0
    elif "midnight" in t:
        hour, minute = 0, 0
    elif "tonight" in t or "this evening" in t or "in the evening" in t:
        hour, minute = 19, 0
    elif "afternoon" in t:
        hour, minute = 15, 0
    elif "morning" in t:
        hour, minute = 9, 0

    if day is None and hour is None:
        return None
    if hour is None:
        hour, minute = default_hour, 0
    if day is None:
        day = now.date()
        candidate = datetime(day.year, day.month, day.day, hour, minute)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    candidate = datetime(day.year, day.month, day.day, hour, minute)
    if candidate < now - timedelta(minutes=1) and any(re.search(r"\b%s\b" % wd, t) for wd in WEEKDAYS):
        candidate += timedelta(days=7)
    return candidate


# ---- the offline keyword tagger ------------------------------------------------------
NEG_FUTURE = r"\b(need to|have to|got to|gotta|should|forgot to|forget to|going to|gonna|will|remind me|don'?t|didn'?t|haven'?t|not yet)\b"
PILL = r"\b(pill|pills|medication|medications|medicine|meds|tablet|tablets|vitamins?|insulin|dose)\b"


def keyword_tags(text, now=None):
    now = now or datetime.now()
    t = text.strip()
    low = t.lower()
    tags = []
    is_q = t.endswith("?") or bool(re.match(r"^(did|do|have|has|is|are|was|were|what|where|when|who|which|how|can|could|should|will)\b", low))

    # pills
    if re.search(PILL, low):
        if is_q and re.search(r"\b(did|have) i\b", low):
            tags.append({"kind": "pill_question"})
        elif re.search(r"\b(took|taken|swallowed|had)\b", low) and not re.search(NEG_FUTURE, low):
            tags.append({"kind": "pill_taken"})
        elif re.search(r"\b(taking|take) (my|the|a|his|her)?\s*\w*\s*" + PILL[2:-2] + r"s?\b.*\b(now|right now)\b", low) \
                or re.search(r"\bhere (goes|go) (my|the)\b", low):
            tags.append({"kind": "pill_taken"})

    # door
    if re.search(r"\bdoor\b", low) or re.search(r"\blocked (up|it)\b", low):
        if is_q and re.search(r"\b(did|have) i\b.*\block", low):
            tags.append({"kind": "door_question"})
        elif re.search(r"\b(locked|locking|lock)\b", low) and not re.search(NEG_FUTURE, low) and not is_q:
            tags.append({"kind": "door_locked"})

    # stove
    if re.search(r"\b(stove|oven|burner|burners|cooktop|kettle)\b", low):
        if re.search(r"\b(off)\b", low) and not re.search(NEG_FUTURE, low) and not is_q:
            tags.append({"kind": "stove_off"})
        elif re.search(r"\b(on|turned on|turn on|turning on|lit|light|lighting|preheat\w*|heating)\b", low) and not is_q:
            tags.append({"kind": "stove_on"})
    elif re.search(r"\b(boiling|frying|simmering|making (some )?(eggs|soup|pasta|rice|tea|oatmeal)|cook(ing)? (some|the|my|dinner|lunch|breakfast))\b", low) and not is_q:
        tags.append({"kind": "stove_on"})

    # leaving
    if re.search(r"\b(head(ing)?|go(ing)?|step(ping)?) out\b|\bleaving (the house|home|now)\b|\bi'?m off\b|"
                 r"\boff to (the )?\w+|\bout the door\b|\bgoing to (the )?(store|market|pharmacy|park|bank|doctor|church|lunch|dinner)\b|"
                 r"\bsee you later\b", low) and not is_q:
        tags.append({"kind": "leaving_home"})
    if re.search(r"\bcheck,? please\b|\b(get|bring|have) (us )?the (check|bill)\b|\blet'?s go\b|\bready to go\b|\bbye\b|"
                 r"\bgoodbye\b|\blet'?s head (out|home)\b|\btime to go\b|\bthanks? (you )?for (lunch|dinner|breakfast|coffee)\b",
                 low):
        tags.append({"kind": "leaving_place"})

    # items: where things are
    m = re.search(r"\b(?:put|set|leave|left|placing|putting|setting|laid|lay|dropped|drop|hung|hang|stuck|keep)\b"
                  r"(?: down)? (?:my|the|his|her|our) (" + _ITEM_RE + r")(?: down)?"
                  r"(?: (on|in|at|under|next to|by|inside|behind|beside|into|onto) ((?:the |my |a )?[a-z' -]{2,40}))?", low)
    if m and not is_q:
        place = (m.group(3) or "").strip(" .,!")
        place = re.split(r"\b(?:so|and|because|while|for now|then)\b", place)[0].strip(" .,!")
        prep = {"into": "in", "onto": "on"}.get(m.group(2) or "", m.group(2) or "")
        if place and not re.match(r"(the|my|a) ", place):
            place = "the " + place
        tags.append({"kind": "item_placed", "item": item_name(m.group(1)),
                     "place": ("%s %s" % (prep, place)) if place else "down somewhere"})
    for m in re.finditer(r"\b(?:got|grab|grabbed|grabbing|have|picked up|pick up|took|take|taking|found)\b"
                         r" (?:my|the|his|her) (" + _ITEM_RE + r")\b", low):
        if not is_q and not re.search(r"\b(don'?t|didn'?t|forgot|where)\b", low):
            tags.append({"kind": "item_picked_up", "item": item_name(m.group(1))})

    # questions people repeat
    m = re.search(r"\bwhere (?:are|is|did i (?:put|leave)|'?s|are all) (?:my|the) (" + _ITEM_RE + r"|[a-z]+)\b", low)
    if m:
        tags.append({"kind": "where_query", "item": item_name(m.group(1))})
    m = re.search(r"\bwho(?: is|'s) ([A-Za-z]+)\b", t, re.I) or re.search(r"\bwhat'?s? (?:is )?my (" + RELATIONS + r")'?s name\b", low)
    if m and m.group(1).lower() not in ("that", "this", "there", "it", "he", "she", "calling"):
        tags.append({"kind": "who_query", "name": m.group(1)})
    if is_q:
        tags.append({"kind": "question", "norm": norm_question(t)})

    # people
    m = re.search(r"\b(?:this is|meet|that'?s|say hi to|here'?s|met|with) (?:my |our )?(" + RELATIONS + r")[, ]+([A-Z][a-z]+)\b", t, re.I)
    if m and m.group(2)[0].isupper():
        tags.append({"kind": "person", "name": m.group(2), "relation": m.group(1).lower()})
    else:
        m = re.search(r"\b([A-Z][a-z]+)(?: is|,) (?:my|our) (" + RELATIONS + r")\b", t)
        if m and m.group(1) not in ("This", "That", "He", "She", "It", "Who"):
            tags.append({"kind": "person", "name": m.group(1), "relation": m.group(2).lower()})

    # appointments and promises
    if not is_q:
        appt = re.search(r"\b(appointment|dentist|doctor|doctor's|checkup|check-up|physical|haircut|hair appointment|"
                         r"therapy|physio|lunch with|dinner with|coffee with|meeting|visit|bridge club|book club|church)\b", low)
        promise = re.search(r"\b(i'?ll|i will|i promise(?:d)? to|i need to|i have to|i've got to|i gotta|remind me to|"
                            r"i should|i must|don'?t let me forget to)\b\s+(.+)", t, re.I)
        when = parse_when(t, now, CONFIG["reminders"]["promise_default_hour"])
        if appt and when:
            tags.append({"kind": "appointment", "what": t.rstrip("."), "due": when.timestamp()})
        elif promise and not re.search(PILL, low) and not any(x["kind"] == "item_placed" for x in tags) \
                and re.match(r"(?:\w+ )?(call|phone|text|email|write|visit|pay|send|buy|pick up|bring|take|water|"
                                   r"feed|book|cancel|return|meet|see|go|mail|check|tell|ask|fix)\b", promise.group(2), re.I):
            if when is None:
                d = (now + timedelta(days=1)).date()
                if re.search(r"\b(today|later|tonight|this)\b", low):
                    d = now.date()
                h = CONFIG["reminders"]["promise_default_hour"]
                when = datetime(d.year, d.month, d.day, h, 0)
                if when <= now:
                    when = now + timedelta(hours=2)
            what = promise.group(2).strip().rstrip(".!")
            tags.append({"kind": "promise", "what": what[0].upper() + what[1:], "due": when.timestamp()})

    # safety words
    if re.search(r"\b(help me|i fell|i'?ve fallen|i have fallen|call (an )?ambulance|call 911|i can'?t get up)\b", low):
        tags.append({"kind": "help"})
    if re.search(r"\bi'?m (fine|okay|ok|alright|all right)\b|\bi am (fine|okay|ok|alright)\b", low):
        tags.append({"kind": "fine"})
    return tags


# ---- language models -----------------------------------------------------------------
PROMPT = """You tag one sentence said by an older adult wearing a memory wristband.
Current local time: {now} ({weekday}).
Return JSON: {{"tags": [ ... ]}}. Each tag has "kind", one of:
pill_taken (they say they just took their pill/medicine), pill_question (asking if they took it),
door_locked (they locked the front door), door_question (asking if they locked it),
leaving_home (they are leaving the house now), leaving_place (leaving a restaurant/shop/visit: "check please", "bye", "let's go"),
item_placed {{"item","place"}} (they put down/stored phone, wallet, keys, glasses, purse...; place with its preposition, e.g. "on the table", "in the kitchen drawer"),
item_picked_up {{"item"}}, stove_on, stove_off,
question {{"norm"}} (any question; norm = short lowercase normalized wording),
where_query {{"item"}} (asking where something is), who_query {{"name"}} (asking who someone is),
person {{"name","relation"}} (introducing someone, e.g. "this is my neighbor Tom"),
appointment {{"what","due"}} and promise {{"what","due"}} (due = local time "YYYY-MM-DDTHH:MM"; promise with no time = tomorrow 10:00),
help (asks for help / fell), fine (says they are fine).
Only things clearly said. Plans or questions are not events ("I need to take my pill" is not pill_taken).
Items are single lowercase words like "phone", "keys". Return {{"tags": []}} if nothing applies.
Sentence: {text}"""


def _clean(tags, now):
    out = []
    for tag in tags if isinstance(tags, list) else []:
        if not isinstance(tag, dict) or tag.get("kind") not in KINDS:
            continue
        tag = {k: v for k, v in tag.items() if isinstance(v, (str, int, float))}
        if "item" in tag:
            tag["item"] = item_name(str(tag["item"]))
        if tag["kind"] in ("appointment", "promise"):
            due = tag.get("due")
            try:
                tag["due"] = float(due) if isinstance(due, (int, float)) else datetime.fromisoformat(str(due)[:16]).timestamp()
            except ValueError:
                continue
            tag["what"] = str(tag.get("what") or "")[:200]
        if tag["kind"] == "question":
            tag["norm"] = norm_question(str(tag.get("norm") or ""))
        out.append(tag)
    return out


def _json_from(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?|```$", "", text).strip()
    return json.loads(text).get("tags", [])


def gemini_tags(text, now):
    key = env("GEMINI_API_KEY")
    model = env("GEMINI_MODEL", "gemini-2.5-flash")
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
    body = {"contents": [{"parts": [{"text": PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"), weekday=now.strftime("%A"), text=text)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError("Gemini status %s" % r.status_code)
    parts = r.json()["candidates"][0]["content"]["parts"]
    return _json_from("".join(p.get("text", "") for p in parts))


def openai_tags(text, now):
    key = env("OPENAI_API_KEY")
    model = env("OPENAI_MODEL", "gpt-6-luna")      # the kit's default model name (examples/python/ai.py)
    body = {"model": model, "store": False,
            "input": PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"), weekday=now.strftime("%A"), text=text),
            "text": {"format": {"type": "json_object"}}}
    r = requests.post("https://api.openai.com/v1/responses", json=body,
                      headers={"Authorization": "Bearer " + key}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError("OpenAI status %s" % r.status_code)
    data = r.json()
    out = data.get("output_text") or ""
    if not out:
        for item in data.get("output", []):
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    out += c.get("text", "")
    return _json_from(out)


def engine():
    choice = env("MEMORY_TAGGER", "auto").lower()
    if choice in ("keywords", "keyword", "offline"):
        return "keywords"
    if choice == "gemini" or (choice == "auto" and env("GEMINI_API_KEY")):
        return "gemini" if env("GEMINI_API_KEY") else "keywords"
    if choice == "openai" or (choice == "auto" and env("OPENAI_API_KEY")):
        return "openai" if env("OPENAI_API_KEY") else "keywords"
    return "keywords"


def tag(text, now=None):
    """Returns (tags, engine_used). Never raises."""
    now = now or datetime.now()
    use = engine()
    if use != "keywords":
        try:
            tags = _clean((gemini_tags if use == "gemini" else openai_tags)(text, now), now)
            return tags, use
        except Exception as e:                       # network, quota, bad JSON: fall back
            log.warning("%s tagger failed (%s); using keywords", use, e.__class__.__name__ + ": " + str(e)[:80])
    try:
        return keyword_tags(text, now), "keywords"
    except Exception as e:
        log.exception("keyword tagger failed: %s", e)
        return [], "none"
