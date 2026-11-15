"""Memory Band: proactive memory assistant on top of the Vexo band.

Pipeline: band mic -> BLE -> band_bridge.py -> Deepgram text -> POST /api/transcript
-> tagger (Gemini / OpenAI / keyword fallback) -> store -> rules loop -> ntfy push + dashboard.
"""
