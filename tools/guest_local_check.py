# tools/guest_local_check.py
# A check with a real browser and no meeting: starts Koli's browser the way
# the Meet guest does, gives a stand-in call page the drawn board as its
# camera, and counts the video frames that go through a real WebRTC
# connection, first with the page shown and then hidden. With --board it
# saves pictures of the board in several shapes and languages instead.
#
#   python tools/guest_local_check.py [--seconds 20] [--board] [--exe PATH]
#
# The browser is the installed one (<data>/browser), --exe, or KOLI_BROWSER_EXE.

import argparse
import base64
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from koli.browser import cdp, install, process  # noqa: E402

CALL_PAGE = ROOT / 'docs' / 'research' / 'meet-participant-spike' / 'call-test.html'
BOARD_SCRIPT = ROOT / 'src' / 'koli' / 'meeting' / 'guest_board.js'
SPIKE_SCRIPT = ROOT / 'docs' / 'research' / 'meet-participant-spike' / 'camera-inject.js'

VIEWS = {
    'english': {'title': 'Koli', 'subtitle': 'AI notes · with Noa', 'topic': 'Pricing for the Pro plan and when the new tier ships',
                'decisions': ['Ship the Pro plan on Thursday', 'Keep the free tier at three projects'],
                'tasks': ['Eran sends the QA list by Wednesday', 'Dana updates the pricing page', 'Noa talks to the two pilot customers about the migration'],
                'questions': ['Who owns the migration guide?'], 'say': ''},
    'hebrew': {'title': 'Koli', 'subtitle': 'סיכום AI · עם נועה', 'topic': 'תמחור תוכנית Pro ומתי משיקים את המסלול החדש',
               'decisions': ['משיקים את תוכנית Pro ביום חמישי', 'המסלול החינמי נשאר עם שלושה פרויקטים'],
               'tasks': ['ערן שולח את רשימת ה-QA עד יום רביעי', 'דנה מעדכנת את עמוד המחירים', 'נועה מדבר עם שני לקוחות הפיילוט על המעבר'],
               'questions': ['מי אחראי על מדריך המעבר?'], 'say': ''},
    'say': {'title': 'Koli', 'subtitle': 'AI notes · with Noa', 'topic': 'Pricing for the Pro plan',
            'decisions': [], 'tasks': [], 'questions': [],
            'say': 'We decided to ship the Pro plan on Thursday and to keep the free tier at three projects.'},
}
SHAPES = [(1280, 720), (960, 720), (608, 720)]


def serve(page: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(page.read_bytes())
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def find_exe(given: str) -> Path:
    for candidate in (given, os.environ.get('KOLI_BROWSER_EXE')):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    exe = install.executable()
    if exe is None:
        raise SystemExit("No browser: install Koli's browser, or pass --exe / set KOLI_BROWSER_EXE")
    return exe


def frames(page, seconds: float) -> dict:
    before = page.evaluate('window.__call.stats()')
    time.sleep(seconds)
    after = page.evaluate('window.__call.stats()')
    return {'seconds': seconds, 'sent': after['sent'] - before['sent'], 'decoded': after['decoded'] - before['decoded'],
            'size': f"{after['width']}x{after['height']}", 'hidden': page.evaluate('document.hidden')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--board', action='store_true', help='save pictures of the board instead of counting frames')
    parser.add_argument('--exe', default='')
    parser.add_argument('--out', default=str(Path(tempfile.gettempdir()) / 'koli-board-check'))
    args = parser.parse_args()

    script = (BOARD_SCRIPT if BOARD_SCRIPT.is_file() else SPIKE_SCRIPT).read_text(encoding='utf-8')
    httpd = serve(CALL_PAGE)
    browser = process.BrowserProcess()
    profile = Path(tempfile.mkdtemp(prefix='koli-browser-check-'))
    connection = None
    try:
        port = browser.start(find_exe(args.exe), profile)
        connection = cdp.connect(port)
        page = cdp.first_page(connection)
        page.add_script(script)
        page.navigate(f'http://127.0.0.1:{httpd.server_address[1]}/')
        for _ in range(50):
            time.sleep(0.2)
            if page.evaluate('!!(window.__call && (window.__call.error || window.__call.ready))'):
                break
        error = page.evaluate('window.__call.error || null')
        if error:
            raise SystemExit(f'The call page failed: {error}')
        print('tracks:', page.evaluate('window.__call.tracks'))

        if args.board:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            for name, view in VIEWS.items():
                for width, height in SHAPES:
                    page.evaluate(f'window.__koli.resize({width}, {height}); window.__koli.board({json.dumps(view)})')
                    data = page.evaluate('window.__koli.picture()')
                    target = out / f'board-{name}-{width}x{height}.png'
                    target.write_bytes(base64.b64decode(data.split(',', 1)[1]))
                    print('saved', target)
            return

        time.sleep(3)
        print('shown: ', frames(page, args.seconds))
        window = connection.send('Browser.getWindowForTarget', {'targetId': page.target_id})
        connection.send('Browser.setWindowBounds', {'windowId': window['windowId'], 'bounds': {'windowState': 'minimized'}})
        time.sleep(2)
        print('hidden:', frames(page, args.seconds))
    finally:
        browser.stop(close_browser=(lambda: connection.send('Browser.close', timeout=3)) if connection else None)
        if connection:
            connection.close()
        httpd.shutdown()


if __name__ == '__main__':
    main()
