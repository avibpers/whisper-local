# ui/floating_bar.py
# Stands in for the Tk LevelOverlay when the new UI is running: same methods,
# so StateManager calls it exactly as before, but each call becomes a 'bar'
# event for the floating-bar page. Position and visibility go to the UI host,
# which owns the actual window.

from typing import Callable, Optional

from .events import publish


class FloatingBar:
    def __init__(self, level_provider: Callable[[], float] = None, click_through: bool = True,
                 position: str = 'bottom-center'):
        self.level_provider = level_provider
        self.position = position
        self.host = None   # set by UiHost

    def start(self):
        pass   # the window is created by UiHost

    def show_recording(self):
        publish('bar', {'cmd': 'recording'})

    def show_processing(self):
        publish('bar', {'cmd': 'processing'})

    def hide(self):
        publish('bar', {'cmd': 'idle'})

    def flash_success(self):
        publish('bar', {'cmd': 'success'})

    def flash_failure(self, message: Optional[str] = None):
        publish('bar', {'cmd': 'failure', 'message': message or ''})

    # The streaming text already goes out as a 'streaming_text' event.
    def set_streaming_text(self, text: str):
        pass

    def set_position(self, name: str):
        self.position = name
        if self.host is not None:
            self.host.place_bar()

    def set_enabled(self, enabled: bool):
        if self.host is not None:
            self.host.set_bar_enabled(enabled)

    def shutdown(self):
        pass


# Replaces the Tk fallback popup: when no text field is focused, the dictation
# opens in the app window instead.
class FallbackToWindow:
    def __init__(self, host):
        self.host = host

    def show(self, transcript: str, reason: Optional[str] = None, allow_clipboard: bool = True):
        publish('notify', {'kind': 'fallback', 'message': reason or '', 'text': transcript,
                           'allow_clipboard': allow_clipboard})
        self.host.show_main('live')
