# llm/__init__.py
# The one way Koli asks a language model anything. Features call
# complete(feature, messages) and get a Result back; which provider and model
# answer is the user's choice in Settings › AI (local Ollama / LM Studio, the
# ChatGPT or SuperGrok subscription, or a Gemini / OpenRouter / OpenAI key).
# complete() never raises: a provider that is down or not set up gives an
# empty Result with the reason, and the next fallback model is tried while the
# feature's time allows. Design: docs/design/ai-providers.md.

import logging
import re
import threading
import time

from . import http, providers, routing
from .oauth import SUBSCRIPTIONS
from .types import ConfigError, ProviderError, ReconnectNeeded, Result

logger = logging.getLogger(__name__)

__all__ = ['Result', 'configure', 'complete', 'list_models', 'provider_status', 'test', 'kind_of',
           'start_background', 'strip_reasoning']

_config_manager = None


# Called once at startup with the ConfigManager, so routes and addresses are
# read fresh on every call (settings apply the moment they change).
def configure(config_manager):
    global _config_manager
    _config_manager = config_manager
    providers.set_setting_reader(_setting)


def _setting(path, default=None):
    if _config_manager is None:
        return default
    value = _config_manager.get_path(path, default)
    return default if value is None else value


# ── Answers ──────────────────────────────────────────────────────────────────

_REASONING = re.compile(r'<(think|thinking|reasoning)>.*?</\1>', re.S | re.I)
_UNCLOSED = re.compile(r'^\s*<(think|thinking|reasoning)>.*', re.S | re.I)


# Reasoning models (Qwen, DeepSeek in LM Studio / Ollama) put their thinking
# in <think> tags before the answer. Only the answer is wanted.
def strip_reasoning(text: str) -> str:
    text = _REASONING.sub('', text or '')
    return _UNCLOSED.sub('', text).strip()


def complete(feature: str, messages: list, json: bool = False, on_text=None,
             model: str = '', timeout: float = None, fallbacks: bool = True) -> Result:
    models, route_timeout = routing.route(_setting, feature, model, fallbacks)
    if not models:
        return Result(error='No AI model chosen. Pick one in Settings › AI.')
    try:
        # A transform's hand-written timeout may be text.
        limit = float(timeout or route_timeout)
    except (TypeError, ValueError):
        limit = route_timeout
    deadline = time.monotonic() + limit
    last = Result()
    for spec in models:
        remaining = deadline - time.monotonic()
        if remaining < 0.5:
            break
        provider_id, model_id = routing.parse(spec)
        started = time.monotonic()
        try:
            provider = providers.get(provider_id)
            text, tokens_in, tokens_out = provider.complete(model_id, messages, json, on_text, remaining)
            text = strip_reasoning(text)
            if text:
                logger.info(f"AI {feature}: {spec} answered in {time.monotonic() - started:.1f}s "
                            f"({tokens_in}→{tokens_out} tokens)")
                return Result(text=text, provider=provider_id, model=model_id,
                              tokens_in=tokens_in, tokens_out=tokens_out)
            last = Result(error=f"{provider.label} gave an empty answer", provider=provider_id, model=model_id)
        except Exception as e:
            last = Result(error=describe_error(provider_id, e), provider=provider_id, model=model_id)
            if not isinstance(e, (ConfigError, ReconnectNeeded, http.NetworkError, http.HttpError, ProviderError)):
                logger.error(f"AI {feature}: {spec} failed unexpectedly", exc_info=True)
        logger.warning(f"AI {feature}: {spec} failed: {last.error}")
    if not last.error:
        last.error = f"No answer within {limit:g} seconds"
    return last


# A sentence the user can act on.
def describe_error(provider_id: str, e: Exception) -> str:
    label = providers.PROVIDERS[provider_id].label if provider_id in providers.PROVIDERS else provider_id
    if isinstance(e, (ConfigError, ReconnectNeeded)):
        return str(e)
    if isinstance(e, http.NetworkError):
        local = provider_id in ('ollama', 'lmstudio')
        return f"{label} isn't reachable{' (is it running?)' if local else ''}: {e}"
    if isinstance(e, http.HttpError):
        if e.status in (401, 403):
            return f"{label} refused the login or key (HTTP {e.status})"
        if e.status == 404:
            return f"{label} doesn't have that model (HTTP 404)"
        if e.status in (402, 429):
            return f"{label}: usage limit or quota reached (HTTP {e.status})"
        if e.status == 426:
            return f"{label} wants a newer client version (HTTP 426); try again"
        return f"{label} error (HTTP {e.status}): {e.body[:160]}"
    return f"{label}: {e}"


# ── Settings screen ──────────────────────────────────────────────────────────

def list_models(provider_id: str) -> list:
    return providers.get(provider_id).list_models()


# Every provider's name, kind and login style, without asking any of them.
def catalog() -> list:
    return [p.describe() for p in providers.PROVIDERS.values()]


def kind_of(spec: str) -> str:
    parsed = routing.parse(spec)
    provider = providers.PROVIDERS.get(parsed[0]) if parsed else None
    return provider.kind if provider else ''


# Every provider (or one) with its status for the cards.
def provider_status(provider_id: str = None) -> list:
    chosen = [providers.get(provider_id)] if provider_id else providers.PROVIDERS.values()
    out = []
    for p in chosen:
        try:
            status = p.status()
        except Exception as e:
            status = {'connected': False, 'detail': 'Error', 'error': str(e)}
        out.append({**p.describe(), **status})
    return out


# One short question to a model, with a long timeout (LM Studio may have to
# load the model first): what the Test buttons call.
def test(spec: str) -> dict:
    started = time.monotonic()
    result = complete('test', [{'role': 'user', 'content': 'Reply with the single word: pong'}],
                      model=spec, timeout=120, fallbacks=False)
    return {'ok': result.ok, 'text': result.text[:200], 'error': result.error,
            'seconds': round(time.monotonic() - started, 1), 'model': spec}


# ── Background upkeep ────────────────────────────────────────────────────────

_background_started = False


# At start and once a day: keep subscription logins fresh and the client
# versions current. Only for subscriptions that are connected.
def start_background():
    global _background_started
    if _background_started:
        return
    _background_started = True

    def loop():
        from . import client_version
        while True:
            for account in SUBSCRIPTIONS.values():
                if account.status()['connected']:
                    client_version.current(account.name)
                    account.keep_alive()
            time.sleep(24 * 3600)

    threading.Thread(target=loop, daemon=True, name='ai-upkeep').start()
