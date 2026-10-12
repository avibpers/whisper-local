# Wake Word: "Hey Koli", or Your Own Word in Your Own Voice

As a *user* I want **to say "Hey Koli, play Spotify" (or my own word, in any
language) and have the command run** so I can control my computer without
touching a hotkey.

Design: `docs/design/wake-word.md` (approved 2026-10-12).

## Background

Voice commands only start from the command hotkey today. The design adds a
wake word that is always listening while Koli runs. It must work in Hebrew
and every other language Koli transcribes, so Whisper does the hearing, not
an English/Chinese keyword spotter. Out of the box "Hey Koli" works for any
voice. Recording your own word makes it yours: only that word, only your
voice.

Most of the parts already exist:

| Need | Already in the code |
|------|---------------------|
| Hear the microphone all the time | `AudioRecorder` keeps one stream open from startup and hands every chunk (mono 16 kHz) to **taps** (`add_tap`), which meeting mode uses. A recorder rebuilt on a mic change carries its taps over. |
| Find speech | `meeting/capture.speech_prob_function()`: its own TEN VAD instance behind a lock (instances must not be shared, see `27fc52a`) |
| Transcribe any language | `WhisperEngine.transcribe_audio(audio, language, use_prompt)`: routes Hebrew to the second model; runs on the one engine thread |
| Is this you? | `MeetingService.my_voice_similarity(audio, mic)` and `learn_my_voice`; `VoiceBook.keep_self` keeps one voiceprint per microphone; `MY_VOICE_MIN = 3` |
| Run a command | `VoiceCommandManager.match_command` / `execute_command`; `StateManager._handle_command_transcription` |
| Media keys | `platform/windows/keyboard.py` already maps `media_play_pause`, `media_next`, `media_prev`, `volume_up`, `volume_down`, `volume_mute` |
| A policy class with fakes in tests | `auto_dictation.py` + `AutoDictationCoordinatorTests` |

## Target Architecture

```
AudioRecorder stream ──tap──► WakeListener (wake_word.py)
  (already running)            audio thread: copy chunk into a queue, nothing else
                               worker thread:
                                 TEN VAD frames ─► bursts (head = first 2 s)
                                 may_listen()? ─► budget ─► [personal: voice check]
                                 ─► Whisper on the head (quiet, beam 1, no prompt)
                                 ─► match_wake(text, spellings)
                                      │ woke
                                      ▼
                                 wake sound + 'wake' event (bubble: "Listening for a command")
                                 keep collecting until silence
                                      ▼
                       StateManager.run_wake_command(audio or text, only_media)
                                 ─► Whisper (routing, hotwords, no prompt) ─► strip wake word
                                 ─► VoiceCommandManager.match_command ─► execute
                                      ▼
                                 follow-up window (5 s), then sleep
```

`may_listen()` is false while hotkeys are paused, a model is loading, the
state manager isn't idle (dictation, auto-dictation, a command, transcribing),
a meeting runs, the PC is locked, or the setup screen is recording your word.
Then audio is simply ignored: there's no stream to open or close.

## Implementation Plan

1. Measure first (decides two numbers in the design)
- [x] `tools/measure_wake_word.py` (with a header comment): record clips
  from the current mic (or read `.wav` files) and print, for each one:
  - time of the **real path**: `transcribe_audio` with the user's config
    (`languages`, `language_model: he`), beam 1, on CPU-only and GPU. On a
    1–2 s clip `_route` may detect the language and then decode (two passes),
    and Hebrew goes to the second model
  - the same with the language fixed (the one detected at setup); if
    clearly cheaper and steadier, a personal word saves its language and
    checks use it
  - voiceprint similarity to your voiceprint on that mic, for your own
    wake-word clips (0.5 s, 1 s, 2 s) and for other voices or the TV
  - audio-thread cost of a tap on WASAPI: `_notify_taps` resamples every
    chunk while any tap exists (today only during meetings). If it shows,
    add `add_tap(fn, raw=True)` and resample in the wake worker
- [ ] Choose `WAKE_VOICE_MATCH`, and whether the voice check uses the head
  or the whole burst (`LEARN_VOICEPRINT_S` is 1.5 s for a reason: short clips
  may be too noisy)
- [x] Choose how setup keeps your voice (phase 9 depends on it):
  - (a) setup records ≥ 1.5 s (the word twice in a row) so each sample may
    join the shared voiceprint, or
  - (b) a separate wake voiceprint per mic (`VoiceBook`, `kind: 'wake'`),
    leaving auto-dictation's alone.
  Either way, 3 recordings must make personal mode work on a mic you never
  dictated on.
- [ ] Choose the check budget numbers (design: 20 a minute, back off after 10 misses)
- [ ] **Real test with the user:** run it on their mic, agree on the numbers

2. Wake-word matching and the check budget (pure Python, `wake_word.py`)
- [x] Module header (also on `platform/*/session.py`;
  `test_every_module_has_a_header_comment` fails without one);
  `DEFAULT_SPELLINGS` for "Hey Koli" (`hey koli`,
  `hey coli`, `hey colly`, `hey kolly`, `hey cooley`, `היי קולי`)
- [x] `match_wake(text, spellings, min_ratio) -> Optional[str]`: normalise
  like commands (`_normalize_spoken`), compare the transcript's first N
  words (N = words in the spelling, ±1 so "heykoli" and "hey ko li" count)
  with `difflib.SequenceMatcher`. Return the rest of the text, or None
- [x] `SENSITIVITY_RATIO = {'low': 0.9, 'medium': 0.8, 'high': 0.7}`
- [x] `CheckBudget(clock)`: `allow()`, `missed()`, `woke()`; at most N
  checks a minute, slower after M misses in a row, reset on a wake
- [x] Tests: English and Hebrew spellings, a near miss ("hey kelly" at
  high but not low), punctuation ("Hey, Koli!"), the rest of the sentence
  kept, the wake word not at the start (no match), budget with a fake clock

3. Commands: phrases in several languages, media-only follow-ups
- [x] `voice_commands.py`: optional `other_triggers: [..]` (strings). The
  `trigger` stays one string, because it is the id that layers shipped
  commands under yours (`id_key="trigger"`). Matching walks
  (phrase, command) pairs sorted by phrase length, longest first. `{n}`
  works in any of the phrases
- [x] `match_command(text, only=None)`: `only` filters which commands may
  match. `is_media_command(cmd)`: a `hotkey` made only of media keys, with
  no then-steps
- [x] `services/commands.py`: `save_command` cleans `other_triggers` (trim,
  drop empties and duplicates, check `{n}` per phrase); `test_phrase` says
  which phrase matched
- [x] Commands screen (`js/screens/commands.js`): "Also say" phrases under
  the main phrase (add/remove), shown on the command's card; `mock-api.js`
- [x] `commands.defaults.yaml`: media commands with Hebrew phrases: play/pause
  music, next/previous song, louder/quieter, mute (`other_triggers` such as
  `שיר הבא`, `תגביר`, `תנמיך`, `השתק`)
- [x] Tests: Hebrew phrase matches; a longer phrase wins over a shorter one
  across commands; `only=is_media_command` blocks "undo" and "lock screen";
  saving drops duplicate phrases; `{n}` in another phrase
- [ ] **Test in the app:** add "play spotify" / "תפעיל ספוטיפיי" →
  `run: start spotify:` from the Commands screen and run it with the command
  hotkey, both languages

4. Quiet short transcriptions (`whisper_engine.py`)
- [x] `transcribe_audio(..., beam_size=None, quiet=False)`: `beam_size`
  overrides for one call; `quiet` prints and logs no text (not even with
  `log_transcriptions`) and leaves `last_info` alone, so a wake check never
  overwrites a dictation's info
- [x] Same parameters on `whisper_engine_cpp.py` (mirrored API)
- [x] Test: a quiet call with a stub model logs no text and keeps `last_info`

5. The listener (`wake_word.py`, `WakeListener`)
- [x] Tap: copies the chunk into a bounded queue (drop the oldest when
  full). The worker thread does everything else
- [x] Bursts: 16 ms TEN VAD frames from `speech_prob_function()`; a burst
  starts on speech (≥ 0.5) and ends after 0.6 s of silence; a 0.3 s
  pre-roll so the first sound isn't clipped; head = the first 2 s; a burst
  is cut at 8 s
- [x] States: `sleeping → awake (in the wake burst) → waiting for command →
  running → follow-up → sleeping`
  - **sleeping:** at the head (or at the burst's end if it's shorter):
    `may_listen()`, budget, voice check (personal word), quiet Whisper,
    `match_wake`. One check per burst
  - **woke, burst already over:** the head's text holds the whole thing. If
    the rest is a command → `run_wake_command(text=rest)`, with no second
    Whisper pass; empty → wait for the command
  - **woke, burst still going:** keep collecting until it ends →
    `run_wake_command(audio=whole burst)`
  - **waiting for command:** the next burst within `command_timeout_seconds`
    → `run_wake_command(audio)`; nothing → sleep quietly
  - **follow-up:** the next burst within `follow_up_seconds`. Personal word:
    voice check first. "Hey Koli": `only_media=True`
- [x] `may_listen()`: enabled, not paused, `sm.is_idle()`, no model loading,
  no meeting running, not locked, not recording your word. When it turns
  false mid-wake (you pressed the hotkey), drop everything and sleep
- [x] Personal word without a usable voice check (`my_voice_similarity`
  returns None: unknown mic, model not loaded, no meeting service under
  `--legacy-ui`): don't wake; publish a `wake` notice once per mic
- [x] Events: `publish('wake', {'phase': 'awake' | 'follow_up' | 'sleeping'})`;
  `AudioFeedback.play_wake_sound()` (uses `app_ready.wav` until a dedicated
  sound exists)
- [x] `start() / stop() / apply_settings()` like `AutoDictation`; registers
  the tap on `sm.audio_recorder`
- [x] Tests with fakes (clock, transcribe, voice, state manager), fed frames
  of fake speech/silence: wake + pause + command; one breath; wake alone
  then timeout; follow-up runs "next" but not "undo" in "Hey Koli" mode;
  personal word rejects another voice before Whisper runs; budget stops
  checks under a TV; hotkey mid-wake drops the wake; nothing happens while a
  meeting runs or the PC is locked

6. Running the command (`state_manager.py`, `main.py`)
- [x] `run_wake_command(audio=None, text=None, only_media=False) -> bool`:
  refuses unless idle and not paused. The idle check and setting
  `is_processing` happen together under `_state_lock`, so a hotkey pressed
  in between can't start a dictation alongside the command;
  transcribes with routing and hotwords but no prompt; strips the wake word
  (`match_wake`, when present); `_handle_command_transcription(text, only=...)`;
  emits the `command` pipeline event with `mode='wake'`; bubble flash
  success / "Didn't catch that"
- [x] `_handle_command_transcription` gains `only`
- [x] `set_wake_listener()`; `main.setup_wake_word()` next to
  `setup_auto_dictation()`, never fatal; `shutdown_app` stops it
- [x] Tests: a stub engine and command manager; no-match flashes failure and
  types nothing; refuses while a dictation runs

7. Locked PC (`platform/*/session.py`)
- [x] Windows `is_locked()`: `OpenInputDesktop` + `SwitchDesktop` fails
  while the lock screen is up; the listener polls it every 2 s, not per frame
- [x] macOS stub returns False; import in `platform/__init__.py`
- [ ] **Manual test:** Win+L, say "Hey Koli", unlock → nothing ran

8. Settings
- [x] `config.defaults.yaml`: the `wake_word:` block from the design
  (`enabled`, `phrases`, `only_my_voice`, `sensitivity`,
  `command_timeout_seconds`, `follow_up_seconds`)
- [x] `settings/schema.py`: `wake_word.enabled` toggle in the commands
  section, Simple mode; `sensitivity` (low/medium/high) and the two
  timeouts in Advanced; `phrases` and `only_my_voice` internal and hidden
  (the setup screen writes them)
- [x] `settings/appliers.py`: `('wake_word.', Appliers.wake_word)` →
  `listener.apply_settings()`, live
- [x] `tools/make_mock_data.py` → refresh `mock-data.json`
- [x] Test: turning it on and off in settings starts and stops the listener
  (tap added/removed)

9. Recording your word (setup screen)
- [x] `services/meetings.py`: `ensure_voiceprint_model()` (starts
  `_ensure_embedder`, reports ready), `voiceprint_of(audio)` (public
  `_voiceprint`), and keeping the setup voiceprints the way phase 1 chose
  (shared with the length rule, or a separate wake entry)
- [x] `WakeListener.capture_word(timeout)`: pauses wake detection, waits
  for one burst (≤ 3 s, or longer if phase 1 chose "the word twice"),
  returns its audio. Setup works with the feature off: the setup calls
  register the tap for themselves and remove it when done (the listener's
  tap is tied to `enabled` otherwise)
- [x] `ui/bridge.py`, all `{ok, data}`:
  - `wake_word_prepare()`: voiceprint model ready? (download progress
    through the existing task events)
  - `wake_word_record()`: one sample → `{heard, voiceprint: bool}`; a
    sample without a voiceprint is refused ("too short or too quiet, say
    it again")
  - `wake_word_save(samples)`: needs 3 good samples whose texts agree
    (`match_wake` between them), else "Whisper heard different words,
    record again"; saves distinct spellings + `only_my_voice: true`
    through the settings service; adds the voiceprints for this mic
  - `wake_word_test()`: one burst → `{woke, heard, similarity}`
  - `wake_word_reset()`: back to "Hey Koli" for anyone
- [x] Commands screen, "Wake word" card: on/off, what wakes it now
  ("Hey Koli, anyone" / "קולי, only your voice"), Record ×3 with what was
  heard, Save, Test (shows the similarity, which helps tune), Reset; a
  notice when your voice isn't known on this mic; the personal word is
  disabled with a reason under `--legacy-ui`. In-page popups only, no
  `alert()`
- [x] `bar.js`: `wake` event → "Listening for a command" pill while awake
  or in follow-up; the `command` pipeline event already flashes
  success/failure
- [x] `mock-api.js`: the five calls
- [ ] **Real test with the user:** record a Hebrew word, test it, have
  someone else say it, play a video saying it

10. Docs
- [x] `docs/design/wake-word.md`: the microphone is a tap (no own stream);
  `other_triggers` instead of a list `trigger`; a short command is run from
  the head's text without a second pass; the numbers from phase 1
- [x] `docs/voice-commands.md`: wake word, `other_triggers`, media commands
- [x] `docs/project-index.md`: a **Wake Word** row (`wake_word.py`) and
  `platform/*/session.py`
- [x] `CHANGELOG.md` → Unreleased / Added

## Implementation Details

### Why a tap, not a stream of its own
The recorder's stream is already open all the time (the "first word
clipped" fix), so listening costs no new device handle. Windows already
shows the mic as in use. The tap is called on the audio thread, so it only
queues the chunk, like meeting mode's tap.

### Why `other_triggers`, not a list in `trigger`
`load_layered`, `save_entry`, `set_entry_disabled` and `delete_entry` all key
commands by `trigger`. A list there would break overriding or disabling a
shipped command. One id plus extra phrases keeps the layering untouched.

### Matching the wake word
```python
# "Hey, Koli! play spotify" with spellings ["hey koli", "היי קולי"]
match_wake("Hey, Koli! play spotify", spellings, 0.8)  # -> "play spotify"
match_wake("hey kelly what's up", spellings, 0.8)      # -> None
match_wake("היי קולי", spellings, 0.8)                 # -> ""  (woke, no command yet)
```

### Whisper contention
The engine thread runs one decode at a time. The listener only submits when
the state manager is idle, so a wake check can delay a hotkey dictation by
at most one short decode (measured in phase 1). Meetings never compete:
the listener is off while one runs.

### Media while awake
Music keeps playing while Koli waits for a command, and Koli doesn't pause
it ("next song" must still work). Loud music may hide the command; that's a
known limit for v1.

## Scope

| File | Changes |
|------|---------|
| `wake_word.py` (new) | Matcher, check budget, `WakeListener` |
| `tools/measure_wake_word.py` (new) | Phase 1 measurements |
| `platform/{windows,macos}/session.py` (new) | `is_locked()` |
| `platform/__init__.py` | Import `session` |
| `voice_commands.py` | `other_triggers`, `only` filter, `is_media_command` |
| `commands.defaults.yaml` | Media commands with Hebrew phrases |
| `services/commands.py` | Save/test `other_triggers` |
| `whisper_engine.py`, `whisper_engine_cpp.py` | `beam_size`, `quiet` |
| `state_manager.py` | `run_wake_command`, `set_wake_listener`, `only` in command handling |
| `main.py` | `setup_wake_word`, shutdown |
| `audio_feedback.py` | `play_wake_sound` |
| `services/meetings.py` | Voiceprint model ready, voiceprint of a clip, keep several |
| `config.defaults.yaml`, `settings/schema.py`, `settings/appliers.py` | `wake_word` settings |
| `ui/bridge.py` | Five `wake_word_*` calls |
| `ui/web/js/screens/commands.js`, `bar.js`, `mock-api.js`, `mock-data.json` | Wake word card, "Also say", bubble pill |
| `tests/test_smoke.py` | Matcher, budget, listener, commands, engine, state manager, settings |
| Docs | Design doc, voice-commands, project index, changelog |

## Status

Built on `feat/wake-word` (2026-10-12); 756 tests pass. The real chain (TEN VAD, cutter, beam-1 Whisper on large-v3-turbo, matcher) wakes and runs on synthesised "Hey Koli, next song" in one breath and with a pause, ~0.3 s per check on the GPU, and stays asleep on other speech. Open: the real tests
(marked above) and the two numbers phase 1 measures with you,
`wake_word.voice_match` (0.45 for now) and the check budget (20 a minute, back
off after 10). Decided while building:

- Your wake voice is kept apart from the shared voiceprint, per microphone, in
  `wake_voice.json` (option b): no `VoiceBook` change, and a word is compared
  with recordings of the same word.
- Matching margins: low 0.97 / medium 0.9 / high 0.8, plus 0.15 for a personal
  word; the spelling list carries the variants.
- A tap costs the audio thread 0.65% (measured), so no raw tap was needed.
- The setup service lives in `services/commands.py` (the Commands screen's
  service), not a new module.

## Success Criteria

- [ ] "Hey Koli" then a pause then "next song": the next track plays
- [ ] "Hey Koli play spotify" in one breath opens Spotify; the same in Hebrew
  with "תפעיל ספוטיפיי"
- [ ] "Hey Koli" and silence: back to sleep after 5 s, nothing runs
- [ ] After a command, "louder" within 5 s works without the wake word;
  in "Hey Koli" mode "undo" within that window does nothing
- [ ] A recorded Hebrew word wakes Koli for you; another person saying it,
  or a video of it, doesn't; "Hey Koli" no longer does
- [ ] A TV talking for 10 minutes in "Hey Koli" mode: Whisper checks stay
  within the budget (Live log)
- [ ] No wake while dictating, during a meeting, while paused, or while the PC is locked
- [ ] Non-wake speech never appears in the console or the log
- [ ] Turning it off removes the tap and stops the thread, live
- [x] All tests pass: `PYTHONUTF8=1 python -m unittest discover -s tests`

## Risks

| Risk | Mitigation |
|------|------------|
| A 1 s clip is too short for a reliable voiceprint | Phase 1 measures it first; fall back to checking the whole burst |
| Whisper on CPU is too slow or too hungry in "Hey Koli" mode | Phase 1 numbers; check budget; recording a personal word moves the voice check in front of Whisper; "match your recordings" from the design's Later list |
| Whisper writes the wake word in ways the spellings miss | Spellings come from your own recordings; sensitivity setting; the Test button shows what was heard |
| Whisper invents text from noise ("Thank you") | No prompt on checks; a match must be at the start of the text |
| A wake check delays a dictation that starts right then | Submit only when idle; one short decode at most |
| The lock-screen check misbehaves on some setups | It only gates listening; worst case Koli listens while locked, with commands that run after unlock |

## References

- Design: `docs/design/wake-word.md`
- Patterns: `auto_dictation.py` (coordinator with fakes), `meeting/capture.py`
  (tap + own TEN VAD), `services/meetings.py` (your voice per microphone)
