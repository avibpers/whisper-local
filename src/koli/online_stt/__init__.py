# online_stt/__init__.py
# Speech-to-text in the cloud (OpenAI, Gemini) with the API keys of Settings ›
# AI, in front of local Whisper. An online model picked in the Model table
# (whisper.online_model, mode.py) takes dictation and commands and keeps only a small
# listening model loaded; meetings use meeting.model, else follow dictation.
# Design: docs/design/online-models.md.

from .engine import OnlineEngine, record_local, usable  # noqa: F401
from . import mode  # noqa: F401
