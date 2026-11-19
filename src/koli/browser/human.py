# browser/human.py
# Mouse and keyboard the way a hand does them. Measured on Google Meet
# (2026-11-18): a name pasted in one step and a single click on Join was
# refused six times of six; a pointer that travels to its target and a name
# typed key by key joined eight times of eight. The numbers below are the
# ones that ran; change them only with a new measurement.

import random
import time
from typing import Callable, Optional

MOVE_STEPS = (18, 27)          # pointer positions on the way to a click
MOVE_WOBBLE_PX = 1.5           # how far a step strays from the straight path
MOVE_PAUSE_S = (0.012, 0.034)  # between two pointer positions
REST_S = (0.18, 0.43)          # the pointer resting on its target before the press
HOLD_S = (0.06, 0.13)          # the button held down
KEY_HOLD_S = (0.04, 0.10)      # a key held down
KEY_GAP_S = (0.09, 0.25)       # between two keys
SHIFT = 8                      # the protocol's modifier bit for Shift

KEYS = {
    'Enter': {'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'text': '\r'},
    'Escape': {'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27},
    'Tab': {'key': 'Tab', 'code': 'Tab', 'windowsVirtualKeyCode': 9},
}


class Hands:
    """Sends input to one page. `page` needs `send(method, params)`."""

    def __init__(self, page, sleep: Callable[[float], None] = time.sleep, rng: Optional[random.Random] = None):
        self._page = page
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._at = (420.0, 520.0)   # where the pointer was last left

    def _pause(self, span: tuple):
        self._sleep(self._rng.uniform(*span))

    def _mouse(self, kind: str, x: float, y: float, **more):
        self._page.send('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, **more})

    # The pointer travels from where it was in small uneven steps (slow at
    # both ends), rests on the target, then presses and releases there.
    def click(self, x: float, y: float):
        from_x, from_y = self._at
        steps = self._rng.randint(*MOVE_STEPS)
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)
            self._mouse('mouseMoved',
                        from_x + (x - from_x) * ease + self._rng.uniform(-MOVE_WOBBLE_PX, MOVE_WOBBLE_PX),
                        from_y + (y - from_y) * ease + self._rng.uniform(-MOVE_WOBBLE_PX, MOVE_WOBBLE_PX))
            self._pause(MOVE_PAUSE_S)
        self._mouse('mouseMoved', x, y)
        self._pause(REST_S)
        self._mouse('mousePressed', x, y, button='left', buttons=1, clickCount=1)
        self._pause(HOLD_S)
        self._mouse('mouseReleased', x, y, button='left', clickCount=1)
        self._at = (float(x), float(y))

    # One key down and up per character, Shift around a capital letter.
    def type(self, text: str):
        for ch in text:
            upper = ch != ch.lower()
            code = f'Key{ch.upper()}' if ch.isascii() and ch.isalpha() else 'Space' if ch == ' ' else ''
            own_key = ch.isascii() and (ch.isalnum() or ch == ' ')
            key = {'key': ch, 'code': code, 'windowsVirtualKeyCode': ord(ch.upper()) if own_key else 0,
                   'modifiers': SHIFT if upper else 0}
            if upper:
                self._page.send('Input.dispatchKeyEvent', {'type': 'rawKeyDown', 'key': 'Shift', 'code': 'ShiftLeft',
                                                           'windowsVirtualKeyCode': 16, 'modifiers': SHIFT})
            self._page.send('Input.dispatchKeyEvent', {'type': 'keyDown', 'text': ch, 'unmodifiedText': ch.lower(), **key})
            self._pause(KEY_HOLD_S)
            self._page.send('Input.dispatchKeyEvent', {'type': 'keyUp', **key})
            if upper:
                self._page.send('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'Shift', 'code': 'ShiftLeft',
                                                           'windowsVirtualKeyCode': 16})
            self._pause(KEY_GAP_S)

    # Enter, Escape or Tab.
    def press(self, name: str):
        key = KEYS[name]
        self._page.send('Input.dispatchKeyEvent', {'type': 'keyDown', **key})
        self._page.send('Input.dispatchKeyEvent', {'type': 'keyUp', **key})

    # A letter with Ctrl and/or Alt held: a page's keyboard shortcut (Meet's
    # Ctrl+Alt+H). Keys are not tied to the page's frames, so this lands at
    # once on a hidden page, where a pointer path waits a second for every
    # move (measured 2026-11-19: over a minute for one click).
    def shortcut(self, letter: str, ctrl: bool = False, alt: bool = False):
        letter = letter.lower()
        key = {'modifiers': (1 if alt else 0) | (2 if ctrl else 0), 'key': letter, 'code': f'Key{letter.upper()}',
               'windowsVirtualKeyCode': ord(letter.upper())}
        self._page.send('Input.dispatchKeyEvent', {'type': 'rawKeyDown', **key})
        self._page.send('Input.dispatchKeyEvent', {'type': 'keyUp', **key})

    # Text put in the focused field in one step. Used after the join (a chat
    # message), where Meet took it; never for the name before joining.
    def insert(self, text: str):
        self._page.send('Input.insertText', {'text': text})


__all__ = ['Hands']
