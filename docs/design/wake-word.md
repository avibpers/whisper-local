# Wake word: "Hey Koli", or your own word in your own voice

*Design spec, 2026-10-12. Approved in conversation; no implementation plan yet.*

## Goal

Run voice commands with no hotkey. Say "Hey Koli" and Koli wakes, waits for a
command, and runs it: "Hey Koli, play Spotify". It works in every language
Koli transcribes. Out of the box the wake word works for anyone. You can
record your own word in any language, and from then on Koli wakes only for
your voice.

## Behaviour (agreed)

| Situation | What Koli does |
|-----------|----------------|
| Feature off (default) | No microphone stream, no thread, nothing loaded. |
| Feature on, no word recorded | Wakes on "Hey Koli" from any voice. |
| You recorded your own word | Wakes only on that word, and only when it sounds like you on the current microphone. "Hey Koli" no longer works. |
| Wake word, then a pause | Wake sound. The floating bar shows "Listening for a command". Koli records until you stop speaking, then runs the matching command. |
| Wake word and command in one breath ("Hey Koli play Spotify") | The rest of the sentence is the command. No pause needed. |
| Wake word, then silence for `command_timeout_seconds` (5) | Goes back to sleep quietly. |
| Command recognised | Runs it like a hotkey command. Then stays awake `follow_up_seconds` (5) for another command without the wake word ("next", "louder"). With a personal word, follow-ups must sound like you. With "Hey Koli" there is no voice check, so follow-ups only run media commands (play/pause, next, volume). Lyrics from the song you just started can't trigger "undo" or "lock screen". |
| No command matches | The bar shows "Didn't catch that", then sleeps. Nothing is typed. |
| Personal word, but your voice on this microphone isn't known (new headset, or the voiceprint model isn't loaded) | Doesn't wake. The Commands screen says to record the word on this microphone. See open questions. |
| Dictation (hotkey or auto-dictation), meeting running, or command mode from the hotkey | Not listening. It resumes when that ends. |
| PC locked | Not listening. |
| Hotkeys paused, or model loading | Not listening. |

Default: **off**. Turning it on or off, or recording a new word, applies
live with no restart.

## Why Whisper hears the wake word

Typed-word wake-word spotters (sherpa-onnx KWS) only have English and Chinese
models, and the word must be spelled in that model's sounds. A Hebrew
word like "קולי" can't be set up there. Whisper already transcribes every
language Koli supports, including the Hebrew model (`language_model`), so it
checks for the wake word. The cost is CPU:

- **Personal word:** cheap. The voice check drops other voices (TV, people,
  music) before Whisper runs, so Whisper only hears your own short bursts.
- **"Hey Koli" for anyone:** nothing filters before Whisper, so a TV, a
  call on speakers or a song would send every burst to Whisper. A check
  budget caps it: at most 20 Whisper checks a minute. After 10 non-wake
  bursts in a row, the next minute allows one check every 10 s. A wake
  resets the budget. Missing a wake word while the TV is loud is the price.
  Recording a personal word is the fix, and the Commands screen says so.

## How it listens

```
mic ──► TEN VAD ──► speech burst ──► [personal word: is it you?] ──► Whisper on the first ~2 s ──► starts with the wake word?
        (own instance)  (≤ 2 s head)       my_voice_similarity()          beam 1, no prompt              fuzzy match
                                                                                                          │ yes
                                                                                    record until silence ◄┘
                                                                                    Whisper on the whole thing
                                                                                    drop the wake word, match a command
```

1. **Microphone.** The recorder's stream is already open all the time, and
   the listener is a tap on it (`AudioRecorder.add_tap`), the same way
   meeting mode listens. No second stream. While listening isn't allowed
   (see the table), it ignores what the tap delivers. The command is
   collected from the same tap, so nothing is lost between the wake word
   and the command.
2. **Speech bursts.** It runs its own TEN VAD instance; the speech check and
   live detection stopped sharing one in `27fc52a`. When speech starts,
   Koli keeps the audio. The check runs on the first ~2 s, or on the whole
   burst if it is shorter. A burst that has already been checked and wasn't
   the wake word is ignored until the next silence.
3. **Your voice (personal word only).** `my_voice_similarity(audio, mic)`
   compares the burst with your voiceprint on this microphone. Below the
   threshold, the burst is dropped before Whisper runs. That keeps the TV
   and other people cheap.
4. **Whisper.** The burst head is transcribed with beam size 1 and no
   initial prompt, so prompt words can't fake a match. The language follows
   `whisper.languages`. If Whisper is busy, the burst is skipped, never
   queued.
5. **Match.** The transcript is normalised the way commands are
   (`_normalize_spoken`). Its first words are compared with each accepted
   spelling by similarity ratio. `sensitivity` sets the threshold:
   low = 0.9, medium = 0.8, high = 0.7.
6. **Command.** Recording continues until silence. Koli transcribes the
   whole recording, strips the matched wake word from the start, and sends
   the rest through `VoiceCommandManager`, the same path as hotkey
   commands. If nothing is left, Koli waits for the command (pause case).

Non-wake bursts are never saved or logged, even with `log_transcriptions`
on. Audio stays in memory and is dropped after the check.

## Recording your own word

In the app, under Commands → Wake word:

0. Opening the screen loads the voiceprint model (`_ensure_embedder`). It
   is downloaded the first time, with progress shown. **Record** stays
   disabled until it is ready, so no recording is wasted on a model that
   isn't loaded yet.
1. Press **Record** and say your word. Do this 3 times. A recording that
   gives no voiceprint (too short, too quiet) doesn't count and must be
   redone. After the three, personal mode must work on this microphone
   even if you have never dictated on it.
2. Each recording is transcribed and shown ("Heard: קולי"). Every distinct
   spelling Whisper produced is saved as an accepted spelling, so you never
   type the word, and the spellings Whisper actually writes are the ones
   that match.
3. The recordings give Koli your voice for the wake check. How they are
   kept is decided by measurement (see open questions). A word is ~1 s, and
   Koli never teaches your shared voiceprint from anything shorter than
   1.5 s (`LEARN_VOICEPRINT_S`). Either setup asks for something longer (the
   word twice in a row), or the wake clips are kept as a separate voiceprint
   that leaves the one auto-dictation uses alone.
4. **Test**: say it once more and see whether it wakes.
5. **Reset to "Hey Koli"** clears the spellings and the voice requirement.

If the three recordings come back as very different text, Koli asks you to
record again: Whisper isn't hearing the word reliably.

The built-in "Hey Koli" ships with the spellings Whisper usually writes:
`hey koli`, `hey coli`, `hey colly`, `hey kolly`, `hey cooley`, `היי קולי`.
Real use will add more.

## Commands in any language

- `other_triggers` adds more phrases to a command, so it can be said
  several ways or in several languages. `trigger` stays one string because
  it is the id that layers shipped commands under yours:
  ```yaml
  - trigger: "play spotify"
    other_triggers: ["תפעיל ספוטיפיי"]
    run: "start spotify:"
  ```
  Matching already keeps non-Latin letters (`\w` is Unicode), so Hebrew
  phrases match today.
- **Media keys** for play/pause, next, previous, volume up/down/mute, as
  `hotkey` values. Spotify and most players respond without any account
  link. Check the key names the keyboard module accepts.
- **Spoken numbers** (`{n}`) understand English number words only. Hebrew
  number words are out of scope for the first version; digits from Whisper
  already work.

## Configuration

```yaml
wake_word: # Say a word to run a voice command, no hotkey
    enabled: false
    # Accepted spellings of your own word, filled by recording it in the app.
    # Empty = the built-in "Hey Koli", for any voice.
    phrases: []
    # Wake only when the word sounds like you (set by recording your word)
    only_my_voice: false
    # How close the heard words must be to the wake word: low, medium, high
    sensitivity: medium
    # How long to wait for a command after the wake word alone
    command_timeout_seconds: 5
    # How long to keep listening for another command after one runs
    follow_up_seconds: 5
```

Settings schema: one switch in Simple mode ("Wake word"), the rest in
Advanced. Recording and reset happen on the Commands screen, not as raw
settings.

## Pieces

| Piece | Where | Does |
|-------|-------|------|
| Listener | new `wake_word.py` | Mic stream, TEN VAD bursts, voice check, Whisper check, the sleep/awake/follow-up state machine. Pure Python with a fake clock in tests, like `auto_dictation.py`. |
| Matcher | `wake_word.py` | Normalise + fuzzy-compare the start of a transcript with the spellings; strip the wake word. |
| Lock and session state | `platform/*/` | "Is the PC locked" (Windows: session-lock notification; macOS: no-op stub that says unlocked). |
| Coordination | `state_manager.py` | Pauses the listener around dictation, command mode and meetings; runs the command through the existing command path. |
| Voice | `services/meetings.py` | Reuse `my_voice_similarity` and `learn_my_voice`. |
| Commands | `voice_commands.py`, `services/commands.py`, Commands screen | `trigger` as a list; media-key actions. |
| UI | `ui/bridge.py`, Commands screen, `bar.js` | Record / test / reset the word; "Listening for a command" and "Didn't catch that" in the bar. |
| Sound | `audio_feedback.py` | A wake sound (distinct from the dictation start sound). |

## Later

- **Match your recordings directly.** Compare new audio with your recorded
  word, sound against sound, as a cheap first check before Whisper.
  Language-independent and low CPU. No ready-made open-source library does
  this today, so it is a build, and only worth it if Whisper checks cost too
  much CPU in practice.
- Hebrew number words for `{n}`.
- A wake word per profile.

## Open questions

- A personal word on a microphone with no voiceprint yet (new headset).
  The table says "doesn't wake, ask to record there". The other choice is to
  wake for anyone on that mic until you record. Confirm with real use.
- Voiceprints of wake-word-length clips. `MATCH`, `FIRST_MATCH` and the
  auto-dictation threshold were tuned on meeting chunks and whole
  dictations. A wake word is 0.5–1 s. Measure how far your own clips and
  other voices' clips score from your voiceprint before choosing the
  threshold. If short clips are too noisy, check the voice on the whole
  burst instead.
- Real CPU cost of a Whisper check on CPU-only machines, and whether the
  check budget numbers (20 a minute, back off after 10) feel right. Measure
  before deciding whether "Later: match your recordings" moves up.
