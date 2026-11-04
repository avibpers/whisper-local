# services/commands.py
# The Commands & AI screen: voice commands and AI transforms. Both live in
# layered files (shipped entries plus the user's overrides), edited one entry
# at a time through defaults_merge's write API. The running app picks up
# changes on its own (both files hot-reload); transforms also re-register
# their shortcuts.

import re
import statistics
from pathlib import Path

import numpy as np

from .. import defaults_merge as dm
from .. import transforms as tf
from .. import voice_commands as vc
from ..utils import get_user_app_data_path

ACTION_KEYS = ('hotkey', 'type', 'run', 'rephrase', 'script', 'agent')


def _commands_path() -> Path:
    return Path(get_user_app_data_path()) / 'commands.yaml'


def _transforms_path() -> Path:
    return Path(get_user_app_data_path()) / tf.USER_FILE


def _action_of(entry: dict) -> str:
    for key in ACTION_KEYS:
        if entry.get(key) is not None:
            return key
    return ''


# One action as saved: the action itself, plus where its keys go when it
# presses keys and isn't aimed at the app in front ("windows" or an .exe).
def _clean_action(source: dict, action: str) -> dict:
    clean = {action: source[action]}
    send_to = str(source.get('send_to') or '').strip().lower()
    if action == 'hotkey' and send_to:
        clean['send_to'] = send_to
    if action == 'script':
        # Checked now, so a broken script can't be saved; it is checked again
        # (and rehearsed) every time it runs.
        from ..agent import actions, sandbox
        try:
            sandbox.check(str(source['script']), actions.Recorder(actions.Backend()).functions())
        except sandbox.ScriptRejected as e:
            raise ValueError(f"The script isn't allowed: {e}")
        clean['made_by'] = 'ai'
    if action == 'agent' and source['agent'] != 'undo':
        raise ValueError("Unknown AI action")
    return clean


# Saves an AI action that you repeat as a voice command: `phrase` runs
# `script` from now on, through the same checks, without the AI.
def save_ai_command(phrase: str, script: str) -> str:
    phrase = ' '.join(str(phrase or '').split())
    if not phrase:
        raise ValueError("Say what the command should be called")
    return save_command({'trigger': phrase, 'script': script})


# `{n}` (a spoken number) appears at most once in the phrase, and keys or text
# can only use it when the phrase has it.
def _check_number_placeholder(trigger: str, actions: list):
    count = trigger.count(vc.NUMBER_PLACEHOLDER)
    if count > 1:
        raise ValueError("Use {n} only once in the phrase")
    uses = any(vc.NUMBER_PLACEHOLDER in str(a.get(f) or '') for a in actions for f in vc.NUMBER_FIELDS)
    if uses and not count:
        raise ValueError("The keys use the number you say, so add {n} to the phrase, like “d {n}”")


# More ways to say a command ("תפעיל ספוטיפיי" next to "play spotify"):
# trimmed, empties and repeats of the trigger or each other dropped.
def _clean_other_triggers(raw, trigger: str) -> list:
    if raw in (None, ''):
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Other phrases must be a list")
    seen = {vc.normalize_spoken(trigger)}
    others = []
    for phrase in raw:
        phrase = str(phrase or '').strip()
        key = vc.normalize_spoken(phrase)
        if key and key not in seen:
            seen.add(key)
            others.append(phrase)
    return others


def list_commands() -> list:
    view = dm.layered_view(vc.DEFAULTS_FILE, _commands_path(), 'commands', vc.USER_FILE_HEADER,
                           id_key='trigger')
    for item in view:
        item['action'] = _action_of(item['entry'])
    return view


# Validate and save one command. `entry` holds trigger (or match_regex), one
# action (keys may carry send_to) and optional then-steps / confirm.
def save_command(entry: dict, original_id: str = None) -> str:
    entry = {k: v for k, v in dict(entry).items() if v not in (None, '') or k in ACTION_KEYS}
    trigger = str(entry.get('trigger', '')).strip()
    if not trigger and not entry.get('match_regex'):
        raise ValueError("A command needs a trigger phrase")
    if entry.get('match_regex'):
        import re
        try:
            re.compile(entry['match_regex'])
        except re.error as e:
            raise ValueError(f"The pattern isn't valid: {e}")
    actions = [k for k in ACTION_KEYS if entry.get(k) not in (None, '')]
    if len(actions) != 1:
        raise ValueError("Pick exactly one thing for the command to do")
    clean = {'trigger': trigger} if trigger else {}
    others = _clean_other_triggers(entry.get('other_triggers'), trigger)
    if others:
        clean['other_triggers'] = others
    if entry.get('match_regex'):
        clean['match_regex'] = entry['match_regex']
    clean.update(_clean_action(entry, actions[0]))
    steps = []
    for step in entry.get('then') or []:
        if not isinstance(step, dict):
            continue
        if step.get('delay') not in (None, ''):
            steps.append({'delay': max(0.0, min(60.0, float(step['delay'])))})
            continue
        step_actions = [k for k in ACTION_KEYS if step.get(k) not in (None, '')]
        if len(step_actions) == 1:
            steps.append(_clean_action(step, step_actions[0]))
    for phrase in [trigger] + others:
        _check_number_placeholder(phrase, [clean] + steps)
    if steps:
        clean['then'] = steps
    if entry.get('confirm') is True:
        clean['confirm'] = True
    return dm.save_entry(vc.DEFAULTS_FILE, _commands_path(), 'commands', vc.USER_FILE_HEADER,
                         clean, id_key='trigger', original_id=original_id)


def set_command_enabled(ident: str, enabled: bool):
    dm.set_entry_disabled(vc.DEFAULTS_FILE, _commands_path(), 'commands', vc.USER_FILE_HEADER,
                          ident, not enabled, id_key='trigger')


def delete_command(ident: str):
    dm.delete_entry(vc.DEFAULTS_FILE, _commands_path(), 'commands', vc.USER_FILE_HEADER,
                    ident, id_key='trigger')


# Which command a phrase would run, without running it.
def test_phrase(manager, text: str) -> dict:
    if manager is None or not getattr(manager, 'enabled', False) or not hasattr(manager, 'commands'):
        return {'matched': False, 'reason': 'Voice commands are off'}
    command = manager.match_command(text or '')
    if not command:
        return {'matched': False}
    return {'matched': True, 'trigger': command.get('trigger') or command.get('match_regex'),
            'phrase': vc.phrase_heard(command, text or ''),
            'action': _action_of(command), 'value': command.get(_action_of(command))}


# ── Transforms ───────────────────────────────────────────────────────────────

def list_transforms() -> list:
    return dm.layered_view(tf.DEFAULTS_FILE, _transforms_path(), 'transforms', tf.USER_FILE_HEADER,
                           id_key='name')


def save_transform(entry: dict, original_id: str = None) -> str:
    name = str(entry.get('name', '')).strip()
    if not name:
        raise ValueError("A transform needs a name")
    if not str(entry.get('prompt', '')).strip():
        raise ValueError("A transform needs an instruction")
    clean = {'name': name, 'prompt': str(entry['prompt'])}
    for key in ('description', 'hotkey', 'model'):
        if entry.get(key):
            clean[key] = str(entry[key])
    if entry.get('timeout') not in (None, ''):
        clean['timeout'] = float(entry['timeout'])
    return dm.save_entry(tf.DEFAULTS_FILE, _transforms_path(), 'transforms', tf.USER_FILE_HEADER,
                         clean, id_key='name', original_id=original_id)


def set_transform_enabled(ident: str, enabled: bool):
    dm.set_entry_disabled(tf.DEFAULTS_FILE, _transforms_path(), 'transforms', tf.USER_FILE_HEADER,
                          ident, not enabled, id_key='name')


def delete_transform(ident: str):
    dm.delete_entry(tf.DEFAULTS_FILE, _transforms_path(), 'transforms', tf.USER_FILE_HEADER,
                    ident, id_key='name')


# ── Wake word ────────────────────────────────────────────────────────────────
# The Wake word card: what wakes Koli now, and recording your own word.
# Each recording gives what Whisper wrote (the spellings that will match) and
# a voiceprint of you saying it; three make your word. "Add recordings"
# teaches a saved word more: each one joins your voiceprint, and an odd
# spelling ("Deus" for "Viush") joins the spellings when the recording
# sounds like your others. Design: docs/design/wake-word.md

WAKE_RECORDINGS = 3
WAKE_MAX_ADDED = 10      # recordings added in one go
# An added spelling must be a word or two like yours, not a short scrap
# ("a") or a sentence: either would wake Koli on far too much.
MIN_SPELLING_LETTERS = 3
# A spelling stays for good, so its recording must sound like you by the
# default bar even when "Sounds like you" is set lower (0.24 on 2026-10-16,
# when the room's noise scored up to 0.29).
SPELLING_VOICE_MATCH = 0.45


def _wake_listener(sm):
    listener = getattr(sm, '_wake_listener', None)
    if listener is None:
        raise ValueError("The wake word isn't available in this mode")
    return listener


def _wake_mic(sm) -> str:
    try:
        return sm._mic_key()
    except Exception:
        return 'default'


def wake_status(sm) -> dict:
    listener = _wake_listener(sm)
    mic = _wake_mic(sm)
    personal = listener.personal()
    return {
        'personal': personal,
        'word': (listener.spelling_hint() or listener.spellings()[0]) if personal else 'Hey Koli',
        'spellings': listener.spellings() if personal else [],
        'mic': mic,
        'recorded_here': listener.wake_voices.get(mic) is not None and not listener.recording_is_stale(mic),
        # Microphones with their own recording; one without is checked against these.
        'recorded_on': [m for m in listener.wake_voices.mics() if not listener.recording_is_stale(m)],
        'samples': listener.wake_voices.count(mic),
        'recordings': [r['heard'] for r in listener.recordings],
        'needed': WAKE_RECORDINGS,
        'can_record': getattr(sm, 'meetings', None) is not None,
    }


# Starts a fresh set of recordings of the word spelled `spelling` (optional:
# Whisper is told to expect it). `more`: recordings added to your saved word
# on this microphone instead, spelled as saved. `ready` is False while the
# voiceprint model loads (a background task); ask again until it is True.
def wake_prepare(sm, spelling: str = '', more: bool = False) -> dict:
    listener = _wake_listener(sm)
    meetings = getattr(sm, 'meetings', None)
    if meetings is None:
        raise ValueError("Recording your own word needs the app window")
    if more:
        if not listener.personal():
            raise ValueError("Record your own word first")
        if not listener.wake_voices.mics():
            raise ValueError("Record your word first")
        spelling = listener.spelling_hint()
    listener.recordings = []
    listener.recording_more = bool(more)
    listener.recording_spelling = str(spelling or '').strip()[:40]
    return {'ready': bool(meetings.voiceprint_ready()), 'needed': 1 if more else WAKE_RECORDINGS,
            'most': WAKE_MAX_ADDED if more else WAKE_RECORDINGS}


# One recording of your word. Refused (say it again) when nothing was heard,
# Whisper made out no word, or it was too short to learn your voice from.
def wake_record(sm) -> dict:
    listener = _wake_listener(sm)
    meetings = getattr(sm, 'meetings', None)
    if meetings is None:
        raise ValueError("Recording your own word needs the app window")
    more = listener.recording_more
    if more and len(listener.recordings) >= WAKE_MAX_ADDED:
        raise ValueError(f"That's {WAKE_MAX_ADDED} for now. Save them, then add more if you like.")
    audio = listener.capture_word()
    if audio is None:
        raise ValueError("Didn't hear anything. Press Record, then say your word.")
    heard = (listener.hear(audio, spelling=listener.recording_spelling) or '').strip()
    if not vc.normalize_spoken(heard):
        raise ValueError("Couldn't make out a word. Say it again, a little louder.")
    voiceprint = meetings.voiceprint_of(audio)
    if voiceprint is None:
        raise ValueError("Too short or too quiet to learn your voice from. Say it again.")
    from ..voice_filter import speech_level_db
    listener.recordings.append({'heard': heard, 'voiceprint': voiceprint, 'level': speech_level_db(audio)})
    result = {'heard': heard, 'count': len(listener.recordings), 'needed': 1 if more else WAKE_RECORDINGS}
    if more:
        # Shown next to the recording: how it scores against your saved voice
        # today, and whether Whisper wrote it a new way.
        from .. import wake_word as ww
        score = listener.similarity(audio)
        result['similarity'] = None if score is None else round(score, 2)
        result['new_spelling'] = ww.match_wake(heard, listener.spellings(), listener.match_ratio()) is None
    return result


# Your recordings become the wake word: the spellings Whisper wrote and your
# voice on this microphone. The same word recorded on another microphone adds
# to it; a different word replaces it everywhere.
def wake_save(sm, settings) -> dict:
    from .. import wake_word as ww
    listener = _wake_listener(sm)
    recordings = list(listener.recordings)
    if listener.recording_more:
        return _wake_save_more(sm, settings, listener, recordings)
    if len(recordings) < WAKE_RECORDINGS:
        raise ValueError(f"Record your word {WAKE_RECORDINGS} times first")
    spellings = ww.spellings_from_recordings([r['heard'] for r in recordings])
    if spellings is None:
        listener.recordings = []
        raise ValueError("Whisper heard different words each time. Record your word again.")
    typed = listener.recording_spelling
    if typed and vc.normalize_spoken(typed):
        spellings = [vc.normalize_spoken(typed)] + [s for s in spellings if s != vc.normalize_spoken(typed)]
    existing = listener.spellings() if listener.personal() else []
    same_word = existing and all(ww.match_wake(s, [existing[0]], ww.RECORDINGS_AGREE_RATIO) == '' for s in spellings)
    if same_word:
        spellings = existing + [s for s in spellings if s not in existing]
    else:
        listener.wake_voices.clear()
    # How loud you said it: quieter sound on this microphone (music from the
    # speakers) then doesn't hold the wake word's bursts open.
    levels = [r['level'] for r in recordings if r.get('level') is not None]
    level_db = float(statistics.median(levels)) if levels else None
    listener.wake_voices.set(_wake_mic(sm), [r['voiceprint'] for r in recordings], level_db=level_db)
    settings.set('wake_word.phrases', spellings, source='app', record_undo=False)
    settings.set('wake_word.spelling', typed, source='app', record_undo=False)
    settings.set('wake_word.only_my_voice', True, source='app', record_undo=False)
    listener.recordings = []
    listener.recording_spelling = ''
    return wake_status(sm)


# Added recordings join your voiceprint on this microphone; on a microphone
# without one (or one made with another speaker model) they start its own,
# weighed against your best-recorded other microphone. What Whisper
# wrote a new way becomes a spelling when the recording sounds like your
# other ones (the saved voice and the rest of this set, itself left out)
# and is a word or two long; the status says which were added or skipped.
def _wake_save_more(sm, settings, listener, recordings) -> dict:
    from .. import wake_word as ww
    if not recordings:
        raise ValueError("Record your word at least once first")
    mic = _wake_mic(sm)
    vectors = [r['voiceprint'] for r in recordings]
    shape = np.shape(vectors[0])
    saved, count = listener.wake_voices.raw(mic)
    own = saved is not None and saved.shape == shape
    if not own:
        saved, count = listener.wake_voices.reference(shape, exclude=mic)
    scores = ww.leave_one_out_scores(vectors, saved, count)
    existing = listener.spellings()
    most_words = len(vc.normalize_spoken(existing[0]).split()) + 1
    needs = max(listener.voice_match(), SPELLING_VOICE_MATCH)
    added, skipped = [], []
    for recording, score in zip(recordings, scores):
        spelling = vc.normalize_spoken(recording['heard'])
        if ww.match_wake(spelling, existing + added, listener.match_ratio()) is not None:
            continue      # already matches
        letters = len(re.sub(r'[\W\d_]', '', spelling))
        if letters < MIN_SPELLING_LETTERS or len(spelling.split()) > most_words:
            skipped.append({'spelling': spelling, 'why': 'too short or too long to be your word'})
        elif score is None or score < needs:
            skipped.append({'spelling': spelling, 'why': "didn't sound enough like your other recordings"
                            + ('' if score is None else f" ({score:.2f}, needs {needs:.2f})")})
        else:
            added.append(spelling)
    levels = [r['level'] for r in recordings if r.get('level') is not None]
    level_db = float(statistics.median(levels)) if levels else None
    if own:
        listener.wake_voices.add(mic, vectors, level_db=level_db)
    else:
        listener.wake_voices.set(mic, vectors, level_db=level_db)
    if added:
        settings.set('wake_word.phrases', existing + added, source='app', record_undo=False)
    listener.recordings = []
    listener.recording_more = False
    listener.recording_spelling = ''
    return {**wake_status(sm), 'added_recordings': len(recordings), 'added_spellings': added,
            'skipped_spellings': skipped}


# Say the wake word once: would it have woken Koli? With your own word, the
# voice score shows next to what it needs, to help set "Sounds like you".
def wake_test(sm) -> dict:
    from .. import wake_word as ww
    listener = _wake_listener(sm)
    audio = listener.capture_word()
    if audio is None:
        raise ValueError("Didn't hear anything. Press Test, then say the wake word.")
    listener._last_pieces = None
    heard = (listener.hear(audio) or '').strip()
    # As the live check: a word Whisper wasn't sure of doesn't wake.
    sure = listener._whisper_sure()
    word_ok = ww.match_wake(heard, listener.spellings(), listener.match_ratio()) is not None \
        and (sure is None or sure[0])
    result = {'heard': heard, 'word': word_ok, 'woke': word_ok}
    if sure is not None:
        result['sure'] = sure[1]
    if listener.personal():
        score = listener.similarity(audio)
        result.update(similarity=None if score is None else round(score, 2), needs=listener.voice_match())
        result['woke'] = word_ok and score is not None and score >= listener.voice_match()
    return result


# Back to "Hey Koli" for anyone: your spellings and voiceprints are removed.
def wake_reset(sm, settings) -> dict:
    listener = _wake_listener(sm)
    listener.wake_voices.clear()
    listener.recordings = []
    listener.recording_more = False
    settings.set('wake_word.phrases', [], source='app', record_undo=False)
    settings.set('wake_word.spelling', '', source='app', record_undo=False)
    settings.set('wake_word.only_my_voice', False, source='app', record_undo=False)
    return wake_status(sm)
