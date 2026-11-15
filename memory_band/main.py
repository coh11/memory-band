"""Memory Band: passive door check, phone push alerts, double-tap
confirmation, fall alert. Runs beside Mamoune's speech_assistant/ on its own port:

    .venv/bin/uvicorn memory_band.main:app --port 8001          (from the repo root)
    open http://127.0.0.1:8001/  (dashboard)    http://127.0.0.1:8001/docs  (API)
"""
from fastapi import FastAPI

if __package__:
    from .memory import api as memory_api            # uvicorn memory_band.main:app (repo root)
else:
    from memory import api as memory_api             # cd memory_band && uvicorn main:app

app = FastAPI(title="Memory Band")
app.middleware("http")(memory_api.tunnel_guard)     # requests through a tunnel need MEMORY_TOKEN
app.include_router(memory_api.router)
app.add_api_route("/", memory_api.dashboard, include_in_schema=False)
app.add_api_route("/dashboard", memory_api.dashboard, include_in_schema=False)
app.add_api_route("/setup", memory_api.setup_page, include_in_schema=False)


@app.get("/health")
def health():
    return {"message": "Memory Band is running"}


memory_api.start_background()                        # rules loop: checks every 30 s and sends alerts
