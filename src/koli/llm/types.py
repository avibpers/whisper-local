# llm/types.py
# What a model call returns, and the errors the layer tells apart. Callers only
# ever see Result: complete() turns every error into Result.error, so a
# provider that is down can never break dictation.

from dataclasses import dataclass


@dataclass
class Result:
    text: str = ''
    error: str = ''
    provider: str = ''
    model: str = ''
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


# Something the user has to set up: no model chosen, no key, not connected.
# Not worth retrying or falling back silently; the message says what to do.
class ConfigError(Exception):
    pass


# The saved login can't be refreshed any more; only Connect again fixes it.
class ReconnectNeeded(Exception):
    pass


# The provider answered with an error, or the answer was unusable.
class ProviderError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status
