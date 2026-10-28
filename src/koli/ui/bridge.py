# ui/bridge.py
# The one object the web pages call (window.pywebview.api.<name>). Every public
# method returns {"ok": true, "data": ...} or {"ok": false, "error": "..."} and
# never raises into the page. Long work starts a background task and returns
# its id; progress arrives as 'task' events. Internals are kept in
# underscore attributes so pywebview doesn't expose them to JavaScript.

import functools
import html as html_lib
import logging
import os
import re
import time
import webbrowser
from pathlib import Path

from .events import publish
from .tasks import get_task_manager

logger = logging.getLogger(__name__)


# A file name a save dialog accepts: characters Windows refuses become "-".
def _safe_name(text: str, fallback: str = 'Meeting') -> str:
    import re
    text = re.sub(r'\s*:\s*', ' - ', str(text or ''))
    return re.sub(r'[\\/*?"<>|]', '-', text).strip() or fallback


# "2026-10-19 Dev sync - integrations" for a meeting's export files.
def _export_name(meeting: dict) -> str:
    day = str(meeting.get('started_at') or '')[:10]
    return _safe_name(f"{day} {meeting.get('title') or ''}".strip(), str(meeting.get('id') or 'Meeting'))

# Links the app may open in the default browser.
_URL_ALLOW = ('https://github.com/drajb/whisper-local', 'https://ollama.com', 'https://huggingface.co',
              'https://developer.microsoft.com/microsoft-edge/webview2', 'https://go.microsoft.com',
              # AI providers: sites, key pages and the device-login pages
              'https://lmstudio.ai', 'https://chatgpt.com', 'https://auth.openai.com', 'https://grok.com',
              'https://accounts.x.ai', 'https://auth.x.ai', 'https://aistudio.google.com', 'https://openrouter.ai',
              'https://platform.openai.com',
              # Google Drive: where the redirect URI is added to the OAuth client
              'https://console.cloud.google.com')


def _api(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return {'ok': True, 'data': fn(self, *args, **kwargs)}
        except Exception as e:
            from ..settings.service import SettingsError
            if not isinstance(e, (SettingsError, ValueError)):
                logger.error(f"UI call {fn.__name__} failed: {e}", exc_info=True)
            return {'ok': False, 'error': str(e) or e.__class__.__name__}
    return wrapper


class Bridge:
    def __init__(self, host):
        self._host = host
        self._tasks = get_task_manager()

    # ── shortcuts to the app ─────────────────────────────────────────────────
    @property
    def _sm(self):
        return self._host.ctx.state_manager

    @property
    def _settings(self):
        return self._host.settings

    @property
    def _cm(self):
        return self._host.ctx.config_manager

    def _task(self, label, fn, kind='general', ref=None):
        return self._tasks.run(label, fn, kind=kind, ref=ref).id

    # ── App ──────────────────────────────────────────────────────────────────

    @_api
    def app_bootstrap(self, which='main'):
        return self._host.bootstrap(which)

    @_api
    def app_quit(self):
        self._host.quit()

    @_api
    def app_open_url(self, url):
        if not any(str(url).startswith(p) for p in _URL_ALLOW):
            raise ValueError("That link isn't on the allowed list")
        webbrowser.open(url)

    @_api
    def app_state(self):
        return self._host.current_state()

    @_api
    def app_welcome_done(self):
        self._cm.set_path('ui.welcome_done', True)
        return True

    @_api
    def js_error(self, message, source='', line=0):
        logging.getLogger('koli.ui.page').warning(f"Page error: {message} ({source}:{line})")

    # ── Windows ──────────────────────────────────────────────────────────────

    @_api
    def window_open(self, route=None):
        self._host.show_main(route)

    @_api
    def window_close(self):
        self._host.hide_main()

    @_api
    def window_minimize(self):
        self._host.main_window.minimize()

    @_api
    def window_toggle_maximize(self):
        self._host.toggle_maximize()

    @_api
    def bar_resize(self, width, height, resting=None, pane=False):
        self._host.resize_bar(int(width), int(height), resting, bool(pane))
        # The page caps its menu at this height and scrolls the rest.
        return {'max_height': self._host._max_bar_height()}

    # What the header monitor shows: the graphics card Koli runs on (its memory
    # in use, and how much of it is Koli's) or, on the CPU, Koli's memory and
    # processor use; plus which models are loaded. Polled every few seconds
    # while the window is open.
    @_api
    def resources(self):
        import psutil
        from ..platform import gpu
        engine = self._sm.whisper_engine
        if getattr(self, '_process', None) is None:
            self._process = psutil.Process()
        process = self._process
        models = [getattr(engine, 'model_key', None)]
        if getattr(engine, 'language_model', None) is not None:
            models.append(engine.language_model_key)
        out = {'device': getattr(engine, 'device', 'cpu'), 'models': [m for m in models if m],
               'ram_mb': process.memory_info().rss // 2 ** 20,
               'cpu_percent': round(process.cpu_percent(None) / (psutil.cpu_count() or 1), 1)}
        if out['device'] == 'cuda':
            index = int(getattr(engine, 'device_index', 0) or 0)
            out['gpu'] = gpu.gpu_memory(index)
            out['gpu_index'] = index
            out['koli_gpu_mb'] = gpu.process_gpu_memory_mb(os.getpid())
        return out

    # The toast of a problem was closed: it is not shown again.
    @_api
    def problem_dismiss(self, problem_id):
        from .events import dismiss_problem
        dismiss_problem(int(problem_id))

    # Press-and-drag on the bubble: numbered moves in page pixels from where
    # the press began; the last one (done) remembers the spot.
    @_api
    def bar_drag(self, seq, dx, dy, done=False):
        self._host.drag_bar(int(seq), float(dx), float(dy), bool(done))

    @_api
    def pick_folder(self, title='Choose a folder'):
        return self._host.pick('folder', title)

    @_api
    def pick_file(self, title='Choose a file', types=None):
        return self._host.pick('open', title, types)

    @_api
    def pick_save(self, title='Save as', filename=''):
        return self._host.pick('save', title, filename=filename)

    # ── Logs ─────────────────────────────────────────────────────────────────

    @_api
    def log_tail(self, n=300):
        from .log_stream import get_handler
        return get_handler().tail(int(n))

    @_api
    def log_clear(self):
        from .log_stream import get_handler
        get_handler().clear()

    # ── Dictation ────────────────────────────────────────────────────────────

    @_api
    def dictation_start(self):
        self._sm.start_recording()

    @_api
    def dictation_stop(self, send_enter=False):
        return self._sm.stop_recording(bool(send_enter))

    @_api
    def dictation_cancel(self):
        self._sm.cancel_active_recording()

    # ── The dictation bubble (live_session_flow.py) ──
    # You clicked into the bubble's words to fix something.
    @_api
    def bubble_open(self):
        return self._sm.bubble_open()

    # The bubble's text and caret after you typed or selected (code points).
    @_api
    def bubble_edit(self, text, start, end):
        self._sm.bubble_edit(str(text or ''), int(start), int(end))

    # Write the text into the app; `send` also presses Enter there.
    @_api
    def bubble_write(self, send=False):
        return self._sm.bubble_write(bool(send))

    @_api
    def bubble_listen(self, on):
        return self._sm.bubble_listen(bool(on))

    @_api
    def bubble_cancel(self):
        self._sm.bubble_cancel()

    # The bubble's Stop on "Writing it down": drop that dictation.
    @_api
    def processing_stop(self):
        return self._sm.stop_processing()

    # The bubble's buttons while an auto-send counts down before Enter.
    @_api
    def auto_send_cancel(self):
        ds = self._sm.clipboard_manager.delayed_send
        return bool(ds and ds.cancel('you pressed Cancel'))

    @_api
    def auto_send_now(self):
        ds = self._sm.clipboard_manager.delayed_send
        return bool(ds and ds.send_now())

    @_api
    def dictation_pause(self, paused):
        listener = getattr(self._sm, '_hotkey_listener_ref', None)
        if listener is not None and bool(paused) != bool(listener.is_paused):
            listener._pause_hotkey_pressed()
        else:
            self._sm.set_paused(bool(paused))
        return bool(self._sm.is_paused)

    # Shortcuts the OS grabs before any app sees them, so the shortcut
    # recorder can say why a key went missing (Windows' Alt+Shift+1 "Switch
    # to English" leaves only Alt+Shift).
    @_api
    def hotkeys_reserved(self):
        from ..platform import hotkeys
        return hotkeys.os_reserved_hotkeys()

    @_api
    def paste_last(self):
        return self._host.paste_last()

    @_api
    def copy_text(self, text):
        import pyperclip
        pyperclip.copy(str(text or ''))
        return True

    @_api
    def recent(self):
        return self._sm.get_recent_transcriptions()

    # ── Settings ─────────────────────────────────────────────────────────────

    @_api
    def settings_all(self):
        return self._settings.snapshot()

    @_api
    def settings_set(self, path, value):
        return self._settings.set(path, value)

    @_api
    def settings_set_many(self, patch, label=''):
        return self._settings.set_many(dict(patch or {}), label=label)

    @_api
    def settings_undo(self):
        return self._settings.undo()

    @_api
    def settings_reset_section(self, section):
        return self._settings.reset_section(section)

    @_api
    def settings_reset_all(self):
        return self._settings.reset_all()

    @_api
    def simple_get(self):
        return self._host.simple_answers()

    @_api
    def simple_set(self, question, answer):
        from ..settings import simple_mode
        ctx = self._host.simple_context()
        if question == 'language':
            ctx['language'] = answer
        if question == 'startup':
            from ..services import system
            return system.set_autostart(bool(answer))
        patch = simple_mode.to_patch(question, answer, ctx)
        result = self._settings.set_many(patch, label='Simple setting')
        return result

    # ── Audio ────────────────────────────────────────────────────────────────

    @_api
    def audio_devices(self):
        return self._host.audio_info()

    @_api
    def mic_test(self, on):
        self._sm.audio_recorder.level_preview = bool(on)
        self._host.mic_testing = bool(on)
        return bool(on)

    # Settings → "Does Koli recognize you?": listens for `seconds` (nothing is
    # recorded or typed) and says what auto-dictation would do with it.
    @_api
    def voice_check(self, seconds=5):
        return self._sm.check_my_voice(max(2.0, min(15.0, float(seconds))))

    # Your learned speaking level on this microphone, for "Set it by ear".
    @_api
    def my_voice_level(self):
        return self._sm.my_voice_level()

    @_api
    def sound_preview(self, path_setting):
        from ..utils import resolve_asset_path
        rel = self._settings.get(path_setting)
        path = resolve_asset_path(rel) if rel else None
        if not path or not os.path.exists(path):
            raise ValueError("Sound file not found")
        from playsound3 import playsound
        playsound(path, block=False)

    # ── Models and GPU ───────────────────────────────────────────────────────

    @_api
    def models_list(self):
        return self._host.models_list()

    @_api
    def model_download(self, key):
        from ..services import models as svc
        registry = self._host.ctx.model_registry
        return self._task(f"Downloading {key}", lambda r: svc.ensure_downloaded(registry, key, r), kind='download',
                          ref=key)

    @_api
    def model_delete(self, key):
        from ..services import models as svc
        return svc.delete_cached(self._host.ctx.model_registry, key, self._settings.get('whisper.model'))

    @_api
    def model_set_visible(self, key, visible):
        models = dict(self._settings.get('whisper.models') or {})
        if key not in models:
            raise ValueError(f"Unknown model {key}")
        entry = dict(models[key])
        entry['enabled'] = bool(visible)
        models[key] = entry
        return self._settings.set('whisper.models', models)

    @_api
    def model_add(self, kind, value, label=''):
        from ..services import models as svc
        if kind == 'hf':
            key, entry = svc.model_entry_for_repo(str(value).strip(), label)
        else:
            key, entry = svc.model_entry_for_folder(str(value), label)
        models = dict(self._settings.get('whisper.models') or {})
        models[key] = entry
        self._settings.set('whisper.models', models)
        return key

    @_api
    def model_remove(self, key):
        models = dict(self._settings.get('whisper.models') or {})
        if key == self._settings.get('whisper.model'):
            raise ValueError("That model is in use")
        models.pop(key, None)
        return self._settings.set('whisper.models', models)

    @_api
    def extra_status(self, name):
        from ..services import system
        return {'installed': system.extra_installed(name)}

    @_api
    def extra_install(self, name):
        from ..services import system
        if name not in system.EXTRAS:
            raise ValueError(f"Unknown optional package: {name}")

        # The loopback package turns meeting detection on as soon as it lands.
        def done(task):
            meetings = getattr(self._host.ctx, 'meetings', None)
            if name == 'loopback' and task.status == 'done' and meetings is not None:
                meetings.package_installed()
        return self._tasks.run(f"Installing the {name} package", lambda r: system.extra_install(name, r),
                               kind='install', on_done=done).id

    @_api
    def gpu_list(self):
        from ..services import system
        return system.gpu_list()

    @_api
    def gpu_detect(self):
        from ..services import system
        return system.gpu_detect(self._settings.get('whisper.device', 'cpu'))

    @_api
    def gpu_install(self):
        from ..services import system
        info = system.gpu_detect(self._settings.get('whisper.device', 'cpu'))
        settings = self._settings

        def job(reporter):
            system.gpu_install(info.get('class'), reporter)
            settings.cm.set_path('onboarding.gpu', 'complete')
            settings.cm.set_path('onboarding.gpu_class', info.get('class'))
            reporter.report(0.95, 'Switching to the GPU')
            settings.set_many({'whisper.device': 'cuda', 'whisper.compute_type': 'float16'},
                              label='Use the GPU')
        return self._task('Installing GPU support', job, kind='gpu')

    # ── History and insights ─────────────────────────────────────────────────

    @_api
    def history_query(self, text='', app='', period='all', offset=0, limit=50):
        from ..services import history
        return history.query(text, app, period, int(offset), int(limit))

    @_api
    def history_apps(self):
        from ..services import history
        return history.apps()

    @_api
    def history_export(self, fmt='txt'):
        from ..services import history
        import datetime
        name = f"koli-history-{datetime.date.today().isoformat()}.{fmt}"
        path = self._host.pick('save', 'Export history', filename=name)
        if not path:
            return None
        return history.export(path)

    @_api
    def history_clear(self):
        from ..services import history
        return history.clear()

    @_api
    def history_fix(self, frm, to):
        from ..services import history
        return history.fix_everywhere(self._settings, frm, to)

    @_api
    def history_suggest(self):
        from ..services import history
        return history.suggest_words(self._settings)

    @_api
    def insights(self, days=14):
        from ..services import insights
        return insights.summary(int(days))

    # ── Vocabulary ───────────────────────────────────────────────────────────

    @_api
    def vocab_add(self, words):
        from ..services import vocabulary
        if isinstance(words, str):
            words = [words]
        return vocabulary.add_words(self._settings, list(words or []))

    @_api
    def vocab_remove(self, word):
        from ..services import vocabulary
        return vocabulary.remove_word(self._settings, word)

    @_api
    def vocab_scan(self, folder=None):
        from ..services import vocabulary
        folder = folder or self._host.pick('folder', 'Choose a folder of your documents')
        if not folder:
            return None
        return vocabulary.scan_folder(self._settings, folder)

    @_api
    def vocab_set_corrections(self, data):
        from ..services import vocabulary
        return vocabulary.set_corrections(self._settings, data)

    # Blocks until you've said it (or a few seconds of nothing).
    @_api
    def vocab_record_correction(self, canonical):
        from ..services import vocabulary
        return vocabulary.record_correction(self._sm, self._settings, canonical)

    @_api
    def vocab_set_replacements(self, items):
        from ..services import vocabulary
        return vocabulary.set_replacements(self._settings, items)

    @_api
    def vocab_set_snippets(self, items):
        from ..services import vocabulary
        return vocabulary.set_snippets(self._settings, items)

    # ── Commands and transforms ──────────────────────────────────────────────

    @_api
    def commands_list(self):
        from ..services import commands
        return commands.list_commands()

    @_api
    def command_save(self, entry, original_id=None):
        from ..services import commands
        return commands.save_command(entry, original_id)

    @_api
    def command_enable(self, ident, enabled):
        from ..services import commands
        commands.set_command_enabled(ident, bool(enabled))

    @_api
    def command_delete(self, ident):
        from ..services import commands
        commands.delete_command(ident)

    @_api
    def command_test(self, text):
        from ..services import commands
        return commands.test_phrase(self._sm.voice_command_manager, text)

    # ── Wake word (the Wake word card on the Commands screen) ──

    @_api
    def wake_word_status(self):
        from ..services import commands
        return commands.wake_status(self._sm)

    @_api
    def wake_word_prepare(self, spelling='', more=False):
        from ..services import commands
        return commands.wake_prepare(self._sm, spelling, more)

    # Blocks until you've said the word (or a few seconds of nothing).
    @_api
    def wake_word_record(self):
        from ..services import commands
        return commands.wake_record(self._sm)

    @_api
    def wake_word_save(self):
        from ..services import commands
        return commands.wake_save(self._sm, self._settings)

    @_api
    def wake_word_test(self):
        from ..services import commands
        return commands.wake_test(self._sm)

    @_api
    def wake_word_reset(self):
        from ..services import commands
        return commands.wake_reset(self._sm, self._settings)

    # The apps open right now, for a "Press keys" step's Send to picker.
    @_api
    def command_open_apps(self):
        from ..platform import window_focus
        return window_focus.open_apps()

    @_api
    def transforms_list(self):
        from ..services import commands
        return commands.list_transforms()

    @_api
    def transform_save(self, entry, original_id=None):
        from ..services import commands
        name = commands.save_transform(entry, original_id)
        self._sm.reload_transforms()
        return name

    @_api
    def transform_enable(self, ident, enabled):
        from ..services import commands
        commands.set_transform_enabled(ident, bool(enabled))
        self._sm.reload_transforms()

    @_api
    def transform_delete(self, ident):
        from ..services import commands
        commands.delete_transform(ident)
        self._sm.reload_transforms()

    @_api
    def transform_apply(self, name):
        self._host.restore_external_foreground()
        time.sleep(0.15)
        return self._sm.apply_transform(name)

    # ── Meetings ─────────────────────────────────────────────────────────────

    @property
    def _meetings(self):
        svc = getattr(self._host.ctx, 'meetings', None)
        if svc is None:
            raise ValueError("Meeting mode is not running")
        return svc

    @_api
    def meeting_state(self):
        return self._meetings.state()

    @_api
    def meeting_start(self):
        self._meetings.start()

    @_api
    def meeting_end(self):
        self._meetings.end()

    @_api
    def meeting_new(self):
        self._meetings.new()

    @_api
    def meeting_pause(self):
        self._meetings.pause()

    @_api
    def meeting_resume(self):
        self._meetings.resume()

    @_api
    def meeting_exclude(self, on):
        self._meetings.set_excluded(bool(on))

    @_api
    def meeting_offer(self, accept):
        self._meetings.offer(bool(accept))

    @_api
    def meeting_rename(self, speaker_id, name, meeting_id=None):
        svc = self._meetings
        current = svc.state().get('id')
        if meeting_id and meeting_id != current:
            svc.rename_saved_speaker(meeting_id, speaker_id, name)
        else:
            svc.rename_speaker(speaker_id, name)

    # Voices saved from meetings, with their ids, names and meetings (never voiceprints).
    @_api
    def meeting_voices(self):
        return self._meetings.voices_list()

    @_api
    def meeting_forget_voice(self, voice_id):
        return self._meetings.forget_voice(str(voice_id))

    @_api
    def meeting_continue_previous(self):
        self._meetings.continue_previous()

    @_api
    def meeting_split(self):
        self._meetings.split_resumed()

    @_api
    def meetings_list(self):
        return self._meetings.list()

    # Saved meetings holding every word of the query (title, speakers, summary,
    # screenshot captions, transcript), with where it matched.
    @_api
    def meetings_search(self, query):
        return self._meetings.search(str(query or ''))

    @_api
    def meeting_get(self, meeting_id):
        return self._meetings.get(meeting_id)

    @_api
    def meeting_delete(self, meeting_id):
        return self._meetings.delete(meeting_id)

    # Writes the export where the user picks; returns the path or None. An
    # md export with screenshots gets its PNGs copied into "<name>-shots/"
    # next to the file, where its image links point.
    @_api
    def meeting_export(self, meeting_id, fmt='txt'):
        svc = self._meetings
        meeting = svc.get(meeting_id)
        path = self._host.pick('save', 'Export meeting', filename=f'{_export_name(meeting)}.{fmt}')
        if not path:
            return None
        shots = meeting.get('screenshots', []) if fmt == 'md' else []
        folder = ''
        if shots:
            import shutil
            folder = os.path.splitext(os.path.basename(path))[0] + '-shots'
            target = os.path.join(os.path.dirname(path), folder)
            os.makedirs(target, exist_ok=True)
            for shot in shots:
                try:
                    shutil.copy2(svc.store.shot_path(meeting_id, shot['image']), os.path.join(target, shot['image']))
                except FileNotFoundError:
                    logger.warning(f"Screenshot {shot['image']} is missing; the export links it anyway")
        text = svc.export(meeting_id, fmt, shots_folder=folder)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    # ── Meeting brief: the summary the AI writes, and its PDF ────────────────

    # The meeting's brief, or None when there is no summary yet.
    @_api
    def meeting_brief(self, meeting_id):
        return self._meetings.brief(str(meeting_id))

    # What the Summary tab needs to offer a summary: an AI model chosen, a
    # job already running, the template (the last summary's, the one picked
    # for the meeting, or the default) and the last summary's instructions.
    @_api
    def meeting_summary_state(self, meeting_id):
        svc = self._meetings
        meeting_id = str(meeting_id)
        brief = svc.brief(meeting_id) or {}
        last = brief.get('template') if brief.get('template') and svc.template_exists(brief['template']) else ''
        return {'ai_ready': svc.ai_ready(), 'running': svc.summary_running(meeting_id),
                'template': last or svc.meeting_template(meeting_id) or svc.default_template(),
                'instructions': brief.get('instructions', '')}

    # Writes the summary with the AI in the background; the page hears
    # 'meeting_brief' when it is saved. `instructions` are your own words for
    # this summary. Returns the task's id.
    @_api
    def meeting_summarize(self, meeting_id, template='', language='', instructions=''):
        return self._meetings.summarize(str(meeting_id), str(template or ''), str(language or ''),
                                        instructions=str(instructions or ''))

    # The summary template for the meeting running now (or the call offered),
    # picked in the meeting panel; '' is the default.
    @_api
    def meeting_set_template(self, template_id=''):
        self._meetings.set_summary_template(str(template_id or ''))

    # A new title for the meeting, typed by you; live or saved.
    @_api
    def meeting_retitle(self, meeting_id, title):
        return self._meetings.rename_meeting(str(meeting_id), str(title or ''))

    # Who was there besides the voices heard: the names you add by hand.
    @_api
    def meeting_attendees(self, meeting_id, names):
        return self._meetings.set_attendees(str(meeting_id), [str(n) for n in (names or [])])

    # ── Chat about a meeting (meeting/chat.py) ───────────────────────────────

    @_api
    def meeting_chat(self, meeting_id):
        return self._meetings.chat_history(str(meeting_id))

    # Waits for the AI's answer; returns {'messages', 'changed'}.
    @_api
    def meeting_chat_ask(self, meeting_id, question):
        return self._meetings.chat_ask(str(meeting_id), str(question or ''))

    @_api
    def meeting_chat_clear(self, meeting_id):
        self._meetings.chat_clear(str(meeting_id))

    # ── Summary templates (docs/design/meeting-summary.md) ───────────────────

    @_api
    def summary_templates(self):
        return self._meetings.templates_list()

    # One template: its row, the whole file's text, and the layout.
    @_api
    def summary_template(self, template_id):
        return self._meetings.template_get(str(template_id))

    # How a brief written with this template is laid out (blocks, headings).
    @_api
    def summary_template_layout(self, template_id=''):
        return self._meetings.template_layout(str(template_id or ''))

    @_api
    def summary_template_default(self, template_id):
        self._meetings.templates.get(str(template_id))
        self._settings.set('meeting.summary.template', str(template_id))
        return str(template_id)

    # Saves the editor's text as one of the user's templates (checked first).
    # A renamed default stays the default under its new id.
    @_api
    def summary_template_save(self, text, replaces=''):
        svc = self._meetings
        row = svc.template_save(str(text or ''), str(replaces or ''))
        if replaces and replaces != row['id'] and svc.default_template() == replaces:
            self._settings.set('meeting.summary.template', row['id'])
            row['default'] = True
        return row

    # Deletes one of the user's templates; the default falls back to the
    # built-in brief when its template is gone.
    @_api
    def summary_template_delete(self, template_id):
        svc = self._meetings
        svc.template_delete(str(template_id))
        if svc.default_template() == template_id and not svc.template_exists(str(template_id)):
            self._settings.set('meeting.summary.template', 'standard')
        return True

    @_api
    def summary_template_copy(self, template_id, name=''):
        return self._meetings.template_copy(str(template_id), str(name or ''))

    # The AI drafts a template: kind 'describe' (text = what the user wants)
    # or 'examples' (text = past summaries). Returns {text, problems}.
    @_api
    def summary_template_draft(self, kind, text, name=''):
        return self._meetings.template_draft('examples' if kind == 'examples' else 'describe', str(text or ''),
                                             str(name or ''))

    # Past summaries from files, as text for drafting a template: Markdown,
    # text, HTML (tags dropped) or a Koli brief (.json). None = cancelled.
    @_api
    def summary_template_read_files(self):
        paths = self._host.pick('open', 'Pick past summaries', multiple=True,
                                types=('Summaries (*.md;*.txt;*.html;*.htm;*.json)', 'All files (*.*)'))
        if not paths:
            return None
        parts = []
        for path in paths[:10]:
            text = Path(path).read_text(encoding='utf-8', errors='replace')
            if path.lower().endswith(('.html', '.htm')):
                text = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', text)
                text = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>', '\n', text)
                text = html_lib.unescape(re.sub(r'<[^>]+>', '', text))
                text = re.sub(r'\n\s*\n+', '\n\n', text)
            parts.append(f"--- {Path(path).name} ---\n{text.strip()[:20000]}")
        return {'names': [Path(p).name for p in paths[:10]], 'text': '\n\n'.join(parts)}

    # The Summary tab as a PDF where the user picks; the page opens its
    # sections and applies its print styles before calling. None = cancelled.
    @_api
    def meeting_export_pdf(self, meeting_id, title=''):
        name = _safe_name(title, str(meeting_id))
        path = self._host.pick('save', 'Export the summary as PDF', types=('PDF (*.pdf)',), filename=f'{name}.pdf')
        if not path:
            return None
        if not self._host.print_pdf(path):
            raise RuntimeError("Couldn't write the PDF")
        return path

    # ── Meeting in Google Drive ──────────────────────────────────────────────

    # Saves the meeting as a Google Doc (Quick summary, Report, Transcript)
    # in "Koli/Meetings/<name>/"; a second save replaces the doc there.
    @_api
    def meeting_drive_export(self, meeting_id):
        return self._meetings.export_to_drive(str(meeting_id))

    # Opens the saved doc (or its folder) in the browser, where Google's own
    # Share button decides who can see it. Only Google links ever open.
    @_api
    def meeting_drive_open(self, meeting_id, which='doc'):
        saved = self._meetings.get(str(meeting_id)).get('drive') or {}
        url = str(saved.get('folder_url' if which == 'folder' else 'doc_url') or '')
        if not url:
            raise ValueError("This meeting isn't in Google Drive yet")
        if not url.startswith(('https://docs.google.com/', 'https://drive.google.com/')):
            raise ValueError("That isn't a Google Drive link")
        webbrowser.open(url)
        return url

    # Google's own Share window: Drive for desktop's, for the doc it synced
    # (platform/*/drive_share.py). Without Drive for desktop, or before the
    # doc has synced, the doc opens in the browser and its Share button does
    # the same. {'window': True} when the Share window opened.
    @_api
    def meeting_drive_share(self, meeting_id):
        import os
        from pathlib import Path
        svc = self._meetings
        meeting = svc.get(str(meeting_id))
        saved = meeting.get('drive') or {}
        if not saved.get('doc_id'):
            raise ValueError("This meeting isn't in Google Drive yet")
        share = getattr(self, '_drive_share', None)
        if share is None:
            from ..platform import drive_share as share
        email = (getattr(self, '_drive_email', None) or self._drive_account_email)()
        exists = getattr(self, '_file_exists', None) or os.path.exists
        name = saved.get('name')
        if not name:
            from ..meeting.gdoc import folder_name
            name = folder_name(meeting, svc.brief(str(meeting_id)) or {})
        root = share.local_root(email) if share.available() else None
        if root is not None:
            path = Path(root) / 'My Drive' / 'Koli' / 'Meetings' / name / f"{name}.gdoc"
            if exists(str(path)) and share.show_share(path):
                return {'window': True}
        self.meeting_drive_open(meeting_id)
        return {'window': False}

    @staticmethod
    def _drive_account_email() -> str:
        from .. import drive
        return str(drive.ACCOUNT.status().get('email') or '')

    # ── Meeting screenshots (docs/design/meeting-screenshots.md) ─────────────

    # The camera button: snip into the running meeting. {'ok', 'reason'}.
    @_api
    def meeting_screenshot(self):
        return self._meetings.screenshot()

    @_api
    def meeting_caption(self, meeting_id, image, caption):
        return self._meetings.set_caption(str(meeting_id), str(image), str(caption or ''))

    # A screenshot as a data URL for an <img>.
    @_api
    def meeting_image(self, meeting_id, image):
        return self._meetings.image(str(meeting_id), str(image))

    @_api
    def meeting_resize(self, width, height):
        self._host.resize_panel(int(width), int(height))

    @_api
    def meeting_collapse(self, collapsed, width=None, height=None):
        self._host.set_panel_collapsed(bool(collapsed), width, height)
        return {'collapsed': bool(collapsed)}

    @_api
    def meeting_drag(self, seq, dx, dy, done=False):
        self._host.drag_panel(int(seq), float(dx), float(dy), bool(done))

    @_api
    def meeting_show_in_sharing(self, on):
        self._host.show_panel_in_sharing(bool(on))
        return {'shown': bool(on)}

    # ── App rules and profiles ───────────────────────────────────────────────

    @_api
    def rules_list(self):
        from ..services import rules
        items = rules.list_rules()
        fg = self._host.last_external
        return {'rules': items, 'foreground': fg,
                'matching': rules.matching_rule(items, fg.get('exe', ''), fg.get('title', ''))}

    @_api
    def rule_save(self, entry, original_id=None):
        from ..services import rules
        ident = rules.save_rule(entry, original_id)
        self._rules_changed()
        return ident

    @_api
    def rule_enable(self, ident, enabled):
        from ..services import rules
        rules.set_rule_enabled(ident, bool(enabled))
        self._rules_changed()

    @_api
    def rule_delete(self, ident):
        from ..services import rules
        rules.delete_rule(ident)
        self._rules_changed()

    # A rule may turn auto-dictation on for its app: hook or unhook focus now.
    def _rules_changed(self):
        coordinator = getattr(self._sm, '_auto_dictation', None)
        if coordinator is not None:
            coordinator.apply_settings()

    @_api
    def rules_reorder(self, ids):
        from ..services import rules
        rules.reorder_rules(list(ids or []))

    @_api
    def profiles_list(self):
        from ..services import rules
        return rules.list_profiles(self._sm.profile_manager)

    @_api
    def profile_activate(self, name):
        from ..services import rules
        result = rules.activate_profile(self._settings, self._sm.profile_manager, name)
        self._sm.system_tray.refresh_menu()
        publish('profile', {'active': name})
        return result

    @_api
    def profile_save(self, entry, name, original_id=None):
        from ..services import rules
        return rules.save_profile(entry, name, original_id)

    @_api
    def profile_delete(self, name):
        from ..services import rules
        rules.delete_profile(name)

    @_api
    def profile_enable(self, name, enabled):
        from ..services import rules
        rules.set_profile_enabled(name, bool(enabled))

    # ── AI providers ─────────────────────────────────────────────────────────
    # Settings › AI: provider cards (status, keys, device logins, Test) and the
    # model pickers. Addresses and routes are ordinary settings.

    @_api
    def ai_catalog(self):
        from .. import llm
        return llm.catalog()

    @_api
    def ai_providers(self, provider=None):
        from .. import llm
        return llm.provider_status(provider)

    @_api
    def ai_models(self, provider):
        from .. import llm
        return llm.list_models(provider)

    @_api
    def ai_test(self, model):
        from .. import llm
        return llm.test(model)

    # A key provider's API key, or LM Studio's optional token. Returns how many
    # models the key can use.
    @_api
    def ai_set_key(self, provider, key):
        from ..llm import providers
        target = providers.get(provider)
        if not hasattr(target, 'set_key'):
            raise ValueError(f"{target.label} doesn't take a key")
        return target.set_key(key)

    # Remove a key, or disconnect a subscription.
    @_api
    def ai_remove(self, provider):
        from ..llm import providers
        from ..llm.oauth import SUBSCRIPTIONS
        if provider in SUBSCRIPTIONS:
            SUBSCRIPTIONS[provider].disconnect()
        else:
            providers.get(provider).remove()

    @_api
    def ai_login_start(self, provider):
        from ..llm.oauth import SUBSCRIPTIONS
        if provider not in SUBSCRIPTIONS:
            raise ValueError(f"{provider} doesn't sign in with a code")
        return SUBSCRIPTIONS[provider].start_login()

    @_api
    def ai_login_status(self, provider):
        from ..llm.oauth import SUBSCRIPTIONS
        return SUBSCRIPTIONS[provider].login_state()

    # Opens the sign-in page of the login in progress. The address came from
    # the provider, not the page, so it isn't held to the link allow list
    # (xAI may send a host we don't know in advance).
    @_api
    def ai_login_open(self, provider):
        from ..llm.oauth import SUBSCRIPTIONS
        url = str(SUBSCRIPTIONS[provider].login_state().get('url') or '')
        if not url.startswith('https://'):
            raise ValueError("There's no sign-in page to open; press Connect again")
        webbrowser.open(url)

    @_api
    def ai_login_cancel(self, provider):
        from ..llm.oauth import SUBSCRIPTIONS
        SUBSCRIPTIONS[provider].cancel_login()

    # ── Google Drive ─────────────────────────────────────────────────────────
    # One Google account, signed in through the browser (design:
    # docs/design/google-drive.md). The pasted client id and secret and the
    # login are stored encrypted; the page only ever sees a masked secret.

    @_api
    def drive_status(self):
        from .. import drive
        from ..drive import auth
        return {**drive.ACCOUNT.status(), 'client_id': drive.get_client()['client_id'],
                'client_hint': drive.client_hint(), 'redirect_uri': auth.REDIRECT_URI}

    @_api
    def drive_set_client(self, client_id, client_secret):
        from .. import drive
        drive.set_client(client_id, client_secret)

    @_api
    def drive_login_start(self):
        from .. import drive
        return drive.ACCOUNT.start_login()

    @_api
    def drive_login_status(self):
        from .. import drive
        return drive.ACCOUNT.login_state()

    # Opens Google's consent page for the login in progress.
    @_api
    def drive_login_open(self):
        from .. import drive
        url = str(drive.ACCOUNT.login_state().get('url') or '')
        if not url.startswith('https://'):
            raise ValueError("There's no sign-in page to open; press Connect again")
        webbrowser.open(url)

    @_api
    def drive_login_cancel(self):
        from .. import drive
        drive.ACCOUNT.cancel_login()

    @_api
    def drive_disconnect(self):
        from .. import drive
        drive.ACCOUNT.disconnect()

    # ── Practice and Learned vocabulary ──────────────────────────────────────
    # A round: intake → sentences (two batches) → paragraph → results → apply.
    # Recording a reading blocks until you pause, like voice_check.

    @property
    def _practice(self):
        if getattr(self, '_practice_service', None) is None:
            from ..services.practice import PracticeService
            self._practice_service = PracticeService(self._sm, self._cm)
        return self._practice_service

    @_api
    def practice_state(self):
        return self._practice.state()

    @_api
    def practice_save_intake(self, intake):
        return self._practice.save_intake(intake)

    @_api
    def practice_start(self):
        return self._practice.start()

    @_api
    def practice_record(self, index):
        return self._practice.record(int(index))

    @_api
    def practice_stop(self):
        self._practice.stop()

    @_api
    def practice_skip(self, index):
        return self._practice.skip(int(index))

    @_api
    def practice_flag(self, index):
        return self._practice.flag(int(index))

    @_api
    def practice_next(self):
        return self._practice.next()

    @_api
    def practice_results(self):
        return self._practice.results()

    @_api
    def practice_apply(self, ids):
        return self._practice.apply(list(ids or []))

    @_api
    def practice_close(self):
        self._practice.close()

    @_api
    def learned_list(self):
        return self._practice.learned()

    @_api
    def learned_delete(self, ids):
        return self._practice.delete(list(ids or []))

    @_api
    def learned_delete_all(self):
        self._practice.delete_all()

    # ── Training (Koli learns from your dictations) ──────────────────────────
    # What is kept and how much disk it takes, and the delete buttons. The
    # switch and the cap are ordinary settings (learning.*).

    @property
    def _learning(self):
        recorder = getattr(self._sm, 'clip_recorder', None)
        if recorder is None:
            raise ValueError("Learning from dictations isn't running")
        from ..services import learning
        return learning.get(recorder, engine=lambda: getattr(self._sm, 'whisper_engine', None))

    @_api
    def learning_state(self):
        return self._learning.state()

    @_api
    def learning_recent(self, limit=20, offset=0, show='all'):
        return self._learning.recent(int(limit), int(offset), str(show))

    @_api
    def learning_audio(self, clip_id):
        return self._learning.audio(str(clip_id))

    # Blocks while the night model hears the clip (a few seconds on a GPU).
    @_api
    def learning_test(self, clip_id):
        return self._learning.test(str(clip_id))

    @_api
    def learning_delete_recent(self, span):
        return self._learning.delete_recent(str(span))

    @_api
    def learning_delete_range(self, start, end):
        return self._learning.delete_range(str(start), str(end))

    @_api
    def learning_delete_all(self):
        return self._learning.delete_all()

    @_api
    def learning_open_folder(self):
        from ..utils import open_file
        folder = self._learning.store.folder
        folder.mkdir(parents=True, exist_ok=True)
        open_file(str(folder))
        return str(folder)

    # ── AI actions ───────────────────────────────────────────────────────────
    # The bubble's buttons (cancel, approve, undo, save as a command) and the
    # Commands screen's card (password, activity). See agent/runner.py.

    @property
    def _agent(self):
        agent = getattr(self._sm, 'agent', None)
        if agent is None:
            raise ValueError("AI actions aren't available")
        return agent

    @_api
    def agent_cancel(self):
        self._agent.cancel()

    @_api
    def agent_approve(self):
        self._agent.approve_click()

    @_api
    def agent_undo(self, run_id=None):
        return self._agent.undo(run_id or None)

    @_api
    def agent_save(self, run_id, phrase):
        from ..services import commands
        run = self._agent.journal.get(run_id)
        if run is None:
            raise ValueError("That action isn't in the history any more")
        saved = commands.save_ai_command(phrase, run['script'])
        self._agent.saved(run_id)
        return saved

    # Explorer at what an AI action made or moved (from its own results).
    @_api
    def agent_show(self, run_id):
        if not self._agent.show(run_id):
            raise ValueError("It isn't there any more")

    @_api
    def agent_decline(self, run_id):
        self._agent.decline(run_id)

    @_api
    def agent_history(self, limit=20):
        runs = self._agent.journal.runs(int(limit))
        return [{'id': r.get('id'), 'at': r.get('at'), 'sentence': r.get('sentence'), 'summary': r.get('summary'),
                 'lines': r.get('lines') or [], 'outcome': r.get('outcome'), 'undone': bool(r.get('undone')),
                 'saved_command': bool(r.get('saved_command')),
                 'undoable': any(x.get('undo') and x.get('status') == 'done' for x in r.get('results') or [])}
                for r in reversed(runs)]

    @_api
    def agent_password_status(self):
        approval = self._agent.approval
        return {'set': bool(approval.phrase()), 'voice': approval.can_listen(), 'recorded': approval.recorded_here()}

    # Recording the password, like the wake word: wait until the voice model
    # is ready (False starts loading it; ask again), begin with the phrase as
    # typed, say it three times (each call blocks until you have), then save.
    @_api
    def agent_password_ready(self):
        meetings = getattr(self._sm, 'meetings', None)
        if meetings is None:
            raise ValueError('Recording the password needs the app window')
        return bool(meetings.voiceprint_ready())

    @_api
    def agent_password_begin(self, phrase):
        self._agent.approval.begin_recording(phrase)
        return True

    @_api
    def agent_password_record(self):
        return self._agent.approval.record_take()

    @_api
    def agent_password_save(self):
        self._agent.approval.save_recording()
        return True

    @_api
    def agent_password_clear(self):
        self._agent.approval.clear()

    # Say the password once: whether it would approve, and why not.
    @_api
    def agent_password_test(self):
        approval = self._agent.approval
        if not approval.phrase():
            raise ValueError("Set a password first")
        if not approval.can_listen():
            raise ValueError("Your voice isn't recorded on this microphone: record your own wake word first")
        ok, why = approval.ask_once()
        return {'ok': ok, 'why': why}

    # ── Diagnostics ──────────────────────────────────────────────────────────

    @_api
    def diag_checks(self):
        from ..services import diagnostics
        return diagnostics.collect_checks()

    @_api
    def diag_selftest(self):
        from ..services import diagnostics
        sm = self._sm
        return self._task('Self-test', lambda r: diagnostics.run_selftest(sm, r), kind='selftest')

    @_api
    def diag_bundle(self):
        from ..services import diagnostics
        import datetime
        name = f"koli-bundle-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        path = self._host.pick('save', 'Save the bug report', filename=name)
        if not path:
            return None
        return diagnostics.bundle(path)

    @_api
    def diag_shortcuts(self):
        from ..services import commands, diagnostics
        return diagnostics.shortcuts(self._settings.snapshot(), commands.list_transforms())

    @_api
    def diag_paths(self):
        from ..services import diagnostics
        return diagnostics.paths(self._host.ctx.model_registry)

    @_api
    def diag_open(self, kind):
        from ..services import diagnostics
        return diagnostics.open_path(kind, self._host.ctx.model_registry)

    # ── System ───────────────────────────────────────────────────────────────

    @_api
    def system_autostart(self):
        from ..services import system
        return system.autostart_state()

    @_api
    def system_set_autostart(self, enabled):
        from ..services import system
        return system.set_autostart(bool(enabled))

    @_api
    def system_export_settings(self):
        from ..services import system
        folder = self._host.pick('folder', 'Choose where to save the backup')
        return system.export_settings(folder) if folder else None

    @_api
    def system_import_settings(self):
        from ..services import system
        folder = self._host.pick('folder', 'Choose a backup folder')
        return system.import_settings(folder, self._cm) if folder else None

    @_api
    def system_export_model(self):
        from ..services import system
        folder = self._host.pick('folder', 'Choose where to put the model (USB drive, share…)')
        if not folder:
            return None
        cm = self._cm
        return self._task('Exporting the model', lambda r: system.export_model(folder, cm), kind='model-transfer')

    @_api
    def system_import_model(self, keep_in_place=False):
        from ..services import system
        folder = self._host.pick('folder', 'Choose the exported model folder')
        if not folder:
            return None
        cm = self._cm
        return self._task('Importing the model',
                          lambda r: system.import_model(folder, bool(keep_in_place), cm), kind='model-transfer')

    @_api
    def system_uninstall_plan(self):
        from ..services import system
        return system.uninstall_plan()

    @_api
    def system_uninstall(self, remove_models=False):
        from ..services import system
        self._cm.flush()
        result = system.uninstall(bool(remove_models))
        self._host.quit()
        return result

    @_api
    def system_check_update(self):
        from ..services import system
        return system.check_update()

    @_api
    def server_status(self):
        server = self._host.ctx.server
        return server.status() if server else {'running': False}

    # ── Tasks ────────────────────────────────────────────────────────────────

    @_api
    def tasks_list(self):
        return self._tasks.list()

    @_api
    def task_cancel(self, task_id):
        return self._tasks.cancel(int(task_id))

    @_api
    def task_result(self, task_id):
        task = self._tasks.get(int(task_id))
        if task is None:
            return None
        return {**task.to_dict(), 'result': task.result}
