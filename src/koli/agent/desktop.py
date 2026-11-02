# agent/desktop.py
# Koli's hands in other apps: the agent-desktop program
# (github.com/vercel-labs/agent-desktop) reads any app's buttons, fields and
# menus through Windows UI Automation (the accessibility tree on macOS and
# Linux) and presses them without moving the mouse or taking focus. Here:
# finding the program, running one command and reading its JSON answer,
# matching a spoken app name to a running app, finding a control, doing one
# step for real, and what the AI sees of an app each round of the step-by-step
# loop (its controls, numbered, and a picture of its window). Koli never
# passes --headed: nothing here types keys or moves the pointer.

import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional

RUN_SECONDS = 15.0
FIND_TIMEOUT_MS = 8000        # Chromium and Electron apps read thin before they settle
APPEAR_SECONDS = 6.0          # how long a step waits for a control an earlier step makes appear
APPEAR_POLL_S = 0.5
FIND_LIMIT = 50
SEE_LIMIT = 150               # controls the AI sees of an app in one round
PICTURE_WIDTH = 1280          # a window's picture is made at most this wide before it is sent
INSTALL_HINT = 'npm install -g agent-desktop'
UNCONFIRMED = "the app didn't confirm it"
# A ref the program no longer knows: find the control again and try once more.
# Only these, and only when the program says nothing was delivered.
REFIND_CODES = ('STALE_REF', 'SNAPSHOT_NOT_FOUND')

# The step names Koli uses → the program's commands.
COMMANDS = {'click': 'click', 'set_text': 'set-value', 'toggle': 'toggle', 'select': 'select',
            'check': 'check', 'uncheck': 'uncheck'}


class DesktopError(Exception):
    """agent-desktop refused or failed; the message is for the bubble."""


# ── Finding and running the program ──────────────────────────────────────────

_program: Optional[str] = None


# The native program inside the npm package (no Node launcher in between:
# about 125 ms a call less), or whatever "agent-desktop" is on the PATH.
def program() -> Optional[str]:
    global _program
    if _program is None:
        _program = _locate() or ''
    return _program or None


def _locate() -> Optional[str]:
    system = {'win32': 'win32', 'darwin': 'darwin'}.get(sys.platform, 'linux')
    arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
    suffix = '.exe' if system == 'win32' else ''
    roots = []
    if system == 'win32' and os.environ.get('APPDATA'):
        roots.append(os.path.join(os.environ['APPDATA'], 'npm', 'node_modules'))
    else:
        roots += ['/opt/homebrew/lib/node_modules', '/usr/local/lib/node_modules',
                  os.path.expanduser('~/.npm-global/lib/node_modules')]
    for root in roots:
        exe = os.path.join(root, 'agent-desktop', 'bin', f'agent-desktop-{system}-{arch}{suffix}')
        if os.path.isfile(exe):
            return exe
    return shutil.which('agent-desktop')


def _failure(code: str, message: str) -> dict:
    return {'ok': False, 'error': {'code': code, 'message': message,
                                   'disposition': {'delivery': 'not_delivered', 'retry': 'safe'}}}


# One command → its JSON envelope ({'ok', 'data'} or {'ok': False, 'error'}).
# Never raises: a missing program or a timeout is an envelope too.
def run(args: List[str], timeout: float = RUN_SECONDS) -> dict:
    exe = program()
    if not exe:
        return _failure('NOT_INSTALLED', f'agent-desktop is not installed ({INSTALL_HINT})')
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        done = subprocess.run([exe, *[str(a) for a in args]], capture_output=True, timeout=timeout,
                              creationflags=flags, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        # It may have acted: never "safe" to repeat.
        return {'ok': False, 'error': {'code': 'TIMEOUT', 'message': f'it took longer than {int(timeout)} s',
                                       'disposition': {'delivery': 'unknown', 'retry': 'unsafe'}}}
    except OSError as e:
        return _failure('NOT_RUN', str(e))
    try:
        envelope = json.loads(done.stdout.decode('utf-8', errors='replace'))
    except ValueError:
        return _failure('BAD_ANSWER', done.stderr.decode('utf-8', errors='replace').strip()[:200] or 'no answer')
    return envelope if isinstance(envelope, dict) else _failure('BAD_ANSWER', 'no answer')


def error_of(envelope: dict) -> dict:
    return envelope.get('error') or {} if not envelope.get('ok') else {}


def message_of(envelope: dict) -> str:
    error = error_of(envelope)
    return error.get('message') or error.get('code') or 'it failed'


def _safe_to_repeat(envelope: dict) -> bool:
    return (error_of(envelope).get('disposition') or {}).get('retry') == 'safe'


# ── Apps ─────────────────────────────────────────────────────────────────────

# The running apps' names as the program knows them, without ".exe"
# ("WhatsApp.Root", "chrome", "Notepad").
def running_apps(call: Callable[[List[str]], dict]) -> List[str]:
    envelope = call(['list-apps'])
    if not envelope.get('ok'):
        raise DesktopError(message_of(envelope))
    names = []
    for app in (envelope.get('data') or {}).get('apps') or []:
        name = str(app.get('name') or '')
        name = name[:-4] if name.lower().endswith('.exe') else name
        if name and name not in names:
            names.append(name)
    return names


# The running app a spoken name means: "whatsapp" is "WhatsApp.Root",
# "word" is "WINWORD" only by its own words, so the name's words first, then
# its start, then a near spelling. None when nothing running fits.
def match_app(spoken: str, names: List[str]) -> Optional[str]:
    wanted = ' '.join(str(spoken or '').lower().replace('.exe', '').split())
    if not wanted:
        return None
    keyed = {n.lower(): n for n in names}
    if wanted in keyed:
        return keyed[wanted]
    words = lambda n: n.replace('.', ' ').replace('-', ' ').replace('_', ' ').split()
    by_word = [n for n in keyed if wanted in words(n) or wanted.replace(' ', '') in words(n)]
    starts = [n for n in keyed if n.startswith(wanted.replace(' ', ''))] if len(wanted) >= 3 else []
    close = difflib.get_close_matches(wanted.replace(' ', ''), list(keyed), n=1, cutoff=0.8)
    # "google chrome" is the process "chrome": one of the spoken words.
    spoken_words = [w for w in wanted.split() if len(w) >= 3]
    any_word = [n for n in keyed if any(w in words(n) for w in spoken_words)] if len(spoken_words) > 1 else []
    match = (sorted(by_word, key=len) or sorted(starts, key=len) or close or sorted(any_word, key=len) or [None])[0]
    return keyed[match] if match else None


# ── Controls ─────────────────────────────────────────────────────────────────

# Runs an observation of `app` (`args` without --app). Two windows of the
# app (two Notepads) make the program ask which: the focused one, else the
# first it lists (the one on top). `window`: a window chosen before, kept so
# the real run looks in the same one. Returns (envelope, window id or None).
def _observe(call, args: List[str], app: str, window: Optional[str] = None) -> tuple:
    target = ['--window-id', window] if window else ['--app', app]
    envelope = call([args[0], *target, *args[1:]])
    error = error_of(envelope)
    if not window and error.get('code') == 'AMBIGUOUS_TARGET':
        candidates = (error.get('details') or {}).get('candidates') or []
        pick = next((c for c in candidates if c.get('is_focused')), candidates[0] if candidates else None)
        if pick and pick.get('id'):
            window = pick['id']
            envelope = call([args[0], '--window-id', window, *args[1:]])
            error = error_of(envelope)
    if not envelope.get('ok'):
        if error.get('code') in ('APP_NOT_FOUND', 'WINDOW_NOT_FOUND'):
            raise DesktopError(f"{app} has no open window")
        raise DesktopError(message_of(envelope))
    return envelope, window


# Controls of `app` by exact name (and role), or containing `text`: the
# program's matches ({'ref_id', 'role', 'name', 'value', 'states'}, and
# 'window' when the app has several and one was chosen).
def find(call: Callable[[List[str]], dict], app: str, name: str = None, role: str = None, text: str = None,
         exact: bool = True, limit: int = FIND_LIMIT, window: Optional[str] = None) -> List[dict]:
    args = ['find', '--timeout-ms', str(FIND_TIMEOUT_MS), '--limit', str(limit)]
    if name:
        args += ['--name', name] + (['--exact'] if exact else [])
    if text:
        args += ['--text', text]
    if role:
        args += ['--role', role]
    envelope, window = _observe(call, args, app, window)
    data = envelope.get('data') or {}
    matches = data.get('matches') if 'matches' in data else [data['match']] if data.get('match') else []
    return [{**m, 'window': window} if window else m for m in matches or [] if m.get('ref_id')]


# Controls you could reach now: scrolled out of view (Spotify's eight "Play"
# buttons on cards below the fold) or greyed out don't count.
def _usable(control: dict) -> bool:
    states = control.get('states') or []
    return 'offscreen' not in states and 'disabled' not in states


# Of several matches, the ones on screen when there are any: the player's
# button over a card's button scrolled away.
def _on_screen_first(matches: List[dict]) -> List[dict]:
    if len(matches) < 2:
        return matches
    return [m for m in matches if _usable(m)] or matches


# One control by its name: the exact name first, then a name containing it
# when that gives exactly one. [] or several are for the caller to judge.
def find_control(call, app: str, name: str, role: str = None, window: Optional[str] = None) -> List[dict]:
    found = find(call, app, name=name, role=role, exact=True, window=window)
    if found:
        return _on_screen_first(found)
    return _on_screen_first([m for m in find(call, app, text=name, role=role, window=window)
                             if m.get('interactive', True)])


# ── Seeing an app (the step-by-step loop, agent/app_steps.py) ────────────────

# What the AI sees of an app in one round: its controls, numbered, and a
# picture of its window. Controls on screen come first; one scrolled out of
# view is listed only when its label has words of the request ("Daily Mix 6"
# below the fold), a greyed-out one never. Each is {'n', 'role', 'name', 'ref',
# 'value', 'states'}; `window` is the window looked at (two Notepads: the
# focused one), `image` {'mime', 'data'} or None. Reading the controls and
# taking the picture run side by side: each takes 1.5-3 s in a Chromium app.
def see(call, app: str, words: List[str] = (), picture: bool = True, window: Optional[str] = None) -> dict:
    shot = {}

    def take_picture():
        shot['envelope'] = call(['screenshot', *(['--window-id', window] if window else ['--app', app])])
    painter = threading.Thread(target=take_picture, daemon=True) if picture else None
    if painter:
        painter.start()
    envelope, window = _observe(call, ['snapshot', '-i', '--compact', '--timeout-ms', str(FIND_TIMEOUT_MS)],
                                app, window)
    controls = _numbered((envelope.get('data') or {}).get('tree'), words)
    image = None
    if painter:
        painter.join(RUN_SECONDS)
        shot_envelope = shot.get('envelope') or {}
        if error_of(shot_envelope).get('code') == 'AMBIGUOUS_TARGET' and window:
            # Two windows: the picture of the one whose controls were read.
            shot_envelope = call(['screenshot', '--window-id', window])
        image = _small_picture(((shot_envelope.get('data') or {}) if shot_envelope.get('ok') else {}).get('data'))
    return {'app': app, 'window': window, 'controls': controls, 'image': image}


# Out of view, a label needs two of the request's words (one when it has only
# one): "Daily Mix 5" for "play daily mix 5", not every card's hidden "Play".
def _numbered(tree, words) -> List[dict]:
    wanted = list(dict.fromkeys(w for w in (str(w).lower() for w in words) if len(w) >= 3))
    needed = min(2, len(wanted))
    on_screen, hidden = [], []

    def walk(node):
        if not isinstance(node, dict):
            return
        name = ' '.join(str(node.get('name') or '').split())[:100]
        if node.get('ref_id') and name:
            entry = {'role': node.get('role') or 'control', 'name': name, 'ref': node['ref_id'],
                     'value': node.get('value'), 'states': node.get('states') or []}
            if _usable(node):
                on_screen.append(entry)
            elif needed and 'disabled' not in entry['states'] \
                    and sum(w in name.lower() for w in wanted) >= needed:
                hidden.append(entry)
        for child in node.get('children') or []:
            walk(child)
    walk(tree)
    controls = (on_screen + hidden)[:SEE_LIMIT]
    for n, entry in enumerate(controls, 1):
        entry['n'] = n
    return controls


# A window's PNG (base64) made small enough to send each round: at most
# PICTURE_WIDTH wide, as JPEG. The PNG as it is when Pillow can't read it.
def _small_picture(data: Optional[str]) -> Optional[dict]:
    if not data:
        return None
    try:
        import base64
        import io
        from PIL import Image
        picture = Image.open(io.BytesIO(base64.b64decode(data))).convert('RGB')
        if picture.width > PICTURE_WIDTH:
            picture = picture.resize((PICTURE_WIDTH, round(picture.height * PICTURE_WIDTH / picture.width)))
        out = io.BytesIO()
        picture.save(out, 'JPEG', quality=70)
        return {'mime': 'image/jpeg', 'data': base64.b64encode(out.getvalue()).decode('ascii')}
    except Exception:
        return {'mime': 'image/png', 'data': data}


# Other names some apps go by when spoken ("edge" is the process "msedge").
SPOKEN_NAMES = {'msedge': ['edge', 'microsoft edge'], 'winword': ['word'], 'powerpnt': ['powerpoint'],
                'whatsapp.root': ['whatsapp'], 'olk': ['outlook'], 'ms-teams': ['teams']}


# The open app a sentence names, by its whole name or first word ("Spotify",
# "notepad", "WhatsApp" for "WhatsApp.Root") or a spoken name above; None when
# it names none. Stricter than match_app: "write a note to Dana" names no app.
def named_app(sentence: str, names: List[str]) -> Optional[str]:
    text = f" {' '.join(re.sub(r'[^\w]+', ' ', (sentence or '').lower()).split())} "
    for name in sorted(names, key=len, reverse=True):
        key = name.lower()
        first = re.split(r'[\s._-]+', key)[0]
        spoken = [key.replace('.', ' ')] + ([first] if len(first) >= 4 else []) + SPOKEN_NAMES.get(key, [])
        if any(f" {word} " in text for word in spoken):
            return name
    return None


# ── A step for real ──────────────────────────────────────────────────────────

# Waits for a control to appear (a menu item after the menu opened): exactly
# one match, or [] / several when time runs out.
def wait_for_control(call, app: str, name: str, role: str = None, seconds: float = APPEAR_SECONDS,
                     sleep: Callable[[float], None] = time.sleep, window: Optional[str] = None) -> List[dict]:
    deadline = time.monotonic() + seconds
    found: List[dict] = []
    while True:
        try:
            found = find_control(call, app, name, role, window=window)
        except DesktopError:
            found = []
        if len(found) == 1 or time.monotonic() >= deadline:
            return found
        sleep(APPEAR_POLL_S)


# The running app a spoken name means, waiting for it to start (up to 10 s).
def wait_for_app(call, spoken: str, seconds: float = 10.0, sleep: Callable[[float], None] = time.sleep):
    deadline = time.monotonic() + seconds
    while True:
        try:
            app = match_app(spoken, running_apps(call))
        except DesktopError:
            app = None
        if app or time.monotonic() >= deadline:
            return app
        sleep(APPEAR_POLL_S)


# Runs one recorded step ({'op': 'ui', 'action', 'app', 'name', 'role', 'ref',
# 'text', 'option'}). Returns (status, detail): 'done', 'skipped' (the control
# isn't there, nothing was pressed) or 'failed'. A step that may have landed
# is never repeated: only a stale ref the program says it didn't use is
# found again and tried once more.
def perform(op: dict, call, sleep: Callable[[float], None] = time.sleep) -> tuple:
    if op.get('pending_app'):
        # Opened by an earlier step: its running name, once it is there. The
        # op keeps it, so the history and Undo name the real app.
        app = wait_for_app(call, op['app'], sleep=sleep)
        if app is None:
            return 'skipped', f"{op['app']} didn't open"
        op['app'] = app
        op.pop('pending_app', None)
    action, app = op['action'], op['app']
    if action == 'close_app':
        envelope = call(['close-app', app])
        return ('done', '') if envelope.get('ok') else ('failed', message_of(envelope))
    extra = {'set_text': [op.get('text', '')], 'select': [op.get('option', '')]}.get(action, [])
    command = COMMANDS[action]
    envelope = call([command, '--', op['ref'], *extra]) if op.get('ref') else None
    if envelope is not None and not envelope.get('ok'):
        if error_of(envelope).get('code') in REFIND_CODES and _safe_to_repeat(envelope):
            envelope = None
    if envelope is None:
        found = wait_for_control(call, app, op['name'], op.get('role'), sleep=sleep, window=op.get('window'))
        if not found:
            return 'skipped', f"“{op['name']}” isn't in {app}"
        if len(found) > 1:
            return 'skipped', f"{len(found)} controls in {app} are called “{op['name']}”"
        envelope = call([command, '--', found[0]['ref_id'], *extra])
    if envelope.get('ok'):
        return 'done', ''
    # It landed, but reading it back didn't show the change (a Notepad menu
    # item says nothing about being on): done, and the bubble says so.
    if (error_of(envelope).get('details') or {}).get('kind') == 'post_action_verification':
        return 'done', UNCONFIRMED
    return 'failed', message_of(envelope)
