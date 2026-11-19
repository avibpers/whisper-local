# tests/test_browser.py
# Koli's own browser (src/koli/browser): the DevTools Protocol connection
# against a fake socket, the launch flags, the sweep of leftover browsers,
# the install from a local server, and input that moves like a person.

import io
import json
import queue
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from koli.browser import cdp, human, install, process  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeSocket:
    """A WebSocket whose incoming messages the test feeds by hand."""

    def __init__(self):
        self.sent = []
        self.incoming = queue.Queue()
        self.closed = False

    def send_text(self, text):
        if self.closed:
            raise ConnectionError('WebSocket closed')
        self.sent.append(json.loads(text))

    def recv(self):
        item = self.incoming.get()
        return None if item is None else ('text', json.dumps(item))

    def feed(self, message):
        self.incoming.put(message)

    def close(self):
        self.closed = True
        self.incoming.put(None)

    # Waits until `count` messages were sent, so a test can answer them.
    def wait_sent(self, count, timeout=2.0):
        end = time.time() + timeout
        while len(self.sent) < count and time.time() < end:
            time.sleep(0.005)
        return self.sent


class RecordingPage:
    """Takes the place of cdp.Page: remembers every protocol call."""

    def __init__(self):
        self.calls = []

    def send(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        return {}


# ── The connection ───────────────────────────────────────────────────────────

class CdpConnectionTests(unittest.TestCase):
    def setUp(self):
        self.ws = FakeSocket()
        self.connection = cdp.Connection(self.ws)
        self.addCleanup(self.connection.close)

    def call(self, *args, **kwargs):
        box = {}

        def run():
            try:
                box['result'] = self.connection.send(*args, **kwargs)
            except Exception as e:
                box['error'] = e
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, box

    def test_an_answer_reaches_its_own_call_past_events_and_other_ids(self):
        first, first_box = self.call('Target.getTargets')
        second, second_box = self.call('Browser.getVersion')
        sent = self.ws.wait_sent(2)
        ids = {m['method']: m['id'] for m in sent}
        self.ws.feed({'method': 'Target.targetCreated', 'params': {}})
        self.ws.feed({'id': ids['Browser.getVersion'], 'result': {'product': 'Chrome/158'}})
        self.ws.feed({'id': ids['Target.getTargets'], 'result': {'targetInfos': []}})
        first.join(2)
        second.join(2)
        self.assertEqual(first_box['result'], {'targetInfos': []})
        self.assertEqual(second_box['result'], {'product': 'Chrome/158'})

    def test_a_session_id_travels_with_the_call(self):
        thread, box = self.call('Page.enable', {}, 'S1')
        sent = self.ws.wait_sent(1)
        self.assertEqual(sent[0]['sessionId'], 'S1')
        self.ws.feed({'id': sent[0]['id'], 'result': {}})
        thread.join(2)
        self.assertEqual(box['result'], {})

    def test_an_error_answer_raises_with_the_browsers_words(self):
        thread, box = self.call('Page.navigate', {'url': 'x'})
        sent = self.ws.wait_sent(1)
        self.ws.feed({'id': sent[0]['id'], 'error': {'code': -32000, 'message': 'Cannot navigate to invalid URL'}})
        thread.join(2)
        self.assertIsInstance(box['error'], cdp.CdpError)
        self.assertIn('Cannot navigate to invalid URL', str(box['error']))
        self.assertIn('Page.navigate', str(box['error']))

    def test_events_reach_the_listener(self):
        seen = []
        self.connection.on_event(seen.append)
        self.ws.feed({'method': 'Page.loadEventFired', 'params': {'timestamp': 1}})
        end = time.time() + 2
        while not seen and time.time() < end:
            time.sleep(0.005)
        self.assertEqual(seen[0]['method'], 'Page.loadEventFired')

    def test_a_closed_socket_fails_a_waiting_call_at_once(self):
        thread, box = self.call('Browser.getVersion', None, None, 30.0)
        self.ws.wait_sent(1)
        started = time.time()
        self.ws.incoming.put(None)          # the browser went away
        thread.join(3)
        self.assertLess(time.time() - started, 2.0)
        self.assertIsInstance(box['error'], cdp.CdpError)
        self.assertTrue(self.connection.closed)

    def test_no_answer_in_time_raises(self):
        with self.assertRaises(cdp.CdpError) as raised:
            self.connection.send('Browser.getVersion', timeout=0.05)
        self.assertIn('no answer', str(raised.exception))


class ConnectTests(unittest.TestCase):
    def test_the_browser_on_this_pc_is_asked_directly_never_through_a_proxy(self):
        import os
        from unittest import mock

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({'webSocketDebuggerUrl': 'ws://127.0.0.1:1/devtools/browser/x'}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        asked = []

        def fake_connect(url, headers, timeout=5):
            asked.append(url)
            return FakeSocket()
        dead_proxy = {'HTTP_PROXY': 'http://127.0.0.1:9', 'http_proxy': 'http://127.0.0.1:9', 'NO_PROXY': '', 'no_proxy': ''}
        with mock.patch.dict(os.environ, dead_proxy):
            connection = cdp.connect(httpd.server_address[1], timeout=3, connect=fake_connect)
        self.addCleanup(connection.close)
        self.assertEqual(asked, ['ws://127.0.0.1:1/devtools/browser/x'])


class PageTests(unittest.TestCase):
    def setUp(self):
        self.ws = FakeSocket()
        self.connection = cdp.Connection(self.ws)
        self.addCleanup(self.connection.close)
        self.page = cdp.Page(self.connection, 'T1', 'S1')

    # Answers every call the page makes with `result`.
    def answer(self, result):
        def run():
            seen = 0
            while not self.ws.closed:
                sent = self.ws.wait_sent(seen + 1, timeout=0.5)
                if len(sent) > seen:
                    self.ws.feed({'id': sent[seen]['id'], 'result': result})
                    seen += 1
        threading.Thread(target=run, daemon=True).start()

    def test_evaluate_returns_the_value(self):
        self.answer({'result': {'type': 'number', 'value': 42}})
        self.assertEqual(self.page.evaluate('6 * 7'), 42)
        sent = self.ws.sent[0]
        self.assertEqual(sent['method'], 'Runtime.evaluate')
        self.assertEqual(sent['sessionId'], 'S1')
        self.assertTrue(sent['params']['awaitPromise'])
        self.assertTrue(sent['params']['returnByValue'])

    def test_evaluate_raises_on_a_page_exception(self):
        self.answer({'result': {'type': 'object'}, 'exceptionDetails': {'text': 'Uncaught', 'exception': {'description': 'TypeError: x is not a function'}}})
        with self.assertRaises(cdp.CdpError) as raised:
            self.page.evaluate('x()')
        self.assertIn('x is not a function', str(raised.exception))

    def test_runtime_enable_is_never_sent(self):
        # Sites notice a browser with the Runtime domain on; the join was
        # measured without it (docs/design/meet-participant.md).
        self.answer({'targetInfos': [{'type': 'page', 'targetId': 'T9'}], 'sessionId': 'S9'})
        cdp.first_page(self.connection)
        self.assertNotIn('Runtime.enable', [m['method'] for m in self.ws.sent])
        self.assertIn('Page.enable', [m['method'] for m in self.ws.sent])


# ── Starting and stopping ────────────────────────────────────────────────────

class LaunchArgsTests(unittest.TestCase):
    def test_the_flags_the_join_was_measured_with(self):
        args = process.launch_args(Path('C:/data/browser/profile'), 51234)
        for flag in ('--headless=new', '--remote-debugging-port=51234', '--no-first-run', '--no-default-browser-check',
                     '--mute-audio', '--lang=en-US', '--use-fake-device-for-media-stream', '--window-size=1100,760'):
            self.assertIn(flag, args)
        self.assertTrue(any(a.startswith('--user-data-dir=') and 'profile' in a for a in args))
        self.assertEqual(args[-1], 'about:blank')

    def test_a_window_only_when_asked(self):
        args = process.launch_args(Path('p'), 5000, headless=False)
        self.assertNotIn('--headless=new', args)

    def test_the_port_is_never_zero(self):
        # Port 0 is said to mark the browser as automated; Koli picks one.
        with self.assertRaises(ValueError):
            process.launch_args(Path('p'), 0)

    def test_extra_flags_come_before_the_page(self):
        args = process.launch_args(Path('p'), 5000, extra_args=('--disable-gpu',))
        self.assertLess(args.index('--disable-gpu'), args.index('about:blank'))


class LeftoverTests(unittest.TestCase):
    def test_only_main_processes_of_this_profile_are_killed(self):
        profile = Path('C:/Users/me/AppData/Roaming/koli/browser/profile')
        running = [
            {'pid': 10, 'command': f'chrome.exe --user-data-dir={profile} --remote-debugging-port=5000 about:blank'},
            {'pid': 11, 'command': f'chrome.exe --type=renderer --user-data-dir={profile}'},
            {'pid': 12, 'command': 'chrome.exe --user-data-dir=C:/Users/me/AppData/Local/Google/Chrome/User Data'},
            {'pid': 13, 'command': 'chrome.exe'},
        ]
        killed = []
        count = process.kill_leftovers(profile, list_processes=lambda: running, kill=killed.append)
        self.assertEqual(killed, [10])
        self.assertEqual(count, 1)

    def test_the_path_is_matched_whatever_the_slashes(self):
        profile = Path('C:/data/browser/profile')
        running = [{'pid': 7, 'command': 'chrome.exe --user-data-dir=C:\\data\\browser\\profile about:blank'}]
        killed = []
        process.kill_leftovers(profile, list_processes=lambda: running, kill=killed.append)
        self.assertEqual(killed, [7])

    def test_a_list_that_cannot_be_read_kills_nothing(self):
        def broken():
            raise OSError('no access')
        self.assertEqual(process.kill_leftovers(Path('p'), list_processes=broken, kill=lambda pid: None), 0)


# ── Installing ───────────────────────────────────────────────────────────────

def browser_zip(broken=False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('chrome-win64/chrome.exe', b'MZ' + bytes(range(256)) * 800)
        z.writestr('chrome-win64/resources.pak', bytes(range(255, -1, -1)) * 200)
    data = buffer.getvalue()
    return data[: len(data) // 2] if broken else data


class ZipServer:
    """Serves one zip at http://127.0.0.1:<port>/chrome.zip."""

    def __init__(self, data: bytes):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(outer.data)))
                self.end_headers()
                self.wfile.write(outer.data)
        self.data = data
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/chrome.zip'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeReporter:
    def __init__(self, cancel_after=None):
        self.reports = []
        self.cancel_after = cancel_after

    def report(self, progress=None, detail=None):
        self.reports.append((progress, detail))

    def check_cancelled(self):
        if self.cancel_after is not None and len(self.reports) >= self.cancel_after:
            raise install.InstallCancelled()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / 'browser'

    def serve(self, data):
        server = ZipServer(data)
        self.addCleanup(server.close)
        return server.url

    def test_nothing_is_installed_at_first(self):
        self.assertIsNone(install.executable(self.folder))

    def test_ensure_downloads_unpacks_and_reports_rising_progress(self):
        reporter = FakeReporter()
        exe = install.ensure(reporter, url=self.serve(browser_zip()), folder=self.folder, block=1_024)
        self.assertTrue(exe.is_file())
        self.assertEqual(exe.name, 'chrome.exe')
        self.assertEqual(install.executable(self.folder), exe)
        fractions = [p for p, _ in reporter.reports if p is not None]
        self.assertGreater(len(fractions), 2)
        self.assertEqual(fractions, sorted(fractions))
        self.assertLessEqual(fractions[-1], 1.0)
        self.assertTrue(any("Koli's browser" in (d or '') for _, d in reporter.reports))
        self.assertEqual(list(self.folder.glob('*.zip*')), [])   # the download is not kept

    def test_ensure_does_not_download_twice(self):
        url = self.serve(browser_zip())
        first = install.ensure(None, url=url, folder=self.folder)
        second = install.ensure(None, url='http://127.0.0.1:9/never', folder=self.folder)
        self.assertEqual(first, second)

    def test_a_broken_download_installs_nothing(self):
        with self.assertRaises(install.InstallError):
            install.ensure(None, url=self.serve(browser_zip(broken=True)), folder=self.folder)
        self.assertIsNone(install.executable(self.folder))
        self.assertFalse((self.folder / f'chrome-{install.VERSION}').exists())
        self.assertEqual(list(self.folder.glob('*.part')), [])

    def test_cancelling_stops_and_leaves_nothing(self):
        with self.assertRaises(install.InstallCancelled):
            install.ensure(FakeReporter(cancel_after=2), url=self.serve(browser_zip()), folder=self.folder, block=512)
        self.assertIsNone(install.executable(self.folder))
        self.assertEqual(list(self.folder.glob('*.part')), [])

    def test_an_older_version_is_removed_after_a_new_one_is_in(self):
        old = self.folder / 'chrome-1.0.0.0' / 'chrome-win64'
        old.mkdir(parents=True)
        (old / 'chrome.exe').write_bytes(b'old')
        (self.folder / 'profile').mkdir()
        install.ensure(None, url=self.serve(browser_zip()), folder=self.folder)
        self.assertFalse((self.folder / 'chrome-1.0.0.0').exists())
        self.assertTrue((self.folder / 'profile').exists())      # her profile is not a version

    def test_no_network_says_so_in_plain_words(self):
        with self.assertRaises(install.InstallError) as raised:
            install.ensure(None, url='http://127.0.0.1:9/chrome.zip', folder=self.folder)
        self.assertIn("Koli's browser", str(raised.exception))


# ── Input like a person ──────────────────────────────────────────────────────

class HumanInputTests(unittest.TestCase):
    def setUp(self):
        self.page = RecordingPage()
        self.sleeps = []
        self.hands = human.Hands(self.page, sleep=self.sleeps.append)

    def mouse(self):
        return [(p['type'], p.get('x'), p.get('y')) for m, p in self.page.calls if m == 'Input.dispatchMouseEvent']

    def test_a_click_travels_then_presses_and_releases_on_the_spot(self):
        self.hands.click(300, 200)
        events = self.mouse()
        self.assertGreaterEqual(sum(1 for e in events if e[0] == 'mouseMoved'), 18)
        self.assertEqual([e[0] for e in events[-3:]], ['mouseMoved', 'mousePressed', 'mouseReleased'])
        for kind, x, y in events[-3:]:
            self.assertEqual((x, y), (300, 200))
        pressed = [p for m, p in self.page.calls if p.get('type') == 'mousePressed'][0]
        self.assertEqual(pressed['button'], 'left')
        self.assertEqual(pressed['buttons'], 1)
        self.assertEqual(pressed['clickCount'], 1)

    def test_the_next_click_starts_where_the_last_one_ended(self):
        self.hands.click(300, 200)
        before = len(self.mouse())
        self.hands.click(320, 210)
        first_move = self.mouse()[before]
        self.assertLess(abs(first_move[1] - 300), 12)
        self.assertLess(abs(first_move[2] - 200), 12)

    def test_typing_sends_one_real_key_press_per_letter_with_shift_for_capitals(self):
        self.hands.type('Ko')
        keys = [(p['type'], p.get('key'), p.get('text'), p.get('modifiers', 0))
                for m, p in self.page.calls if m == 'Input.dispatchKeyEvent']
        self.assertEqual(keys, [
            ('rawKeyDown', 'Shift', None, 8),
            ('keyDown', 'K', 'K', 8),
            ('keyUp', 'K', None, 8),
            ('keyUp', 'Shift', None, 0),
            ('keyDown', 'o', 'o', 0),
            ('keyUp', 'o', None, 0),
        ])
        self.assertNotIn('Input.insertText', [m for m, _ in self.page.calls])

    def test_a_space_and_hebrew_are_typed_as_text(self):
        self.hands.type('a ק')
        downs = [p for m, p in self.page.calls if p.get('type') == 'keyDown']
        self.assertEqual([p['text'] for p in downs], ['a', ' ', 'ק'])
        self.assertEqual(downs[1]['code'], 'Space')

    def test_nothing_happens_faster_than_a_hand(self):
        self.hands.click(500, 400)
        self.hands.type('Koli')
        self.assertTrue(self.sleeps)
        self.assertGreaterEqual(min(self.sleeps), 0.012)

    def test_punctuation_is_text_with_no_key_code(self):
        # ord('.') is the Delete key's code and ord('%') the Left arrow's.
        self.hands.type('.%')
        downs = [p for m, p in self.page.calls if p.get('type') == 'keyDown']
        self.assertEqual([(p['text'], p['windowsVirtualKeyCode']) for p in downs], [('.', 0), ('%', 0)])

    def test_a_shortcut_is_a_letter_with_ctrl_and_alt_held(self):
        # Meet's Ctrl+Alt+H. Keys are not tied to the page's frames: at once on a hidden page.
        self.hands.shortcut('h', ctrl=True, alt=True)
        keys = [(m, p) for m, p in self.page.calls if m == 'Input.dispatchKeyEvent']
        self.assertEqual([p['type'] for _, p in keys], ['rawKeyDown', 'keyUp'])
        self.assertEqual({(p['modifiers'], p['key'], p['code'], p['windowsVirtualKeyCode']) for _, p in keys}, {(3, 'h', 'KeyH', 72)})

    def test_enter_is_a_key_press(self):
        self.hands.press('Enter')
        keys = [p for m, p in self.page.calls if m == 'Input.dispatchKeyEvent']
        self.assertEqual([p['type'] for p in keys], ['keyDown', 'keyUp'])
        self.assertEqual(keys[0]['windowsVirtualKeyCode'], 13)

    def test_insert_pastes_text_in_one_step(self):
        self.hands.insert('hello there')
        self.assertEqual(self.page.calls, [('Input.insertText', {'text': 'hello there'})])


if __name__ == '__main__':
    unittest.main()
