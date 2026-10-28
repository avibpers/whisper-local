# online_stt/__init__.py
# Speech-to-text in the cloud (OpenAI, Gemini) with the API keys of Settings ›
# AI, in front of local Whisper. Dictation picks its model with
# whisper.online_model, meetings with meeting.model; empty is local. Design:
# docs/design/online-models.md.

from .engine import OnlineEngine, record_local, usable  # noqa: F401
