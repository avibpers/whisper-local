# How Claude Code's voice dictation works (reference for Koli)

Reverse-engineered on 2026-10-27 from the shipped artifacts on this machine:
Claude Code CLI 2.1.311 (`claude.exe`, a Bun-compiled binary whose JavaScript
is embedded as plain text) and the VS Code extension 2.1.310
(`extension.js`, `webview/index.js`, `webview/index.css`). Everything marked
**observed** was read from that code. Everything marked **inferred** is a
conclusion from it. No network traffic was captured and nothing was run.

The short version: the "amazing" part is not in the client. The client is a
thin shell around a server-side streaming recognizer. What the user sees as
"fixing itself while I talk" is the server re-sending a better interim
transcript and the client replacing the whole italic span each time.

## 1. Architecture (observed)

```
mic ──16 kHz mono int16──▶ client ──WebSocket──▶ Anthropic speech-to-text server
                                        ◀── TranscriptInterim / TranscriptEndpoint
input box = committed text + " " + current interim (styled italic/dim)
```

- **No local model.** The CLI and the extension both open a WebSocket to
  `wss://api.anthropic.com/api/ws/speech_to_text/voice_stream`. Authentication
  is the user's claude.ai OAuth bearer token. Without a claude.ai sign-in the
  feature is hidden ("Voice mode requires a Claude.ai account").
- **Microphone capture** is a native Node addon (`audio-capture.node`,
  logged as `audio-capture-napi`) producing raw PCM. On platforms without the
  addon the CLI falls back to SoX `rec -q --buffer 1024 -t raw -r 16000 -e
  signed -b 16 -c 1 -`. On Windows there is no fallback.
- **Audio goes out as soon as it is captured.** While the socket is still
  connecting, chunks are buffered and flushed on open as coalesced frames of
  at most 32 000 bytes (1 second of audio). Chunks after `CloseStream` are
  dropped.

## 2. The wire protocol (observed)

Query parameters on the WebSocket URL:

| Parameter | Value | Meaning |
|---|---|---|
| `encoding` | `linear16` | raw signed 16-bit PCM |
| `sample_rate` | `16000` | |
| `channels` | `1` | |
| `endpointing_ms` | `300` | silence that closes an utterance |
| `utterance_end_ms` | `1000` | silence that ends a turn |
| `language` | from settings, default `en` | |
| `use_conversation_engine` | `true` | server-side model switch, see §4 |
| `forward_interims` | `typed` (gated by feature flag `tengu_brick_follow`) | interim frames carry a type |

Headers: `Authorization: Bearer <oauth>`, `x-app: cli` or `vscode`,
`anthropic-client-platform`, and `x-config-keyterms` (§4).

Client to server: binary frames of PCM, `{"type":"KeepAlive"}` right after
open and every 8 s, `{"type":"CloseStream"}` to finish.

Server to client, JSON:

| Type | Client reaction |
|---|---|
| `TranscriptInterim` / `TranscriptText` | `data` replaces the *whole* current interim; shown as `committed + " " + data` |
| `TranscriptEndpoint` | the pending interim becomes final: appended to `committed`, interim cleared |
| `TranscriptError` | `description` shown as an error |
| `error` | `message` shown as an error |

**Inferred:** this vocabulary (`linear16`, `endpointing`, `utterance_end_ms`,
`KeepAlive`, `CloseStream`, a keyterms list capped at 1024 bytes) matches the
Deepgram streaming API word for word. The server is most likely a Deepgram
Nova-class model behind an Anthropic proxy. Not observed; the client never
names the vendor.

## 3. Why it looks like it "fixes itself" (observed)

Both clients keep two strings:

- `committed`: finals so far, joined with single spaces.
- `interim`: the latest interim frame, verbatim.

On every frame the input is rewritten as
`prefix + committed + " " + interim + suffix`, where prefix and suffix are
whatever was already in the box left and right of the cursor when recording
started (the "anchor"). The cursor is placed right after the interim.

- The interim is **replaced, never appended**. When the recognizer revises
  "I want to right" into "I want to write", the client simply redraws the
  span with the new text. There is no diffing, no client-side language model,
  no correction logic at all.
- On `TranscriptEndpoint` the interim is promoted to committed text and
  styled normally. A new utterance starts a new interim.
- If the user types while dictating (the box no longer equals what the client
  last set), dictation stops and the typed text wins. Telemetry calls this
  `input_diverged`.

Styling:

- CLI (Ink): the interim character range is passed as `dimRange` to the text
  input and rendered with `dimColor` only.
- VS Code webview: `<span class="voiceInterim">` with
  `color: var(--app-secondary-foreground); font-style: italic`. This is the
  italic look seen in the editor. There is no bold.

## 4. Where the "understands context" comes from (observed)

Two inputs, both cheap:

1. `use_conversation_engine=true` on the URL. A server-side switch; what it
   does is not visible to the client. **Inferred:** a model tuned for
   conversational dictation with punctuation and self-correction handling.
2. `x-config-keyterms`: a comma-separated list, deduplicated, ASCII only,
   capped at 1024 bytes and at most 50 terms, built from:
   - a fixed list: `MCP symlink grep regex localhost codebase TypeScript JSON
     OAuth webhook gRPC dotfiles subagent worktree` (VS Code adds `VS Code
     IDE webview IntelliSense`);
   - the basename of the working directory;
   - the current git branch, split on `-_./` and camelCase, keeping tokens of
     3 to 20 characters;
   - in the CLI, file names from the conversation.

This is the equivalent of Koli's hotwords and learned vocabulary, fed per
session instead of per model load.

## 5. Interaction model (observed)

| Behaviour | Claude Code | Koli counterpart |
|---|---|---|
| Key | CLI: `space` on an empty prompt (`voice:pushToTalk`). VS Code: Ctrl+D (Cmd+D on macOS), or the mic button | `hotkey_listener.py` |
| Hold vs tap | one key, decided by time held: under 200 ms is a tap (toggle), longer is hold-to-talk; release after the threshold stops | `hotkey_gestures.py` double-tap lock |
| Tap mode auto-stop | 15 s without a new transcript, hard cap 120 s | VAD silence stop |
| Hold mode release | 200 ms grace after the last key-repeat | |
| Submit | tap mode or `voice.autoSubmit`: a final of 3 or more words submits the prompt; hold mode arms a "double tap to send" | `delayed_send.py` |
| Typing while dictating | stops dictation, keeps the typed text | `auto_dictation.py` cancel on typing |
| Level meter | RMS of each PCM chunk, normalised to 0..1 with a square root, last 16 values kept; VS Code throttles to one level per 50 ms | `level` events to the floating bar |
| Early failure | one retry after 250 ms if the socket fails before the first transcript; a circuit breaker pauses voice after 3 early failures in 10 s | |
| Silent drop | if the server sent nothing for a whole recording, the buffered audio is replayed once on a fresh connection | |
| Finish | `CloseStream`, then wait up to 5 s for `TranscriptEndpoint` (1.5 s if nothing arrives); any pending interim is promoted to final | `finalize()` in `streaming_manager.py` |
| No speech | after 2 s of recording with no transcript: "No audio detected" if the RMS never passed 0.01, else "No speech detected" | |

## 6. What this means for Koli

**What Koli already has.** The same two-string model exists today, behind the
experimental streaming flag: `streaming_manager.py` emits revising partials
and finals from sherpa-onnx with endpoint detection; `state_manager.py`
publishes `streaming_text` events to the bar and hands only finals to
`streaming_delivery.py`, which types them at the cursor. The bar shows the
last 48 characters of the partial. Structurally this is Claude Code's design.

**What is different.**

1. **Recogniser quality.** Claude Code streams to a server-grade model.
   Koli's live partials come from a small on-device sherpa-onnx model, and the
   accurate text only arrives from faster-whisper after the recording ends.
   The live italic text can only be as good as the streaming model. Options:
   a better streaming model for the partials (sherpa-onnx has larger
   Zipformer and Paraformer models), or a cloud streaming provider as an
   opt-in engine behind the existing `llm/`-style provider abstraction.
   Anthropic's endpoint itself is not usable: it needs a claude.ai OAuth
   token and is not a public API.
2. **Display.** Koli shows the partial as a tail in the bar with no
   committed-versus-interim distinction. Claude Code's effect is: committed
   text in normal style, the current utterance in italic secondary colour,
   whole span replaced on every frame, cursor after it. On Koli's own Preact
   pages (Live screen, meeting panel, bar) this is one `<span>` with a class.
3. **Where the text lives.** Claude Code owns its text box. Koli dictates into
   other apps by pasting, so the italic revising interim cannot be shown
   inside Notepad or a browser field. The honest options are: show the
   interim in the floating bar or a caret-following bubble and type only on
   endpoint (what `deliver_to_cursor` does), or type-and-retract with
   backspaces, which is fragile and visible. Owning the editor is what makes
   the Claude Code effect possible; outside Koli's own windows the bar is the
   place for it.
4. **Context terms per session.** Claude Code rebuilds the keyterm list every
   recording from the working directory and branch. Koli could pass the
   foreground app's rule vocabulary, the active profile's terms and the
   learned vocabulary to the streaming recogniser as hotwords at recording
   start (sherpa-onnx supports hotword lists for its transducer models).
5. **Hold-or-tap on one key with a 200 ms threshold** is a simpler gesture
   than a double-tap lock and worth copying.

## 7. Evidence trail

- CLI stream module: constants `L="/api/ws/speech_to_text/voice_stream"`,
  `F=8000` (keep-alive), `x={safety:5000,noData:1500}`; function `zSo`
  builds the URL and handles the four message types.
- CLI controller class `A` (file chunk `chunk-1mct7jnw.js`): state machine
  idle → recording → processing → idle; constants `N=200` release grace,
  `G=600` auto-repeat fallback, `U=5000` focus silence, `Y=15000` tap
  silence, `q=120000` tap cap, `X=16` level history, `D=1e4, L=3` circuit
  breaker.
- CLI input hook `Idt`: anchor prefix/suffix, `setValueWithCursor`,
  `setInterimRange`, `voice_transcript_insert` telemetry; `dimRange` reaches
  the `yw` text input and `es()` renders it with `dimColor`.
- CLI audio module: `k=16000, x=1`, SoX arguments, `audio-capture.node`
  loader, Windows "no fallback" path.
- Keymap defaults: `space: "voice:pushToTalk"` in the Chat context; Ctrl+D
  in the CLI is `app:exit` and cannot be rebound.
- VS Code `extension.js`: `BB6` URL, `HB6` fixed keyterms, `MB6` keyterm
  packer (1024 bytes), `Q$4` connect (same query string plus `x-app:
  vscode`), `X$4` compose (`committed` + interim), `Qu=15000`, `aY1=120000`,
  `O=160000` bytes replay buffer, retry delay `j=250`, level every 50 ms.
- VS Code `webview/index.js`: keydown handler for Ctrl/Cmd+D with
  `G4=200` ms hold threshold, `"edited"` branch stops dictation on typing;
  `webview/index.css`: `.voiceInterim_cKsPxg{color:var(--app-secondary-foreground);font-style:italic}`.
