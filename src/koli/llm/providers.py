# llm/providers.py
# The seven providers, each a small entry on top of one wire format: where it
# lives, what headers it wants, where its login comes from, and how to list
# its models. complete() raises (http.HttpError, http.NetworkError,
# ProviderError, ConfigError, ReconnectNeeded); llm.complete() turns that into
# a Result and tries the next model.

import logging

from . import client_version, credentials, http
from .oauth import CODEX, GROK
from .transports import chat_completions, codex as codex_wire, gemini as gemini_wire, ollama as ollama_wire
from .types import ConfigError

logger = logging.getLogger(__name__)

LOCAL, CLOUD = 'local', 'cloud'

# Reads a setting by dotted path; set by llm.configure().
_setting = lambda path, default=None: default


def set_setting_reader(reader):
    global _setting
    _setting = reader


class Provider:
    id = ''
    label = ''
    kind = CLOUD
    auth = 'none'          # none | key | device
    site = ''              # where to get it / sign up

    def complete(self, model, messages, json_mode, on_text, timeout):
        raise NotImplementedError

    def list_models(self) -> list:
        return []

    def status(self) -> dict:
        return {}

    def describe(self) -> dict:
        return {'id': self.id, 'label': self.label, 'kind': self.kind, 'auth': self.auth, 'site': self.site}


# ── Local ────────────────────────────────────────────────────────────────────

class Ollama(Provider):
    id, label, kind, site = 'ollama', 'Ollama', LOCAL, 'https://ollama.com/download'

    def address(self) -> str:
        return str(_setting('ai.ollama.address') or 'http://localhost:11434').rstrip('/')

    def complete(self, model, messages, json_mode, on_text, timeout):
        return ollama_wire.complete(self.address(), model, messages, json_mode, on_text, timeout)

    def list_models(self) -> list:
        return ollama_wire.list_models(self.address())

    def status(self) -> dict:
        try:
            models = self.list_models()
            return {'connected': True, 'detail': f"Running · {len(models)} models", 'address': self.address()}
        except Exception as e:
            return {'connected': False, 'detail': 'Not running', 'error': str(e), 'address': self.address()}


class LMStudio(Provider):
    id, label, kind, site = 'lmstudio', 'LM Studio', LOCAL, 'https://lmstudio.ai'

    # The server root: a pasted ".../v1" or ".../api/v1" is trimmed.
    def root(self) -> str:
        url = str(_setting('ai.lmstudio.address') or 'http://127.0.0.1:1234').rstrip('/')
        for suffix in ('/api/v1', '/api/v0', '/v1'):
            if url.endswith(suffix):
                return url[: -len(suffix)]
        return url

    def headers(self) -> dict:
        token = credentials.load(self.id).get('key')
        return {'Authorization': f"Bearer {token}"} if token else {}

    # The optional API token, for when "Require authentication" is on in
    # LM Studio's server settings. Saved even if LM Studio isn't running now.
    def set_key(self, key: str) -> int:
        key = str(key or '').strip()
        if not key:
            raise ValueError('Paste the API token first')
        credentials.save(self.id, {'key': key})
        return 0

    def remove(self):
        credentials.delete(self.id)

    def complete(self, model, messages, json_mode, on_text, timeout):
        # LM Studio rejects response_format json_object, so JSON is asked in words.
        return chat_completions.complete(f"{self.root()}/v1", self.headers(), model, messages,
                                         'instruction' if json_mode else None, on_text, timeout)

    # LM Studio's own list says which models are loaded and leaves out
    # embedding models; older versions only have the OpenAI-style list.
    def list_models(self) -> list:
        for path, rows_key in (('/api/v1/models', 'models'), ('/api/v0/models', 'data')):
            try:
                data = http.get_json(self.root() + path, self.headers(), timeout=5)
            except http.HttpError as e:
                if e.status == 404:
                    continue
                raise
            out = []
            for row in data.get(rows_key) or []:
                if row.get('type') not in (None, 'llm', 'vlm'):
                    continue
                mid = row.get('key') or row.get('id')
                loaded = bool(row.get('loaded_instances')) or row.get('state') == 'loaded'
                if mid:
                    out.append({'id': mid, 'label': row.get('display_name') or mid, 'loaded': loaded})
            return out
        return [{'id': m, 'label': m} for m in chat_completions.list_model_ids(f"{self.root()}/v1", self.headers(), 5)]

    def status(self) -> dict:
        try:
            models = self.list_models()
            loaded = sum(1 for m in models if m.get('loaded'))
            detail = f"Running · {len(models)} models" + (f", {loaded} loaded" if loaded else '')
            return {'connected': True, 'detail': detail, 'address': self.root(),
                    'key_hint': _hint(credentials.load(self.id).get('key'))}
        except Exception as e:
            return {'connected': False, 'detail': 'Not running', 'error': str(e), 'address': self.root(),
                    'key_hint': _hint(credentials.load(self.id).get('key'))}


# ── Subscriptions ────────────────────────────────────────────────────────────

class _SubscriptionProvider(Provider):
    auth = 'device'
    account = None   # the oauth.Subscription

    # One retry after a 401 (refresh the token) or a 426 (newer client version).
    def complete(self, model, messages, json_mode, on_text, timeout):
        access, account_id = self.account.bearer()
        try:
            return self._call(access, account_id, model, messages, json_mode, on_text, timeout)
        except http.HttpError as e:
            if e.status == 401:
                access, account_id = self.account.bearer(stale=access)
            elif e.status == 426:
                client_version.note_refusal(self.id, e.body)
            else:
                raise
            return self._call(access, account_id, model, messages, json_mode, on_text, timeout)

    def list_models(self) -> list:
        access, account_id = self.account.bearer()
        return self._list(access, account_id)

    def status(self) -> dict:
        st = self.account.status()
        st['detail'] = ('Reconnect needed' if st['reconnect'] else
                        f"Connected as {st['email']}" if st['connected'] and st['email'] else
                        'Connected' if st['connected'] else 'Not connected')
        st['login'] = self.account.login_state()
        return st


class Codex(_SubscriptionProvider):
    id, label, site = 'codex', 'ChatGPT (Codex)', 'https://chatgpt.com'
    account = CODEX
    base_url = 'https://chatgpt.com/backend-api/codex'
    # The model list hides models from an older client, so it asks as a
    # version ahead of any release. Chat calls use the real version.
    models_client_version = '99.0.0'

    def headers(self, access, account_id, version) -> dict:
        h = {'Authorization': f"Bearer {access}", 'originator': 'codex_cli_rs',
             'User-Agent': f"codex_cli_rs/{version}", 'version': version}
        if account_id:
            h['chatgpt-account-id'] = account_id
        return h

    def _call(self, access, account_id, model, messages, json_mode, on_text, timeout):
        headers = self.headers(access, account_id, client_version.current('codex'))
        return codex_wire.complete(self.base_url, headers, model, messages, json_mode, on_text, timeout)

    def _list(self, access, account_id):
        headers = self.headers(access, account_id, self.models_client_version)
        return codex_wire.list_models(self.base_url, headers, self.models_client_version)


class Grok(_SubscriptionProvider):
    id, label, site = 'grok', 'SuperGrok', 'https://grok.com'
    account = GROK
    base_url = 'https://cli-chat-proxy.grok.com/v1'

    def headers(self, access, model=None) -> dict:
        version = client_version.current('grok')
        h = {'Authorization': f"Bearer {access}", 'X-XAI-Token-Auth': 'xai-grok-cli',
             'x-grok-client-version': version, 'x-grok-client-mode': 'cli', 'User-Agent': f"grok/{version}"}
        if model:
            h['x-grok-model-override'] = model
        return h

    def _call(self, access, account_id, model, messages, json_mode, on_text, timeout):
        return chat_completions.complete(self.base_url, self.headers(access, model), model, messages,
                                         'instruction' if json_mode else None, on_text, timeout)

    def _list(self, access, account_id):
        return [{'id': m, 'label': m} for m in chat_completions.list_model_ids(self.base_url, self.headers(access))]


# ── API keys ─────────────────────────────────────────────────────────────────

class _KeyProvider(Provider):
    auth = 'key'

    def key(self) -> str:
        key = credentials.load(self.id).get('key')
        if not key:
            raise ConfigError(f"{self.label} has no API key. Add one in Settings › AI.")
        return key

    # Checked by listing models with it before it is saved.
    def set_key(self, key: str) -> int:
        key = str(key or '').strip()
        if not key:
            raise ValueError('Paste the API key first')
        models = self._list_with(key)
        credentials.save(self.id, {'key': key})
        return len(models)

    def remove(self):
        credentials.delete(self.id)

    def list_models(self) -> list:
        return self._list_with(self.key())

    def status(self) -> dict:
        key = credentials.load(self.id).get('key')
        return {'connected': bool(key), 'detail': 'Key saved' if key else 'No key', 'key_hint': _hint(key)}


class Gemini(_KeyProvider):
    id, label, site = 'gemini', 'Gemini', 'https://aistudio.google.com/apikey'

    def complete(self, model, messages, json_mode, on_text, timeout):
        return gemini_wire.complete(self.key(), model, messages, json_mode, on_text, timeout)

    def _list_with(self, key):
        return gemini_wire.list_models(key)


class _OpenAIStyle(_KeyProvider):
    base_url = ''
    json_mode = 'instruction'
    extra_headers = {}

    def _headers(self, key):
        return {'Authorization': f"Bearer {key}", **self.extra_headers}

    def complete(self, model, messages, json_mode, on_text, timeout):
        return chat_completions.complete(self.base_url, self._headers(self.key()), model, messages,
                                         self.json_mode if json_mode else None, on_text, timeout)

    def _list_with(self, key):
        return [{'id': m, 'label': m} for m in sorted(chat_completions.list_model_ids(self.base_url, self._headers(key)))
                if self._is_chat_model(m)]

    def _is_chat_model(self, model_id: str) -> bool:
        return True


class OpenRouter(_OpenAIStyle):
    id, label, site = 'openrouter', 'OpenRouter', 'https://openrouter.ai/keys'
    base_url = 'https://openrouter.ai/api/v1'
    extra_headers = {'X-Title': 'Koli'}


class OpenAI(_OpenAIStyle):
    id, label, site = 'openai', 'OpenAI API', 'https://platform.openai.com/api-keys'
    base_url = 'https://api.openai.com/v1'
    json_mode = 'native'
    _NOT_CHAT = ('audio', 'realtime', 'tts', 'transcribe', 'image', 'embedding', 'moderation', 'search', 'dall-e', 'whisper')

    def _is_chat_model(self, model_id: str) -> bool:
        m = model_id.lower()
        return m.startswith(('gpt', 'o1', 'o3', 'o4', 'chatgpt')) and not any(x in m for x in self._NOT_CHAT)


def _hint(key) -> str:
    return f"…{str(key)[-4:]}" if key else ''


PROVIDERS = {p.id: p for p in (Ollama(), LMStudio(), Codex(), Grok(), Gemini(), OpenRouter(), OpenAI())}


def get(provider_id: str) -> Provider:
    provider = PROVIDERS.get(provider_id)
    if not provider:
        raise ConfigError(f"Unknown AI provider '{provider_id}'")
    return provider
