# settings/appliers.py
# How each setting reaches the running app. Live settings poke the component
# directly; background settings build a replacement off-thread (download a
# model, load an engine, reopen the microphone) and swap it in once nothing is
# recording, so dictation never stops. A failed background change puts the old
# value back and says why. Nothing here asks the user to restart.

import logging
from typing import Any, Callable, Optional

from ..ui.tasks import get_task_manager
from ..utils import OptionalComponent, parse_hotkey

logger = logging.getLogger(__name__)

# Hotkey settings the listener can re-register by name.
_LISTENER_KEYS = {
    'hotkey.recording_hotkey': 'recording_hotkey', 'hotkey.stop_key': 'stop_key',
    'hotkey.auto_send_key': 'auto_send_key', 'hotkey.cancel_combination': 'cancel_combination',
    'hotkey.command_hotkey': 'command_hotkey', 'hotkey.rephrase_hotkey': 'rephrase_hotkey',
    'hotkey.pause_hotkey': 'pause_hotkey', 'hotkey.paste_last_hotkey': 'paste_last_hotkey',
    'hotkey.recording_mode': 'recording_mode',
}

# Settings that are simply read from the config whenever they are used.
_READ_ON_USE = {
    'whisper.prompt_from_selection', 'streaming.deliver_to_cursor', 'audio.pause_media_on_record',
    'audio.continuous_mode', 'audit.enabled', 'ui.mode', 'ui.theme', 'ui.open_on_start',
    'console.start_hidden', 'overlay.click_through', 'onboarding.gpu', 'onboarding.gpu_class',
    'ui.welcome_done', 'ui.window.width', 'ui.window.height', 'ui.window.x', 'ui.window.y',
    'ui.window.maximized', 'streaming.models',
}


# Everything the appliers may touch, filled in by the UI host after startup.
class AppContext:
    def __init__(self, state_manager, config_manager, *, system_tray=None, model_registry=None,
                 streaming_manager=None, server=None, bar=None,
                 reconfigure_logging: Optional[Callable[[], None]] = None):
        self.state_manager = state_manager
        self.config_manager = config_manager
        self.system_tray = system_tray
        self.model_registry = model_registry
        self.streaming_manager = streaming_manager
        self.server = server
        self.bar = bar
        self.reconfigure_logging = reconfigure_logging
        self.settings = None   # SettingsService, set once it exists


class Appliers:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.tasks = get_task_manager()

    # Apply one changed setting. Returns {'applied': kind, 'task_id': id|None}.
    def apply(self, path: str, value: Any, old: Any) -> dict:
        if path in _READ_ON_USE or path.startswith('postprocess.'):
            return self._live()
        if path in _LISTENER_KEYS or path.startswith('hotkey.'):
            return self._hotkeys(path, value)
        handler = _HANDLERS.get(path)
        if handler is None:
            for prefix, fn in _PREFIX_HANDLERS:
                if path.startswith(prefix):
                    handler = fn
                    break
        if handler is None:
            logger.debug(f"No applier for {path}; it is read from the config when used")
            return self._live()
        return handler(self, path, value, old) or self._live()

    # ── helpers ──────────────────────────────────────────────────────────────

    @property
    def sm(self):
        return self.ctx.state_manager

    @property
    def cm(self):
        return self.ctx.config_manager

    @staticmethod
    def _live() -> dict:
        return {'applied': 'live', 'task_id': None}

    # Run a background job; on failure put the old value back.
    def _background(self, label: str, kind: str, path: str, old: Any, fn, ref: str = None) -> dict:
        def done(task):
            # Failed or cancelled: the old engine/microphone is still the one in
            # use, so the setting goes back to match it.
            if task.status in ('failed', 'cancelled') and self.ctx.settings is not None and old is not None:
                self.ctx.settings.revert_silently(path, old)
                if task.status == 'failed':
                    self.sm.system_tray.notify(f"{label} failed: {task.error}")
        task = self.tasks.run(label, fn, kind=kind, on_done=done, ref=ref)
        return {'applied': 'background', 'task_id': task.id}

    # ── Whisper engine ───────────────────────────────────────────────────────

    def engine_attr(self, path, value, old):
        engine = self.sm.whisper_engine
        if path == 'whisper.language':
            engine.language = None if value in (None, '', 'auto') else value
        elif path == 'whisper.task':
            engine.task = value if value in ('transcribe', 'translate') else 'transcribe'
        elif path == 'whisper.beam_size':
            engine.beam_size = int(value)
        elif path == 'whisper.initial_prompt':
            engine.initial_prompt = value or None
        elif path == 'whisper.hotwords':
            words = [str(w) for w in (value or [])]
            engine.hotwords = (", ".join(words) or None) if not isinstance(engine.hotwords, list) else words

    def models_map(self, path, value, old):
        from ..model_registry import ModelRegistry
        whisper_cfg = self.cm.get_whisper_config()
        streaming_cfg = self.cm.get_streaming_config()
        registry = ModelRegistry(whisper_models_config=whisper_cfg.get('models', {}),
                                 streaming_models_config=streaming_cfg.get('models', {}))
        self.ctx.model_registry = registry
        try:
            self.sm.whisper_engine.registry = registry
        except Exception:
            pass
        if self.ctx.system_tray is not None:
            self.ctx.system_tray.model_registry = registry
            self.ctx.system_tray.refresh_menu()

    # A new engine for a new model, device, precision or backend. Built and
    # warmed up off-thread, then swapped in when idle.
    def engine_rebuild(self, path, value, old):
        labels = {'whisper.model': f"Switching to the {value} model",
                  'whisper.device': 'Switching to GPU' if value == 'cuda' else 'Switching to CPU',
                  'whisper.compute_type': f"Switching precision to {value}",
                  'whisper.device_index': 'Switching graphics card',
                  'whisper.backend': f"Switching engine to {value}"}
        label = labels.get(path, 'Reloading the model')
        sm, cm = self.sm, self.cm

        def job(reporter):
            from ..main import setup_whisper_engine
            from ..services import models as model_service
            whisper_cfg = cm.get_whisper_config()
            registry = self.ctx.model_registry
            if whisper_cfg.get('backend', 'faster_whisper') == 'faster_whisper' and registry is not None:
                model_service.ensure_downloaded(registry, whisper_cfg['model'], reporter)
            reporter.report(None, 'Loading the model…')
            log_t = cm.get_logging_config().get('log_transcriptions', False)
            engine = setup_whisper_engine(whisper_cfg, sm.vad_manager, registry, log_t, None)
            reporter.report(0.98, 'Ready — switching over')
            swapped_now = sm.run_when_idle(lambda: sm.replace_whisper_engine(engine))
            if not swapped_now:
                reporter.report(0.99, 'Switches as soon as the current dictation finishes')
            if self.ctx.system_tray is not None:
                self.ctx.system_tray.refresh_menu()
            return {'model': whisper_cfg['model'], 'device': whisper_cfg['device']}

        running = self.tasks.running('engine')
        if running:
            for t in running:
                self.tasks.cancel(t.id)
        # A model switch is "about" the new model, so its row shows the download.
        return self._background(label, 'engine', path, old, job, ref=value if path == 'whisper.model' else None)

    # ── Audio ────────────────────────────────────────────────────────────────

    def recorder_live(self, path, value, old):
        audio = self.cm.get_audio_config()
        self.sm.audio_recorder.apply_live_config(
            max_duration=audio.get('max_duration'),
            noise_suppression=audio.get('noise_suppression') or {},
            whisper_mode=audio.get('whisper_mode') or {},
        )

    def recorder_rebuild(self, path, value, old):
        sm = self.sm
        audio = self.cm.get_audio_config()
        overrides = {}
        if path == 'audio.input_device':
            overrides['device'] = value if isinstance(value, int) else None
            label = 'Switching microphone'
        elif path == 'audio.channels':
            overrides['channels'] = int(value)
            label = 'Reopening the microphone'
        elif path == 'audio.dtype':
            overrides['dtype'] = value
            label = 'Reopening the microphone'
        else:
            label = 'Reopening the microphone'

        def job(reporter):
            reporter.report(None, 'Waiting for the current dictation to finish' if not sm.is_idle() else 'Opening…')
            box = {}

            def swap():
                try:
                    box['rec'] = sm.rebuild_audio_recorder(**overrides)
                except Exception as e:
                    box['err'] = e
            sm.run_when_idle(swap)
            import time
            deadline = time.time() + 120
            while 'rec' not in box and 'err' not in box and time.time() < deadline:
                time.sleep(0.1)
            if 'err' in box:
                raise box['err']
            from ..ui.events import publish
            publish('devices_changed', {})
            return {'device': audio.get('input_device')}
        return self._background(label, 'audio', path, old, job)

    def audio_host(self, path, value, old):
        sm = self.sm

        def job(reporter):
            from ..state_manager import StateManager
            if value:
                # The class method, not sm.set_audio_host: the UI host reroutes
                # that one through the settings service, which would loop back here.
                if not StateManager.set_audio_host(sm, value):
                    raise RuntimeError(f"{value} isn't available on this computer")
            else:
                sm._initialize_audio_host()
                sm._ensure_audio_device_for_host(sm._current_audio_host)
            from ..ui.events import publish
            publish('devices_changed', {})
        return self._background('Switching audio driver', 'audio', path, old, job)

    def vad_live(self, path, value, old):
        vad = self.sm.vad_manager
        attr = path.split('.', 1)[1]
        setattr(vad, attr, value)
        if path == 'vad.vad_precheck_enabled' and value and vad.ten_vad is None:
            vad.ten_vad = vad._check_and_init_ten_vad()
        try:
            self.sm.audio_recorder.refresh_vad()
        except Exception as e:
            logger.debug(f"VAD refresh failed: {e}")

    # Turning realtime detection on or off changes the stream's block size, so
    # the microphone is reopened.
    def vad_realtime(self, path, value, old):
        vad = self.sm.vad_manager
        vad.vad_realtime_enabled = bool(value)
        if value and vad.ten_vad is None:
            vad.ten_vad = vad._check_and_init_ten_vad()
        return self.recorder_rebuild('vad.vad_realtime_enabled', value, old)

    def streaming(self, path, value, old):
        sm, cm, ctx = self.sm, self.cm, self.ctx

        def job(reporter):
            from ..streaming_manager import StreamingManager
            cfg = cm.get_streaming_config()
            reporter.report(None, 'Preparing the live preview model…' if cfg.get('streaming_enabled') else 'Turning off live preview…')
            manager = StreamingManager(streaming_enabled=cfg.get('streaming_enabled', False),
                                       streaming_model=cfg.get('streaming_model', ''),
                                       model_registry=ctx.model_registry)
            manager.initialize()
            if cfg.get('streaming_enabled') and not manager.is_available():
                raise RuntimeError("The live preview model could not be loaded")
            ctx.streaming_manager = manager
            box = {}

            def swap():
                try:
                    box['rec'] = sm.rebuild_audio_recorder(streaming_manager=manager)
                except Exception as e:
                    box['err'] = e
            sm.run_when_idle(swap)
            import time
            deadline = time.time() + 120
            while not box and time.time() < deadline:
                time.sleep(0.1)
            if 'err' in box:
                raise box['err']
        label = 'Turning on live preview' if value else 'Turning off live preview'
        if path == 'streaming.streaming_model':
            label = 'Switching the preview model'
        return self._background(label, 'streaming', path, old, job)

    # ── Hotkeys ──────────────────────────────────────────────────────────────

    def _hotkeys(self, path, value):
        listener = getattr(self.sm, '_hotkey_listener_ref', None)
        if listener is None:
            return self._live()
        if path in ('hotkey.double_tap_to_lock', 'hotkey.double_tap_window_ms', 'hotkey.recording_mode'):
            if path == 'hotkey.recording_mode':
                listener.recording_mode = value
            self._rebuild_tap_latch(listener)
            listener.stop_listening()
            listener._setup_hotkeys()
            listener.start_listening()
            return self._live()
        name = _LISTENER_KEYS.get(path)
        if name:
            if name == 'command_hotkey' and not self.cm.get_path('voice_commands.enabled', True):
                value = None
            listener.change_hotkey_config(name, value or None)
        return self._live()

    def _rebuild_tap_latch(self, listener):
        from ..hotkey_gestures import TapLatch
        hk = self.cm.get_hotkey_config()
        listener.tap_latch = None
        if hk.get('double_tap_to_lock') and listener.recording_mode == 'push_to_talk':
            try:
                window = int(hk.get('double_tap_window_ms', 400))
            except (TypeError, ValueError):
                window = 400
            listener.tap_latch = TapLatch(
                start=listener._standard_hotkey_pressed,
                stop=listener._push_to_talk_released,
                window_ms=window,
                is_recording=lambda: self.sm.audio_recorder.get_recording_status(),
            )

    # ── Delivery ─────────────────────────────────────────────────────────────

    def clipboard(self, path, value, old):
        cb = self.sm.clipboard_manager
        key = path.split('.', 1)[1]
        if key == 'auto_paste':
            cb.update_auto_paste(bool(value))
        elif key == 'delivery_method':
            from ..platform import keyboard
            cb.delivery_method = keyboard.validate_delivery_method(value)
        elif key == 'paste_hotkey':
            cb.paste_hotkey = value
            cb.paste_keys = parse_hotkey(value)
        elif key == 'macos_key_simulation_delay':
            from ..platform import keyboard
            keyboard.set_delay(value)
        else:
            setattr(cb, key, value)

    # ── Sounds, logging, tray, title, commands, bar, server ──────────────────

    def audio_feedback(self, path, value, old):
        from ..audio_feedback import AudioFeedback
        cfg = self.cm.get_audio_feedback_config()
        feedback = AudioFeedback(
            enabled=cfg['enabled'],
            transcription_complete_enabled=cfg['transcription_complete_enabled'],
            ready_enabled=cfg['ready_enabled'],
            start_sound=cfg['start_sound'], stop_sound=cfg['stop_sound'],
            cancel_sound=cfg['cancel_sound'],
            transcription_complete_sound=cfg['transcription_complete_sound'],
            ready_sound=cfg['ready_sound'],
        )
        self.sm.audio_feedback = OptionalComponent(feedback)

    def logging_cfg(self, path, value, old):
        if path == 'logging.log_transcriptions':
            for target in (self.sm.whisper_engine, self.sm.voice_command_manager):
                if target is not None:
                    try:
                        target.log_transcriptions = bool(value)
                    except Exception:
                        pass
            return
        if self.ctx.reconfigure_logging:
            self.ctx.reconfigure_logging()

    def terminal_title(self, path, value, old):
        from ..terminal_title import TerminalTitle
        try:
            self.sm.terminal_title.stop()
        except Exception:
            pass
        title = TerminalTitle(self.cm.get_terminal_title_config())
        title.start()
        self.sm.terminal_title = OptionalComponent(title)

    def tray(self, path, value, old):
        tray = self.ctx.system_tray
        if tray is None:
            return
        if path == 'system_tray.tooltip':
            tray.tray_config['tooltip'] = value
            if tray.icon is not None:
                tray._on_ui_thread(lambda: setattr(tray.icon, 'title', value or 'Whisper Local'), 'tooltip')
            return

        def job(reporter):
            if value:
                tray.start()
            else:
                tray.stop()
        return self._background('Showing the tray icon' if value else 'Hiding the tray icon', 'tray', path, old, job)

    def voice_commands(self, path, value, old):
        vcm = self.sm.voice_command_manager
        if vcm is None:
            return
        vcm.enabled = bool(value)
        if value and not hasattr(vcm, 'commands'):
            import os
            from ..utils import get_user_app_data_path
            vcm.commands_path = os.path.join(get_user_app_data_path(), "commands.yaml")
            vcm.commands = vcm._load_commands()
            vcm._commands_mtime = vcm._read_mtime()
        listener = getattr(self.sm, '_hotkey_listener_ref', None)
        if listener is not None:
            listener.change_hotkey_config('command_hotkey',
                                          self.cm.get_path('hotkey.command_hotkey') if value else None)

    def overlay(self, path, value, old):
        bar = self.ctx.bar or self.sm.level_overlay
        if bar is None:
            return
        if path == 'overlay.enabled' and hasattr(bar, 'set_enabled'):
            bar.set_enabled(bool(value))
        elif path == 'overlay.position':
            bar.set_position(value)

    def update_check(self, path, value, old):
        if value:
            from ..update_check import maybe_check_for_update
            maybe_check_for_update(lambda msg: self.sm.system_tray.notify(msg), {'enabled': True})

    def server(self, path, value, old):
        controller = self.ctx.server
        if controller is None:
            return
        cfg = self.cm.config.get('server') or {}

        def job(reporter):
            if cfg.get('enabled'):
                controller.start(cfg.get('host', '127.0.0.1'), cfg.get('port', 7777))
                reporter.report(1.0, controller.status().get('url', ''))
            else:
                controller.stop()
        label = 'Starting the local API' if cfg.get('enabled') else 'Stopping the local API'
        if not cfg.get('enabled') and path != 'server.enabled':
            return self._live()   # host/port edits while it's off just get saved
        return self._background(label, 'server', path, old, job)


_HANDLERS = {
    'whisper.language': Appliers.engine_attr,
    'whisper.task': Appliers.engine_attr,
    'whisper.beam_size': Appliers.engine_attr,
    'whisper.initial_prompt': Appliers.engine_attr,
    'whisper.hotwords': Appliers.engine_attr,
    'whisper.models': Appliers.models_map,
    'whisper.model': Appliers.engine_rebuild,
    'whisper.device': Appliers.engine_rebuild,
    'whisper.device_index': Appliers.engine_rebuild,
    'whisper.compute_type': Appliers.engine_rebuild,
    'whisper.backend': Appliers.engine_rebuild,
    'audio.input_device': Appliers.recorder_rebuild,
    'audio.channels': Appliers.recorder_rebuild,
    'audio.dtype': Appliers.recorder_rebuild,
    'audio.host': Appliers.audio_host,
    'audio.max_duration': Appliers.recorder_live,
    'vad.vad_realtime_enabled': Appliers.vad_realtime,
    'streaming.streaming_enabled': Appliers.streaming,
    'streaming.streaming_model': Appliers.streaming,
    'voice_commands.enabled': Appliers.voice_commands,
    'update_check.enabled': Appliers.update_check,
}

_PREFIX_HANDLERS = [
    ('audio.noise_suppression.', Appliers.recorder_live),
    ('audio.whisper_mode.', Appliers.recorder_live),
    ('vad.', Appliers.vad_live),
    ('clipboard.', Appliers.clipboard),
    ('audio_feedback.', Appliers.audio_feedback),
    ('logging.', Appliers.logging_cfg),
    ('terminal_title.', Appliers.terminal_title),
    ('system_tray.', Appliers.tray),
    ('overlay.', Appliers.overlay),
    ('server.', Appliers.server),
]
