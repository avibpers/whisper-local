# AI actions: implementation plan

Spec: `docs/design/voice-agent.md`. Worktree `S:\Koli-voice-agent`, branch
`feat/voice-agent` from master. Each phase leaves the app working.

## Phase 1: the engine, no model
1. `platform/{windows,macos}/files.py`: `to_recycle_bin(paths)`, `open_recycle_bin()`,
   `start_menu_apps()`; imported in `platform/__init__.py`.
2. `agent/policy.py`: your folders, `inside_own(path)`, `level(ops)`.
3. `agent/actions.py`: records, `Recorder` (rehearsal) and `replay(ops)` (real),
   undo of each op, caps, unique names, executable refusal.
4. `agent/sandbox.py`: AST check, `rehearse(script) -> ops, said`.
5. `agent/journal.py`: history file, `signature(trace)`, `repeats(sig)`, `undo(run)`.
6. Tests: `tests/test_agent.py`.

## Phase 2: models, hook, bubble
1. `agent/planner.py`: writer and reviewer messages and parsing.
2. `agent/runner.py`: the flow, `agent` events, timeouts.
3. Config `ai.commands.*`, schema; `state_manager` hand-off with sources;
   `voice_commands` `script`/`agent` actions; "undo last action" shipped command.
4. Bridge methods; `bar.js` agent phases; mock API.

## Phase 3: approval
1. `agent/approval.py`: password in credentials, listen once, words and voice,
   click fallback.
2. Commands screen: AI actions card with password setup and Activity.

## Phase 4: repeats and saved commands
1. Offer on the third same signature; save as a `script:` command; editor and
   table show "AI script".
2. Docs (`voice-commands.md`, project index, CHANGELOG); full tests; live run
   with Luna; then the real test.
