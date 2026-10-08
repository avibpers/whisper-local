# ui/host.py
# Runs the app window and the floating bar (pywebview, WebView2 on Windows) on
# the main thread, and connects them to the running app:
#   - pushes engine events to the pages in small batches (~20 per second)
#   - keeps the bar from ever taking focus or showing a taskbar button
#   - hides the main window on close (the "closed" state is the bar)
#   - swaps the main window's title bar for the page's own (Windows)
#   - watches which app is in front, for the Live screen and app rules
#   - routes tray actions and second launches to the window
# Dictation never depends on the windows: if they fail, hotkeys keep working.

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from ..settings import schema, simple_mode
from ..settings.appliers import Appliers
from ..settings.service import SettingsService
from . import win32
from .bridge import Bridge
from .events import get_bus, publish
from .floating_bar import FallbackToWindow, FloatingBar
from .tasks import get_task_manager

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / 'web'
APP_ICON = Path(__file__).resolve().parent.parent / 'platform' / 'windows' / 'assets' / 'koli-icon.ico'
APP_ID = 'Koli.App'
MAIN_TITLE = 'Koli'
BAR_TITLE = 'Koli · bubble'

# Topics each page cares about. Latest-wins topics are coalesced per batch.
BAR_TOPICS = {'state', 'bar', 'pipeline', 'notify', 'task', 'level', 'streaming_text', 'settings',
              'profile', 'window', 'auto_send'}
COALESCE = {'level', 'vad', 'foreground', 'streaming_text'}
PUSH_INTERVAL = 0.05


def ui_available() -> tuple:
    """(available, reason). The new UI needs pywebview and, on Windows, WebView2."""
    try:
        import webview  # noqa: F401
    except Exception as e:
        return False, f"pywebview is not installed ({e})"
    if win32.IS_WINDOWS and not win32.webview2_version():
        return False, "the Microsoft Edge WebView2 runtime is missing"
    if not (WEB_DIR / 'index.html').exists():
        return False, "the UI files are missing from the install"
    return True, ''


# Turns on WebView2's drag regions, so `app-region: drag` in the page acts as
# the title bar: native moving, snapping, double-click to maximize and the
# right-click window menu. pywebview has no option for it, so this wraps its
# WebView2 setup, which runs before the first page loads. Imported the way
# pywebview imports it (through its WinForms module). True when the wrapper is
# in place; _drag_regions_applied says whether WebView2 accepted the setting.
_drag_regions_applied = threading.Event()


def _enable_drag_regions() -> bool:
    if not win32.IS_WINDOWS:
        return False
    try:
        import webview.platforms.winforms  # noqa: F401  (loads edgechromium)
        from webview.platforms import edgechromium
    except Exception as e:
        logger.info(f"Custom title bar unavailable: {e}")
        return False
    original = edgechromium.EdgeChrome.on_webview_ready
    if getattr(original, 'koli_drag_regions', False):
        return True

    def on_webview_ready(chrome, sender, args):
        try:
            if args.IsSuccess:
                sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
                _drag_regions_applied.set()
        except Exception as e:
            logger.warning(f"WebView2 drag regions not enabled: {e}")
        return original(chrome, sender, args)

    on_webview_ready.koli_drag_regions = True
    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready
    return True


class _PageChannel:
    def __init__(self, name, topics=None):
        self.name = name
        self.topics = topics
        self.window = None
        self.ready = False
        self.queue = queue.SimpleQueue()


class UiHost:
    def __init__(self, ctx, open_page=None, dev=False, instance_name='Koli', notices=None):
        self.ctx = ctx
        # Startup problems to show once the window opens (see main.STARTUP_NOTICES).
        self.notices = list(notices or [])
        self.dev = dev
        self.instance_name = instance_name
        self.settings = SettingsService(ctx.config_manager, Appliers(ctx))
        ctx.settings = self.settings
        self.bridge = Bridge(self)
        self.main = _PageChannel('main')
        self.bar = _PageChannel('bar', BAR_TOPICS)
        self.main_visible = False
        self.bar_enabled = True
        self.bar_hwnd = 0
        self.main_hwnd = 0
        self.mic_testing = False
        self.last_external = {}
        self._last_external_hwnd = 0
        self._quitting = False
        self._shutdown_event = None
        self._open_page = open_page
        self._open_at_start = open_page is not None
        self._bar_size = (14, 48)   # the resting tab, until the page reports its size
        self._last_pipeline = []
        self._state = {'state': 'idle', 'mode': 'dictate', 'paused': False}
        self._listener = None
        self._unsubscribe = None
        self._maximized = False
        self._drag_regions = False   # WebView2 treats `app-region: drag` as the title bar
        self.custom_frame = False    # the page draws the title bar and window buttons

    @property
    def main_window(self):
        return self.main.window

    # ── Wiring into the app ──────────────────────────────────────────────────

    # Called before components are attached, so StateManager builds the bar
    # adapter instead of the Tk overlay.
    @staticmethod
    def overlay_factory(**kwargs):
        return FloatingBar(**kwargs)

    def attach(self):
        sm = self.ctx.state_manager
        bar = sm.level_overlay if isinstance(sm.level_overlay, FloatingBar) else None
        if bar is not None:
            bar.host = self
            self.ctx.bar = bar
        sm.fallback_window = FallbackToWindow(self)
        self.bar_enabled = bool(self.settings.get('overlay.enabled', True))

        # Tray and hotkey actions that change settings go through the settings
        # service, so every window hears about them and Undo works.
        svc = self.settings
        sm.set_language = lambda code: svc.set('whisper.language', code)['applied'] != 'unchanged'
        sm.update_transcription_mode = lambda value: svc.set('clipboard.auto_paste', bool(value))
        sm.request_model_change = lambda key: bool(svc.set('whisper.model', key))
        sm.set_overlay_position = lambda name: bool(svc.set('overlay.position', name))
        sm.set_audio_host = lambda name: bool(svc.set('audio.host', name))
        sm.request_audio_device_change = lambda device_id, name=None: bool(
            svc.set('audio.input_device', device_id if device_id != -1 else 'default'))

        def activate(name):
            from ..services import rules
            rules.activate_profile(svc, sm.profile_manager, name)
            sm.system_tray.refresh_menu()
            publish('profile', {'active': name})
            return True
        sm.activate_profile = activate

        tray = self.ctx.system_tray
        if tray is not None:
            tray.on_open = lambda page=None: self.show_main(page)
            tray.on_quit = self.quit
            tray.on_pause = lambda paused: self.bridge.dictation_pause(paused)
            tray.refresh_menu()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def run(self, shutdown_event):
        import webview
        self._shutdown_event = shutdown_event
        self._previous_foreground = win32.foreground()
        cfg = self.settings
        self._choose_initial_mode()
        win = cfg.get('ui.window') or {}
        theme_dark = self._is_dark()
        first_run = not cfg.get('ui.welcome_done', False)
        if first_run or cfg.get('ui.open_on_start', False):
            self._open_at_start = True
            if first_run and not self._open_page:
                self._open_page = 'welcome'

        self._unsubscribe = get_bus().subscribe('*', self._on_event)
        win32.on_console_close(self.quit)
        win32.set_app_id(APP_ID)
        self._drag_regions = _enable_drag_regions()

        self.main.window = webview.create_window(
            MAIN_TITLE, url=str(WEB_DIR / 'index.html'), js_api=self.bridge,
            width=int(win.get('width') or 1280), height=int(win.get('height') or 860),
            x=win.get('x'), y=win.get('y'), min_size=(940, 640), hidden=True,
            text_select=True, background_color='#171C20' if theme_dark else '#ECEFF1')
        self.main.window.events.loaded += lambda: self._on_loaded(self.main)
        self.main.window.events.closing += self._on_main_closing
        # A window left maximized opens maximized next time (on its first show).
        self._restore_maximized = bool(win.get('maximized'))
        self.main.window.events.maximized += lambda: self._remember_maximized(True)
        self.main.window.events.restored += lambda: self._remember_maximized(False)
        self.main.window.events.resized += lambda w, h: self._remember_geometry()
        self.main.window.events.moved += lambda x, y: self._remember_geometry()

        bx, by = self._bar_origin(*self._bar_size)
        self.bar.window = webview.create_window(
            BAR_TITLE, url=str(WEB_DIR / 'bar.html'), js_api=self.bridge,
            width=self._bar_size[0], height=self._bar_size[1], x=bx, y=by,
            frameless=True, easy_drag=False, resizable=False, on_top=True, focus=False,
            min_size=(10, 20), shadow=False, background_color='#1E252B' if theme_dark else '#FFFFFF',
            hidden=not self.bar_enabled)
        self.bar.window.events.loaded += lambda: self._on_loaded(self.bar)

        # The Koli icon on the window and its taskbar button (pywebview's
        # Windows backend reads `icon` too, not just GTK/Qt as documented).
        icon = str(APP_ICON) if win32.IS_WINDOWS and APP_ICON.exists() else None
        webview.start(self._on_started, http_server=True, debug=self.dev, private_mode=True, icon=icon)
        self._teardown()

    # New installs start in Simple mode. Someone upgrading who already tuned
    # settings by hand starts in Advanced, where those settings are visible.
    def _choose_initial_mode(self):
        cm = self.ctx.config_manager
        overrides = cm.user_overrides()
        if 'ui' in overrides:
            return
        if simple_mode.is_existing_user(overrides):
            cm.set_path('ui.mode', 'advanced')
            cm.set_path('ui.welcome_done', True)
            logger.info("Existing settings found; starting the app window in Advanced mode")

    def _on_started(self):
        threading.Thread(target=self._pump, daemon=True, name='ui-pump').start()
        threading.Thread(target=self._watch_shutdown, daemon=True, name='ui-shutdown').start()
        threading.Thread(target=self._watch_foreground, daemon=True, name='ui-foreground').start()
        self.settings.start_watching()
        try:
            from .instance_ipc import InstanceListener
            self._listener = InstanceListener(self.instance_name, lambda page: self.show_main(page),
                                              on_quit=self.quit)
            self._listener.start()
        except Exception as e:
            logger.debug(f"Instance listener not started: {e}")
        if self._open_at_start:
            self.show_main(self._open_page)

    def _on_loaded(self, channel):
        channel.ready = True
        if channel is self.bar:
            self.bar_hwnd = win32.find_window(BAR_TITLE)
            win32.make_tool_window(self.bar_hwnd)
            win32.round_corners(self.bar_hwnd)
            self.place_bar()
            if not self.bar_enabled:
                win32.hide(self.bar_hwnd)
            # WebView2 takes focus when a page loads; give it back to whatever
            # the user was typing into (Phase 0, spike B).
            if not self.main_visible and self._previous_foreground:
                win32.restore_foreground(self._previous_foreground)
        else:
            self.main_hwnd = win32.find_window(MAIN_TITLE)
            win32.set_dark_title_bar(self.main_hwnd, self._is_dark())
            # Without drag regions the page couldn't move the window, so the
            # normal frame stays.
            if self._drag_regions and _drag_regions_applied.is_set() and not self.custom_frame:
                self.custom_frame = win32.remove_title_bar(self.main_hwnd)
                logger.info(f"Custom title bar: {'on' if self.custom_frame else 'off'}")
            publish('window', {'action': 'frame', **self.frame_state()})
        logger.info(f"UI page ready: {channel.name}")

    def _watch_shutdown(self):
        while not self._shutdown_event.wait(0.2):
            pass
        self.quit()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        logger.info("Closing the app window")
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        for channel in (self.bar, self.main):
            try:
                if channel.window is not None:
                    channel.window.destroy()
            except Exception:
                pass

    def _teardown(self):
        self.settings.stop_watching()
        try:
            self.ctx.config_manager.flush()
        except Exception:
            pass
        if self._listener:
            self._listener.stop()
        if self._unsubscribe:
            self._unsubscribe()
        if self.ctx.server is not None:
            self.ctx.server.stop()

    # ── Main window ──────────────────────────────────────────────────────────

    def show_main(self, page=None):
        if self.main.window is None:
            return
        self.main_visible = True
        if page:
            publish('window', {'action': 'navigate', 'page': page})
        try:
            self.main.window.show()
            if getattr(self, '_restore_maximized', False) or getattr(self, '_maximized', False):
                self._restore_maximized = False
                self.main.window.maximize()
            else:
                self.main.window.restore()
        except Exception as e:
            logger.debug(f"Show window failed: {e}")
        if self.main_hwnd:
            win32.restore_foreground(self.main_hwnd)
        self._push_now(self.main, [{'topic': 'window', 'payload': {'action': 'shown', 'page': page}}])

    def hide_main(self):
        self.main_visible = False
        try:
            self.main.window.hide()
        except Exception:
            pass
        if self.mic_testing:
            self.ctx.state_manager.audio_recorder.level_preview = False
            self.mic_testing = False

    # Follows the maximized/restored events, so it is right after a snap or
    # Win+Up too, not only after our own button.
    def toggle_maximize(self):
        if self._maximized:
            self.main.window.restore()
        else:
            self.main.window.maximize()

    # What the page needs to draw its title bar: whether to draw the window
    # buttons at all, and which icon the middle one shows.
    def frame_state(self):
        return {'custom': bool(self.custom_frame), 'maximized': bool(self._maximized)}

    def _on_main_closing(self):
        if self._quitting:
            return True
        # The close button hides the window; the bar (closed state) remains.
        threading.Thread(target=self.hide_main, daemon=True).start()
        return False

    def _remember_maximized(self, maximized):
        self._maximized = maximized
        publish('window', {'action': 'frame', **self.frame_state()})
        try:
            self.ctx.config_manager.set_path('ui.window.maximized', bool(maximized))
        except Exception:
            pass

    def _remember_geometry(self):
        w = self.main.window
        # The size to come back to is the normal one, not the maximized one.
        if w is None or not self.main_visible or getattr(self, '_maximized', False):
            return
        cm = self.ctx.config_manager
        try:
            cm.set_path('ui.window.width', int(w.width), save=False)
            cm.set_path('ui.window.height', int(w.height), save=False)
            cm.set_path('ui.window.x', int(w.x), save=False)
            cm.set_path('ui.window.y', int(w.y))
        except Exception:
            pass

    def pick(self, kind, title, types=None, filename=''):
        import webview
        dialog = {'folder': webview.FileDialog.FOLDER, 'open': webview.FileDialog.OPEN,
                  'save': webview.FileDialog.SAVE}[kind]
        kwargs = {}
        if types:
            kwargs['file_types'] = tuple(types)
        if filename:
            kwargs['save_filename'] = filename
        result = self.main.window.create_file_dialog(dialog, **kwargs)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    # ── Floating bar ─────────────────────────────────────────────────────────

    def _bar_origin(self, w, h):
        left, top, right, bottom = win32.work_area()
        scale = win32.dpi_scale(self.bar_hwnd)
        pw, ph = int(w * scale), int(h * scale)
        margin = int(28 * scale)
        pos = self.settings.get('overlay.position', 'right-edge') or 'right-edge'
        # Koli's bubble: flush with a side of the screen, halfway down.
        if pos in ('right-edge', 'left-edge'):
            x = right - pw if pos == 'right-edge' else left
            return x, top + (bottom - top - ph) // 2
        if pos.endswith('left'):
            x = left + margin
        elif pos.endswith('right'):
            x = right - pw - margin
        else:
            x = left + (right - left - pw) // 2
        y = top + margin if pos.startswith('top') else bottom - ph - margin
        # Never off screen, even when the bar is bigger than the space left.
        x = max(left, min(x, right - pw))
        y = max(top, min(y, bottom - ph))
        return x, y

    # The tallest the bar may be (in page pixels) and still fit on screen with
    # its margins; the quick-actions menu scrolls past that.
    def _max_bar_height(self):
        left, top, right, bottom = win32.work_area()
        scale = win32.dpi_scale(self.bar_hwnd) or 1.0
        return max(20, int((bottom - top - 2 * int(28 * scale)) / scale))

    def resize_bar(self, width, height):
        width = max(10, min(width, 640))
        height = max(20, min(height, self._max_bar_height()))
        self._bar_size = (width, height)
        self.place_bar()

    def place_bar(self):
        if not self.bar_hwnd:
            return
        scale = win32.dpi_scale(self.bar_hwnd)
        w, h = self._bar_size
        x, y = self._bar_origin(w, h)
        win32.place(self.bar_hwnd, x, y, int(w * scale), int(h * scale))

    def set_bar_enabled(self, enabled):
        self.bar_enabled = bool(enabled)
        if not self.bar_hwnd:
            return
        if enabled:
            win32.show_no_activate(self.bar_hwnd)
            self.place_bar()
        else:
            win32.hide(self.bar_hwnd)

    # ── Actions that need the user's app in front ────────────────────────────

    def restore_external_foreground(self):
        if self._last_external_hwnd:
            win32.restore_foreground(self._last_external_hwnd)

    def paste_last(self):
        sm = self.ctx.state_manager
        if not sm.last_transcription:
            raise ValueError("Nothing dictated yet")
        if self.main_visible and self._last_external_hwnd:
            win32.restore_foreground(self._last_external_hwnd)
            time.sleep(0.2)
        return sm.paste_last_transcription()

    # ── Data for the pages ───────────────────────────────────────────────────

    def _is_dark(self):
        theme = self.settings.get('ui.theme', 'light')
        if theme == 'system':
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                    return winreg.QueryValueEx(k, 'AppsUseLightTheme')[0] == 0
            except Exception:
                return False
        return theme == 'dark'

    def current_state(self):
        sm = self.ctx.state_manager
        state = dict(self._state)
        try:
            state['state'] = sm.get_current_state()
        except Exception:
            pass
        state['paused'] = bool(sm.is_paused)
        return state

    def audio_info(self):
        sm = self.ctx.state_manager
        host = sm.get_current_audio_host()
        devices = sm.get_available_audio_devices(host)
        return {'host': host, 'hosts': [h['name'] for h in sm.get_available_audio_hosts()],
                'devices': devices, 'current': sm.audio_recorder.describe()}

    def models_list(self):
        from ..services import models as svc
        cm = self.ctx.config_manager
        return {'models': svc.list_models(self.ctx.model_registry, cm.get_path('whisper.model'),
                                          cm.get_path('whisper.models') or {}),
                'groups': [{'id': g, 'title': svc.GROUP_TITLES[g]} for g in svc.GROUP_ORDER],
                'streaming': list((cm.get_path('streaming.models') or {}).keys())}

    def simple_context(self):
        cm = self.ctx.config_manager
        return {'language': cm.get_path('whisper.language') or 'auto',
                'gpu': cm.get_path('whisper.device') == 'cuda' or cm.get_path('onboarding.gpu') == 'complete',
                'available_models': list((cm.get_path('whisper.models') or {}).keys())}

    def simple_answers(self):
        from ..services import system
        answers = simple_mode.from_settings(self.settings.get, self.simple_context())
        answers['startup'] = system.autostart_state().get('enabled', False)
        return answers

    def bootstrap(self, which):
        from ..utils import get_version
        from .log_stream import get_handler
        cm = self.ctx.config_manager
        data = {
            'version': get_version(),
            'platform': os.name,
            'settings': self.settings.snapshot(),
            'state': self.current_state(),
            'tasks': get_task_manager().list(),
            'pipeline': list(self._last_pipeline),
            'recent': self.ctx.state_manager.get_recent_transcriptions(),
            'foreground': self.last_external,
            'profile': self.ctx.state_manager.profile_manager.get_active(),
            'dev': self.dev,
            'notices': self.notices,
        }
        if which == 'main':
            data['schema'] = schema.export(lambda p: self.settings.has(p))
            data['logs'] = get_handler().tail(300)
            data['webview2'] = win32.webview2_version()
            data['undo'] = self.settings.can_undo()
            data['frame'] = self.frame_state()
        return data

    # ── Event pump ───────────────────────────────────────────────────────────

    def _on_event(self, topic, payload):
        if topic == 'state' and isinstance(payload, dict):
            self._state.update(payload)
        elif topic == 'pipeline' and isinstance(payload, dict):
            if payload.get('stage') == 'captured':
                self._last_pipeline = []
            self._last_pipeline.append(payload)
        elif topic == 'settings' and isinstance(payload, dict):
            self._on_setting(payload)
        item = {'topic': topic, 'payload': payload}
        self.main.queue.put(item)
        if topic in self.bar.topics:
            self.bar.queue.put(item)

    def _on_setting(self, payload):
        path = payload.get('path', '')
        if path == 'ui.theme' and self.main_hwnd:
            win32.set_dark_title_bar(self.main_hwnd, self._is_dark())
        if path in ('overlay.position',):
            self.place_bar()
        tray = self.ctx.system_tray
        if tray is not None and path.split('.')[0] in ('whisper', 'clipboard', 'audio', 'overlay'):
            try:
                tray.refresh_menu()
            except Exception:
                pass

    def _pump(self):
        while not self._quitting:
            time.sleep(PUSH_INTERVAL)
            self._sample_levels()
            for channel in (self.bar, self.main):
                if not channel.ready:
                    continue
                if channel is self.main and not self.main_visible:
                    # Keep only what a page needs on reopen; it re-bootstraps.
                    self._drain(channel)
                    continue
                batch = self._drain(channel)
                if batch:
                    self._push_now(channel, batch)

    def _sample_levels(self):
        sm = self.ctx.state_manager
        try:
            recorder = sm.audio_recorder
            recording = recorder.get_recording_status()
        except Exception:
            return
        if recording or self.mic_testing:
            publish('level', {'level': round(recorder.get_current_level(), 4)})
            vad = getattr(recorder, 'continuous_vad', None)
            if recording and vad is not None:
                publish('vad', vad.snapshot())

    def _drain(self, channel):
        items, latest = [], {}
        try:
            while True:
                item = channel.queue.get_nowait()
                if item['topic'] in COALESCE:
                    latest[item['topic']] = item
                else:
                    items.append(item)
        except queue.Empty:
            pass
        logs = [i for i in items if i['topic'] == 'log']
        if len(logs) > 300:
            drop = set(id(i) for i in logs[:-300])
            items = [i for i in items if id(i) not in drop]
        return items + list(latest.values())

    def _push_now(self, channel, batch):
        try:
            script = 'window.wl && window.wl.receive(' + json.dumps(batch, default=str) + ')'
            runner = getattr(channel.window, 'run_js', None) or channel.window.evaluate_js
            runner(script)
        except Exception as e:
            logger.debug(f"Push to {channel.name} failed: {e}")

    # ── Foreground app ───────────────────────────────────────────────────────

    def _watch_foreground(self):
        from ..platform import foreground
        sm = self.ctx.state_manager
        last_key = None
        while not self._quitting:
            time.sleep(0.5)
            hwnd = win32.foreground()
            if not hwnd or win32.is_own_window(hwnd):
                continue
            self._last_external_hwnd = hwnd
            try:
                info = foreground.get_foreground_app() or {}
            except Exception:
                continue
            key = (info.get('exe'), info.get('title'))
            if key == last_key:
                continue
            last_key = key
            rule = None
            try:
                matched = sm.app_rules.match_for(info.get('exe') or '', info.get('title') or '')
                if matched:
                    rule = matched.get('id') or ', '.join(map(str, matched.get('match') or []))
            except Exception:
                pass
            self.last_external = {'exe': info.get('exe', ''), 'title': info.get('title', ''), 'rule': rule}
            publish('foreground', self.last_external)
