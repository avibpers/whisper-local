# settings/simple_mode.py
# Simple mode asks plain questions ("Speed or accuracy?") instead of showing
# engine settings. Each answer becomes a patch of real settings, and the current
# settings are read back into answers. When the settings match no answer (the
# user tuned them in Advanced), the answer is "custom" and is never overwritten
# until the user picks something.

from typing import Optional

CUSTOM = 'custom'

# Speed or accuracy presets: (language family, hardware) → model per answer.
# Hebrew presets apply only when the ivrit.ai models are configured.
_PRESETS = {
    ('en', 'cpu'): {'fast': 'base.en', 'balanced': 'small.en', 'accurate': 'large-v3-turbo'},
    ('en', 'gpu'): {'fast': 'small.en', 'balanced': 'large-v3-turbo', 'accurate': 'large'},
    ('he', 'cpu'): {'fast': 'small', 'balanced': 'hebrew-large-v3-turbo', 'accurate': 'hebrew-large-v3'},
    ('he', 'gpu'): {'fast': 'hebrew-large-v3-turbo', 'balanced': 'hebrew-large-v3-turbo', 'accurate': 'hebrew-large-v3'},
    ('other', 'cpu'): {'fast': 'base', 'balanced': 'small', 'accurate': 'large-v3-turbo'},
    ('other', 'gpu'): {'fast': 'small', 'balanced': 'large-v3-turbo', 'accurate': 'large'},
}

_LOOKS = {
    # The factory settings: Whisper's own capitals and punctuation, untouched.
    'standard': {'postprocess.style': '', 'postprocess.strip_filler_words': False,
                 'postprocess.remove_repeated_words': False},
    'exact': {'postprocess.style': 'verbatim', 'postprocess.strip_filler_words': False,
              'postprocess.remove_repeated_words': False},
    'clean': {'postprocess.style': 'formal', 'postprocess.strip_filler_words': True,
              'postprocess.remove_repeated_words': True},
    'relaxed': {'postprocess.style': 'casual', 'postprocess.strip_filler_words': True,
                'postprocess.remove_repeated_words': True},
}

_STOP_CHOICES = {'never': None, '10': 10, '30': 30, '60': 60}


def _family(language: str, available: set) -> str:
    if language == 'en':
        return 'en'
    if language == 'he' and 'hebrew-large-v3-turbo' in available:
        return 'he'
    return 'other'


def presets_for(language: str, gpu: bool, available: set) -> dict:
    table = _PRESETS[(_family(language, available), 'gpu' if gpu else 'cpu')]
    # Fall back to a multilingual model if a preset model isn't configured.
    fallback = _PRESETS[('other', 'gpu' if gpu else 'cpu')]
    return {answer: (key if key in available else fallback[answer]) for answer, key in table.items()}


# The settings patch for one answer. `ctx` carries what some answers depend on:
# {'language', 'gpu', 'available_models'}.
def to_patch(question: str, answer, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    if question == 'language':
        return {'whisper.language': answer}
    if question == 'start':
        return {'hotkey.recording_mode': 'push_to_talk' if answer == 'hold' else 'toggle'}
    if question == 'keys':
        return {'hotkey.recording_hotkey': answer}
    if question == 'where':
        return {'clipboard.auto_paste': answer == 'type'}
    if question == 'quality':
        gpu = bool(ctx.get('gpu'))
        presets = presets_for(ctx.get('language', 'auto'), gpu, set(ctx.get('available_models') or ()))
        return {'whisper.model': presets[answer],
                'whisper.device': 'cuda' if gpu else 'cpu',
                'whisper.compute_type': 'float16' if gpu else 'int8'}
    if question == 'microphone':
        return {'audio.input_device': answer}
    if question == 'noise':
        return {'audio.noise_suppression.enabled': bool(answer)}
    if question == 'quiet':
        return {'audio.whisper_mode.enabled': bool(answer)}
    if question == 'music':
        return {'audio.pause_media_on_record': bool(answer)}
    if question == 'stop':
        seconds = _STOP_CHOICES[str(answer)]
        if seconds is None:
            return {'vad.vad_realtime_enabled': False}
        return {'vad.vad_realtime_enabled': True, 'vad.vad_silence_timeout_seconds': seconds}
    if question == 'sounds':
        return {'audio_feedback.enabled': bool(answer)}
    if question == 'look':
        return dict(_LOOKS[answer])
    if question == 'punctuation':
        return {'postprocess.inline_formatting': bool(answer)}
    if question == 'bar':
        return {'overlay.enabled': bool(answer)}
    if question == 'volume':
        # 'off', or the percent to turn the computer down to while talking.
        if answer in (None, '', 'off', False):
            return {'audio.lower_volume.enabled': False}
        return {'audio.lower_volume.enabled': True, 'audio.lower_volume.level': int(answer)}
    raise ValueError(f"Unknown Simple-mode question: {question}")


# Current answers from settings. `get` reads a dotted path.
def from_settings(get, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    answers = {
        'language': get('whisper.language') or 'auto',
        'start': 'hold' if get('hotkey.recording_mode') == 'push_to_talk' else 'press',
        'keys': get('hotkey.recording_hotkey'),
        'where': 'type' if get('clipboard.auto_paste') else 'copy',
        'microphone': get('audio.input_device'),
        'noise': bool(get('audio.noise_suppression.enabled')),
        'quiet': bool(get('audio.whisper_mode.enabled')),
        'music': bool(get('audio.pause_media_on_record')),
        'sounds': bool(get('audio_feedback.enabled')),
        'punctuation': bool(get('postprocess.inline_formatting')),
        'bar': bool(get('overlay.enabled')),
        'volume': str(get('audio.lower_volume.level') or 20) if get('audio.lower_volume.enabled') else 'off',
    }

    if not get('vad.vad_realtime_enabled'):
        answers['stop'] = 'never'
    else:
        timeout = get('vad.vad_silence_timeout_seconds')
        match = [k for k, v in _STOP_CHOICES.items() if v is not None and float(v) == float(timeout or 0)]
        answers['stop'] = match[0] if match else CUSTOM

    answers['look'] = CUSTOM
    for name, patch in _LOOKS.items():
        if all(get(p) == v for p, v in patch.items()):
            answers['look'] = name
            break

    gpu = get('whisper.device') == 'cuda'
    presets = presets_for(answers['language'], gpu, set(ctx.get('available_models') or ()))
    expected_compute = 'float16' if gpu else 'int8'
    answers['quality'] = CUSTOM
    if get('whisper.compute_type') == expected_compute:
        for name in ('balanced', 'fast', 'accurate'):
            if presets[name] == get('whisper.model'):
                answers['quality'] = name
                break
    answers['quality_presets'] = presets
    return answers


# Settings the app saves by itself on a first start (the audio driver it
# picked, GPU onboarding, the local API), which say nothing about the user.
_SELF_WRITTEN = {'onboarding': None, 'server': None, 'audio': {'host'}}


# True when user_settings.yaml holds a choice the user made, i.e. they used an
# earlier version: they get Advanced mode and no welcome steps.
def is_existing_user(overrides: dict) -> bool:
    for section, value in (overrides or {}).items():
        if section not in _SELF_WRITTEN:
            return True
        keys = _SELF_WRITTEN[section]
        if keys is not None and isinstance(value, dict) and set(value) - keys:
            return True
    return False
