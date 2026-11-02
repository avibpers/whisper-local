# agent/actions.py
# Koli's actions, the only things an AI-written script can call. A script runs
# once, in rehearsal: the Recorder finds files for real (read-only) and writes
# down every change it would make as an operation, without touching anything.
# replay() then runs that list for real, checking each source again, and
# returns what each step did with what undoes it. Nothing here overwrites a
# file: a taken name becomes "name (2)".

import difflib
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

MAX_RESULTS = 500
MAX_DEPTH = 3
FIND_SECONDS = 3.0
MAX_OPEN = 10
MAX_SYSTEM = 5            # system commands in one request
PLAIN_VALUE = re.compile(r'[\w .,:+\-]{1,200}')   # what may fill {before} in an undo
MISSING_LIKE = 0.5        # how alike a real folder's name must be to offer it ("API" → "avi")
MAX_WEIGHT_COUNT = 1000
# Calls into agent-desktop while rehearsing one request (each can take 2-3 s in
# a Chromium app; a ui() whose exact label misses makes two).
MAX_UI_READS = 24
MAX_UI_STEPS = 20         # steps in other apps in one request
MAX_UNDO_TEXT = 2000      # a field's old text kept so Undo can put it back

KINDS = {
    'image': {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic', '.tif', '.tiff', '.svg', '.raw', '.cr2', '.nef'},
    'video': {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.wmv', '.m4v'},
    'audio': {'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.wma', '.opus'},
    'document': {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.rtf', '.odt', '.csv', '.md'},
    'archive': {'.zip', '.rar', '.7z', '.tar', '.gz'},
}
# Opening one of these runs it.
EXECUTABLE = {'.exe', '.bat', '.cmd', '.ps1', '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh', '.msi', '.msc',
              '.lnk', '.scr', '.com', '.reg', '.pif', '.cpl', '.hta', '.jar', '.url', '.appref-ms', '.py', '.pyw'}
# What open() opens: an allow-list, since any unknown extension may be tied
# to a program that runs it (.py opens in the Python launcher).
OPENABLE = set().union(*KINDS.values()) | {'.html', '.htm', '.json', '.log', '.xml', '.epub'}
OPEN_REFUSED = "Koli opens documents, pictures, music, videos, web pages and folders, not {name}"
KNOWN_FOLDERS = ('downloads', 'desktop', 'documents', 'pictures', 'music', 'videos', 'home')
# Why other apps can't be used, by Backend.desktop_missing().
DESKTOP_MISSING = {
    'off': "Using other apps is turned off (Settings › AI › AI actions › Use other apps)",
    'not installed': "Koli can't use other apps yet: install agent-desktop (npm install -g agent-desktop)",
}


class ActionError(Exception):
    """Something a script asked for that can't be done; the message is shown."""


# ── Records ──────────────────────────────────────────────────────────────────

class Item:
    # A file or folder. Scripts may read only these fields (sandbox.ATTRS).
    __slots__ = ('name', 'path', 'ext', 'size', 'modified', 'age_days', 'folder', 'is_dir')

    def __init__(self, path: str, is_dir: bool, stat=None):
        p = Path(path)
        self.path = str(p)
        self.name = p.name
        self.ext = '' if is_dir else p.suffix.lower()
        self.folder = str(p.parent)
        self.is_dir = is_dir
        mtime = stat.st_mtime if stat is not None else time.time()
        self.size = 0 if is_dir or stat is None else int(stat.st_size)
        self.modified = datetime.fromtimestamp(mtime).date().isoformat()
        self.age_days = round((time.time() - mtime) / 86400, 1)

    def __repr__(self):
        return f"{'Folder' if self.is_dir else 'File'}({self.path!r})"


# Roles whose on/off state agent-desktop reports ('checked' among the states).
TOGGLE_ROLES = ('checkbox', 'switch', 'togglebutton', 'radiobutton')


class Element:
    # A button, field, menu item... in another app. Scripts may read only
    # .name, .role, .value and .app (sandbox.ATTRS). `ref` is the program's
    # handle on it now; `found` is False for one a step before it makes
    # appear (a menu item after "File"), looked up when the steps really run.
    # `window`: which of the app's windows, when it has several. `.checked`:
    # True or False for a checkbox or switch, None when the control doesn't
    # say (Notepad's menu items don't).
    __slots__ = ('app', 'name', 'role', 'value', 'ref', 'found', 'window', 'checked')

    def __init__(self, app: str, name: str, role: str = '', value=None, ref: str = '', found: bool = True,
                 window: Optional[str] = None, states=None):
        self.app, self.name, self.role, self.ref, self.found = app, name, role or '', ref or '', found
        self.value = value if isinstance(value, str) else None
        self.window = window
        states = [str(s).lower() for s in states or []]
        self.checked = True if 'checked' in states else False if self.role in TOGGLE_ROLES else None

    def __repr__(self):
        return f"Element({self.app!r}, {self.role or 'control'} {self.name!r})"


def can_open(path: str) -> bool:
    return os.path.isdir(path) or (Path(path).suffix.lower() in OPENABLE and Path(path).suffix.lower() not in EXECUTABLE)


def _items(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(_items(v))
        return out
    return [value]


# ── The platform ─────────────────────────────────────────────────────────────

class Backend:
    # What the actions need from the system; tests pass a fake.
    # `desktop_enabled()`: the setting "Use other apps"; `pictures_enabled()`:
    # "Show the AI the app's screen" (and the model takes pictures).
    def __init__(self, desktop_enabled: Callable[[], bool] = lambda: True,
                 pictures_enabled: Callable[[], bool] = lambda: True):
        self.desktop_enabled = desktop_enabled
        self.pictures_enabled = pictures_enabled

    def known_folder(self, name: str) -> Optional[str]:
        from ..platform import files
        return files.known_folder(name)

    def apps(self) -> dict:
        from ..platform import files
        return files.start_menu_apps()

    def open_path(self, target: str):
        from ..platform import files
        files.open_path(target)

    def start_app(self, shortcut: str):
        from ..platform import files
        files.start_app(shortcut)

    def to_recycle_bin(self, paths: List[str]) -> bool:
        from ..platform import files
        return files.to_recycle_bin(paths)

    def can_recycle(self, path: str) -> bool:
        from ..platform import files
        return files.can_recycle(path)

    def open_recycle_bin(self):
        from ..platform import files
        files.open_recycle_bin()

    def reveal(self, path: str):
        from ..platform import files
        files.reveal(path)

    # (ok, output) of a system command the AI wrote (agent/system.py).
    def run_system(self, command: str) -> tuple:
        from . import system
        return system.run(command)

    # Other apps (agent/desktop.py): '' when Koli may use them, else why not.
    def desktop_missing(self) -> str:
        from . import desktop
        if not self.desktop_enabled():
            return 'off'
        return '' if desktop.program() else 'not installed'

    # One agent-desktop command → its JSON envelope.
    def desktop(self, args: List[str]) -> dict:
        from . import desktop
        return desktop.run(args)


# ── Rehearsal ────────────────────────────────────────────────────────────────

class Recorder:
    # Runs the actions in rehearsal. `ops` is the list of operations, `said`
    # what the script wants shown, `notes` what Koli found it needn't do (a
    # folder that's already there), `trace` each call with its literal
    # arguments (files left out), for telling repeats apart.
    # `command_exists(phrase)`: whether a fixed voice command has that phrase.
    # `previous`: where the last action put things, for last(). `aliases`:
    # folder names you confirmed ({'api': 'avi'}); `missing`: folders looked
    # for and not found, each with the closest real one (_note_missing).
    def __init__(self, backend: Backend, command_exists: Callable[[str], bool] = lambda p: False,
                 previous: List[str] = (), aliases: Optional[dict] = None):
        self.backend = backend
        self.command_exists = command_exists
        self.previous = list(previous or [])
        self.aliases = {str(k).lower(): v for k, v in (aliases or {}).items()}
        self.missing: List[dict] = []
        self.ops: List[dict] = []
        self.said: List[str] = []
        self.notes: List[str] = []
        self.trace: List[tuple] = []
        self._planned_dirs = set()
        self._taken = {}          # folder → names the plan will create there
        self._apps = None
        self._running = None      # apps open now, from agent-desktop
        self._ui_reads = 0
        # Seconds spent waiting for agent-desktop: not the script's own time
        # (sandbox.MAX_SECONDS), since one look into Chrome takes 2-3 s.
        self.waited = 0.0

    def functions(self) -> dict:
        names = ('folder', 'find_files', 'find_folders', 'newest', 'oldest', 'largest', 'last', 'open', 'open_app',
                 'copy', 'move', 'rename', 'make_folder', 'delete', 'system', 'run_command', 'say',
                 'apps_open', 'app_is_open', 'ui', 'ui_find', 'ui_click', 'ui_set_text', 'ui_toggle', 'ui_check', 'ui_select',
                 'close_app')
        return {n: self._traced(n, getattr(self, n)) for n in names}

    # say() isn't traced: its text carries counts ("Moved 12 photos") that
    # differ every run and would make the same plan look new each time.
    def _traced(self, name, fn):
        def call(*args, **kwargs):
            if name != 'say':
                literal = lambda v: v if isinstance(v, (str, int, float, bool, type(None))) else '<items>'
                self.trace.append((name, tuple(literal(a) for a in args),
                                   tuple(sorted((k, literal(v)) for k, v in kwargs.items()))))
            return fn(*args, **kwargs)
        return call

    # Your known folders, your home folder and drive roots are where Koli
    # works, never what it changes: renaming or moving Documents breaks
    # Windows' own links to it.
    def _protected(self) -> set:
        if getattr(self, '_protected_paths', None) is None:
            paths = set()
            for name in KNOWN_FOLDERS:
                try:
                    path = self.backend.known_folder(name)
                except Exception:
                    path = None
                if path:
                    paths.add(os.path.normcase(os.path.abspath(path)))
            self._protected_paths = paths
        return self._protected_paths

    def _guard(self, item: 'Item') -> 'Item':
        path = os.path.normcase(os.path.abspath(item.path))
        if path in self._protected() or os.path.dirname(path) == path:
            raise ActionError(f"Koli works inside your folders, not on {item.name or item.path} itself")
        return item

    # ── Where ──

    def folder(self, name: str) -> Item:
        name = str(name or '').strip()
        key = name.lower()
        if key in KNOWN_FOLDERS:
            path = self.backend.known_folder(key)
            if not path:
                raise ActionError(f"Can't find your {name} folder")
            return self._dir_item(path)
        if len(name) == 2 and name[1] == ':' and name[0].isalpha():
            return self._dir_item(name.upper() + os.sep)
        raise ActionError(f"Unknown folder “{name}”: use downloads, desktop, documents, pictures, music, videos, home or a drive like D:")

    def _dir(self, where) -> str:
        if isinstance(where, Item):
            if not where.is_dir:
                raise ActionError(f"“{where.name}” is a file, not a folder")
            return where.path
        text = str(where or '').strip()
        if text.lower() in KNOWN_FOLDERS or (len(text) == 2 and text[1] == ':'):
            return self.folder(text).path
        path = os.path.abspath(os.path.expanduser(text))
        alias = self.aliases.get(os.path.basename(path).lower())
        if alias:
            path = os.path.join(os.path.dirname(path), alias)
        if not (os.path.isdir(path) or os.path.normcase(path) in self._planned_dirs):
            self._note_missing(os.path.dirname(path), os.path.basename(path))
            raise ActionError(f"There's no folder {text}")
        return path

    def _dir_item(self, path: str) -> Item:
        try:
            return Item(path, True, os.stat(path))
        except OSError:
            if os.path.normcase(path) in self._planned_dirs:
                return Item(path, True)
            raise ActionError(f"There's no folder {path}")

    # ── Finding ──

    def _walk(self, root: str, subfolders: bool, want_dirs: bool, accept) -> List[Item]:
        out, started = [], time.monotonic()
        stack = [(root, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if time.monotonic() - started > FIND_SECONDS or len(out) >= MAX_RESULTS:
                    return out
                if entry.name.startswith(('.', '$')) or entry.name.lower() in ('desktop.ini', 'thumbs.db'):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir and subfolders and depth + 1 < MAX_DEPTH:
                    stack.append((entry.path, depth + 1))
                if is_dir == want_dirs:
                    item = Item(entry.path, is_dir, stat)
                    if accept(item):
                        out.append(item)
        return out

    def find_files(self, where, kind=None, name_contains=None, ext=None, newer_than_days=None,
                   older_than_days=None, subfolders=False) -> List[Item]:
        root = self._dir(where)
        kinds = KINDS.get(str(kind).lower()) if kind else None
        if kind and kinds is None:
            raise ActionError(f"Unknown kind “{kind}”: image, video, audio, document or archive")
        exts = {('.' + str(e).lower().lstrip('.')) for e in _items(ext)} if ext else None
        needle = str(name_contains).lower() if name_contains else None

        def accept(item: Item) -> bool:
            return ((kinds is None or item.ext in kinds) and (exts is None or item.ext in exts)
                    and (needle is None or needle in item.name.lower())
                    and (newer_than_days is None or item.age_days <= float(newer_than_days))
                    and (older_than_days is None or item.age_days >= float(older_than_days)))
        return self._walk(root, bool(subfolders), False, accept)

    def find_folders(self, where, name_contains=None, subfolders=False) -> List[Item]:
        root = self._dir(where)
        needle = str(name_contains).lower() if name_contains else None
        needle = self.aliases.get(needle, needle) if needle else None
        found = self._walk(root, bool(subfolders), True, lambda i: needle is None or needle in i.name.lower())
        if needle and not found:
            self._note_missing(root, str(name_contains))
        return found

    # ── Names that weren't found ──
    # "Move the pictures to API" when the folder is "avi" (Whisper wrote
    # "API"): the closest real folder is noted, and the runner asks "Did you
    # mean avi?". A yes comes back as an alias, and the rehearsal runs again.

    def _note_missing(self, parent: str, name: str) -> None:
        if any(m['name'].lower() == name.lower() for m in self.missing):
            return
        try:
            names = [e.name for e in os.scandir(parent) if e.is_dir() and not e.name.startswith(('.', '$'))]
        except OSError:
            return
        match = difflib.get_close_matches(name.lower(), [n.lower() for n in names], n=1, cutoff=MISSING_LIKE)
        if match:
            closest = next(n for n in names if n.lower() == match[0])
            self.missing.append({'name': name, 'where': parent, 'closest': closest})

    @staticmethod
    def _pick(items, count, key, reverse) -> object:
        ranked = sorted(_items(items), key=key, reverse=reverse)
        count = max(1, int(count))
        return ranked[0] if count == 1 and ranked else ranked[:count] if count > 1 else None

    def newest(self, items, count=1):
        return self._pick(items, count, lambda i: -i.age_days, True)

    def oldest(self, items, count=1):
        return self._pick(items, count, lambda i: -i.age_days, False)

    def largest(self, items, count=1):
        return self._pick(items, count, lambda i: i.size, True)

    # ── Opening ──

    def open(self, target) -> None:
        targets = _items(target)
        if not targets:
            return
        if len(targets) > MAX_OPEN:
            raise ActionError(f"That would open {len(targets)} things at once; Koli opens up to {MAX_OPEN}")
        for t in targets:
            if isinstance(t, str) and t.lower().startswith(('https://', 'http://')):
                self.ops.append({'op': 'open', 'url': t})
                continue
            path = t.path if isinstance(t, Item) else os.path.abspath(os.path.expanduser(str(t)))
            if not os.path.exists(path):
                raise ActionError(f"There's nothing at {path}")
            if not can_open(path):
                raise ActionError(OPEN_REFUSED.format(name=Path(path).name)
                                  + ". To start an app, say “open” and its name.")
            self.ops.append({'op': 'open', 'target': path})

    def open_app(self, name) -> None:
        if self._apps is None:
            self._apps = self.backend.apps() or {}
        wanted = str(name or '').strip().lower()
        shortcut = self._apps.get(wanted)
        if shortcut is None:
            # "chrome" is "Google Chrome": the name's own words, then its start,
            # then a near spelling ("calculater").
            words = [n for n in self._apps if wanted in n.split()] if wanted else []
            starts = [n for n in self._apps if n.startswith(wanted)] if len(wanted) >= 4 else []
            close = difflib.get_close_matches(wanted, list(self._apps), n=1, cutoff=0.8)
            match = (sorted(words, key=len) or sorted(starts, key=len) or close or [None])[0]
            if match is None:
                # Not a Start-menu shortcut (Notepad is a Store app), but open
                # already: nothing to start.
                running = self._running_as(name)
                if running:
                    self.notes.append(f"{running} is already open")
                    return
                raise ActionError(f"No app called “{name}” in the Start menu")
            wanted, shortcut = match, self._apps[match]
        self.ops.append({'op': 'open_app', 'name': Path(shortcut).stem, 'shortcut': shortcut})

    def run_command(self, phrase) -> None:
        phrase = str(phrase or '').strip()
        if not self.command_exists(phrase):
            raise ActionError(f"You have no voice command “{phrase}”")
        self.ops.append({'op': 'run_command', 'phrase': phrase})

    # What the last action made, moved or renamed, still where it put them:
    # "make a folder", then "call it Invoices" is rename(last()[0], ...).
    def last(self) -> List[Item]:
        out = []
        for path in self.previous[:MAX_RESULTS]:
            try:
                out.append(Item(path, os.path.isdir(path), os.stat(path)))
            except OSError:
                continue
        return out

    # A system command the AI wrote from what it knows (PowerShell on
    # Windows, the shell or AppleScript on a Mac), with the one that reverses
    # it when there is one. Written down here like everything else; it runs
    # only after the danger list, the reviewer and the policy (and your
    # password when they ask for it). Its output is shown in the bubble.
    # `before` reads the value the command changes (the brightness now); what
    # it prints replaces {before} in `undo`, so Undo can put it back.
    def system(self, command, undo='', before='') -> None:
        from .system import MAX_COMMAND_CHARS
        command, undo, before = (str(v or '').strip() for v in (command, undo, before))
        if not command:
            raise ActionError('An empty system command')
        if max(len(command), len(undo), len(before)) > MAX_COMMAND_CHARS:
            raise ActionError('That system command is too long')
        if '{before}' in undo and not before:
            undo = ''            # nothing would fill it in
        if sum(1 for op in self.ops if op['op'] == 'system') >= MAX_SYSTEM:
            raise ActionError(f'At most {MAX_SYSTEM} system commands in one request')
        self.ops.append({'op': 'system', 'command': command, 'undo': undo, 'before': before})

    def say(self, text) -> None:
        text = str(text or '').strip()
        if text:
            self.said.append(text[:300])

    # ── Other apps (agent/desktop.py) ──
    # Looking runs for real and only reads: which apps are open, which
    # controls they have. Clicking, writing, switching, choosing and closing
    # are written down like file changes and done only in replay, where each
    # control is found again. An app or control that an earlier step makes
    # appear ("open Notepad, then write in it"; a menu item after "File") is
    # accepted unseen, and looked for when the steps really run.

    def _desktop_call(self, args: List[str]) -> dict:
        missing = self.backend.desktop_missing()
        if missing:
            raise ActionError(DESKTOP_MISSING.get(missing, missing))
        self._ui_reads += 1
        if self._ui_reads > MAX_UI_READS:
            raise ActionError(f"Koli looks into other apps at most {MAX_UI_READS} times in one request")
        started = time.monotonic()
        try:
            return self.backend.desktop(args)
        finally:
            self.waited += time.monotonic() - started

    def _apps_running(self) -> List[str]:
        if self._running is None:
            from .desktop import DesktopError, running_apps
            try:
                self._running = running_apps(self._desktop_call)
            except DesktopError as e:
                raise ActionError(str(e))
        return self._running

    # (the app's name as agent-desktop knows it, whether it opens only later):
    # "whatsapp" is "WhatsApp.Root". An app that isn't open is accepted only
    # after an open_app() in the same plan.
    def _app(self, name) -> tuple:
        from .desktop import match_app
        spoken = str(name or '').strip()
        if not spoken:
            raise ActionError('Which app?')
        app = match_app(spoken, self._apps_running())
        if app:
            return app, False
        if any(op['op'] == 'open_app' for op in self.ops):
            return spoken, True
        raise ActionError(f"{spoken} isn't open")

    # The running app a name means, or None (also when other apps can't be used).
    def _running_as(self, name: str) -> Optional[str]:
        from .desktop import match_app
        if self.backend.desktop_missing():
            return None
        try:
            return match_app(name, self._apps_running())
        except ActionError:
            return None

    # After the script: open_app() for an app that is already open and that
    # later steps use is dropped. Starting Notepad again would open a second
    # window, and the steps would have two to choose from.
    def finish(self) -> None:
        if self._running is None:
            return
        used = {op['app'] for op in self.ops if op['op'] == 'ui'}
        self.ops = [op for op in self.ops
                    if not (op['op'] == 'open_app' and self._running_as(op['name']) in used)]

    def _changed_app_before(self, app: str) -> bool:
        return any(op['op'] == 'open_app' or (op['op'] == 'ui' and op['app'] == app) for op in self.ops)

    def apps_open(self) -> List[str]:
        return list(self._apps_running())

    # Whether an app is open, by name as you say it ("notepad" is "Notepad").
    def app_is_open(self, name) -> bool:
        from .desktop import match_app
        return match_app(str(name or ''), self._apps_running()) is not None

    # One control by its name (and role): "Play" in Spotify, "File" in
    # Notepad. The exact name first, then a name containing it. Two with the
    # same name but different roles (Notepad's "File" menu item and the
    # button inside it): the first. Two of the same role: the script must say
    # which, since pressing the wrong "Delete" isn't something to guess.
    def ui(self, app, name, role=None) -> Element:
        from .desktop import DesktopError, find_control
        app, pending = self._app(app)
        name = str(name or '').strip()
        role = str(role or '').strip().lower() or None
        if not name:
            raise ActionError('Which button or field?')
        if pending:
            return Element(app, name, role, found=False)
        try:
            found = find_control(self._desktop_call, app, name, role)
        except DesktopError as e:
            raise ActionError(str(e))
        if len(found) > 1 and len({m.get('role') for m in found}) == len(found):
            found = found[:1]
        if len(found) == 1:
            match = found[0]
            return Element(app, match.get('name') or name, match.get('role') or role, match.get('value'),
                           match['ref_id'], window=match.get('window'), states=match.get('states'))
        if found:
            raise ActionError(f"{len(found)} controls in {app} are called “{name}”")
        if self._changed_app_before(app):
            # In the window the step before it worked in (the menu's).
            window = next((op.get('window') for op in reversed(self.ops)
                           if op['op'] == 'ui' and op['app'] == app and op.get('window')), None)
            return Element(app, name, role, found=False, window=window)
        raise ActionError(f"There's no “{name}” in {app}")

    # Controls whose name or value contains `text` (all of them without it),
    # to read (.value: what a field shows) or to pick from. At most 50.
    def ui_find(self, app, text=None, role=None) -> List[Element]:
        from .desktop import DesktopError, find
        app, pending = self._app(app)
        if pending:
            return []
        try:
            found = find(self._desktop_call, app, text=str(text) if text else None,
                         role=str(role).lower() if role else None)
        except DesktopError as e:
            raise ActionError(str(e))
        return [Element(app, m.get('name') or '', m.get('role') or '', m.get('value'), m['ref_id'],
                        window=m.get('window'), states=m.get('states')) for m in found]

    def _step(self, action: str, element, **extra) -> None:
        if not isinstance(element, Element):
            raise ActionError(f"ui_{action}() takes a control from ui() or ui_find()")
        if sum(1 for op in self.ops if op['op'] == 'ui') >= MAX_UI_STEPS:
            raise ActionError(f"At most {MAX_UI_STEPS} steps in other apps in one request")
        op = {'op': 'ui', 'action': action, 'app': element.app, 'name': element.name, 'role': element.role,
              'ref': element.ref, 'seen': element.found}
        if element.window:
            op['window'] = element.window
        if element.app not in (self._running or []):
            op['pending_app'] = True
        op.update(extra)
        self.ops.append(op)

    def ui_click(self, element) -> None:
        self._step('click', element)

    # Replaces what the field holds. Its old text is kept for Undo when it
    # isn't long. An empty field reports no value at all (Notepad), so none
    # counts as empty: Undo then only takes away what Koli wrote.
    def ui_set_text(self, element, text) -> None:
        text = str(text if text is not None else '')
        if len(text) > 5000:
            raise ActionError('That text is too long to write in one go')
        extra = {'text': text}
        if isinstance(element, Element) and element.found:
            old = element.value or ''
            extra['old_value'] = old if len(old) <= MAX_UNDO_TEXT else None
            extra['old_length'] = len(old)
        self._step('set_text', element, **extra)

    def ui_toggle(self, element) -> None:
        self._step('toggle', element)

    # Turns a checkbox or switch on (or off with on=False), whatever it is
    # now: "turn on word wrap" must not turn it off when it already was. One
    # already so is noted, not pressed. Undo puts back the state it had, when
    # the control said what that was.
    def ui_check(self, element, on=True) -> None:
        on = bool(on)
        if isinstance(element, Element) and element.found and element.checked is on:
            self.notes.append(f"“{element.name}” is already {'on' if on else 'off'} in {element.app}")
            return
        was = element.checked if isinstance(element, Element) and element.found else None
        self._step('check' if on else 'uncheck', element, **({'was_checked': was} if was is not None else {}))

    def ui_select(self, element, option) -> None:
        option = str(option or '').strip()
        if not option:
            raise ActionError('Which option?')
        self._step('select', element, option=option)

    # Asks the app to close, as its own close button would (it may ask about
    # unsaved work). Always waits for your password.
    def close_app(self, app) -> None:
        app, pending = self._app(app)
        self.ops.append({'op': 'ui', 'action': 'close_app', 'app': app, 'name': app, 'role': '', 'ref': '',
                         'seen': not pending, **({'pending_app': True} if pending else {})})

    # ── Changes (written down, not done) ──

    def _free_name(self, folder: str, name: str) -> str:
        taken = self._taken.setdefault(os.path.normcase(folder), set())
        stem, suffix = os.path.splitext(name)
        candidate, n = name, 2
        while os.path.exists(os.path.join(folder, candidate)) or candidate.lower() in taken:
            candidate = f"{stem} ({n}){suffix}"
            n += 1
        taken.add(candidate.lower())
        return candidate

    @staticmethod
    def _weight(item: Item) -> int:
        if not item.is_dir:
            return 1
        count = 0
        for _root, _dirs, files in os.walk(item.path):
            count += len(files)
            if count >= MAX_WEIGHT_COUNT:
                break
        return max(1, count)

    def _source(self, item) -> Item:
        if isinstance(item, Item):
            return item
        path = os.path.abspath(os.path.expanduser(str(item)))
        try:
            return Item(path, os.path.isdir(path), os.stat(path))
        except OSError:
            raise ActionError(f"There's nothing at {path}")

    def _transfer(self, kind: str, items, to) -> None:
        target_dir = self._dir(to)
        for item in [self._source(i) for i in _items(items)]:
            if kind == 'move':
                self._guard(item)
            if os.path.normcase(item.folder) == os.path.normcase(target_dir) and kind == 'move':
                continue      # already there
            if item.is_dir and os.path.normcase(target_dir).startswith(os.path.normcase(item.path) + os.sep):
                raise ActionError(f"Can't {kind} {item.name} into itself")
            name = self._free_name(target_dir, item.name)
            self.ops.append({'op': kind, 'source': item.path, 'target_dir': target_dir, 'name': name,
                             'is_dir': item.is_dir, 'weight': self._weight(item)})

    def copy(self, items, to) -> None:
        self._transfer('copy', items, to)

    def move(self, items, to) -> None:
        self._transfer('move', items, to)

    def rename(self, item, new_name) -> None:
        source = self._guard(self._source(item))
        new_name = str(new_name or '').strip()
        if not new_name or any(c in new_name for c in '\\/:*?"<>|') or new_name in ('.', '..'):
            raise ActionError(f"“{new_name}” isn't a valid name")
        if not source.is_dir and not Path(new_name).suffix and source.ext:
            new_name += source.ext     # keep the extension unless a new one was given
        if os.path.exists(os.path.join(source.folder, new_name)):
            raise ActionError(f"There's already a {new_name} in {source.folder}")
        self.ops.append({'op': 'rename', 'source': source.path, 'path': os.path.join(source.folder, new_name),
                         'name': new_name, 'is_dir': source.is_dir, 'weight': self._weight(source)})

    # A folder that's already there is used as it is, and noted, so the bubble
    # says so instead of "Created". `new=True` ("make a new folder") always
    # makes one, at a free name like Explorer's "New folder (2)".
    def make_folder(self, where, name, new=False) -> Item:
        parent = self._dir(where)
        name = str(name or '').strip()
        if not name or any(c in name for c in '\\/:*?"<>|'):
            raise ActionError(f"“{name}” isn't a valid folder name")
        if new:
            name = self._free_name(parent, name)
        path = os.path.join(parent, name)
        if os.path.isdir(path) or os.path.normcase(path) in self._planned_dirs:
            if os.path.isdir(path):
                self.notes.append(f"“{os.path.basename(path)}” is already in {_short(parent)}")
        else:
            self._planned_dirs.add(os.path.normcase(path))
            self.ops.append({'op': 'make_folder', 'path': path, 'weight': 1})
        return Item(path, True, os.stat(path) if os.path.isdir(path) else None)

    # Only where a Recycle Bin exists: on a USB stick or a network share
    # Windows would delete for good.
    def delete(self, items) -> None:
        for item in [self._guard(self._source(i)) for i in _items(items)]:
            if not self.backend.can_recycle(item.path):
                raise ActionError(f"{item.name} is on a drive without a Recycle Bin, so Koli doesn't delete it")
            self.ops.append({'op': 'delete', 'source': item.path, 'is_dir': item.is_dir, 'weight': self._weight(item)})


# ── Describing a list ────────────────────────────────────────────────────────

# "Move 37 files from Downloads to Pictures" style lines, one per kind of
# change and place, for the bubble, the reviewer and the history. `past`
# words them as done ("Moved 37 files…") for the bubble after the run.
def describe(ops: List[dict], limit: int = 4, past: bool = False) -> List[str]:
    groups = {}
    for op in ops:
        kind = op['op']
        if kind in ('copy', 'move'):
            key = (kind, os.path.dirname(op['source']), op['target_dir'])
        elif kind in ('rename', 'delete'):
            key = (kind, os.path.dirname(op['source']), '')
        elif kind == 'make_folder':
            key = (kind, op['path'], '')
        elif kind == 'open':
            key = (kind, op.get('target') or op.get('url'), '')
        elif kind == 'system':
            key = (kind, op['command'], '')
        elif kind == 'ui':
            key = (kind, str(len(groups)), '')      # every step its own line, in order
        else:
            key = (kind, op.get('name') or op.get('phrase') or '', '')
        groups.setdefault(key, []).append(op)
    lines = []
    for (kind, a, b), group in groups.items():
        n = sum(op.get('weight', 1) for op in group)
        things = f"{n} item{'s' if n != 1 else ''}" if kind in ('copy', 'move', 'delete') else ''
        if kind in ('copy', 'move'):
            lines.append(f"{kind.capitalize()} {things} from {_short(a)} to {_short(b)}")
        elif kind == 'delete':
            lines.append(f"Delete {things} from {_short(a)} (to the Recycle Bin)")
        elif kind == 'rename':
            lines.append(f"Rename {len(group)} in {_short(a)}: " + ', '.join(f"{os.path.basename(o['source'])} → {o['name']}" for o in group[:3]))
        elif kind == 'make_folder':
            lines.append(f"New folder {_short(a)}")
        elif kind == 'system':
            # The reviewer's plain words for each command: "Turn Wi-Fi off"
            # before it runs, "Turned Wi-Fi off" after.
            lines.extend((op.get('done') if past else None) or op.get('explain') or f"Run: {op['command'][:80]}"
                         for op in group)
        elif kind == 'ui':
            lines.extend((op.get('done') if past else None) or op.get('explain') or ui_line(op) for op in group)
        elif kind == 'open':
            lines.append(f"Open {a if str(a).startswith('http') else os.path.basename(str(a)) or a}")
        elif kind == 'open_app':
            lines.append(f"Open {a}")
        elif kind == 'run_command':
            lines.append(f"Run your command “{a}”")
    if past:
        lines = [_past(line) for line in lines]
    if len(lines) > limit:
        lines = lines[:limit] + [f"and {len(lines) - limit} more"]
    return lines


PAST = {'Move ': 'Moved ', 'Copy ': 'Copied ', 'Rename ': 'Renamed ', 'Delete ': 'Deleted ',
        'New folder ': 'Created folder ', 'Open ': 'Opened ', 'Run ': 'Ran ', 'Click ': 'Clicked ',
        'Write ': 'Wrote ', 'Switch ': 'Switched ', 'Choose ': 'Chose ', 'Close ': 'Closed ', 'Turn ': 'Turned ',
        'Clear ': 'Cleared '}


# One step in another app in plain words: "Click “Play” in Spotify".
def ui_line(op: dict) -> str:
    action, app, name = op.get('action'), op.get('app', ''), op.get('name', '')
    if action == 'close_app':
        return f"Close {app}"
    if action == 'set_text':
        text = op.get('text', '')
        shown = text if len(text) <= 40 else text[:39] + '…'
        return f"Write “{shown}” in “{name}” in {app}" if text else f"Clear “{name}” in {app}"
    if action == 'toggle':
        return f"Switch “{name}” in {app}"
    if action in ('check', 'uncheck'):
        return f"Turn {'on' if action == 'check' else 'off'} “{name}” in {app}"
    if action == 'select':
        return f"Choose “{op.get('option', '')}” in “{name}” in {app}"
    return f"Click “{name}” in {app}"


def _past(line: str) -> str:
    for now, done in PAST.items():
        if line.startswith(now):
            return done + line[len(now):]
    return line


# Everything a run made, moved or renamed that is still where it put it.
def places_of(results: List[dict]) -> List[str]:
    return [entry['place'] for entry in results or []
            if entry.get('status') == 'done' and entry.get('place') and os.path.exists(entry['place'])]


# Where the bubble's Show takes you: the last of those, or None.
def place_of(results: List[dict]) -> Optional[str]:
    places = places_of(results)
    return places[-1] if places else None


def _short(path: str) -> str:
    home = str(Path.home())
    path = str(path)
    return '~' + path[len(home):] if path.lower().startswith(home.lower()) else path


# ── The real run ─────────────────────────────────────────────────────────────

# Runs the operations in order. Each one checks its source again: a file that
# vanished since the rehearsal is skipped, never replaced by another. Returns
# [{'op', 'status': done|skipped|failed, 'detail', 'undo': {...} or None,
# 'place': where the result is, for the bubble's Show}].
def replay(ops: List[dict], backend: Backend, run_command: Callable[[str], bool] = lambda p: False) -> List[dict]:
    results = []
    # Steps in other apps build on each other (the menu, then its item): once
    # one didn't happen, the rest aren't pressed in a screen that isn't the
    # one they were planned for.
    ui_stopped = False
    for op in ops:
        kind = op['op']
        entry = {'op': kind, 'status': 'done', 'detail': '', 'undo': None}
        try:
            if kind == 'ui':
                if ui_stopped:
                    entry.update(status='skipped', detail="an earlier step in an app didn't happen")
                else:
                    from . import desktop
                    status, detail = desktop.perform(op, backend.desktop)
                    entry.update(status=status, detail=detail)
                    if status == 'done':
                        entry['undo'] = _ui_undo(op)
                    else:
                        ui_stopped = True
            elif kind == 'open':
                target = op.get('url') or op.get('target')
                if op.get('target') and (not os.path.exists(target) or not can_open(target)):
                    raise ActionError('it is gone or not something Koli opens')
                backend.open_path(target)
            elif kind == 'open_app':
                backend.start_app(op['shortcut'])
            elif kind == 'run_command':
                if not run_command(op['phrase']):
                    raise ActionError('the command did not run')
            elif kind in ('copy', 'move'):
                if not os.path.exists(op['source']):
                    entry.update(status='skipped', detail=f"{os.path.basename(op['source'])} is gone")
                    results.append(entry)
                    continue
                os.makedirs(op['target_dir'], exist_ok=True)
                target = _free_path(op['target_dir'], op['name'])
                if kind == 'move':
                    shutil.move(op['source'], target)
                    entry['undo'] = {'op': 'move_back', 'from': target, 'to': op['source']}
                elif op.get('is_dir'):
                    shutil.copytree(op['source'], target)
                    entry['undo'] = {'op': 'recycle', 'path': target}
                else:
                    shutil.copy2(op['source'], target)
                    entry['undo'] = {'op': 'recycle', 'path': target}
                entry['detail'] = entry['place'] = target
            elif kind == 'rename':
                if not os.path.exists(op['source']):
                    entry.update(status='skipped', detail=f"{os.path.basename(op['source'])} is gone")
                    results.append(entry)
                    continue
                if os.path.exists(op['path']):
                    raise ActionError(f"{op['name']} is already there")
                os.rename(op['source'], op['path'])
                entry['undo'] = {'op': 'move_back', 'from': op['path'], 'to': op['source']}
                entry['place'] = op['path']
            elif kind == 'make_folder':
                if not os.path.isdir(op['path']):
                    os.makedirs(op['path'])
                    entry['undo'] = {'op': 'remove_folder', 'path': op['path']}
                entry['place'] = op['path']
            elif kind == 'system':
                from .system import dangers
                undo = op.get('undo') or ''
                if op.get('before'):
                    # Only a plain value goes into the command (a number, a
                    # name): never quotes, ";", "|", "$", brackets or backticks.
                    read, value = backend.run_system(op['before'])
                    value = value.strip()
                    undo = undo.replace('{before}', value) if read and PLAIN_VALUE.fullmatch(value) else ''
                # It may have run without a password because it can be put
                # back: without a safe undo now, it doesn't run at all.
                if op.get('undo') and (not undo or dangers(undo)):
                    entry.update(status='skipped', detail="couldn't read the current value, so it couldn't be undone")
                    results.append(entry)
                    continue
                ok, output = backend.run_system(op['command'])
                if not ok:
                    raise ActionError(output)
                entry['output'] = output
                if undo:
                    entry['undo'] = {'op': 'system', 'command': undo}
            elif kind == 'delete':
                if not os.path.exists(op['source']):
                    entry.update(status='skipped', detail=f"{os.path.basename(op['source'])} is gone")
                    results.append(entry)
                    continue
                if not backend.to_recycle_bin([op['source']]):
                    raise ActionError("the Recycle Bin didn't take it")
                entry['undo'] = {'op': 'restore', 'path': op['source']}
        except Exception as e:
            entry.update(status='failed', detail=str(e) or e.__class__.__name__)
        results.append(entry)
    return results


# What puts a step in another app back: a switch is switched again, a field
# gets its old text. A click or a choice can't be known to reverse.
def _ui_undo(op: dict) -> Optional[dict]:
    where = {'op': 'ui', 'app': op['app'], 'name': op['name'], 'role': op.get('role') or '', 'ref': ''}
    if op.get('window'):
        where['window'] = op['window']
    if op['action'] == 'toggle':
        return {**where, 'action': 'toggle'}
    if op['action'] in ('check', 'uncheck') and isinstance(op.get('was_checked'), bool):
        return {**where, 'action': 'check' if op['was_checked'] else 'uncheck'}
    if op['action'] == 'set_text' and isinstance(op.get('old_value'), str):
        return {**where, 'action': 'set_text', 'text': op['old_value']}
    return None


def _free_path(folder: str, name: str) -> str:
    stem, suffix = os.path.splitext(name)
    candidate, n = os.path.join(folder, name), 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem} ({n}){suffix}")
        n += 1
    return candidate


# Reverses a run's results, last first. Returns (undone, needs_you): how many
# were undone, and the names that must come back from the Recycle Bin by hand
# (the Recycle Bin is opened for them).
def undo(results: List[dict], backend: Backend) -> tuple:
    undone, by_hand = 0, []
    for entry in reversed(results):
        step = entry.get('undo')
        if not step or entry.get('status') != 'done':
            continue
        try:
            if step['op'] == 'move_back':
                if os.path.exists(step['from']):
                    os.makedirs(os.path.dirname(step['to']), exist_ok=True)
                    shutil.move(step['from'], _free_path(os.path.dirname(step['to']), os.path.basename(step['to'])))
                    undone += 1
            elif step['op'] == 'recycle':
                if os.path.exists(step['path']) and backend.to_recycle_bin([step['path']]):
                    undone += 1
            elif step['op'] == 'remove_folder':
                if os.path.isdir(step['path']) and not os.listdir(step['path']):
                    os.rmdir(step['path'])
                    undone += 1
            elif step['op'] == 'restore':
                by_hand.append(os.path.basename(step['path']))
            elif step['op'] == 'system':
                # The reverse command was reviewed with the one it reverses,
                # and is checked against the danger list once more as stored.
                from .system import dangers
                if not dangers(step['command']) and backend.run_system(step['command'])[0]:
                    undone += 1
            elif step['op'] == 'ui':
                from . import desktop
                if desktop.perform(step, backend.desktop)[0] == 'done':
                    undone += 1
        except Exception:
            continue
    if by_hand:
        try:
            backend.open_recycle_bin()
        except Exception:
            pass
    return undone, by_hand
