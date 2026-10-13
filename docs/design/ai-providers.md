# AI providers: one call, local or cloud models

*Design spec, 2026-10-13. Approved in conversation. Step 1 of 4: providers →
smart correction → teaching sessions → cloud transcription (Gemini). Ported
from the Node implementations in Soul (`D:\DEV\Soul`) and BaileysWhatsAPP.*

## Goal

Koli stops being Ollama-only. Every feature that needs a language model asks
one function, and the user chooses which model answers: a local one (Ollama,
LM Studio) or a cloud one, through a subscription they already pay for
(ChatGPT through Codex, SuperGrok) or an API key (Gemini, OpenRouter, OpenAI).

Later steps build on this layer: smart correction (fixing "תמונת בריכה" to
"תמונת כריכה" from context), teaching sessions (reading text the LLM writes so
Koli learns how you pronounce things on each microphone), meeting summaries,
and cloud transcription.

## Providers

| Provider | Kind | Login | Wire format |
|----------|------|-------|-------------|
| Ollama | local | none | Ollama `/api/chat`, models from `/api/tags` |
| LM Studio | local | none, optional API token | OpenAI `/v1/chat/completions`, models from `/api/v1/models` (falls back to `/v1/models`) |
| Codex (ChatGPT subscription) | cloud | device code | Responses API at `chatgpt.com/backend-api/codex/responses`, always streamed |
| Grok (SuperGrok subscription) | cloud | device code | OpenAI chat at `cli-chat-proxy.grok.com/v1` with Grok CLI headers |
| Gemini | cloud | API key | `generateContent` |
| OpenRouter | cloud | API key | OpenAI chat at `openrouter.ai/api/v1` |
| OpenAI | cloud | API key | OpenAI chat at `api.openai.com/v1` |

Seven providers, four wire formats. Code is organised by format; each provider
is a small entry (base URL, headers, where the login comes from, how to list
models) on top of one.

Codex and Grok borrow the public client ids of the official Codex and Grok
command-line tools, as Soul and BaileysWhatsAPP do. That route is not an
official API and can change without notice. Grok deliberately does not use
`api.x.ai`: that may bill API credits instead of the subscription.

## The one call

```python
from koli import llm
result = llm.complete('polish', messages, json=False, on_text=None)
# result.text, result.error, result.provider, result.model
```

- `feature` picks the route (below). Features today: `polish`, `rewrite`.
- `messages` is a chat list (`{'role': 'system'|'user'|'assistant', 'content': str}`),
  so multi-turn features (teaching) fit without a new interface.
- `json=True` asks for a JSON answer: Ollama `format: json`, OpenAI
  `response_format`, Gemini `responseMimeType`, an instruction elsewhere.
- `on_text(delta)` receives text as it arrives (Codex and Grok stream; the
  others call it once with the whole answer).
- It never raises. Failure is `result.text == ''` with `result.error` set, the
  same contract `_ollama_polish` had, so a dead provider never breaks dictation.
- `<think>…</think>` reasoning blocks are removed from answers (local reasoning
  models emit them).

## Routes

A model is named `provider/model`: `lmstudio/qwen/qwen3-8b`,
`codex/gpt-5.5`, `ollama/llama3.2`. The first `/` separates the provider.

```yaml
ai:
    model: ''              # the default model, '' until you choose one
    fallbacks: []          # tried in order when the model fails
    timeout: 30            # seconds for the whole call, fallbacks included
    polish:                # Polish every dictation
        enabled: false
        model: ''          # '' = the default
        timeout: 5
        prompt: ...
    rewrite:               # Transforms, the Rephrase hotkey and command
        model: ''
        timeout: 30
    ollama:
        address: http://localhost:11434
    lmstudio:
        address: http://127.0.0.1:1234
```

A feature with its own `model` uses that model and then the default
`fallbacks`. A transform may name its own model (`model: codex/gpt-5.5`).
The deadline covers the whole call: a 5 s polish gives up after 5 s even with
fallbacks left, and the dictation goes out without polish.

## Secrets

`platform/*/secrets.py` mirrors one API: `protect(bytes)` and
`unprotect(bytes)`. Windows uses DPAPI (`win32crypt.CryptProtectData`), so only
your Windows account can read the file; macOS returns the bytes unchanged
until a Keychain version replaces it (the file is still readable only by you).

`llm/credentials.py` keeps one file per provider in
`%APPDATA%\koli\credentials\<provider>.bin`: the Codex and Grok tokens, the
Gemini, OpenRouter and OpenAI keys, and the optional LM Studio token. No key
is ever in `user_settings.yaml`, so settings exports and bug-report bundles
never contain one (neither copies `credentials\`).

## Device-code login (Codex, Grok)

1. **Connect** asks for a code (Codex:
   `auth.openai.com/api/accounts/deviceauth/usercode`; Grok:
   `auth.x.ai/oauth2/device/code`).
2. A popup shows the code large, with Copy code, Open page, "Waiting for
   approval…" and Cancel. The page polls `ai_login_status` every 2 seconds.
3. A thread polls the provider at its interval (Grok's `slow_down` adds 5 s).
   Codex's approval returns an authorization code and verifier, exchanged at
   `auth.openai.com/oauth/token`.
4. Tokens are saved; the card shows "Connected as you@…".

## Keeping the login alive

Codex refresh tokens rotate and are single-use: refreshing the same one twice
kills the login. So:

- Refresh before a call when the token expires within 5 minutes or was last
  refreshed over 6 days ago. At start and once a day, Koli refreshes any token
  older than 6 days, so a rarely used subscription doesn't lapse.
- One refresh at a time per provider: a thread lock in the app, plus a lock
  file (`credentials\<provider>.lock`, stale after 45 s) for `--doctor`
  running beside the app. A waiter re-reads the saved token instead of
  refreshing again.
- 401: refresh once, retry once. A dead refresh token (`invalid_grant`,
  "already used") marks the card **Reconnect needed** and stops retrying.
- Grok often answers a refresh without a new refresh token; the old one is
  kept.
- 426 means the client version Koli reports is too old, not the login. Koli
  reports the highest of a built-in floor, the latest `@openai/codex` /
  `@xai-official/grok` on npm (checked daily, kept in
  `%APPDATA%\koli\ai-client-versions.json`) and any version a 426 names, and
  retries once.
- Codex lists models as client version `99.0.0` (older versions hide new
  models); chat calls use the real version.

## Settings › AI

The section is renamed **AI**. Its description no longer says nothing leaves
the computer; each route shows a **Local** or **Cloud** badge instead.

- **Providers**: one card each. Status chip, then:
  - Ollama, LM Studio: the address, the model count, Test.
  - Codex, Grok: Connect (the device-code popup), or "Connected as …",
    the last refresh, Test, Disconnect.
  - Gemini, OpenRouter, OpenAI: Add key (checked by listing models, shown as
    `…abcd`), Test, Remove.
- **Default model**: provider then model, from the provider's live list,
  plus **Fallbacks** (add, remove).
- **Polish every dictation**: on/off, its own model (or the default), timeout,
  instruction.
- **Rewrite** (Transforms and Rephrase): its own model, timeout.

The Transforms card on the Commands screen shows the rewrite model and its
Local/Cloud badge instead of "Ollama reachable".

## What changes in existing code

- `text_postprocess._ollama_polish` goes. `postprocess()` takes an optional
  `polish` callable and runs it last (snippets still skip it); `state_manager`
  passes `llm` polish when it is on.
- Transforms, the Rephrase hotkey and the Rephrase voice command call
  `llm.complete('rewrite', …)`. The rephrase prompt lives once, in
  `llm/prompts.py`. The `ollama_config_provider` lambdas go.
- `services/system.ollama_probe` and `doctor._probe_ollama` become
  `llm.provider_status()`; the doctor checks the default model's provider.
- `postprocess.ollama.*` is gone (no backward compatibility). The legacy Tk
  settings show `ai.polish.enabled` and `ai.model`.
- User-facing "Ollama" text becomes "AI" or names the provider in use.

## Testing

Codex and Grok can't run in CI. Unit tests cover, with recorded payloads:
the Codex SSE parser (text deltas, `response.completed` fallback, failures),
chat-completions streaming, route parsing and fallbacks with the deadline,
the refresh rules (skew, stale age, rotation kept or reused, dead token),
the 426 version parse, credential round-trip, and `<think>` stripping. A fake
HTTP server covers Ollama and LM Studio end to end. The live check is each
card's Test button, and the real test you run before the commit.
