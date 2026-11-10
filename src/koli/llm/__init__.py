# llm/__init__.py
# The one way Koli asks a language model anything. Features call
# complete(feature, messages) and get a Result back; which provider and model
# answer is the user's choice in Settings › AI (local Ollama / LM Studio, the
# ChatGPT or SuperGrok subscription, or a Gemini / OpenRouter / OpenAI key).
# complete() never raises: a provider that is down or not set up gives an
# empty Result with the reason, and the next fallback model is tried while the
# feature's time allows. A message may carry pictures (llm/images.py); a model
# that refuses them is asked again without. Design: docs/design/ai-providers.md.

import json
import logging
import re
import threading
import time

from . import http, images, providers, routing
from .oauth import SUBSCRIPTIONS
from .types import AnswerStopped, ConfigError, ProviderError, ReconnectNeeded, Result
from ..usage import budget, ledger, prices

logger = logging.getLogger(__name__)

__all__ = ['Result', 'configure', 'complete', 'list_models', 'provider_status', 'test', 'kind_of',
           'start_background', 'strip_reasoning', 'parse_json', 'model_for', 'takes_pictures']

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


# The JSON object in a model's answer, or None. Models without a JSON mode
# (Codex, Grok) often wrap it in a ```json fence or a sentence; the first
# {...} block that parses is taken.
def parse_json(text: str):
    import json as _json
    text = (text or '').strip()
    fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for candidate in candidates:
        start = candidate.find('{')
        while start != -1:
            depth, in_string, escaped = 0, False, False
            for end in range(start, len(candidate)):
                ch = candidate[end]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == '\\':
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            value = _json.loads(candidate[start:end + 1])
                            if isinstance(value, dict):
                                return value
                        except ValueError:
                            pass
                        break
            start = candidate.find('{', start + 1)
    return None


def complete(feature: str, messages: list, json: bool = False, on_text=None,
             model: str = '', timeout: float = None, fallbacks: bool = True, options: dict = None) -> Result:
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
        # A paid model waits for next month once the online budget is used
        # up; a subscription or local model in the fallbacks still answers.
        if feature != 'test' and not prices.is_free(spec) and budget.over():
            last = Result(error=budget.used_up_message(), provider=provider_id, model=model_id)
            continue
        started = time.monotonic()
        try:
            provider = providers.get(provider_id)
            sent = images.without_images(messages) if spec in _NO_PICTURES else messages
            # `options` are Ollama's own request fields; other providers don't take them.
            extra = {'options': options} if options and provider_id == 'ollama' else {}
            try:
                text, tokens_in, tokens_out = provider.complete(model_id, sent, json, on_text, remaining, **extra)
            except (http.HttpError, ProviderError) as e:
                if not (images.has_images(sent) and _refuses_pictures(e)):
                    raise
                # A model that reads text only: asked again without the
                # picture, and sent none for the rest of the session.
                logger.info(f"AI {feature}: {spec} takes no pictures; asking without")
                _NO_PICTURES.add(spec)
                text, tokens_in, tokens_out = provider.complete(
                    model_id, images.without_images(sent), json, on_text, max(deadline - time.monotonic(), 1.0))
            text = strip_reasoning(text)
            if text:
                # Every answer is counted (Usage screen); a meeting's own calls
                # land on that meeting (usage.for_meeting).
                cost = None
                if feature != 'test':
                    cost = ledger.record(ledger.ai_feature(feature), spec, tokens_in=tokens_in,
                                         tokens_out=tokens_out).get('cost')
                logger.info(f"AI {feature}: {spec} answered in {time.monotonic() - started:.1f}s "
                            f"({tokens_in}→{tokens_out} tokens"
                            f"{f', ${cost:.4f}' if cost else ''})")
                return Result(text=text, provider=provider_id, model=model_id,
                              tokens_in=tokens_in, tokens_out=tokens_out, cost=cost)
            last = Result(error=f"{provider.label} gave an empty answer", provider=provider_id, model=model_id)
        except AnswerStopped:
            raise
        except Exception as e:
            last = Result(error=describe_error(provider_id, e), provider=provider_id, model=model_id)
            if not isinstance(e, (ConfigError, ReconnectNeeded, http.NetworkError, http.HttpError, ProviderError)):
                logger.error(f"AI {feature}: {spec} failed unexpectedly", exc_info=True)
        logger.warning(f"AI {feature}: {spec} failed: {last.error}")
    if not last.error:
        last.error = f"No answer within {limit:g} seconds"
    return last


# Loads an Ollama model ahead of its first use. Raises when it can't.
def warm(spec: str, options: dict = None):
    provider_id, model_id = routing.parse(spec)
    providers.get(provider_id).warm(model_id, options)


# Whether a model can be loaded ahead of use.
def can_warm(spec: str) -> bool:
    parsed = routing.parse(spec)
    return bool(parsed) and hasattr(providers.PROVIDERS.get(parsed[0]), 'warm')


# Models ("provider/model") that refused a picture this session.
_NO_PICTURES = set()


# Whether a provider's error says it can't read pictures ("image_url is only
# supported by certain models", "model does not support images").
def _refuses_pictures(e: Exception) -> bool:
    words = (e.body if isinstance(e, http.HttpError) else str(e)).lower()
    status_fits = not isinstance(e, http.HttpError) or e.status in (400, 404, 415, 422)
    return status_fits and any(w in words for w in ('image', 'vision', 'multimodal', 'multi-modal'))


# Whether the model a feature asks first takes pictures, as far as Koli knows:
# False once it refused one, so the caller needn't take a screenshot.
def takes_pictures(feature: str) -> bool:
    return model_for(feature) not in _NO_PICTURES


# A sentence the user can act on.
def describe_error(provider_id: str, e: Exception) -> str:
    label = providers.PROVIDERS[provider_id].label if provider_id in providers.PROVIDERS else provider_id
    if isinstance(e, (ConfigError, ReconnectNeeded)):
        return str(e)
    if isinstance(e, http.TimedOut):
        return f"{label} didn't answer in time: {e}"
    if isinstance(e, http.NetworkError):
        local = provider_id in ('ollama', 'lmstudio')
        return f"{label} isn't reachable{' (is it running?)' if local else ''}: {e}"
    if isinstance(e, http.HttpError):
        if e.status in (401, 403):
            return f"{label} refused the login or key (HTTP {e.status})"
        if e.status == 404:
            # Google says why (a model closed to new users, and what replaces it).
            why = _error_message(e.body)
            return f"{label} doesn't have that model (HTTP 404){': ' + why if why else ''}"
        if e.status in (402, 429):
            return f"{label}: usage limit or quota reached (HTTP {e.status})"
        if e.status == 426:
            return f"{label} wants a newer client version (HTTP 426); try again"
        return f"{label} error (HTTP {e.status}): {e.body[:160]}"
    return f"{label}: {e}"


# The provider's own error sentence from a JSON error body
# ({"error": {"message": ...}}), its first two sentences, or ''.
def _error_message(body: str) -> str:
    try:
        message = str((json.loads(body or '{}').get('error') or {}).get('message') or '')
    except (ValueError, AttributeError):
        return ''
    return '. '.join(message.split('. ')[:2]).strip()[:200]


# ── Settings screen ──────────────────────────────────────────────────────────

def list_models(provider_id: str) -> list:
    return providers.get(provider_id).list_models()


# Every provider's name, kind and login style, without asking any of them.
def catalog() -> list:
    return [p.describe() for p in providers.PROVIDERS.values()]


# The model a feature would ask first ("provider/model"), or '' when none is
# chosen: so a feature can say which model it will use, or offer to work
# without one.
def model_for(feature: str) -> str:
    models, _ = routing.route(_setting, feature)
    return models[0] if models else ''


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
# `options`: what a feature sends this model with, so the test neither reloads
# it nor shows a load time that feature never pays.
def test(spec: str, options: dict = None) -> dict:
    started = time.monotonic()
    result = complete('test', [{'role': 'user', 'content': 'Reply with the single word: pong'}],
                      model=spec, timeout=120, fallbacks=False, options=options)
    return {'ok': result.ok, 'text': result.text[:200], 'error': result.error,
            'seconds': round(time.monotonic() - started, 1), 'model': spec}


# ── Background upkeep ────────────────────────────────────────────────────────

_background_started = False


# At start and once a day: keep subscription logins fresh and the client
# versions current. Only for subscriptions that are connected. The Google
# Drive login rides along: same rule, same day.
def start_background():
    global _background_started
    if _background_started:
        return
    _background_started = True

    def loop():
        from . import client_version
        from ..drive import ACCOUNT as drive_account
        while True:
            for account in SUBSCRIPTIONS.values():
                if account.status()['connected']:
                    client_version.current(account.name)
                    account.keep_alive()
            if drive_account.status()['connected']:
                drive_account.keep_alive()
            time.sleep(24 * 3600)

    threading.Thread(target=loop, daemon=True, name='ai-upkeep').start()
