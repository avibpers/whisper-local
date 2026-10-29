# online_stt/mode.py
# Online mode: an online model picked in the Model table (whisper.online_model,
# empty when a local model is picked) takes dictation and voice commands, and
# this computer keeps only a small listening model (whisper.listen_model, base by
# default) for the wake word and as the backup, so the big models and the
# second-language model are not loaded at all. Meetings use their own model
# (meeting.model) when one is set, else they follow dictation. When the
# monthly budget is used up (usage/budget.py) the local model set to take
# over is loaded, or with none set dictation stops and says why.
# Design: docs/design/online-models.md.

from ..usage import budget
from .engine import usable

DEFAULT_LISTEN_MODEL = 'base'


# An online model is picked that Koli can send recordings to (budget aside).
def chosen(whisper_cfg: dict) -> bool:
    return usable(str((whisper_cfg or {}).get('online_model') or ''))


# Whether recordings go online now: chosen, and the month's budget isn't used
# up with a local model set to take over (with none set, dictation is
# stopped instead, see blocked()).
def active(whisper_cfg: dict) -> bool:
    return chosen(whisper_cfg) and not (budget.over() and budget.fallback_model())


# Online mode with the budget used up and no local model to take over:
# nothing is transcribed and the bubble says why.
def blocked(whisper_cfg: dict) -> bool:
    return chosen(whisper_cfg) and budget.over() and not budget.fallback_model()


# The online model dictation and commands use, or '' for local Whisper.
# Over budget with nothing to take over it is still the online model: the
# engine itself stops the call and says why (engine.py).
def dictation_spec(whisper_cfg: dict) -> str:
    return str(whisper_cfg.get('online_model') or '').strip() if active(whisper_cfg) else ''


# The online model meetings use: their own when set, else dictation's.
def meeting_spec(meeting_model: str, whisper_cfg: dict) -> str:
    own = str(meeting_model or '').strip()
    if own and usable(own):
        return own
    return dictation_spec(whisper_cfg)


# The whisper settings the local engine is built from: in online mode the
# listening model alone, with no second model; when the budget is used up,
# the model set to take over; else the settings as they are.
def local_config(whisper_cfg: dict) -> dict:
    cfg = dict(whisper_cfg or {})
    if not chosen(cfg):
        return cfg
    takeover = budget.fallback_model() if budget.over() else ''
    if takeover:
        cfg['model'] = takeover
        return cfg
    cfg['model'] = str(cfg.get('listen_model') or DEFAULT_LISTEN_MODEL)
    cfg['language_model'] = {**(cfg.get('language_model') or {}), 'language': ''}
    return cfg


# What the local engine loads, to tell whether a change needs a new engine.
def local_models(whisper_cfg: dict) -> tuple:
    cfg = local_config(whisper_cfg)
    second = cfg.get('language_model') or {}
    return cfg.get('model'), second.get('language') or '', second.get('model') if second.get('language') else ''
