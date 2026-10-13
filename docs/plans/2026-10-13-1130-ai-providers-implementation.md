# AI providers: implementation plan

Spec: `docs/design/ai-providers.md`. Worktree `S:\Koli-ai-providers`, branch
`feat/ai-providers` from master. Each phase leaves the app working.

## Phase 1: the layer, local providers

1. `llm/http.py`: urllib JSON GET/POST, a streaming POST, `HttpError(status, body)`.
2. `llm/sse.py`: split a byte stream into `(event, data)` pairs.
3. `llm/types.py`: `Result`, `ProviderError`, `ConfigError`, `ReconnectNeeded`.
4. `llm/transports/chat_completions.py` (stream and plain) and `ollama.py`.
5. `llm/providers.py`: the seven entries; Ollama and LM Studio complete.
6. `llm/routing.py`: `parse('provider/model')`, the route for a feature.
7. `llm/__init__.py`: `configure`, `complete`, `list_models`, `provider_status`,
   `test`; `<think>` stripping; deadline across fallbacks.
8. Config `ai:` block replaces `postprocess.ollama`; schema entries.
9. Callers: `postprocess(polish=…)`, state_manager polish and rephrase,
   transforms, voice_commands, main; `llm/prompts.py`.
10. Tests: routing, think stripping, fake-server Ollama and LM Studio.

## Phase 2: secrets and API-key providers

1. `platform/{windows,macos}/secrets.py` (`protect`, `unprotect`), imported in
   `platform/__init__.py`.
2. `llm/credentials.py`: load, save, delete, a file lock.
3. Gemini transport; OpenRouter, OpenAI entries; key check by listing models.
4. Tests: credential round trip, Gemini payload mapping.

## Phase 3: subscriptions

1. `llm/client_version.py`: npm latest, 426 minimum, highest-of.
2. `llm/oauth.py`: token store, refresh rules, rotation, locks, Codex and
   Grok device logins, JWT email and account id.
3. `llm/transports/codex.py`: Responses request, SSE parser.
4. Grok entry on chat completions with its headers.
5. Keep-alive thread started from `main`.
6. Tests: SSE parser, refresh rules, 426 parse.

## Phase 4: UI and the rest

1. Bridge: `ai_providers`, `ai_models`, `ai_test`, `ai_set_key`, `ai_remove`,
   `ai_login_start`, `ai_login_status`, `ai_login_cancel`; URL allow list.
2. Settings › AI: provider cards, model pickers (`ai_model`, `ai_models`
   controls), Local/Cloud badges; Commands' Transforms chip; mock API.
3. Doctor, diagnostics, `services/system`, legacy Tk settings, wording.
4. `docs/project-index.md`, CHANGELOG.
5. Full test run, app launch, then ask for the real test.
