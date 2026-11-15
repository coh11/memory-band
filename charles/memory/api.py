"""HTTP API for Memory Band, as an APIRouter that charles/main.py includes under /api,
plus the background rules loop."""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import clock, rules, service, store
from .config import CONFIG, env

log = logging.getLogger("memory.api")
router = APIRouter(prefix="/api", tags=["memory-band"])
STATIC = Path(__file__).resolve().parent / "static"


class Transcript(BaseModel):
    text: str
    source: Optional[str] = "band"


class Tap(BaseModel):
    kind: str = "double-tap"
    source: Optional[str] = "band"


class Fall(BaseModel):
    source: Optional[str] = "band"


class Presence(BaseModel):
    state: str                         # "home" or "away"
    reason: Optional[str] = ""
    rssi: Optional[float] = None
    source: Optional[str] = "band"


class Gesture(BaseModel):
    kind: str                          # lock_twist, cap_twist, to_mouth, set_down, pickup, walking, pill_shake
    angle: Optional[float] = None
    axis: Optional[str] = None
    seconds: Optional[float] = None
    mic_on: Optional[bool] = None
    dz: Optional[float] = None         # metres the wrist went up (+) / down (-)
    tilt: Optional[float] = None       # degrees the forearm tilted
    steps: Optional[int] = None
    how: Optional[str] = None
    source: Optional[str] = "band"


class CalibrateStart(BaseModel):
    action: str                        # lock, twist, pill, setdown


class CalibrateProgress(BaseModel):
    id: str
    state: str                         # recording, sample, done, failed, default
    samples: int = 0
    message: Optional[str] = ""


class Signal(BaseModel):
    kind: str                          # "door_sound"
    value: Optional[float] = None
    source: Optional[str] = "band"


class Ask(BaseModel):
    question: str


class ClockSet(BaseModel):
    time: Optional[str] = None        # "HH:MM" today, or "YYYY-MM-DDTHH:MM"
    advance_minutes: Optional[float] = None
    reset: bool = False


class Band(BaseModel):
    connected: bool
    name: Optional[str] = None
    battery: Optional[int] = None
    note: Optional[str] = None


@router.post("/transcript")
def post_transcript(body: Transcript):
    return service.handle_transcript(body.text, source=body.source or "band")


@router.post("/tap")
def post_tap(body: Tap):
    return {"reply": service.handle_tap(body.kind, source=body.source or "band")}


@router.post("/fall")
def post_fall(body: Fall = Fall()):
    return {"fall": service.handle_fall(source=body.source or "band")}


@router.post("/presence")
def post_presence(body: Presence):
    if body.state not in ("home", "away"):
        raise HTTPException(400, "state is home or away")
    return service.handle_presence(body.state, body.reason or "", body.rssi, source=body.source or "band")


@router.post("/gesture")
def post_gesture(body: Gesture):
    data = {k: v for k, v in (("angle", body.angle), ("axis", body.axis), ("seconds", body.seconds),
                              ("mic_on", body.mic_on), ("dz", body.dz), ("tilt", body.tilt), ("steps", body.steps),
                              ("how", body.how)) if v is not None}
    return {"result": service.handle_gesture(body.kind, data, source=body.source or "band")}


@router.post("/signal")
def post_signal(body: Signal):
    return {"result": service.handle_signal(body.kind, {"value": body.value}, source=body.source or "band")}


async def tunnel_guard(request, call_next):
    """Requests that came through a tunnel (ngrok / cloudflared add X-Forwarded-For) must carry
    MEMORY_TOKEN as ?token= or the X-Memory-Token header. Requests from this Mac or the local
    Wi-Fi go straight through. Without MEMORY_TOKEN set, nothing is checked."""
    want = env("MEMORY_TOKEN")
    if want and (request.headers.get("x-forwarded-for") or request.headers.get("cf-connecting-ip")):
        got = request.query_params.get("token") or request.headers.get("x-memory-token")
        if got != want:
            return JSONResponse({"detail": "wrong or missing token"}, status_code=403)
    return await call_next(request)


def _check_token(token):
    want = env("MEMORY_TOKEN")
    if want and token != want:
        raise HTTPException(403, "wrong or missing token")


@router.api_route("/leaving", methods=["GET", "POST"])
def phone_leaving(token: Optional[str] = None, x_memory_token: Optional[str] = Header(None)):
    """Called by the iPhone Shortcuts automation "When I leave Home" (or Android Tasker)."""
    _check_token(token or x_memory_token)
    return service.handle_presence("away", "phone left Home", source="phone")


@router.api_route("/arrived", methods=["GET", "POST"])
def phone_arrived(token: Optional[str] = None, x_memory_token: Optional[str] = Header(None)):
    """Called by the iPhone Shortcuts automation "When I arrive Home"."""
    _check_token(token or x_memory_token)
    return service.handle_presence("home", "phone arrived Home", source="phone")


@router.post("/demo/leave")
def demo_leave():
    """Dashboard demo button: the same as the phone's "When I leave Home" automation."""
    return service.handle_presence("away", "phone left Home (demo button)", source="demo")


@router.post("/demo/arrive")
def demo_arrive():
    return service.handle_presence("home", "phone arrived Home (demo button)", source="demo")


@router.post("/demo/off-wrist")
def demo_off_wrist(seconds: Optional[float] = None):
    """Demo: the band is taken off. ?seconds=30 instead tells the live listener to report
    off-wrist after 30 s dead still (?seconds=0: back to the config's minutes)."""
    if seconds is not None:
        store.kv_set("offwrist_demo_seconds", max(0.0, seconds))
        return {"result": "listener reports off-wrist after %s" % ("%g s dead still" % seconds if seconds else "the configured minutes")}
    return {"result": service.handle_signal("not_worn", {"value": 0}, source="demo")}


@router.post("/demo/on-wrist")
def demo_on_wrist():
    return {"result": service.handle_signal("worn", {"value": 0}, source="demo")}


@router.post("/demo/drink")
def demo_drink():
    """Dashboard 💧 button: a drink noticed (resolves an open water reminder)."""
    return {"result": service.handle_drink(source="demo")}


@router.post("/demo/hydration-due")
def demo_hydration_due(seconds: Optional[float] = None):
    """Demo: send the water reminder right now. ?seconds=30 also makes the reminder repeat
    every 30 s without a drink (?seconds=0 goes back to the config's interval)."""
    if seconds is not None:
        store.kv_set("hydration_demo_seconds", max(0.0, seconds))
    now = clock.now()
    with rules.LOCK:
        if any(a["rule"] == "water" for a in store.alerts(open_only=True)):
            return {"result": "already open"}
        d = rules.drinks_today(now)
        aid = rules.water_reminder(now, d[0]["ts"] if d else now - rules.hydration_interval(), "water:demo:%d" % int(now))
    return {"result": "sent" if aid else "not sent", "every_seconds": rules.hydration_interval()}


@router.post("/demo/{what}")
def demo_sound(what: str):
    """Demo buttons without a toilet: /api/demo/flush, /api/demo/paper."""
    kinds = {"flush": "toilet_flush", "paper": "toilet_paper"}
    if what not in kinds:
        raise HTTPException(404, "demo is flush or paper")
    return {"result": service.handle_signal(kinds[what], {"value": 1.0}, source="demo")}


# ---- calibration from the setup page ---------------------------------------------------
# The page asks for a calibration here; the running band_listener.py polls GET
# /calibrate/status, records the samples from the live band and reports back with
# POST /calibrate/progress. No restart, no command line.
CAL_ACTIONS = {"lock": "Door lock sound", "twist": "Key-turn twist", "pill": "Pill bottle", "setdown": "Phone set-down",
               "flush": "Toilet flush", "paper": "Toilet paper"}


@router.post("/calibrate/start")
def calibrate_start(body: CalibrateStart):
    if body.action not in CAL_ACTIONS:
        raise HTTPException(400, "action is one of: " + ", ".join(CAL_ACTIONS))
    cal = {"id": "%s-%d" % (body.action, int(time.time() * 1000)), "action": body.action, "label": CAL_ACTIONS[body.action],
           "state": "waiting", "samples": 0, "need": int(CONFIG.get("passive", {}).get("calibration", {}).get("samples", 3)),
           "message": "Waiting for the band listener to start recording...", "started": time.time(), "updated": time.time()}
    store.kv_set("calib", cal)
    return cal


@router.post("/calibrate/default")
def calibrate_default(body: CalibrateStart):
    """Skip / use default: the listener forgets this action's calibration and uses the built-in rule."""
    if body.action not in CAL_ACTIONS:
        raise HTTPException(400, "action is one of: " + ", ".join(CAL_ACTIONS))
    cal = {"id": "%s-default-%d" % (body.action, int(time.time() * 1000)), "action": body.action, "label": CAL_ACTIONS[body.action],
           "state": "use_default", "samples": 0, "need": 0, "message": "Switching to the default...",
           "started": time.time(), "updated": time.time()}
    store.kv_set("calib", cal)
    return cal


@router.post("/calibrate/cancel")
def calibrate_cancel():
    cal = store.kv_get("calib") or {}
    if cal and cal.get("state") in ("waiting", "recording", "use_default"):
        cal.update(state="cancelled", message="Cancelled.", updated=time.time())
        store.kv_set("calib", cal)
    return cal


@router.get("/calibrate/status")
def calibrate_status():
    """The setup page polls this."""
    lis = store.kv_get("listener") or {}
    return {"calibration": store.kv_get("calib") or {},
            "listener_alive": bool(lis.get("seen") and time.time() - lis["seen"] < 6),
            "band": store.kv_get("band", {}), "calibrated": lis.get("calibrated", {})}


@router.post("/listener/heartbeat")
def listener_heartbeat(body: dict):
    """band_listener.py calls this every second: it is alive, which actions it has calibrated,
    and it gets the current calibration request back (it starts one in state "waiting")."""
    store.kv_set("listener", {"seen": time.time(), "calibrated": body.get("calibrated", {}), "mic": body.get("mic"),
                              "sim": body.get("sim")})
    acts = store.kv_get("sim_queue") or [] if body.get("sim") else []
    if acts:
        store.kv_set("sim_queue", [])
    return {"calibration": store.kv_get("calib") or {}, "sim_actions": acts,
            "off_wrist_seconds": store.kv_get("offwrist_demo_seconds") or None}


class SimAct(BaseModel):
    action: str                        # e.g. "@lock-sound", "@pill", "@set-down", "@walk", "@stop", "@pickup"


@router.post("/sim/act")
def sim_act(body: SimAct):
    """Pretend band only (band_listener.py --sim): queue an action for it to act out.
    "flush" / "paper" also work with the real band: the server records them directly."""
    act = body.action.lstrip("@")
    sim_on = (store.kv_get("listener") or {}).get("sim")
    if act in ("flush", "paper") and not sim_on:
        return demo_sound(act)
    if act in ("off_wrist", "on_wrist", "off-wrist", "on-wrist"):
        if not sim_on:
            return demo_off_wrist() if act.startswith("off") else demo_on_wrist()
        body.action = "@off-wrist" if act.startswith("off") else "@on-wrist"
    if act == "drink":
        if not sim_on:
            return demo_drink()
        body.action = "@to-mouth"
    q = store.kv_get("sim_queue") or []
    q.append("@" + act if act in ("flush", "paper") else body.action)
    store.kv_set("sim_queue", q)
    return {"queued": q}


@router.post("/calibrate/progress")
def calibrate_progress(body: CalibrateProgress):
    cal = store.kv_get("calib") or {}
    if cal.get("id") != body.id:
        raise HTTPException(409, "not the current calibration")
    cal.update(state=body.state, samples=body.samples, message=body.message or "", updated=time.time())
    store.kv_set("calib", cal)
    return cal


@router.post("/ask")
def post_ask(body: Ask):
    return {"answer": service.ask(body.question)}


@router.get("/events")
def get_events(limit: int = 200):
    return store.events(limit=limit)


@router.get("/alerts")
def get_alerts(open_only: bool = False):
    return store.alerts(open_only=open_only)


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    if not store.alert(alert_id):
        raise HTTPException(404, "no such alert")
    store.update_alert(alert_id, acked=1)
    return {"ok": True}


@router.get("/state")
def get_state():
    return service.state()


@router.post("/band")
def post_band(body: Band):
    store.kv_set("band", dict(getattr(body, "model_dump", body.dict)(), seen=time.time()))
    return {"ok": True}


@router.post("/clock")
def post_clock(body: ClockSet):
    """Demo only: move the server's clock so a whole day can be shown in minutes."""
    if body.reset:
        clock.reset()
    elif body.time:
        now = clock.now_dt()
        t = body.time.strip()
        if "T" in t or "-" in t:
            target = datetime.fromisoformat(t)
        else:
            h, m = (int(x) for x in t.split(":"))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        clock.set_time(target.timestamp())
    elif body.advance_minutes:
        clock.set_time(clock.now() + body.advance_minutes * 60)
    rules.run_rules()
    return {"now": clock.now(), "now_text": clock.now_dt().isoformat(timespec="minutes")}


@router.post("/tick")
def post_tick():
    rules.run_rules()
    return {"ok": True}


@router.post("/reset")
def post_reset():
    """Demo only: forget everything (events, alerts, reminders, people, places)."""
    store.reset_all()
    clock.reset()
    return {"ok": True}


@router.get("/config")
def get_config():
    return CONFIG


def dashboard():
    return FileResponse(STATIC / "index.html")


def setup_page():
    return FileResponse(STATIC / "setup.html")


def _rules_loop():
    every = float(CONFIG.get("rules_interval_seconds", 30))
    waited = 0.0
    while True:
        time.sleep(2.0)                              # the fall countdown needs a quicker look
        waited += 2.0
        try:
            if waited >= every or store.kv_get("fall"):
                waited = 0.0
                rules.run_rules()
        except Exception:                            # never let the loop die
            log.exception("rules loop")


_started = threading.Event()


def start_background():
    """Start the rules loop once (a daemon thread, so it works with any FastAPI version)."""
    if _started.is_set():
        return
    _started.set()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    threading.Thread(target=_rules_loop, name="memory-rules", daemon=True).start()
