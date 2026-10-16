# platform/macos/files.py
# Mirror of platform/windows/files.py: known folders, the Trash, apps in
# /Applications, opening things. Deleting moves to ~/.Trash under a free name.

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


def known_folder(name: str) -> Optional[str]:
    if name == 'home':
        return str(Path.home())
    folder = Path.home() / name.capitalize()
    return str(folder) if folder.exists() else None


def system_drive() -> str:
    return '/'


def to_recycle_bin(paths: List[str]) -> bool:
    trash = Path.home() / '.Trash'
    try:
        trash.mkdir(exist_ok=True)
        for path in paths:
            source = Path(path)
            target = trash / source.name
            n = 2
            while target.exists():
                target = trash / f"{source.stem} ({n}){source.suffix}"
                n += 1
            shutil.move(str(source), str(target))
        return True
    except OSError:
        return False


def can_recycle(path: str) -> bool:
    return str(path).startswith(str(Path.home()))


def open_recycle_bin() -> None:
    subprocess.Popen(['open', str(Path.home() / '.Trash')])


def start_menu_apps() -> Dict[str, str]:
    apps = {}
    for root in (Path('/Applications'), Path.home() / 'Applications'):
        if root.is_dir():
            for app in root.glob('*.app'):
                apps.setdefault(app.stem.lower(), str(app))
    return apps


def start_app(shortcut: str) -> None:
    subprocess.Popen(['open', shortcut])


def open_path(target: str) -> None:
    subprocess.Popen(['open', target])


def reveal(path: str) -> None:
    subprocess.Popen(['open', '-R', path])
