# services/diagnostics.py
# The Diagnostics screen: the --doctor health check as structured results, an
# in-app self-test that uses the running engine and microphone (no second model
# load), the redacted bug-report zip, the shortcuts in effect, and the files
# and folders worth opening.

import contextlib
import io
import os
import time
from pathlib import Path

from ..utils import get_user_app_data_path, get_version

_STATUS = {'[OK]': 'ok', '[!]': 'warn', '[X]': 'fail', '[i]': 'info'}

# Next steps the screen can offer for known problems.
_ACTIONS = {
    'CUDA libraries': {'label': 'Install GPU support', 'action': 'gpu_install'},
    'Ollama': {'label': 'How to start Ollama', 'action': 'open_url', 'arg': 'https://ollama.com/download'},
}


def _status_of(marker: str) -> str:
    for key, value in _STATUS.items():
        if key in (marker or ''):
            return value
    return 'info'


# Run the doctor and return [{section, name, status, detail, action?}].
def collect_checks() -> list:
    from .. import doctor
    results = []
    section = {'name': ''}
    original_print = doctor.Check.print

    def capture(self):
        item = {'section': section['name'], 'name': self.name, 'status': _status_of(self.status),
                'detail': self.detail}
        for key, action in _ACTIONS.items():
            if key.lower() in self.name.lower() and item['status'] in ('fail', 'warn'):
                item['action'] = action
        results.append(item)

    def module_print(*args, **kwargs):
        text = ' '.join(str(a) for a in args)
        if doctor.BOLD in text:
            clean = text.replace(doctor.BOLD, '').replace(doctor.RESET, '').strip()
            if clean and '=' not in clean and 'Doctor' not in clean and 'issue' not in clean and 'passed' not in clean:
                section['name'] = clean

    doctor.Check.print = capture
    doctor.print = module_print
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            doctor.run_doctor()
    finally:
        doctor.Check.print = original_print
        del doctor.print
    return results


# In-app self-test. Each step reports through the task reporter as it finishes.
def run_selftest(state_manager, reporter) -> list:
    import numpy as np
    steps = []

    def step(name, fn):
        reporter.report(len(steps) / 4, f"{name}…")
        t0 = time.time()
        try:
            detail = fn()
            steps.append({'name': name, 'ok': True, 'detail': detail, 'seconds': round(time.time() - t0, 2)})
        except Exception as e:
            steps.append({'name': name, 'ok': False, 'detail': str(e), 'seconds': round(time.time() - t0, 2)})
        reporter.report(len(steps) / 4, f"{name}: {'passed' if steps[-1]['ok'] else 'failed'}")

    def mic():
        rec = state_manager.audio_recorder
        rec.level_preview = True
        peak = 0.0
        try:
            end = time.time() + 1.5
            while time.time() < end:
                peak = max(peak, rec.get_current_level())
                time.sleep(0.05)
        finally:
            rec.level_preview = False
        if peak <= 0.0005:
            raise RuntimeError("The microphone sent pure silence. Is it muted, or is access blocked in Windows privacy settings?")
        return f"Hearing sound (peak level {peak:.3f})"

    def model():
        engine = state_manager.whisper_engine
        if getattr(engine, 'model', None) is None:
            raise RuntimeError("No model is loaded")
        return f"{engine.model_key} on {engine.device} ({engine.compute_type})"

    def transcribe():
        engine = state_manager.whisper_engine
        audio = np.zeros(32000, dtype=np.float32)
        t0 = time.time()
        if engine.__class__.__name__ == 'WhisperEngine':
            segments, _info = engine.model.transcribe(audio, beam_size=1, vad_filter=False)
            for _ in segments:   # faster-whisper decodes lazily
                pass
        else:
            engine.model.transcribe(audio)
        return f"2 s of audio processed in {time.time() - t0:.2f} s"

    def clipboard():
        import pyperclip
        original = pyperclip.paste()
        probe = f"whisper-local-selftest-{int(time.time())}"
        pyperclip.copy(probe)
        ok = pyperclip.paste() == probe
        pyperclip.copy(original)
        if not ok:
            raise RuntimeError("The clipboard didn't keep what was copied. Another app may be holding it.")
        return "Copy and paste work"

    step('Microphone', mic)
    step('Model', model)
    step('Transcription', transcribe)
    step('Clipboard', clipboard)
    return steps


def bundle(path: str = None) -> str:
    from ..bundle_logs import bundle_logs
    import datetime
    if not path:
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        path = str(Path.home() / 'Desktop' / f"whisper-local-bundle-{stamp}.zip")
        if not Path(path).parent.is_dir():
            path = str(Path.home() / f"whisper-local-bundle-{stamp}.zip")
    with contextlib.redirect_stdout(io.StringIO()):
        bundle_logs(path)
    return path


def shortcuts(config: dict, transforms: list) -> list:
    from ..utils import beautify_hotkey
    hk = config.get('hotkey') or {}
    rows = [
        ('Dictate', hk.get('recording_hotkey')),
        ('Stop and paste', hk.get('stop_key')),
        ('Stop, paste and send', hk.get('auto_send_key')),
        ('Cancel', hk.get('cancel_combination')),
        ('Voice command', hk.get('command_hotkey') if (config.get('voice_commands') or {}).get('enabled', True) else ''),
        ('AI rephrase', hk.get('rephrase_hotkey')),
        ('Paste last', hk.get('paste_last_hotkey')),
        ('Pause everything', hk.get('pause_hotkey')),
    ]
    for t in transforms:
        if not t.get('disabled') and t['entry'].get('hotkey'):
            rows.append((f"Transform · {t['entry'].get('name')}", t['entry']['hotkey']))
    return [{'action': a, 'keys': beautify_hotkey(k) if k else ''} for a, k in rows if k]


def paths(registry=None) -> dict:
    data = Path(get_user_app_data_path())
    crashes = data / 'crashes'
    recent = 0
    if crashes.is_dir():
        week = time.time() - 7 * 86400
        recent = sum(1 for f in crashes.iterdir() if f.stat().st_mtime > week)
    hub = None
    if registry is not None:
        hub = registry.get_hf_cache_path()
    return {
        'data': str(data), 'log': str(data / 'app.log'), 'settings': str(data / 'user_settings.yaml'),
        'commands': str(data / 'commands.yaml'), 'crashes': str(crashes), 'crash_count': recent,
        'model_cache': hub, 'version': get_version(),
    }


def open_path(kind: str, registry=None) -> str:
    from ..utils import open_file
    table = paths(registry)
    target = table.get(kind)
    if not target:
        raise ValueError(f"Unknown location: {kind}")
    p = Path(target)
    if kind in ('crashes', 'model_cache', 'data'):
        p.mkdir(parents=True, exist_ok=True)
    elif not p.exists():
        raise ValueError(f"{p.name} doesn't exist yet")
    open_file(str(p))
    return str(p)
