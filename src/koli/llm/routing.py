# llm/routing.py
# Which models answer a feature. A model is written "provider/model"
# ("lmstudio/qwen/qwen3-8b", "codex/gpt-5.5"); the first "/" separates the
# provider. A feature uses its own model when set, else the default, then the
# default fallbacks in order. The timeout covers the whole call.

import logging

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


def parse(spec: str):
    provider, _, model = str(spec or '').strip().partition('/')
    if not provider or not model:
        return None
    return provider.strip().lower(), model.strip()


# (["provider/model", ...] without repeats, timeout seconds) for `feature`.
# `override` is a model a caller names itself (a transform's own model).
def route(setting, feature: str, override: str = '', fallbacks: bool = True) -> tuple:
    if override and not parse(override):
        # An old-style transform model ("llama3.2") names no provider: use the
        # feature's model instead of answering with fallbacks only.
        logger.warning(f"AI model '{override}' isn't provider/model; using the {feature} model instead")
        override = ''
    first = override or setting(f'ai.{feature}.model') or setting('ai.model') or ''
    chain = [first, *((setting('ai.fallbacks') or []) if fallbacks else [])]
    seen, models = set(), []
    for spec in chain:
        spec = str(spec or '').strip()
        if spec and parse(spec) and spec not in seen:
            seen.add(spec)
            models.append(spec)
    timeout = setting(f'ai.{feature}.timeout') or setting('ai.timeout') or DEFAULT_TIMEOUT
    return models, float(timeout)
