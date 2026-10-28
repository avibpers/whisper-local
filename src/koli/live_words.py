# live_words.py
# Which engine fills the dictation bubble for one recording: off, the local
# English streaming model, Whisper phrase by phrase, or a cloud provider. Pure
# decisions (no audio, no UI) so the rules in docs/design/dictation-bubble.md
# are tested on their own. The flow that runs the chosen engine is
# live_session_flow.py.

from dataclasses import dataclass
from typing import Callable, Optional

ENGINES = ('off', 'local', 'phrases', 'cloud')
PROVIDERS = {'openai': 'OpenAI', 'gemini': 'Gemini'}
DEFAULT_MODELS = {'openai': 'gpt-live-transcribe', 'gemini': 'gemini-3.6-flash'}

# Recordings that never get words in the bubble: there is nothing to fix by
# hand in a command, a rephrase or a translation, and auto-dictation starts on
# every click into a field and must never stream the microphone or cost money.
NO_ENGINE_MODES = ('command', 'rephrase', 'translate', 'auto')


@dataclass
class EngineChoice:
    engine: str               # off | local | phrases | openai | gemini
    provider: str = ''
    model: str = ''
    notice: str = ''          # said once when the choice differs from the setting


# The language hint for a cloud provider: the app rule's, then the dictation
# language when it is not "auto", then the first of your languages, else none
# (the provider detects it). '' means no hint.
def resolve_language(whisper_cfg: dict, rule: Optional[dict]) -> str:
    if rule and rule.get('language'):
        return str(rule['language'])
    language = str((whisper_cfg or {}).get('language') or 'auto')
    if language != 'auto':
        return language
    languages = (whisper_cfg or {}).get('languages') or []
    return str(languages[0]) if languages else ''


# The engine this recording uses. `cfg` is the live_words section, `rule` the
# matched app rule, `mode` dictate / command / rephrase / translate / auto,
# `has_key(provider)` whether that cloud login exists, `local_ready` whether
# the streaming model is loaded. Anything that cannot run falls back to
# Phrases, which always can, and says why.
def choose_engine(cfg: dict, rule: Optional[dict], *, mode: str, language: str,
                  has_key: Callable[[str], bool], local_ready: bool) -> EngineChoice:
    if mode in NO_ENGINE_MODES:
        return EngineChoice('off')
    cfg = cfg or {}
    wanted = (rule or {}).get('live_words') or cfg.get('engine') or 'off'
    if wanted not in ENGINES or wanted == 'off':
        return EngineChoice('off')
    if wanted == 'phrases':
        return EngineChoice('phrases')
    if wanted == 'local':
        if language and not language.startswith('en'):
            return EngineChoice('phrases', notice='Local words is English only, so this dictation shows phrases.')
        if not local_ready:
            return EngineChoice('phrases', notice="The local words model isn't loaded, so this dictation shows phrases.")
        return EngineChoice('local')
    cloud = cfg.get('cloud') or {}
    provider = cloud.get('provider') if cloud.get('provider') in PROVIDERS else 'openai'
    if not has_key(provider):
        return EngineChoice('phrases', notice=f"No {PROVIDERS[provider]} key in AI providers, so this dictation shows phrases.")
    model = str(cloud.get(f'{provider}_model') or DEFAULT_MODELS[provider])
    return EngineChoice(provider, provider=provider, model=model)


# The name usage is counted under (live_words_usage.py).
def usage_key(choice: EngineChoice) -> str:
    if choice.engine in PROVIDERS:
        return f"cloud/{choice.provider or choice.engine}/{choice.model}"
    return choice.engine
