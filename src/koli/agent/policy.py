# agent/policy.py
# Which folders are yours, and how dangerous a list of operations is. Opening
# and finding run at once; copying, moving, renaming and new folders inside your
# folders run at once with Undo, up to MAX_FREE_FILES; deleting, more files, or
# anything outside your folders needs your password. Computed from the list
# the rehearsal produced, so it judges what would really happen. A system
# command the AI wrote runs at once only when it reads something, or changes
# a setting it can put back, and nothing on the danger list (agent/system.py)
# is in it; the reviewer's rating ('risk') is on the operation.

import os
from pathlib import PureWindowsPath
from typing import Iterable, List, Optional

MAX_FREE_FILES = 50
CHANGES = ('copy', 'move', 'rename', 'make_folder', 'delete')

RUN, PASSWORD = 'run', 'password'


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _under(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip('\\/') + os.sep)


class Folders:
    # `home`: your home folder; `known`: the real paths of Desktop, Documents,
    # Downloads, Pictures, Music, Videos (they may live on another drive);
    # `system_drive`: the Windows drive ("C:"). Your folders are the known ones,
    # your home folder except AppData, and every drive but the Windows one.
    def __init__(self, home: str, known: Iterable[str] = (), system_drive: Optional[str] = 'C:'):
        self.home = str(home)
        self.known = [str(k) for k in known if k]
        self.system_drive = (system_drive or '').rstrip('\\/').upper()

    def own(self, path: str) -> bool:
        if not path:
            return False
        if any(_under(path, k) for k in self.known):
            return True
        if _under(path, self.home):
            return not _under(path, os.path.join(self.home, 'AppData'))
        drive = PureWindowsPath(_norm(path)).drive.upper() if os.name == 'nt' else ''
        # A drive letter other than Windows' own; never a network share (\\server\share).
        return len(drive) == 2 and drive[1] == ':' and bool(self.system_drive) and drive != self.system_drive

    @classmethod
    def from_platform(cls) -> 'Folders':
        from pathlib import Path
        try:
            from ..platform import files
            known = [files.known_folder(n) for n in ('desktop', 'documents', 'downloads', 'pictures', 'music', 'videos')]
            return cls(str(Path.home()), known, files.system_drive())
        except (ImportError, AttributeError):
            return cls(str(Path.home()), (), None)


# The paths an operation touches.
def paths_of(op: dict) -> List[str]:
    return [op[k] for k in ('source', 'target_dir', 'path') if op.get(k)]


# (level, reasons): RUN, or PASSWORD with what made it so. `mismatch` is the
# reviewer's reason when it said the list isn't what you asked for.
def level(ops: List[dict], folders: Folders, mismatch: str = '') -> tuple:
    reasons = []
    changes = [op for op in ops if op.get('op') in CHANGES]
    deletes = sum(op.get('weight', 1) for op in changes if op['op'] == 'delete')
    if deletes:
        reasons.append(f"deletes {deletes} item{'s' if deletes != 1 else ''}")
    files = sum(op.get('weight', 1) for op in changes if op['op'] != 'make_folder')   # a new folder isn't a file
    if files > MAX_FREE_FILES:
        reasons.append(f"changes {files} files (more than {MAX_FREE_FILES})")
    outside = sorted({p for op in changes for p in paths_of(op) if not folders.own(p)})
    if outside:
        reasons.append(f"works outside your folders ({outside[0]}{' and more' if len(outside) > 1 else ''})")
    for op in ops:
        if op.get('op') == 'system':
            reasons.extend(system_reasons(op))
    if mismatch:
        reasons.append(f"may not be what you asked: {mismatch}")
    return (PASSWORD if reasons else RUN), reasons


# Why one system command needs your password ([] when it may just run).
def system_reasons(op: dict) -> List[str]:
    from .system import dangers
    what = op.get('explain') or 'a system command'
    found = dangers(op.get('command', '')) + [f"its undo {d}" for d in dangers(op.get('undo', ''))] \
        + [f"its first step {d}" for d in dangers(op.get('before', ''))]
    if found:
        return [f"{what}: {', '.join(found)}"]
    risk = op.get('risk')
    if risk == 'read':
        return []
    if risk == 'setting':
        return [] if op.get('undo') else [f"{what}: can't be undone"]
    if risk in ('change', 'danger'):
        return [f"{what}: {op.get('risk_reason') or 'changes your computer'}"]
    return [f"{what}: it couldn't be checked"]


# Whether the reviewer must look: anything that changes files, and every
# system command (its rating decides the level).
def changes_anything(ops: List[dict]) -> bool:
    return any(op.get('op') in CHANGES or op.get('op') == 'system' for op in ops)
