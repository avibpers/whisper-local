# meeting/guest.py
# Koli as her own participant in a Google Meet call: she opens her browser
# with no window, joins as a guest under her name, and gives Meet a drawn
# board as her camera (guest_board.js). The order, the pauses and the
# person-like input are the ones measured on a real call on 2026-11-18;
# pasted input was refused every time. She is an add-on: when Meet refuses
# her or its page changed, she says why and the rest of the meeting goes on.
# Design: docs/design/meet-participant.md.

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..browser import cdp, human, install as browser_install, process

logger = logging.getLogger(__name__)

STATES = ('off', 'unsupported', 'installing', 'starting', 'joining', 'asking', 'in_call', 'leaving', 'refused', 'failed')

RETRIES = 3              # Meet refuses guests at random (Vexa measured about half); a refusal is tried again
RETRY_WAIT_S = 8.0
ASK_WAIT_S = 180.0       # how long she waits to be admitted after "Ask to join"
TICK_S = 5.0             # how often she checks she is still in the call and what Meet asks her to send
FRAME_S = 0.1            # while her board moves (her words coming in), the app redraws it this often
NAME_BOX_WAIT_S = 25.0
JOIN_WAIT_S = 40.0       # from the click on Join to being in the call
POLL_S = 1.0
START_CALL_S = 10.0      # a call to a browser that just started; one in ten starts lost one (2026-11-18)
# The pauses of the join that worked, in its order.
SETTLE_S = 3.0           # after the name box appears
AFTER_HINT_S = 2.0       # after dismissing the sign-in hint
AFTER_CLICK_S = 1.0      # after clicking the name box
AFTER_TYPING_S = 2.0     # after typing the name
AFTER_CHAT_S = 3.0       # after opening the chat panel

SAY_MAX = 400            # characters of one chat message
FRAME_HEIGHT = 720
SHAPE_MIN, SHAPE_MAX = 0.75, 16 / 9   # the frame's width over height: between a tall tile and a wide one
DENSE_HEIGHT = 270       # when Meet asks for nothing taller, her tile is small: few words, large type
CODE = re.compile(r'^[a-z0-9][a-z0-9-]{2,40}$')
BOARD_SCRIPT = Path(__file__).with_name('guest_board.js')
MEET_ORIGIN = 'https://meet.google.com'


# ── What is asked of Meet's page ─────────────────────────────────────────────
# Each probe is script that returns plain data. Meet's class names change, so
# they go by labels and visible words, in English (the page is opened with
# hl=en). The comment at the start names the probe.

STATE = """/*probe:state*/(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    in_call: !!document.querySelector('[aria-label*="Leave call"]'),
    refused: /You can['’]t join this (video )?call/.test(text),
    asking: /Asking to (be let in|join)|You'll join (the call )?when someone lets you in/i.test(text),
    denied: /denied your request|No one responded to your request/i.test(text),
  };
})()"""

# The box is labelled "Your name", or "Enter your name" after "Continue as
# guest" (both seen on 2026-11-18): any text box that asks for a name.
NAME_BOX = """/*probe:name_box*/(() => {
  const box = [...document.querySelectorAll('input')].find((i) =>
    /your name/i.test((i.getAttribute('aria-label') || '') + ' ' + (i.getAttribute('placeholder') || ''))
    && i.getBoundingClientRect().width >= 50);
  if (!box) return null;
  const r = box.getBoundingClientRect();
  return { x: Math.round(r.x + 40), y: Math.round(r.y + r.height / 2) };
})()"""

# What the name box holds now, to check the typed name landed.
NAME_VALUE = """/*probe:name_value*/(() => {
  const box = [...document.querySelectorAll('input')].find((i) =>
    /your name/i.test((i.getAttribute('aria-label') || '') + ' ' + (i.getAttribute('placeholder') || '')));
  return box ? box.value : '';
})()"""

CHAT_BOX = """/*probe:chat_box*/(() => {
  const box = document.querySelector('textarea');
  if (!box) return false;
  box.focus();
  return document.activeElement === box;
})()"""


# A button by its words or its label: where to click it, or null.
def _button(probe: str, test: str) -> str:
    return f"""/*probe:button:{probe}*/(() => {{
  const all = [...document.querySelectorAll('button, [role="button"]')];
  const b = all.find((b) => !b.disabled && ({test}));
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return r.width ? {{ x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), text: b.innerText.trim() }} : null;
}})()"""


GOT_IT = _button('got_it', "b.innerText.trim() === 'Got it'")
AS_GUEST = _button('guest', "/^Continue as guest/.test(b.innerText.trim())")
JOIN = _button('join', "/^(Join now|Ask to join)$/.test(b.innerText.trim())")
DIALOG_JOIN = _button('dialog_join', "b.closest('[role=\"dialog\"]') && b.innerText.trim() === 'Join now'")
CHAT = _button('chat', "/chat with everyone/i.test(b.getAttribute('aria-label') || '')")

# Her own microphone button and whether it is muted. Meet marks it with
# data-is-muted, and its icon is "mic" or "mic_off" in every language
# (measured on Meet's page, 2026-11-18).
MIC = """/*probe:button:mic*/(() => {
  const b = [...document.querySelectorAll('button[data-is-muted]')].find((b) =>
    [...b.querySelectorAll('i, span')].some((e) => /^mic(_off)?$/.test(e.textContent.trim())));
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return r.width ? { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
                     muted: b.getAttribute('data-is-muted') === 'true' } : null;
})()"""


# ── The frame ────────────────────────────────────────────────────────────────

# The board's frame for a tile of this size on the owner's screen. Meet fills
# a tile and cuts what is left over, so a frame of the tile's own shape is the
# one seen whole. No tile known: 4:3, the middle of the shapes Meet uses.
def frame_size(tile_width, tile_height) -> tuple:
    if not tile_width or not tile_height:
        return (960, FRAME_HEIGHT)
    shape = min(SHAPE_MAX, max(SHAPE_MIN, float(tile_width) / float(tile_height)))
    return (int(round(FRAME_HEIGHT * shape / 16.0)) * 16, FRAME_HEIGHT)


# ── The browser, as the guest uses it ────────────────────────────────────────

class BrowserSession:
    """Koli's browser with one page, ready for Meet."""

    def __init__(self, exe: Path, profile_dir: Path):
        self._exe = exe
        self._profile_dir = profile_dir
        self._process = process.BrowserProcess()
        self._connection = None
        self.page = None

    def open(self):
        process.kill_leftovers(self._profile_dir)
        port = self._process.start(self._exe, self._profile_dir)
        self._connection = cdp.connect(port)
        self.page = cdp.first_page(self._connection)
        version = self._connection.send('Browser.getVersion', timeout=START_CALL_S)
        agent = version.get('userAgent') or ''
        if 'Headless' in agent:
            # With no window Chrome names itself HeadlessChrome; Meet gets the plain name.
            major = (re.search(r'/(\d+)', version.get('product') or '') or [None, '158'])[1]
            self.page.send('Emulation.setUserAgentOverride', {
                'userAgent': agent.replace('HeadlessChrome', 'Chrome'),
                'userAgentMetadata': {
                    'brands': [{'brand': 'Chromium', 'version': major}, {'brand': 'Google Chrome', 'version': major},
                               {'brand': 'Not?A_Brand', 'version': '99'}],
                    'fullVersion': f'{major}.0.0.0', 'platform': 'Windows', 'platformVersion': '15.0.0',
                    'architecture': 'x86', 'model': '', 'mobile': False}}, timeout=START_CALL_S)
        # Meet asks whether the camera is allowed before it asks for it.
        # Allowed is safe: the page only ever gets Koli's tracks, and the
        # browser runs with fake devices, so the real ones can't be reached.
        self._connection.send('Browser.grantPermissions', {'origin': MEET_ORIGIN,
                                                           'permissions': ['videoCapture', 'audioCapture']},
                              timeout=START_CALL_S)

    # Marks the page hidden: the browser stops drawing other people's video
    # (49% of a core down to 13%, measured) while the board keeps going out.
    def hide(self):
        window = self._connection.send('Browser.getWindowForTarget', {'targetId': self.page.target_id})
        self._connection.send('Browser.setWindowBounds', {'windowId': window['windowId'],
                                                          'bounds': {'windowState': 'minimized'}})

    def close(self):
        connection, self._connection = self._connection, None
        self._process.stop(close_browser=(lambda: connection.send('Browser.close', timeout=3)) if connection else None)
        if connection is not None:
            connection.close()


class _Stopped(Exception):
    """leave() was called while she was on her way in."""


class _Refused(Exception):
    """Meet showed its refusal screen."""


class _NoAnswer(Exception):
    """Her browser started and then did not answer; starting it again usually does."""


class _Failed(Exception):
    """A step can't be done; trying again would not help. The message is for the user."""


# ── The guest ────────────────────────────────────────────────────────────────

class MeetGuest:
    """One visit to one call. `on_state(state, detail, progress)` tells the panel."""

    def __init__(self, on_state: Callable, name: str = 'Koli', session_factory: Optional[Callable] = None,
                 installer=None, sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time,
                 announce: str = ''):
        self._on_state = on_state
        self.name = str(name or 'Koli').strip()[:40] or 'Koli'
        self._installer = installer or browser_install
        self._session_factory = session_factory or self._real_session
        self._sleep = sleep
        self._now = now
        self._announce = str(announce or '').strip()
        self._lock = threading.RLock()
        self._say_lock = threading.Lock()   # one chat message at a time: the text, then Enter
        self._stop = threading.Event()
        self._session = None
        self._hands = None
        self._thread = None
        self.state = 'off'
        self._view = {}            # the public view to draw
        self._drawn = None         # the view as last sent to the page
        self._moving = False       # her board is animating: frames are driven from here (animate)
        self._wake = threading.Event()   # set when there is something new to draw
        self._watched = None       # the tallest picture of her Meet asks for; 0 = none; None = not looked yet
        self._dense = False
        self._frame = frame_size(None, None)
        self._frame_sent = None
        self._asked_as_guest = False   # "Continue as guest" was pressed in this try

    @property
    def in_call(self) -> bool:
        return self.state == 'in_call'

    def _real_session(self):
        return BrowserSession(self._installer.executable(), self._installer.profile_dir())

    def _set(self, state: str, detail: str = '', progress: Optional[float] = None):
        self.state = state
        try:
            self._on_state(state, detail, progress)
        except Exception as e:
            logger.debug(f"Guest state listener failed: {e}")

    # ── Coming and going ─────────────────────────────────────────────────────

    # Starts the visit on a thread of its own and returns at once.
    def join(self, code: str, owner: str = ''):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            # In the call: a look at the call every TICK_S, and while her board
            # moves a frame every FRAME_S. A hidden page draws about once a
            # second by itself; driven from here it gave 7 to 9 frames a second
            # (measured 2026-11-19), which is what makes her words flow.
            def run():
                if not self.join_now(code, owner):
                    return
                next_tick = time.monotonic() + TICK_S
                while not self._stop.is_set():
                    if self._moving:
                        self._stop.wait(FRAME_S)
                        self.animate()
                    else:
                        self._wake.wait(0.5)
                        self._wake.clear()
                    if time.monotonic() >= next_tick:
                        next_tick = time.monotonic() + TICK_S
                        self.tick()
            self._thread = threading.Thread(target=run, daemon=True, name='meet-guest')
            self._thread.start()

    # The whole way in, with retries. True once she is in the call.
    def join_now(self, code: str, owner: str = '') -> bool:
        self._stop.clear()
        code = str(code or '').strip().lower()
        if not CODE.match(code):
            self._set('failed', "This call has no Google Meet code Koli can join.")
            return False
        try:
            if not self._installer.supported():
                self._set('unsupported', "Koli can join a call on Windows only, for now.")
                return False
            self._install()
            for attempt in range(1, RETRIES + 1):
                try:
                    self._attempt(code, attempt)
                    if self._stop.is_set():
                        raise _Stopped()
                    self._set('in_call', f"{self.name} is in the call")
                    return True
                except _NoAnswer as e:
                    self._close()
                    if attempt == RETRIES:
                        raise _Failed(f"Koli's browser did not answer ({e}).")
                    self._set('starting', "Koli's browser did not answer. Starting it again…")
                    self._wait(POLL_S)
                except _Refused:
                    self._close()
                    if attempt == RETRIES:
                        raise _Failed(f"Meet refused the join {RETRIES} times. Try again in a while.")
                    self._set('refused', f"Meet refused the join. Trying again ({attempt + 1} of {RETRIES})…")
                    self._wait(RETRY_WAIT_S)
        except _Stopped:
            self._close()
            self._set('off')
        except _Failed as e:
            self._close()
            self._set('failed', str(e))
        except Exception as e:
            self._close()
            # She was taken out while a step was running: the closed browser
            # made it fail, which is the leave, not a failure.
            if self._stop.is_set():
                self._set('off')
                return False
            logger.warning(f"Koli could not join the call: {e}", exc_info=not isinstance(e, cdp.CdpError))
            self._set('failed', f"{self.name} could not join: {e}")
        return False

    # Takes her out: the browser closes, and with it her tile.
    def leave(self):
        self._stop.set()
        with self._lock:
            if self._session is None:
                if self.state != 'off':
                    self._set('off')
                return
            self._set('leaving', f"{self.name} is leaving the call…")
            self._close()
            self._set('off')

    def _close(self):
        with self._lock:
            session, self._session, self._hands = self._session, None, None
            self._drawn = None
            self._moving = False
            self._watched = None
            self._frame_sent = None
        if session is not None:
            try:
                session.close()
            except Exception as e:
                logger.debug(f"Closing Koli's browser failed: {e}")

    # Sleeps unless she was told to leave.
    def _wait(self, seconds: float):
        if self._stop.is_set():
            raise _Stopped()
        self._sleep(seconds)
        if self._stop.is_set():
            raise _Stopped()

    # ── The way in ───────────────────────────────────────────────────────────

    def _install(self):
        if self._installer.executable() is not None:
            return
        guest = self

        class Reporter:
            def report(self, progress=None, detail=None):
                guest._set('installing', detail or "Getting Koli's browser (215 MB, once)…", progress)

            def check_cancelled(self):
                if guest._stop.is_set():
                    raise _Stopped()
        self._set('installing', "Getting Koli's browser (215 MB, once)…", 0.0)
        try:
            self._installer.ensure(Reporter())
        except _Stopped:
            raise
        except Exception as e:
            if self._stop.is_set():
                raise _Stopped()
            raise _Failed(str(e) or "Koli's browser could not be installed.")

    # One try, in the order that was measured. Raises _Refused, _Failed or _Stopped.
    def _attempt(self, code: str, attempt: int):
        self._set('starting', "Starting Koli's browser…")
        session = self._session_factory()
        with self._lock:
            self._session = session
        try:
            session.open()
        except cdp.CdpError as e:
            raise _NoAnswer(str(e))
        page = session.page
        self._hands = human.Hands(page, sleep=self._wait)      # every pause of her hands checks whether she was told to leave
        self._asked_as_guest = False
        page.add_script(self._board_script())
        self._set('joining', f"{self.name} is joining the call…" if attempt == 1
                  else f"{self.name} is joining the call (try {attempt} of {RETRIES})…")
        page.navigate(f'{MEET_ORIGIN}/{code}?hl=en')

        # The name box exists before it is laid out: wait for it to have a width.
        box = self._until(lambda: self._name_box(page), NAME_BOX_WAIT_S,
                          "Koli could not find the name box on Meet's page. Meet may have changed it.")
        self._wait(SETTLE_S)
        # A "Sign in with your Google account" hint can cover the box.
        hint = page.evaluate(GOT_IT)
        if hint:
            self._hands.click(hint['x'], hint['y'])
            self._wait(AFTER_HINT_S)
        # The box moves while the page finishes loading: where it is now. A
        # click that missed leaves the box empty, so the name is checked and
        # typed once more before Join is ever pressed.
        for _ in range(2):
            box = page.evaluate(NAME_BOX) or box
            self._hands.click(box['x'], box['y'])
            self._wait(AFTER_CLICK_S)
            self._hands.type(self.name)
            self._wait(AFTER_TYPING_S)
            if str(page.evaluate(NAME_VALUE) or '').strip() == self.name:
                break
        else:
            raise _Failed("Koli could not type her name on Meet's page. Meet may have changed it.")
        button = page.evaluate(JOIN)
        if not button:
            raise _Failed("Koli could not find the Join button on Meet's page. Meet may have changed it.")
        self._hands.click(button['x'], button['y'])
        self._get_in(page)
        self._settle_in(page, session)

    # Where the name box is, once it is there. Meet sometimes asks first
    # whether to sign in (seen 2026-11-18: "Continue as guest" and "Sign in
    # with Google" instead of the box); she continues as a guest, once.
    def _name_box(self, page):
        self._refused(page)
        box = page.evaluate(NAME_BOX)
        if box or self._asked_as_guest:
            return box
        offer = page.evaluate(AS_GUEST)
        if offer:
            self._asked_as_guest = True
            self._wait(AFTER_HINT_S)
            self._hands.click(offer['x'], offer['y'])
        return None

    def _refused(self, page) -> bool:
        if (page.evaluate(STATE) or {}).get('refused'):
            raise _Refused()
        return False

    # Polls `probe` once a second until it answers, or fails with `missing`.
    def _until(self, probe: Callable, seconds: float, missing: str):
        end = self._now() + seconds
        while True:
            found = probe()
            if found:
                return found
            if self._now() >= end:
                raise _Failed(missing)
            self._wait(POLL_S)

    # From the click on Join to being seen in the call: a dialog that offers
    # "Join now" again is pressed (with it up she sees the call and nobody
    # sees her, measured), a refusal is a refusal, "Ask to join" waits.
    def _get_in(self, page):
        end = self._now() + JOIN_WAIT_S
        asking_until = None
        while True:
            self._wait(POLL_S)
            dialog = page.evaluate(DIALOG_JOIN)
            if dialog:
                self._hands.click(dialog['x'], dialog['y'])
                continue
            state = page.evaluate(STATE) or {}
            if state.get('refused') and asking_until is None:
                raise _Refused()
            if state.get('in_call'):
                return
            if state.get('denied'):
                raise _Failed(f"{self.name} wasn't let in to the call.")
            if state.get('asking'):
                if asking_until is None:
                    asking_until = self._now() + ASK_WAIT_S
                    self._set('asking', f"{self.name} is asking to join. Admit her in Meet.")
                if self._now() >= asking_until:
                    raise _Failed(f"{self.name} wasn't let in to the call: nobody admitted her.")
            elif asking_until is None and self._now() >= end:
                raise _Failed(f"{self.name} pressed Join and the call did not open.")

    # In the call: mute her microphone (it only sends silence until she has a
    # voice; muted, Meet shows it), open the chat (it must be open to write
    # later, and a hidden page doesn't open it), say who she is, then hide
    # the page. The mute comes after the join, so the way in stays as measured.
    def _settle_in(self, page, session):
        mic = page.evaluate(MIC)
        if mic and not mic.get('muted'):
            self._hands.click(mic['x'], mic['y'])
            self._wait(POLL_S)
        chat = page.evaluate(CHAT)
        if chat:
            self._hands.click(chat['x'], chat['y'])
            self._wait(AFTER_CHAT_S)
        can_write = bool(page.evaluate(CHAT_BOX))
        if can_write and self._announce:
            self._hands.insert(self._announce[:SAY_MAX])
            self._wait(POLL_S)
            self._hands.press('Enter')
            self._wait(POLL_S)
        session.hide()
        self._draw(page)

    # ── While in the call ────────────────────────────────────────────────────

    # One look at the call: still in it? what does Meet ask her to send?
    def tick(self):
        with self._lock:
            session = self._session
        if session is None or not self.in_call:
            return
        page = session.page
        try:
            if not (page.evaluate(STATE) or {}).get('in_call'):
                self._ended(f"{self.name} is no longer in the call")
                return
            layers = [l for l in (page.evaluate('/*probe:sending*/window.__koli.sending()') or [])
                      if l.get('active') and l.get('height')]
            self._watched = max((l['height'] for l in layers), default=0)
            if layers:
                self._dense = max(l['height'] for l in layers) <= DENSE_HEIGHT
            if self._frame != self._frame_sent:
                page.evaluate(f'/*probe:resize*/window.__koli.resize({self._frame[0]}, {self._frame[1]})')
                self._frame_sent = self._frame
            self._draw(page)
            # A frame that got no answer stops the redrawing (animate). Chrome
            # stops a page for some seconds once, about a quarter of a minute
            # after it is hidden (measured 2026-11-19), so while she says
            # something every look at the call picks the pace up again; a
            # board at rest ends it at the next frame.
            if self._view.get('say') and not self._moving:
                self._moving = True
                self._wake.set()
        except Exception as e:
            logger.info(f"Koli's browser stopped answering: {e}")
            self._ended(f"{self.name} is no longer in the call")

    def _ended(self, detail: str):
        self._stop.set()
        self._close()
        self._set('off', detail)

    # The public view for the board (meeting/companion.py public_view): the
    # only text that reaches the call's screen.
    def show(self, view: dict):
        self._view = dict(view or {})
        with self._lock:
            session = self._session
        if session is not None and self.in_call:
            try:
                self._draw(session.page)
            except Exception as e:
                logger.debug(f"The board was not updated: {e}")

    def _draw(self, page):
        view = {**self._view, 'dense': self._dense}
        if view == self._drawn or not self._view:
            return
        page.evaluate(f'/*probe:board*/window.__koli.board({json.dumps(view, ensure_ascii=False)})')
        self._drawn = view
        if view.get('say'):            # her words come in, scroll and pulse: frames are needed
            self._moving = True
            self._wake.set()

    @property
    def moving(self) -> bool:
        return self._moving

    # One frame of her board. False once the board says it has come to rest
    # (all her words are out and nothing scrolls), or when it can't be asked.
    def animate(self) -> bool:
        with self._lock:
            session = self._session
        try:
            self._moving = bool(session is not None and self.in_call
                                and session.page.evaluate('/*probe:frame*/window.__koli.frame()', timeout=5))
        except Exception as e:
            logger.debug(f"A frame of Koli's board was not drawn: {e}")
            self._moving = False
        return self._moving

    # The tallest picture of her that Meet asks her to send right now, in
    # lines: 0 when it asks for none (her tile is probably on nobody's
    # screen), None before the first look.
    def watched(self) -> Optional[int]:
        return self._watched

    # The size of her tile on the owner's screen, from the extension.
    def set_tile(self, width, height):
        self._frame = frame_size(width, height)

    # One message in the call's chat, as Koli. False when it wasn't written.
    def say(self, text: str) -> bool:
        text = ' '.join(str(text or '').split())[:SAY_MAX]
        with self._lock:
            session, hands = self._session, self._hands
        if not text or session is None or hands is None or not self.in_call:
            return False
        try:
            with self._say_lock:
                if not session.page.evaluate(CHAT_BOX):
                    return False
                hands.insert(text)
                self._sleep(POLL_S)
                hands.press('Enter')
            return True
        except Exception as e:
            logger.info(f"Koli's chat message was not sent: {e}")
            return False

    # guest_board.js with its first view and frame filled in.
    def _board_script(self) -> str:
        config = {'width': self._frame[0], 'height': self._frame[1], 'tickMs': 1000,
                  'view': {**self._view, 'dense': self._dense} if self._view else {'title': self.name}}
        source = BOARD_SCRIPT.read_text(encoding='utf-8')
        return re.sub(r'/\*CONFIG\*/.*?/\*END\*/', lambda _: json.dumps(config, ensure_ascii=False), source, count=1)


__all__ = ['ASK_WAIT_S', 'BrowserSession', 'MeetGuest', 'RETRIES', 'RETRY_WAIT_S', 'SAY_MAX', 'STATES', 'TICK_S', 'frame_size']
