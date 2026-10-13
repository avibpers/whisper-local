# settings/schema.py
# One entry per setting in config.defaults.yaml: what to call it, where it sits
# in the app window, which control edits it, and how a change takes effect.
# The Settings screen is rendered from this, and a test fails if a key in the
# defaults file has no entry here, so no option can go missing from the UI.

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# How a change reaches the running app.
LIVE = 'live'                    # applied the moment it is saved
NEXT_RECORDING = 'next_recording'  # read at the start of each recording
BACKGROUND = 'background'        # a component is rebuilt off-thread and swapped in
NEXT_LAUNCH = 'next_launch'      # only read at startup
INTERNAL = 'internal'            # managed by the app, not edited directly

ADVANCED = 'advanced'
SIMPLE = 'simple'                # also asked in Simple mode (through a plain question)
HIDDEN = 'hidden'


@dataclass
class Setting:
    path: str
    label: str
    section: str
    control: str                  # toggle | segmented | select | number | range | text | textarea |
                                  # tags | choice_tags | hotkey | sound | microphone | audiohost | model_table |
                                  # cue_table | styles | table | readonly
    help: str = ''
    apply: str = LIVE
    mode: str = ADVANCED
    group: str = ''               # sub-heading inside the section
    choices: Optional[list] = None  # [[value, label], ...]
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: str = ''
    nullable: bool = False        # None is a valid value (e.g. audio host = automatic)
    optional: bool = False        # key may be absent in older/newer defaults files
    source: str = ''              # dynamic choices: streaming_models, ollama_models
    warn: str = ''                # shown under the control when relevant

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, '', False) or k in ('nullable', 'optional')}


SECTIONS = [
    {'id': 'general', 'title': 'General', 'desc': 'How you dictate, day to day.'},
    {'id': 'audio', 'title': 'Microphone & audio', 'desc': 'Which microphone to use, how it sounds, and how silence is handled.'},
    {'id': 'model', 'title': 'Model & engine', 'desc': 'Bigger models are more accurate and slower. Downloads happen once, in the background.'},
    {'id': 'hotkeys', 'title': 'Hotkeys', 'desc': 'Click a shortcut and press new keys. Changes register immediately.'},
    {'id': 'delivery', 'title': 'Delivery & clipboard', 'desc': "How text reaches the app you're in."},
    {'id': 'writing', 'title': 'Writing & formatting', 'desc': 'Applies to the next dictation. App rules can set their own style.'},
    {'id': 'smart', 'title': 'Smart editing', 'desc': 'Offline rules, no AI. Each one is off until you turn it on.'},
    {'id': 'ai', 'title': 'Local AI (Ollama)', 'desc': 'Optional polish and rewrites with a model on your own machine. Nothing leaves it.'},
    {'id': 'overlay', 'title': 'Bubble & sounds', 'desc': 'What you see and hear when the window is closed.'},
    {'id': 'meetings', 'title': 'Meetings', 'desc': 'Calls in Discord, Zoom, Teams and the browser: when Koli listens, who said what, and the panel.'},
    {'id': 'privacy', 'title': 'Privacy & logs', 'desc': 'Everything stays on this computer. Here is what gets written to disk.'},
    {'id': 'system', 'title': 'System', 'desc': 'Startup, tray, API server and backups.'},
]

# Edited on their own screens rather than in Settings.
OTHER_SECTIONS = ('vocabulary', 'commands', 'internal')

LANGUAGES = [
    ['auto', 'Auto-detect'], ['en', 'English'], ['he', 'Hebrew · עברית'], ['ar', 'Arabic'],
    ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'], ['it', 'Italian'],
    ['pt', 'Portuguese'], ['nl', 'Dutch'], ['pl', 'Polish'], ['ru', 'Russian'],
    ['uk', 'Ukrainian'], ['tr', 'Turkish'], ['fa', 'Persian'], ['hi', 'Hindi'],
    ['ja', 'Japanese'], ['zh', 'Chinese'], ['ko', 'Korean'], ['sv', 'Swedish'],
    ['no', 'Norwegian'], ['da', 'Danish'], ['fi', 'Finnish'], ['cs', 'Czech'],
    ['el', 'Greek'], ['ro', 'Romanian'], ['hu', 'Hungarian'], ['id', 'Indonesian'],
    ['vi', 'Vietnamese'], ['th', 'Thai'],
]

ON_OFF_STYLE = [[None, 'From style'], [True, 'On'], [False, 'Off']]
LOG_LEVELS = [[x, x.title()] for x in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')]
POSITIONS = [['right-edge', 'Right edge'], ['left-edge', 'Left edge'],
             ['top-left', 'Top left'], ['top-center', 'Top'], ['top-right', 'Top right'],
             ['bottom-left', 'Bottom left'], ['bottom-center', 'Bottom'], ['bottom-right', 'Bottom right'],
             ['custom', 'Where I dragged it']]
# "Wait before sending": fixed seconds, or Auto by the message's length
# (delayed_send.AUTO_WAITS holds the paces).
SEND_WAITS = [[0, 'Send at once'], [1, '1 second'], [2, '2 seconds'], [3, '3 seconds'], [5, '5 seconds'],
              [10, '10 seconds'], [15, '15 seconds'], [30, '30 seconds'],
              ['auto_fast', 'Auto · fast'], ['auto_medium', 'Auto · medium'], ['auto_slow', 'Auto · slow']]

_S = Setting

SCHEMA = [
    # ── General ──────────────────────────────────────────────────────────────
    _S('whisper.language', 'Language', 'general', 'select', mode=SIMPLE, choices=LANGUAGES,
       help='Auto-detect, or force one. Forcing is faster and avoids wrong-language guesses.'),
    _S('whisper.languages', 'Languages I speak', 'general', 'choice_tags', choices=LANGUAGES[1:],
       help='Auto-detect picks only from these, so a short clip never comes back as a language you '
       "don't speak. Empty = any language. Used only while Language is Auto-detect."),
    _S('whisper.task', 'Output', 'general', 'segmented',
       choices=[['transcribe', 'Transcribe'], ['translate', 'Translate to English']],
       help='Translate turns any spoken language into English.'),
    _S('hotkey.recording_mode', 'Recording mode', 'general', 'segmented', mode=SIMPLE,
       choices=[['push_to_talk', 'Push to talk'], ['toggle', 'Toggle']],
       help='Hold the record keys to talk, or press once to start and again to stop.'),
    _S('clipboard.auto_paste', 'Auto-paste at cursor', 'general', 'toggle', mode=SIMPLE,
       help='Off copies to the clipboard only. App rules can override this per app.'),
    _S('audio.continuous_mode', 'Continuous dictation', 'general', 'toggle',
       help='Starts recording again after each delivery. Press Esc to stop.'),
    _S('auto_dictation.enabled', 'Start listening when a text field gets focus', 'general', 'toggle',
       help='Click into any text field and talk, no hotkey. Quiet if you say nothing, '
       'stops when you pause, cancels the moment you type or leave the field. App rules can turn it off per app. Windows only for now.'),
    _S('auto_dictation.silence_timeout_seconds', 'Auto-listen silence limit', 'general', 'number',
       apply=NEXT_RECORDING, min=1, max=60, step=1, unit='seconds',
       help='How long it listens for you to start, and how long a pause ends it, when it started from a focused field.'),
    _S('auto_dictation.phrase_pause_seconds', 'Paste after each pause of', 'general', 'number',
       apply=NEXT_RECORDING, min=0, max=10, step=0.5, unit='seconds',
       help='While auto-listening, a pause this long pastes what you said and keeps listening. 0 pastes only at the end.'),

    # ── Microphone & audio ───────────────────────────────────────────────────
    _S('audio.input_device', 'Microphone', 'audio', 'microphone', apply=BACKGROUND, mode=SIMPLE,
       help="Switching happens in the background. A USB mic that disconnects is picked up again automatically."),
    _S('audio.host', 'Audio driver', 'audio', 'audiohost', apply=BACKGROUND, nullable=True,
       help='Automatic picks WASAPI on Windows for low latency.'),
    _S('audio.channels', 'Channels', 'audio', 'segmented', apply=BACKGROUND,
       choices=[[1, 'Mono'], [2, 'Stereo']]),
    _S('audio.dtype', 'Sample format', 'audio', 'select', apply=BACKGROUND,
       choices=[['float32', 'float32'], ['int16', 'int16'], ['int24', 'int24'], ['int32', 'int32']]),
    _S('audio.max_duration', 'Longest recording', 'audio', 'number', min=0, max=36000, step=1, unit='seconds',
       help='0 means no limit.'),
    _S('audio.pause_media_on_record', 'Pause music while recording', 'audio', 'toggle', apply=NEXT_RECORDING,
       mode=SIMPLE, help='Pauses Spotify, YouTube and other players once you start talking (at your own loudness, '
                         'not every noise), and plays them again when the recording ends.'),
    _S('audio.lower_volume.enabled', 'Lower the volume while recording', 'audio', 'toggle', apply=NEXT_RECORDING,
       mode=SIMPLE, help='Turns the computer down once you start talking (at your own loudness, not every noise), '
                         'then back up when the recording ends. Only when it is louder than the level below.'),
    _S('audio.lower_volume.level', 'Volume while recording', 'audio', 'range', apply=NEXT_RECORDING,
       mode=SIMPLE, min=0, max=100, step=5, unit='%',
       help='The volume to turn down to. If the computer is already quieter, it is left alone.'),
    _S('audio.noise_suppression.enabled', 'Noise suppression', 'audio', 'toggle', apply=NEXT_RECORDING,
       mode=SIMPLE, help='Cleans fans and keyboard noise. Needs an optional add-on.'),
    _S('audio.noise_suppression.strength', 'Noise suppression strength', 'audio', 'range',
       apply=NEXT_RECORDING, min=0, max=1, step=0.05),
    _S('audio.whisper_mode.enabled', 'Whisper mode', 'audio', 'toggle', apply=NEXT_RECORDING, mode=SIMPLE,
       help='Boosts quiet speech for open offices. Only ever turns the volume up.'),
    _S('audio.whisper_mode.max_gain', 'Maximum boost', 'audio', 'range', apply=NEXT_RECORDING,
       min=1, max=16, step=0.5, unit='×'),
    _S('audio.voice_filter.quieter_than_me', 'Ignore voices quieter than mine', 'audio', 'toggle', mode=SIMPLE,
       group='Only my voice', help='A TV across the room or someone talking in your pauses is left out. '
                                   'Koli learns how loud you are from your hotkey dictations (after 3).'),
    _S('audio.voice_filter.margin_db', 'How much quieter', 'audio', 'range', group='Only my voice',
       min=2, max=30, step=1, unit='dB', help='6 dB is about half as loud. Raise it if your own words get dropped, or the music '
                                         "doesn't pause when you talk; lower it if a TV still counts as you "
                                         '(Set it by ear shows where the line is).'),
    _S('audio.voice_filter.voiceprint', 'Only voices that sound like me', 'audio', 'toggle', group='Only my voice',
       help='Koli learns how you sound on each microphone from your hotkey dictations (after 3), and '
       'auto-dictation only types what sounds like you there. Stored only on this computer.'),
    _S('audio.voice_filter.apply_to', 'Use it for', 'audio', 'segmented', group='Only my voice',
       choices=[['auto', 'Auto-dictation'], ['all', 'All recordings']],
       help='Auto-dictation starts without a key press, so it hears the most around you.'),
    _S('vad.vad_precheck_enabled', 'Skip recordings with no speech', 'audio', 'toggle', group='Voice detection',
       help='Stops Whisper from inventing text on silence.'),
    _S('vad.vad_onset_threshold', 'Speech starts at', 'audio', 'range', apply=NEXT_RECORDING,
       group='Voice detection', min=0, max=1, step=0.05,
       help='How sure the detector must be that you started talking.'),
    _S('vad.vad_offset_threshold', 'Speech ends at', 'audio', 'range', apply=NEXT_RECORDING,
       group='Voice detection', min=0, max=1, step=0.05,
       help="Lower than the start value so the detector doesn't flicker."),
    _S('vad.vad_min_speech_duration', 'Shortest speech', 'audio', 'number', group='Voice detection',
       min=0.001, max=5, step=0.05, unit='seconds', help='Shorter sounds are treated as noise.'),
    _S('vad.vad_realtime_enabled', 'Stop on silence', 'audio', 'toggle', apply=BACKGROUND, mode=SIMPLE,
       group='Voice detection', help='Ends the recording after a quiet stretch.'),
    _S('vad.vad_silence_timeout_seconds', 'Silence before stopping', 'audio', 'number', apply=NEXT_RECORDING,
       mode=SIMPLE, group='Voice detection', min=1, max=36000, step=1, unit='seconds'),

    # ── Model & engine ───────────────────────────────────────────────────────
    _S('whisper.model', 'Model', 'model', 'model_table', apply=BACKGROUND, mode=SIMPLE,
       help='Switching downloads the model if needed, then swaps it in while you keep dictating.'),
    _S('whisper.models', 'Available models', 'model', 'model_table',
       help='Show or hide models in menus, or add your own CTranslate2 model.'),
    _S('whisper.backend', 'Engine', 'model', 'segmented', apply=BACKGROUND,
       choices=[['faster_whisper', 'faster-whisper'], ['whisper_cpp', 'whisper.cpp']],
       help='whisper.cpp is optional: faster on Apple Silicon, uses GGUF models.'),
    _S('whisper.device', 'Run on', 'model', 'segmented', apply=BACKGROUND, mode=SIMPLE,
       choices=[['cpu', 'CPU'], ['cuda', 'GPU (CUDA)']],
       help='The GPU runtime installs in the background on first use.'),
    _S('whisper.device_index', 'Graphics card', 'model', 'gpuselect', apply=BACKGROUND, mode=HIDDEN,
       help='Which card runs dictation when there is more than one. Shown under Graphics card.'),
    _S('whisper.compute_type', 'Precision', 'model', 'segmented', apply=BACKGROUND, mode=SIMPLE,
       choices=[['int8', 'int8'], ['float16', 'float16'], ['float32', 'float32']],
       help='int8 for CPU. float16 for GPU.'),
    _S('whisper.beam_size', 'Accuracy vs. speed', 'model', 'range', min=1, max=10, step=1,
       help='Beam size. Higher tries more candidates per word.'),
    _S('whisper.initial_prompt', 'Context prompt', 'model', 'textarea',
       help='Primes Whisper with style or vocabulary. App rules and profiles add their own after this one.'),
    _S('whisper.prompt_from_selection', 'Use selected text as context', 'model', 'toggle', apply=NEXT_RECORDING,
       help='Grabs your current selection when you start recording.'),
    _S('whisper.language_model.language', 'Second model for', 'model', 'select', apply=BACKGROUND,
       group='A second model for one language',
       choices=[['', 'Off'], ['he', 'Hebrew'], ['ar', 'Arabic'], ['ru', 'Russian'], ['es', 'Spanish'],
                ['fr', 'French'], ['de', 'German']],
       help='Each dictation\'s language is detected; this language goes to the second model, everything '
            'else stays on the main one. Hebrew: the ivrit.ai models are far more accurate.'),
    _S('whisper.language_model.model', 'Second model', 'model', 'select', apply=BACKGROUND, source='whisper_models',
       group='A second model for one language',
       help='Loaded next to the main model (about as much graphics memory again). It gets no prompt, only '
            'your favored words.'),
    _S('streaming.streaming_enabled', 'Show words while you speak', 'model', 'toggle', apply=BACKGROUND,
       group='Live preview (experimental)',
       help='A small streaming model shows text in the bubble. English only for now.'),
    _S('streaming.deliver_to_cursor', 'Type as you speak', 'model', 'toggle', apply=NEXT_RECORDING,
       group='Live preview (experimental)',
       help='Types finished phrases straight away and skips the final Whisper pass. Faster, less accurate.'),
    _S('streaming.streaming_model', 'Preview model', 'model', 'select', apply=BACKGROUND,
       group='Live preview (experimental)', source='streaming_models'),
    _S('streaming.models', 'Preview models', 'model', 'readonly', group='Live preview (experimental)',
       help='Defined in user_settings.yaml.'),

    # ── Hotkeys ──────────────────────────────────────────────────────────────
    _S('hotkey.recording_hotkey', 'Record', 'hotkeys', 'hotkey', mode=SIMPLE,
       help='Hold to talk, or press to toggle.'),
    _S('hotkey.stop_key', 'Stop and paste', 'hotkeys', 'hotkey', help='Toggle mode and command mode.'),
    _S('hotkey.auto_send_key', 'Stop, paste and send', 'hotkeys', 'hotkey', nullable=True,
       help='Presses Enter after pasting.'),
    _S('hotkey.cancel_combination', 'Cancel', 'hotkeys', 'hotkey'),
    _S('hotkey.command_hotkey', 'Voice command', 'hotkeys', 'hotkey', nullable=True),
    _S('hotkey.rephrase_hotkey', 'AI rephrase selection', 'hotkeys', 'hotkey', nullable=True,
       help='Needs Local AI.'),
    _S('hotkey.pause_hotkey', 'Pause all hotkeys', 'hotkeys', 'hotkey', nullable=True),
    _S('hotkey.paste_last_hotkey', 'Paste last dictation', 'hotkeys', 'hotkey', nullable=True),
    _S('hotkey.meeting_hotkey', 'Start or end a meeting', 'hotkeys', 'hotkey', nullable=True),
    _S('hotkey.meeting_mute_hotkey', 'Mute my microphone everywhere', 'hotkeys', 'hotkey', nullable=True,
       help="Mutes your microphone for every app, so the call hears nothing and the meeting leaves you out. "
       "Press again to unmute. The meeting app's own mute keeps the microphone open, so Koli still hears it."),
    _S('hotkey.double_tap_to_lock', 'Double-tap for hands-free', 'hotkeys', 'toggle',
       help='Double-tap Record to keep recording without holding. Tap again to stop.'),
    _S('hotkey.double_tap_window_ms', 'Double-tap window', 'hotkeys', 'number', min=100, max=2000,
       step=10, unit='ms'),

    # ── Delivery & clipboard ─────────────────────────────────────────────────
    _S('clipboard.delivery_method', 'Method', 'delivery', 'segmented',
       choices=[['paste', 'Paste'], ['type', 'Type']],
       help='Paste is fastest. Type injects characters one by one and never touches the clipboard (Windows).'),
    _S('clipboard.auto_send_delay', 'Wait before sending', 'delivery', 'select', choices=SEND_WAITS,
       help='In apps set to auto-send (Slack, WhatsApp and other chats), wait this long after '
            'the text is pasted before Koli presses Enter, so you can read it. Auto follows the '
            "message's length: 1 second for a word or two, longer for more (fast up to 6 s, medium "
            'up to 10 s, slow up to 15 s). Esc, Cancel on the bubble, typing, a new dictation or '
            'switching windows stop it. The send key and your own Enter always send at once. '
            'Profiles can set their own.'),
    _S('clipboard.paste_hotkey', 'Paste shortcut', 'delivery', 'hotkey', group='Paste',
       help='Use Ctrl Shift V for plain-text paste, Shift Insert for terminals.'),
    _S('clipboard.paste_pre_paste_delay', 'Wait before pasting', 'delivery', 'number', group='Paste',
       min=0, max=5, step=0.01, unit='seconds', help='Gives clipboard managers time to read.'),
    _S('clipboard.paste_preserve_clipboard', 'Put my clipboard back', 'delivery', 'toggle', group='Paste',
       help='Restores what you had copied before the dictation.'),
    _S('clipboard.paste_clipboard_restore_delay', 'Restore after', 'delivery', 'number', group='Paste',
       min=0, max=10, step=0.05, unit='seconds'),
    _S('clipboard.type_also_copy_to_clipboard', 'Also copy to clipboard', 'delivery', 'toggle', group='Type'),
    _S('clipboard.type_auto_enter_delay', 'Delay before Enter', 'delivery', 'number', group='Type',
       min=0, max=10, step=0.01, unit='seconds', help='Terminals need a moment to take the typed text.'),
    _S('clipboard.type_auto_enter_delay_per_100_chars', 'Extra delay per 100 characters', 'delivery', 'number',
       group='Type', min=0, max=10, step=0.01, unit='seconds'),
    _S('clipboard.macos_key_simulation_delay', 'Key timing (macOS)', 'delivery', 'number', group='Type',
       min=0, max=1, step=0.01, unit='seconds'),

    # ── Writing & formatting ─────────────────────────────────────────────────
    _S('postprocess.style', 'Writing style', 'writing', 'segmented', mode=SIMPLE,
       choices=[['', 'Custom'], ['formal', 'Formal'], ['casual', 'Casual'], ['very_casual', 'Very casual'],
                ['verbatim', 'Verbatim']],
       help='One choice for capitals, final punctuation and casing.'),
    _S('postprocess.styles', 'Your styles', 'writing', 'styles',
       help='Named sets of the switches below, usable in app rules.'),
    _S('postprocess.capitalize_first', 'Capitalize the first letter', 'writing', 'toggle', mode=SIMPLE),
    _S('postprocess.ensure_punctuation', 'End with punctuation', 'writing', 'toggle', mode=SIMPLE,
       help="Adds a period when the text doesn't end in one."),
    _S('postprocess.strip_trailing_period', 'Drop the final period', 'writing', 'toggle', mode=SIMPLE),
    _S('postprocess.lowercase', 'Lowercase', 'writing', 'toggle',
       help='Keeps acronyms, mixed case like iPhone, and your own terms.'),
    _S('postprocess.strip_filler_words', 'Remove filler words', 'writing', 'toggle', mode=SIMPLE,
       help='“um”, “uh”, “like”, “you know”.'),
    _S('postprocess.filler_words', 'Your filler words', 'writing', 'tags', optional=True,
       help='Your list replaces the English one when you add words.'),
    _S('postprocess.remove_repeated_words', 'Remove stutters', 'writing', 'toggle', mode=SIMPLE,
       help='“I I think” becomes “I think”. Real doubles like “had had” stay.'),
    _S('postprocess.list_formatting', 'Spoken lists', 'writing', 'toggle',
       help='“First, milk. Second, eggs.” becomes a list.'),
    _S('postprocess.list_style', 'List style', 'writing', 'segmented',
       choices=[['numbered', '1. Numbered'], ['bullets', '• Bullets']]),
    _S('postprocess.inline_formatting', 'Say punctuation out loud', 'writing', 'toggle', mode=SIMPLE,
       group='Voice punctuation', help='“comma”, “period”, “new line”, “open quote” and more.'),
    _S('postprocess.inline_formatting_extend', 'Keep the English words too', 'writing', 'toggle',
       group='Voice punctuation', help='When you add your own below.'),
    _S('postprocess.inline_formatting_absorb_punctuation', 'Clean up around cue words', 'writing', 'toggle',
       group='Voice punctuation', help='Removes the extra commas Whisper adds when it hears you say “comma”.'),
    _S('postprocess.inline_formatting_replacements', 'Your cue words', 'writing', 'cue_table',
       group='Voice punctuation'),

    # ── Smart editing ────────────────────────────────────────────────────────
    _S('postprocess.voice_editing', '“Scratch that”', 'smart', 'toggle',
       help='Erases back to the start of the sentence.'),
    _S('postprocess.voice_editing_phrases', 'Your “scratch that” phrases', 'smart', 'tags',
       help='Extra phrases in your language, like “מחק את זה”. The English ones always work.'),
    _S('postprocess.backtrack.enabled', 'Backtrack', 'smart', 'toggle',
       help='“Meet at 2, actually 3” types “Meet at 3”. Only acts on numbers, times, days and months.'),
    _S('postprocess.backtrack.cues', 'Backtrack cue words', 'smart', 'tags'),
    _S('postprocess.smart_formatting.times', 'Times', 'smart', 'toggle', help='“3 p.m.” → “3 PM”'),
    _S('postprocess.smart_formatting.emails', 'Email addresses', 'smart', 'toggle',
       help='“john at example dot com” → “john@example.com”. Can also fire on ordinary sentences.'),
    _S('postprocess.smart_formatting.urls', 'Web addresses', 'smart', 'toggle',
       help='“example dot com” → “example.com”'),

    # ── Local AI ─────────────────────────────────────────────────────────────
    _S('postprocess.ollama.enabled', 'Polish every dictation', 'ai', 'toggle',
       help='Fixes punctuation and capitals after the offline rules.'),
    _S('postprocess.ollama.endpoint', 'Address', 'ai', 'text'),
    _S('postprocess.ollama.model', 'Model', 'ai', 'text', source='ollama_models'),
    _S('postprocess.ollama.timeout', 'Give up after', 'ai', 'number', min=1, max=120, step=1, unit='seconds',
       help='Falls back to the plain transcript.'),
    _S('postprocess.ollama.prompt', 'Polish instruction', 'ai', 'textarea',
       help='{text} is replaced with your dictation.'),

    # ── Bubble & sounds ────────────────────────────────────────────────────
    _S('overlay.enabled', 'Show the Koli bubble', 'overlay', 'toggle', mode=SIMPLE),
    _S('overlay.position', 'Position', 'overlay', 'segmented', choices=POSITIONS,
       help='Or press on the bubble and drag it anywhere.'),
    _S('overlay.follow_focus', 'Follow the text field', 'overlay', 'toggle',
       help='The resting tab moves to the right end of each text field you click into.'),
    _S('overlay.rest_color', 'Resting colour', 'overlay', 'text',
       help='Empty is Koli turquoise, or a colour such as #7C5CFF.'),
    _S('overlay.rest_opacity', 'Resting opacity', 'overlay', 'range', min=20, max=100, step=5, unit='%',
       help='How solid the tab is while Koli waits. Full as soon as it does something.'),
    _S('overlay.click_through', 'Clicks pass through', 'overlay', 'toggle', apply=NEXT_LAUNCH,
       help='Only used by the classic overlay (--legacy-ui). The new bar is sized to its content.'),
    _S('audio_feedback.enabled', 'Start, stop and cancel sounds', 'overlay', 'toggle', mode=SIMPLE,
       group='Sounds'),
    _S('audio_feedback.transcription_complete_enabled', 'Sound when text is delivered', 'overlay', 'toggle',
       group='Sounds'),
    _S('audio_feedback.ready_enabled', 'Chime when ready after launch', 'overlay', 'toggle', group='Sounds'),
    _S('audio_feedback.start_sound', 'Start sound', 'overlay', 'sound', group='Sound files'),
    _S('audio_feedback.stop_sound', 'Stop sound', 'overlay', 'sound', group='Sound files'),
    _S('audio_feedback.cancel_sound', 'Cancel sound', 'overlay', 'sound', group='Sound files'),
    _S('audio_feedback.transcription_complete_sound', 'Delivered sound', 'overlay', 'sound', group='Sound files'),
    _S('audio_feedback.ready_sound', 'Ready sound', 'overlay', 'sound', group='Sound files'),

    _S('overlay.hide_from_sharing', 'Hide the bubble from screen sharing', 'overlay', 'toggle',
       help='Shared screens and recordings leave it out; you still see it.'),

    # ── Meetings ─────────────────────────────────────────────────────────────
    _S('meeting.enabled', 'Notice calls', 'meetings', 'toggle', mode=SIMPLE,
       help='Off hooks nothing. Meetings can still be started by hand from the Meetings screen.'),
    _S('meeting.detect', 'When a call has sound', 'meetings', 'segmented', mode=SIMPLE,
       choices=[['auto', 'Automatically'], ['ask', 'Ask me first'], ['off', 'Never']],
       help='Each app can override this on the Apps screen.'),
    _S('meeting.include_me', 'Include my side', 'meetings', 'toggle',
       help="Your microphone becomes \"You\" in the transcript. The panel's Exclude me button pauses it."),
    _S('meeting.remember_voices', 'Remember voices', 'meetings', 'toggle',
       help="Saves each voice heard on a call (a voiceprint, not the audio) with its own id, so later meetings "
       'recognise the same person, named or not. Stored only on this computer; Known voices on the Meetings '
       'screen lists them.'),
    _S('meeting.keep_audio', 'Keep the audio', 'meetings', 'toggle',
       help="Saves the call's audio with the transcript, to improve telling speakers apart. Everyone's voice "
       'is stored on this computer, so turn it on only with their consent. Deleting the meeting deletes it.'),
    _S('meeting.input_device', 'Microphone for meetings', 'meetings', 'microphone', apply=NEXT_RECORDING,
       help='Same as dictation, or another input. Applies to the next meeting; one that won\'t open falls back to the dictation microphone.'),
    _S('meeting.gap_minutes', 'Resume within', 'meetings', 'number', min=1, max=60, unit='min',
       help='Sound back from the same call within this time resumes the meeting; later starts a new one.'),
    _S('meeting.start_speech_seconds', 'Speech before a meeting starts', 'meetings', 'number', min=1, max=10, unit='s',
       help='So a notification ping never starts one.'),
    _S('meeting.countdown_seconds', 'Countdown before an auto start', 'meetings', 'number', min=0, max=30, unit='s',
       help='The panel shows the detected call with Stop and Start now while it counts down. 0 starts at once.'),
    _S('meeting.quiet_minutes_after_stop', 'After you stop, wait for quiet', 'meetings', 'number', min=0, max=60,
       unit='min', help="Once you stop a meeting, it won't start again by itself until the call has been quiet "
       'this long. Starting by hand always works.'),
    _S('meeting.language', 'Meeting language', 'meetings', 'select', choices=LANGUAGES, apply=NEXT_RECORDING,
       help='Auto-detect handles Hebrew and English in one call.'),
    # Reserved: a separate model for meetings is not wired yet, so it stays out of the page.
    _S('meeting.model', 'Meeting model', 'meetings', 'text', apply=INTERNAL, mode=HIDDEN),
    _S('meeting.speaker_model', 'Voiceprint model', 'meetings', 'select', apply=BACKGROUND,
       choices=[['eres2net-en', '3D-Speaker ERes2Net VoxCeleb (26 MB)'], ['eres2net', '3D-Speaker ERes2Net Chinese (40 MB)']],
       help='Tells speakers apart. Downloaded at the first meeting.'),
    _S('meeting.segmentation_model', 'Split by voice', 'meetings', 'select', apply=BACKGROUND,
       choices=[['pyannote-segmentation', 'On (pyannote, 6 MB)'], ['', 'Off']],
       help='Finds where the voice changes inside the call, so people who talk one after another '
       'get their own turns. Downloaded at the first meeting.'),
    _S('meeting.speaker_models', 'Voiceprint models', 'meetings', 'readonly', help='Defined in user_settings.yaml.'),
    _S('meeting.panel.opacity', 'Panel opacity', 'meetings', 'range', min=40, max=100, step=5, unit='%', group='Panel'),
    _S('meeting.panel.position', 'Panel position', 'meetings', 'segmented', choices=POSITIONS, group='Panel',
       help='Or drag the panel by its header.'),
    _S('meeting.panel.width', 'Panel width', 'meetings', 'number', min=240, max=1200, unit='px', group='Panel'),
    _S('meeting.panel.height', 'Panel height', 'meetings', 'number', min=160, max=1600, unit='px', group='Panel'),
    _S('meeting.panel.hide_from_sharing', 'Hide the panel from screen sharing', 'meetings', 'toggle', group='Panel',
       help="Shared screens and recordings leave it out; you still see it. The panel's menu can show it for one meeting."),
    _S('meeting.panel.custom_x', 'Panel spot x', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN, nullable=True),
    _S('meeting.panel.custom_y', 'Panel spot y', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN, nullable=True),
    _S('meeting.panel.collapsed', 'Panel collapsed', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),

    # ── Privacy & logs ───────────────────────────────────────────────────────
    _S('logging.log_transcriptions', 'Write dictated text into app.log', 'privacy', 'toggle',
       help='Off logs only lengths and timing.'),
    _S('audit.enabled', 'Audit trail', 'privacy', 'toggle',
       help='One line per delivery: time, app, length. Never the text.'),
    _S('logging.level', 'Log detail', 'privacy', 'select', choices=LOG_LEVELS,
       help='Also what the Live console can show.'),
    _S('logging.file.enabled', 'Log to file', 'privacy', 'toggle'),
    _S('logging.file.filename', 'Log file name', 'privacy', 'text'),
    _S('logging.console.enabled', 'Console output', 'privacy', 'toggle',
       help='For the console window when launched from a terminal.'),
    _S('logging.console.level', 'Console detail', 'privacy', 'select', choices=LOG_LEVELS),
    _S('update_check.enabled', 'Check for updates daily', 'privacy', 'toggle',
       help='Asks GitHub for the latest version number. Sends nothing else.'),

    # ── System ───────────────────────────────────────────────────────────────
    _S('system_tray.enabled', 'Tray icon', 'system', 'toggle', apply=BACKGROUND),
    _S('system_tray.tooltip', 'Tray hover text', 'system', 'text'),
    _S('console.start_hidden', 'Hide the console window at start', 'system', 'toggle', apply=NEXT_LAUNCH),
    _S('terminal_title.idle', 'Terminal title · idle', 'system', 'text', group='Terminal tab title',
       help='Prefix shown when launched from a terminal.'),
    _S('terminal_title.recording', 'Terminal title · recording', 'system', 'text', group='Terminal tab title'),
    _S('terminal_title.processing', 'Terminal title · processing', 'system', 'text', group='Terminal tab title'),
    _S('server.enabled', 'OpenAI-compatible transcription API', 'system', 'toggle', apply=BACKGROUND,
       group='Local API', help='For Cursor, Open WebUI, n8n. Keep it on 127.0.0.1 unless you trust your network.'),
    _S('server.host', 'API address', 'system', 'text', apply=BACKGROUND, group='Local API',
       warn='Anything other than 127.0.0.1 exposes the API to your network.'),
    _S('server.port', 'API port', 'system', 'number', apply=BACKGROUND, group='Local API',
       min=1024, max=65535, step=1),
    _S('ui.mode', 'Interface', 'system', 'segmented', choices=[['simple', 'Simple'], ['advanced', 'Advanced']],
       group='App window'),
    _S('ui.theme', 'Theme', 'system', 'segmented',
       choices=[['light', 'Light'], ['dark', 'Dark'], ['system', 'Follow Windows']], group='App window'),
    _S('ui.open_on_start', 'Open this window when Koli starts', 'system', 'toggle', group='App window'),

    # ── Edited on their own screens ──────────────────────────────────────────
    _S('whisper.hotwords', 'Favored words', 'vocabulary', 'tags', mode=SIMPLE,
       help='Whisper leans toward these while listening.'),
    _S('postprocess.corrections', 'Corrections', 'vocabulary', 'table'),
    _S('postprocess.replacements', 'Replacements', 'vocabulary', 'table'),
    _S('postprocess.snippets', 'Snippets', 'vocabulary', 'table'),
    _S('voice_commands.enabled', 'Voice commands', 'commands', 'toggle'),
    _S('wake_word.enabled', 'Wake word', 'commands', 'toggle',
       help='Say “Hey Koli” (or your own word) and a command, no shortcut.'),
    _S('wake_word.sensitivity', 'How easily it wakes', 'commands', 'segmented',
       choices=[['low', 'Strict'], ['medium', 'Normal'], ['high', 'Easy']],
       help='Easy also wakes on words that sound close, and by mistake more often.'),
    _S('wake_word.voice_match', 'Sounds like you at least', 'commands', 'number', min=0.1, max=0.95, step=0.05,
       help='For your own word. Raise it if others wake Koli, lower it if it misses you. Test shows your score.'),
    _S('wake_word.command_timeout_seconds', 'Waits for the command', 'commands', 'number',
       min=2, max=30, step=1, unit='seconds'),
    _S('wake_word.follow_up_seconds', 'Keeps listening after a command', 'commands', 'number',
       min=1, max=30, step=1, unit='seconds',
       help='For another command without the wake word, like “louder”.'),
    _S('wake_word.phrases', 'Your wake word', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),
    _S('wake_word.only_my_voice', 'Wake word: only your voice', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),
    _S('wake_word.spelling', 'How you spell your wake word', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),

    # ── Managed by the app ───────────────────────────────────────────────────
    _S('onboarding.gpu', 'GPU setup status', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),
    _S('onboarding.gpu_class', 'Detected GPU class', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN,
       nullable=True),
    _S('ui.welcome_done', 'Welcome completed', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),
    _S('ui.window.width', 'Window width', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),
    _S('ui.window.height', 'Window height', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),
    _S('ui.window.x', 'Window x', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN, nullable=True),
    _S('ui.window.y', 'Window y', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN, nullable=True),
    _S('ui.window.maximized', 'Window maximized', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN),
    _S('overlay.custom_x', 'Dragged bubble x', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN, nullable=True),
    _S('overlay.custom_y', 'Dragged bubble y', 'internal', 'readonly', apply=INTERNAL, mode=HIDDEN, nullable=True),
]

BY_PATH = {s.path: s for s in SCHEMA}

# Maps whose entries are the user's own: the schema covers the map as a whole.
MAP_PATHS = {'whisper.models', 'streaming.models', 'meeting.speaker_models', 'postprocess.styles', 'postprocess.corrections'}


def get(path: str) -> Optional[Setting]:
    return BY_PATH.get(path)


# The schema as plain data for the web page, limited to keys present in the
# running config (optional keys from other branches are left out when absent).
def export(config_has_path) -> dict:
    return {
        'sections': SECTIONS,
        'settings': [s.to_dict() for s in SCHEMA if not s.optional or config_has_path(s.path)],
    }


# Every leaf path in a defaults tree, treating user-entry maps as one setting.
def leaf_paths(tree: dict, prefix: str = '') -> list:
    out = []
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and path not in MAP_PATHS:
            out.extend(leaf_paths(value, path))
        else:
            out.append(path)
    return out


# Coerce a value from the page to the type the setting expects. Raises
# ValueError with a message the user can read.
def coerce(setting: Setting, value: Any) -> Any:
    if value is None:
        if setting.nullable:
            return None
        if setting.control in ('hotkey',):
            return ''
        raise ValueError(f"{setting.label} needs a value")
    c = setting.control
    if c == 'toggle':
        if isinstance(value, str):
            return value.strip().lower() in ('true', 'yes', 'on', '1')
        return bool(value)
    if c in ('number', 'range'):
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{setting.label} must be a number")
        if setting.min is not None and num < setting.min:
            raise ValueError(f"{setting.label} must be at least {_fmt(setting.min)}")
        if setting.max is not None and num > setting.max:
            raise ValueError(f"{setting.label} must be at most {_fmt(setting.max)}")
        if setting.step is not None and float(setting.step).is_integer() and num.is_integer():
            return int(num)
        return num
    if c in ('segmented', 'select') and setting.choices and not setting.source:
        allowed = [ch[0] for ch in setting.choices]
        if value not in allowed:
            # Numbers arrive as strings from some controls.
            for a in allowed:
                if str(a) == str(value):
                    return a
            raise ValueError(f"{value!r} is not a valid choice for {setting.label}")
        return value
    if c == 'tags':
        if isinstance(value, str):
            value = [v.strip() for v in value.split(',')]
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{setting.label} must be a list")
        seen, out = set(), []
        for v in value:
            v = str(v).strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
        return out
    if c in ('text', 'textarea', 'hotkey', 'sound'):
        return '' if value is None else str(value)
    if c == 'microphone':
        # '' (same as dictation) and 'default' stay words; a device id is a number.
        if value in ('', 'default'):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{setting.label} must be a device")
    return value


def _fmt(n) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)
