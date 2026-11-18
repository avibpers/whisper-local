# browser/cdp.py
# The Chrome DevTools Protocol, as much of it as Koli needs: one WebSocket to
# the browser, calls answered by their id, events handed to listeners, and a
# Page that evaluates script and sends input. The Runtime domain is never
# switched on: sites can tell a browser that has it on, and the Meet join was
# measured without it.

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from ..cloud_stt.ws import WebSocket

logger = logging.getLogger(__name__)

CALL_TIMEOUT_S = 30.0


class CdpError(Exception):
    """A call the browser refused, did not answer, or could not get."""


# ── The connection ───────────────────────────────────────────────────────────

class Connection:
    """One WebSocket to the browser. Any thread may call `send`."""

    def __init__(self, ws):
        self._ws = ws
        self._lock = threading.Lock()
        self._next_id = 0
        self._waiting = {}          # call id -> {'event', 'method', 'answer'}
        self._listeners = []
        self.closed = False
        self._reader = threading.Thread(target=self._read, daemon=True, name='browser-cdp')
        self._reader.start()

    # One protocol call; returns its `result`. `session_id` addresses a page.
    def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None,
             timeout: float = CALL_TIMEOUT_S) -> dict:
        slot = {'event': threading.Event(), 'method': method, 'answer': None}
        with self._lock:
            if self.closed:
                raise CdpError(f"{method}: the browser is closed")
            self._next_id += 1
            call_id = self._next_id
            self._waiting[call_id] = slot
        message = {'id': call_id, 'method': method, 'params': params or {}}
        if session_id:
            message['sessionId'] = session_id
        try:
            self._ws.send_text(json.dumps(message))
        except (ConnectionError, OSError) as e:
            self._waiting.pop(call_id, None)
            raise CdpError(f"{method}: the browser is closed ({e})")
        if not slot['event'].wait(timeout):
            self._waiting.pop(call_id, None)
            raise CdpError(f"{method}: no answer in {timeout:g} s")
        answer = slot['answer']
        if answer is None:
            raise CdpError(f"{method}: the browser closed before it answered")
        if 'error' in answer:
            raise CdpError(f"{method}: {(answer['error'] or {}).get('message') or answer['error']}")
        return answer.get('result') or {}

    # `fn(message)` for every event (a message with a `method` and no id).
    def on_event(self, fn: Callable[[dict], None]):
        self._listeners.append(fn)

    def close(self):
        self._fail_waiting()
        try:
            self._ws.close()
        except Exception:
            pass

    # The reader thread: answers wake their caller, events go to listeners.
    def _read(self):
        while True:
            try:
                item = self._ws.recv()
            except Exception as e:
                logger.debug(f"Browser connection read failed: {e}")
                item = None
            if item is None:
                break
            kind, data = item
            if kind != 'text':
                continue
            try:
                message = json.loads(data)
            except ValueError:
                # A lost answer shows up as a call that times out: say so here.
                logger.warning(f"A message from the browser could not be read ({len(data)} characters)")
                continue
            if 'id' in message:
                slot = self._waiting.pop(message['id'], None)
                if slot is not None:
                    slot['answer'] = message
                    slot['event'].set()
            elif 'method' in message:
                for fn in list(self._listeners):
                    try:
                        fn(message)
                    except Exception as e:
                        logger.debug(f"Browser event listener failed: {e}")
        self._fail_waiting()

    # The socket is gone: every call still waiting learns it now, not at its timeout.
    def _fail_waiting(self):
        with self._lock:
            self.closed = True
            waiting, self._waiting = self._waiting, {}
        for slot in waiting.values():
            slot['event'].set()


# Connects to a browser started with --remote-debugging-port=<port>. The
# address comes from /json/version: with a port named on the command line
# Chrome writes no DevToolsActivePort file (measured on Chrome 157).
def connect(port: int, timeout: float = 15.0, connect: Callable = WebSocket.connect,
            sleep: Callable[[float], None] = time.sleep) -> Connection:
    end = time.time() + timeout
    last = 'no answer'
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never through a proxy
    while time.time() < end:
        try:
            with direct.open(f'http://127.0.0.1:{port}/json/version', timeout=2) as response:
                url = json.loads(response.read().decode('utf-8')).get('webSocketDebuggerUrl')
            if url:
                return Connection(connect(url, {}, timeout=5))
            last = 'no debugger address in the answer'
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
        sleep(0.15)
    raise CdpError(f"The browser did not open its control port ({last})")


# ── A page ───────────────────────────────────────────────────────────────────

class Page:
    """One tab, addressed by its session."""

    def __init__(self, connection: Connection, target_id: str, session_id: str):
        self.connection = connection
        self.target_id = target_id
        self.session_id = session_id

    def send(self, method: str, params: Optional[dict] = None, timeout: float = CALL_TIMEOUT_S) -> dict:
        return self.connection.send(method, params, self.session_id, timeout)

    # Runs script in the page and returns its value (a promise is awaited).
    def evaluate(self, expression: str, timeout: float = CALL_TIMEOUT_S):
        result = self.send('Runtime.evaluate', {'expression': expression, 'awaitPromise': True,
                                                'returnByValue': True, 'userGesture': True}, timeout)
        details = result.get('exceptionDetails')
        if details:
            what = (details.get('exception') or {}).get('description') or details.get('text') or 'script failed'
            raise CdpError(f"The page raised: {what}")
        return (result.get('result') or {}).get('value')

    def navigate(self, url: str):
        self.send('Page.navigate', {'url': url})

    # Script that runs in every frame before the page's own scripts.
    def add_script(self, source: str):
        self.send('Page.addScriptToEvaluateOnNewDocument', {'source': source})


# The browser's first tab, attached in flat mode. Only the Page domain is
# switched on.
def first_page(connection: Connection) -> Page:
    targets = connection.send('Target.getTargets').get('targetInfos') or []
    target = next((t for t in targets if t.get('type') == 'page'), None)
    if target is None:
        raise CdpError("The browser has no page open")
    session_id = connection.send('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
    page = Page(connection, target['targetId'], session_id)
    page.send('Page.enable')
    return page


__all__ = ['CdpError', 'Connection', 'Page', 'connect', 'first_page']
