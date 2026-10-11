# ui/floating_bar.py
# Stands in for the Tk LevelOverlay when the new UI is running: same methods,
# so StateManager calls it exactly as before, but each call becomes a 'bar'
# event for the floating-bar page. Position and visibility go to the UI host,
# which owns the actual window. The placement maths (where the bubble goes,
# whether its resting tab stands upright) lives here as plain functions.

from typing import Callable, Optional

from .events import publish

# ── Where the bubble goes ────────────────────────────────────────────────────
# Rectangles are (left, top, right, bottom) and sizes (width, height), all in
# physical pixels; the distances below are page pixels, scaled per monitor.

EDGE_MARGIN = 6        # from the top or bottom of the screen, close like a dock
FIELD_GAP = 6          # between a followed text field and the tab
SNAP_DISTANCE = 24     # a drop this close to a side sticks to that side
EDGE_POSITIONS = ('left-edge', 'right-edge')


def _clamp(x: int, y: int, area: tuple, size: tuple) -> tuple:
    left, top, right, bottom = area
    return max(left, min(x, right - size[0])), max(top, min(y, bottom - size[1]))


# Top-left corner for a bubble of `size`. A followed text field wins over
# everything; then a side of the screen, a spot the user dragged it to, or one
# of the top and bottom spots. Never off screen.
def bar_origin(area: tuple, size: tuple, scale: float, position: str,
               custom: Optional[tuple] = None, field: Optional[tuple] = None) -> tuple:
    left, top, right, bottom = area
    pw, ph = size
    if field:
        # An upright handle just past the field's right end, or inside it when
        # the field runs to the edge of the screen.
        gap = int(FIELD_GAP * scale)
        x = field[2] + gap
        if x + pw + gap > right:
            x = field[2] - pw - gap
        return _clamp(x, (field[1] + field[3] - ph) // 2, area, size)
    if position in EDGE_POSITIONS:
        x = right - pw if position == 'right-edge' else left
        return x, top + (bottom - top - ph) // 2
    if position == 'custom' and custom:
        cx, cy = custom
        x = left if cx <= left else right - pw if cx >= right - 1 else cx - pw // 2
        return _clamp(x, cy - ph // 2, area, size)
    if position == 'custom':
        position = 'bottom-center'
    margin = int(EDGE_MARGIN * scale)
    if position.endswith('left'):
        x = left + margin
    elif position.endswith('right'):
        x = right - pw - margin
    else:
        x = left + (right - left - pw) // 2
    y = top + margin if position.startswith('top') else bottom - ph - margin
    return _clamp(x, y, area, size)


# The resting tab stands upright when it sits on a side of the screen or
# beside a text field, and lies flat at the top or bottom.
def is_upright(position: str, custom: Optional[tuple], area: tuple, following: bool) -> bool:
    if following or position in EDGE_POSITIONS:
        return True
    if position == 'custom' and custom:
        return custom[0] <= area[0] or custom[0] >= area[2] - 1
    return False


# Top-left corner for the meeting panel of `size`: a side of the screen puts
# it in the lower half with the bubble's margin, a dragged spot is its centre,
# the corners and middles are the bubble's. Never off screen.
def panel_origin(area: tuple, size: tuple, scale: float, position: str, custom: Optional[tuple]) -> tuple:
    left, top, right, bottom = area
    pw, ph = size
    margin = int(EDGE_MARGIN * scale)
    if position == 'custom' and custom:
        return _clamp(custom[0] - pw // 2, custom[1] - ph // 2, area, size)
    if position in EDGE_POSITIONS:
        x = right - pw - margin if position == 'right-edge' else left + margin
        y = bottom - ph - margin
        return _clamp(x, max(y, top + (bottom - top) // 2), area, size)
    return bar_origin(area, size, scale, position if position != 'custom' else 'bottom-right')


# The spot to remember after a drag ends with the window at `rect`: its centre,
# with x pulled onto the side of the screen when it was dropped near one. The
# right side is stored as its last pixel, which is still on this monitor.
def dropped_spot(area: tuple, rect: tuple, scale: float) -> tuple:
    left, _, right, _ = area
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    snap = int(SNAP_DISTANCE * scale)
    if rect[0] - left <= snap:
        cx = left
    elif right - rect[2] <= snap:
        cx = right - 1
    return cx, cy


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
