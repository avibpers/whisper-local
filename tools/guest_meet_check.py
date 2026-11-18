# tools/guest_meet_check.py
# Koli's Meet guest against the real Google Meet, without running the app.
# By default it stops before Join: it opens her browser, loads the call's
# pre-join page, runs the guest's own steps (continue as guest, the sign-in
# hint, the name typed key by key) and says what it found. Nobody is joined.
# With --join she really joins, stays a while, writes one chat line and
# leaves: everyone in the call sees her, so use a call of your own.
#
#   python tools/guest_meet_check.py <meet code or link> [--join] [--seconds 60] [--name Koli]
#
# The browser is the installed one (<data>/browser), --exe, or KOLI_BROWSER_EXE.

import argparse
import base64
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from koli.browser import human, install  # noqa: E402
from koli.meeting import guest as g  # noqa: E402

VIEW = {'title': 'Koli', 'subtitle': 'AI notes · with you', 'topic': 'A check of Koli in the call',
        'decisions': ['The board is her camera'], 'tasks': ['Look at this tile (you)'], 'questions': [], 'say': ''}


def find_exe(given: str) -> Path:
    for candidate in (given, os.environ.get('KOLI_BROWSER_EXE')):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    exe = install.executable()
    if exe is None:
        raise SystemExit("No browser: install Koli's browser, or pass --exe / set KOLI_BROWSER_EXE")
    return exe


class Installer:
    """The guest's installer, pointed at one program and a profile of this check's own."""

    def __init__(self, exe: Path):
        self.exe = exe
        self.profile = Path(tempfile.mkdtemp(prefix='koli-guest-check-'))

    def supported(self):
        return True

    def executable(self):
        return self.exe

    def profile_dir(self):
        return self.profile

    def ensure(self, reporter=None):
        return self.exe


def said(state, detail='', progress=None):
    print(f"  [{time.strftime('%H:%M:%S')}] {state}: {detail}")


# The steps before Join, with the guest's own probes and hands.
def dry_run(guest, code: str, picture: Path):
    session = guest._session_factory()
    try:
        session.open()
        page = session.page
        guest._hands = human.Hands(page)
        page.add_script(guest._board_script())
        page.navigate(f'{g.MEET_ORIGIN}/{code}?hl=en')
        try:
            box = guest._until(lambda: guest._name_box(page), g.NAME_BOX_WAIT_S, 'no name box')
        except g._Refused:
            print("Meet refused at load (\"You can't join this video call\").")
            return
        except g._Failed:
            print('No name box. The page says:', page.evaluate("document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 200)"))
            return
        print('continue as guest:', 'pressed' if guest._asked_as_guest else 'not offered')
        time.sleep(g.SETTLE_S)
        hint = page.evaluate(g.GOT_IT)
        print('sign-in hint:     ', 'dismissed' if hint else 'not shown')
        if hint:
            guest._hands.click(hint['x'], hint['y'])
            time.sleep(g.AFTER_HINT_S)
        box = page.evaluate(g.NAME_BOX) or box
        guest._hands.click(box['x'], box['y'])
        time.sleep(g.AFTER_CLICK_S)
        guest._hands.type(guest.name)
        time.sleep(g.AFTER_TYPING_S)
        print('name in the box:  ', repr(page.evaluate(g.NAME_VALUE)))
        print('join button:      ', page.evaluate(g.JOIN) or 'not found, or disabled')
        print('camera Meet sees: ', page.evaluate("[...document.querySelectorAll('button')].map(b => b.innerText.trim()).filter(t => /Board|Voice/.test(t)).join(' / ')"))
        picture.write_bytes(base64.b64decode(page.send('Page.captureScreenshot', {'format': 'png'})['data']))
        print('picture:          ', picture)
        print('Stopped before Join: nobody was joined.')
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('call', help='a Meet code (abc-defg-hij) or link')
    parser.add_argument('--join', action='store_true', help='really join: everyone in the call sees her')
    parser.add_argument('--seconds', type=float, default=60, help='how long she stays with --join')
    parser.add_argument('--name', default='Koli')
    parser.add_argument('--exe', default='')
    args = parser.parse_args()
    found = re.search(r'([a-z]{3,4}-[a-z]{4}-[a-z]{3,4})', args.call.lower())
    code = found.group(1) if found else args.call.strip().lower()

    installer = Installer(find_exe(args.exe))
    guest = g.MeetGuest(on_state=said, name=args.name, installer=installer,
                        announce=f"Hi, I'm {args.name}, an AI assistant. This is a short test." if args.join else '')
    guest.show({**VIEW, 'title': args.name})
    if not args.join:
        dry_run(guest, code, Path(tempfile.gettempdir()) / 'koli-guest-check.png')
        return
    try:
        if not guest.join_now(code, 'you'):
            return
        print('  her microphone button:', guest._session.page.evaluate(g.MIC))
        end = time.time() + args.seconds
        step = 0
        while time.time() < end and guest.in_call:
            time.sleep(g.TICK_S)
            guest.tick()
            step += 1
            if step == 2:
                guest.show({**VIEW, 'title': args.name, 'topic': 'The board changed while she is in the call'})
            if step == 4:
                print('  chat message sent:', guest.say('A message from Koli, written while her page is hidden.'))
    finally:
        guest.leave()


if __name__ == '__main__':
    main()
