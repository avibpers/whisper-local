# Wake word: "Hey Koli", or your own word in your own voice

*Design spec, 2026-10-12. Approved in conversation; implementation plan in
`docs/plans/2026-10-12-1747-wake-word-implementation.md`.*

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
| No command matches | The bar shows "Didn't catch that - say it again" and Koli listens `command_timeout_seconds` (5) more for the command, without the wake word. A second miss shows "Didn't catch that" and sleeps; one retry only, so a TV can't keep Koli awake. Nothing is typed. |
| Any try that is probably you | The terminal shows what Whisper heard and the voice score: woke, not the word, almost your voice (within 0.10 of "Sounds like you"), or a command too quiet to count. With "Hey Koli" only text at least half like it is shown. Nothing for other voices or the TV. |
| Personal word, but your voice on this microphone isn't known (new headset, or the voiceprint model isn't loaded) | Doesn't wake. The Commands screen says to record the word on this microphone. See open questions. |
| Dictation (hotkey or auto-dictation), meeting running, or command mode from the hotkey | Not listening. It resumes when that ends. |
| PC locked | Not listening. If Koli can’t tell (the check fails), it counts as locked. |
| "Hey Koli" command that runs a program (`run`, also in then-steps) | Always asks first (the confirm dialog), since anyone could have said it; where no dialog can ask, it doesn’t run. Your own word was checked against your voice, so it runs like a hotkey command. |
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
   Koli keeps the audio. The check runs at the burst's first 0.3 s pause
   (the word said on its own wakes Koli then, not after the 0.6 s that ends
   a burst), or 1.2 s in when you keep talking (one breath), whichever comes
   first. If that missed and the burst grew ("Hey… Via" with a gap), it is
   checked once more at its end, unless it is longer than 3 s of speech
   (talk, not a wake word). At most two checks per burst.
   *Speed (2026-10-14):* the chime came 1.0-1.5 s after "Hey Via": the 0.6 s
   wait, a language-detection pass (~0.4 s in the running app) and the
   shared Whisper queue. Each "Wake check" log line now says where in the
   burst it ran and how long Whisper took.
   **Quieter than you is silence.** Music from the speakers reads as speech
   to TEN VAD and never pauses, so in the first real test (2026-10-12) every
   burst ran to the 8 s cut: commands waited ~6 s and a wake word said
   mid-burst was never checked. Speech more than 15 dB (the wake word's own
   margin, not "Only my voice"'s, which at 4 dB took every try for background) under
   your level, on a 0.3 s energy average like the recorder's own gate,
   counts as silence. Your level is the one measured when you recorded your
   word on this microphone, else the one learned from hotkey dictations.
   Whatever keeps sounding, a command is cut 4 s after the wake word, and a
   command after a pause or a follow-up at 4 s.
3. **Your voice (personal word only).** `my_voice_similarity(audio, mic)`
   compares the burst with your voiceprint on this microphone. Below the
   threshold, the burst is dropped before Whisper runs. That keeps the TV
   and other people cheap.
4. **Whisper.** The burst head is transcribed with beam size 1, no
   initial prompt (prompt words can't fake a match) and always as
   "transcribe" (the Translate profile would turn a Hebrew word English).
   The language is the word's own, from how it is spelled (Hebrew letters
   → Hebrew, Latin → English), so Whisper skips detecting it: 106 → 58 ms on
   an idle GPU. Other scripts fall back to the one in Settings. Koli only
   checks when it is idle, so a check never competes with a dictation.
5. **Match.** The transcript is normalised the way commands are
   (`normalize_spoken`). Its first words, with one word more or less and
   the spaces taken out ("heykoli", "hey ko li"), are compared with each
   accepted spelling by similarity ratio. The spelling list carries the
   variants, so the margin is small: `sensitivity` low = 0.97, medium = 0.9,
   high = 0.8. "hey cool" (0.86) only wakes at high; "hey kelly" never. A
   personal word gets 0.15 more (it must sound like you too, and has only
   three recordings' spellings), so "coli" for "koli" passes at medium.
6. **Command.** Recording continues until a 0.45 s pause (shorter than the
   0.6 s that ends a burst while asleep: you were just asked for it), Koli
   transcribes the whole burst in the language from Settings (the wake
   check read it in the word's language, which would spell a Hebrew command
   in English letters), with beam 1, strips the wake word from the start,
   and sends the rest through `VoiceCommandManager`, the same path as hotkey
   commands. If only the wake word was said, Koli waits for the command
   (pause case) without reading the burst again.
7. **The chime.** For 0.6 s after the wake sound (0.45 s long) no new burst starts, so
   the chime through the speakers can't become the command. A burst already
   going (one breath) carries on.

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
3. The recordings give Koli your voice for the wake check: their average
   voiceprint is kept per microphone in `wake_voice.json`, separate from the
   voiceprint auto-dictation and meetings use (that one never learns from
   anything under 1.5 s, `LEARN_VOICEPRINT_S`, and a word is ~1 s).
   Comparing a word with recordings of the same word is also steadier than
   comparing it with whole dictations. The same word recorded on another
   microphone adds that microphone; a different word replaces everything.
   How much a try must score is `wake_word.voice_match` (0.45 until real
   measurements; **Test** shows your score next to it).
4. **Test**: say it once more and see whether it wakes.
5. **Reset to "Hey Koli"** clears the spellings and the voice requirement.

If the three recordings come back as very different text, Koli asks you to
record again: Whisper isn't hearing the word reliably.

The built-in "Hey Koli" ships with the spellings Whisper usually writes:
`hey koli`, `hey coli`, `hey coley` (what Whisper wrote for a synthesised "Hey Koli"), `hey collie`, `hey cooley`, `hey kohli`, `היי קולי`,
`היי כולי`, `הי קולי`. ("hey colly" and "hey kolly" were left out: they put
"hey kelly" within one letter.) Real use will add more.

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
  `hotkey` values (`media_play_pause`, `media_next`, ... already in the
  Windows keyboard map). Spotify and most players respond without any
  account link. Shipped as commands in English and Hebrew. A command made
  only of media keys is what a "Hey Koli" follow-up may run.
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
    # How much your recorded word must sound like you (0-1)
    voice_match: 0.45
    # How long to wait for a command after the wake word alone
    command_timeout_seconds: 5
    # How long to keep listening for another command after one runs
    follow_up_seconds: 5
```

The settings live on the Commands screen's Wake word card (the switch,
sensitivity, voice match for a personal word, the two timeouts). `phrases`
and `only_my_voice` are written by recording and reset, not edited.

## Pieces

| Piece | Where | Does |
|-------|-------|------|
| Listener | `wake_word.py` | Tap on the recorder, burst cutter (TEN VAD), voice check, check budget, Whisper check, the sleeping/awake/waiting/follow-up states. Fed one frame at a time with everything injected, tested with fake audio and a fake clock (`tests/test_wake_word.py`). |
| Matcher | `wake_word.py` | Normalise + fuzzy-compare the start of a transcript with the spellings; strip the wake word. |
| Your wake voice | `wake_word.py` (`WakeVoices`) | The average voiceprint of your recordings, per microphone, in `wake_voice.json`. |
| Locked PC | `platform/*/session.py` | Windows: the input desktop can't be opened or switched to on the lock screen; macOS: stub, unlocked. Polled every 2 s. |
| Running the command | `state_manager.py` (`run_wake_command`) | Claims "processing" under the state lock, transcribes, strips the wake word, runs the command (media only for "Hey Koli" follow-ups), flashes the bubble. |
| Voice | `services/meetings.py` | Lends its voiceprint model (`voiceprint_of`, `voiceprint_ready`). |
| Commands | `voice_commands.py`, `services/commands.py`, Commands screen | `other_triggers` ("Also say"); media commands; `is_media_command`. |
| Setup | `services/commands.py`, `ui/bridge.py` (`wake_word_*`), Commands screen | Record x3 / save / test / reset; the Wake word card. |
| Bubble | `bar.js`, `ui/host.py` | "Listening for a command", then "Anything else?" during the follow-up; "Didn't catch that". |
| Sound | `audio_feedback.py` | The wake sound (the 'ready' chime for now). |
| Measuring | `tools/measure_wake_word.py` | Whisper time on your settings, voice scores of short clips (me / other / tv), tap cost. |

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
- Voiceprints of wake-word-length clips: `voice_match` is 0.45 until
  `tools/measure_wake_word.py` has scored your own tries against other
  voices and the TV. If short clips are too noisy, check the voice on the
  whole burst instead.
- Real CPU cost of a Whisper check on CPU-only machines, and whether the
  check budget numbers (20 a minute, back off after 10) feel right. Measure
  before deciding whether "Later: match your recordings" moves up.
- One breath in two languages ("Hey Koli תפעיל ספוטיפיי"): Whisper may take
  the whole burst as English and mangle the Hebrew. The pause case is fine
  (the command is a burst of its own). If it fails in testing, transcribe
  only the audio after the wake word, by word timestamps.
- Measured already: a tap costs the audio thread ~65 µs per 10 ms chunk
  (0.65%) for resampling on WASAPI, so the tap takes the resampled audio.
