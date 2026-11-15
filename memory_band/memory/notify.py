"""Push notifications through ntfy (free app for iPhone/Android, no account needed).
Install the ntfy app, subscribe to the user topic on the wearer's phone and the family
topic on the family's phones. NTFY_DRY_RUN=1 prints instead of sending."""
import logging
import threading

import requests

from .config import DRY_RUN, TOPICS

log = logging.getLogger("memory.notify")
PRIORITY = {"info": 3, "warn": 4, "urgent": 5, "ok": 3}
TAGS = {"info": ["information_source"], "warn": ["warning"], "urgent": ["rotating_light"], "ok": ["white_check_mark"]}
sent = []          # last messages (for tests and the dashboard), newest last


def _post(topic, title, message, level):
    body = {"topic": topic, "title": title, "message": message,
            "priority": PRIORITY.get(level, 3), "tags": TAGS.get(level, [])}
    sent.append({"topic": topic, "title": title, "message": message, "level": level})
    del sent[:-100]
    if DRY_RUN:
        print("[ntfy dry-run] -> %s | %s | %s" % (topic, title, message), flush=True)
        return
    try:
        r = requests.post(TOPICS["server"], json=body, timeout=8)
        if r.status_code >= 300:
            log.warning("ntfy answered %s for topic %s", r.status_code, topic)
    except requests.RequestException as e:
        log.warning("ntfy not reachable: %s", e.__class__.__name__)


def send(audience, title, message, level="info"):
    """audience: "user", "family" or "both". Never blocks the caller."""
    topics = []
    if audience in ("user", "both"):
        topics.append(TOPICS["user"])
    if audience in ("family", "both"):
        topics.append(TOPICS["family"])
    for t in topics:
        threading.Thread(target=_post, args=(t, title, message, level), daemon=True).start()
