"""Settings: config.json (schedules, windows) + environment / .env (keys, topics)."""
import json
import os
import secrets
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
REPO = PKG.parent
DATA = Path(os.environ.get("MEMORY_DATA_DIR", str(HERE / "data")))
DATA.mkdir(parents=True, exist_ok=True)

try:  # one .env at the repo root; real environment variables win
    from dotenv import load_dotenv
    for p in (REPO / ".env", PKG / ".env"):
        if p.exists():
            load_dotenv(p, override=False)
except ImportError:
    pass


def load_config() -> dict:
    path = Path(os.environ.get("MEMORY_CONFIG", str(HERE / "config.json")))
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _topics() -> dict:
    """ntfy topics. From .env if set; otherwise random hard-to-guess names made once and
    kept in data/topics.json (ntfy topics are public to anyone who knows the name)."""
    path = DATA / "topics.json"
    saved = {}
    if path.exists():
        try:
            saved = json.loads(path.read_text())
        except ValueError:
            saved = {}
    if not saved.get("user") or not saved.get("family"):
        tag = secrets.token_hex(5)
        saved = {"user": "memoryband-me-" + tag, "family": "memoryband-family-" + tag}
        path.write_text(json.dumps(saved))
    return {
        "user": env("NTFY_USER_TOPIC", saved["user"]),
        "family": env("NTFY_FAMILY_TOPIC", saved["family"]),
        "server": env("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
    }


TOPICS = _topics()
DRY_RUN = env("NTFY_DRY_RUN", "0") not in ("", "0", "false", "no")
