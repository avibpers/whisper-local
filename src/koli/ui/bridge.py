# ui/bridge.py
# The one object the web pages call (window.pywebview.api.<name>). Every public
# method returns {"ok": true, "data": ...} or {"ok": false, "error": "..."} and
# never raises into the page. Long work starts a background task and returns
# its id; progress arrives as 'task' events. Internals are kept in
# underscore attributes so pywebview doesn't expose them to JavaScript.

import functools
import logging
import os
import time
import webbrowser

from .events import publish
from .tasks import get_task_manager

logger = logging.getLogger(__name__)

# Links the app may open in the default browser.
_URL_ALLOW = ('https://github.com/drajb/whisper-local', 'https://ollama.com', 'https://huggingface.co',
              'https://developer.microsoft.com/microsoft-edge/webview2', 'https://go.microsoft.com')


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
    def bar_resize(self, width, height, resting=None):
        self._host.resize_bar(int(width), int(height), resting)
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

    @_api
    def meeting_get(self, meeting_id):
        return self._meetings.get(meeting_id)

    @_api
    def meeting_delete(self, meeting_id):
        return self._meetings.delete(meeting_id)

    # Writes the export where the user picks; returns the path or None.
    @_api
    def meeting_export(self, meeting_id, fmt='txt'):
        text = self._meetings.export(meeting_id, fmt)
        path = self._host.pick('save', 'Export meeting', filename=f'{meeting_id}.{fmt}')
        if not path:
            return None
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

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

    # ── Local AI ─────────────────────────────────────────────────────────────

    @_api
    def ai_probe(self, endpoint=None):
        from ..services import system
        return system.ollama_probe(endpoint or self._settings.get('postprocess.ollama.endpoint'))

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
