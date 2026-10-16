# platform/windows/files.py
# What AI actions need from Windows: where your known folders really are
# (Downloads can be moved to another drive), deleting to the Recycle Bin (never
# permanently), opening the Recycle Bin for an undo, and the apps in the Start
# menu, so "open calculator" starts an app by its name and never by a path.
# macOS mirror: platform/macos/files.py.

import ctypes
import os
import subprocess
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional

# ── Known folders ────────────────────────────────────────────────────────────

_KNOWN = {
    'downloads': '{374DE290-123F-4565-9164-39C4925E467B}',
    'desktop': '{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}',
    'documents': '{FDD39AD0-238F-46AF-ADB4-6C85480369C7}',
    'pictures': '{33E28130-4E1E-4676-835A-98395C3BC3BB}',
    'music': '{4BD8D571-6D19-48D3-BE97-422220080E43}',
    'videos': '{18989B1D-99B5-455B-841C-AB7C74E4DDFC}',
}


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                ('Data4', ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


# The real path of a known folder ('downloads', 'desktop', ...), or None.
def known_folder(name: str) -> Optional[str]:
    if name == 'home':
        return str(Path.home())
    guid = _KNOWN.get(name)
    if not guid:
        return None
    try:
        out = ctypes.c_wchar_p()
        ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(_guid(guid)), 0, None, ctypes.byref(out))
        path = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        if path:
            return path
    except Exception:
        pass
    fallback = Path.home() / name.capitalize()
    return str(fallback) if fallback.exists() else None


def system_drive() -> str:
    return (os.environ.get('SystemDrive') or 'C:').upper()


# ── Recycle Bin ──────────────────────────────────────────────────────────────

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', wintypes.LPCWSTR),
                ('pTo', wintypes.LPCWSTR), ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', wintypes.BOOL),
                ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]


_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400


# Sends files and folders to the Recycle Bin. True when every one went.
def to_recycle_bin(paths: List[str]) -> bool:
    paths = [str(p) for p in paths if p]
    if not paths:
        return True
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = '\0'.join(paths) + '\0\0'
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return result == 0 and not op.fAnyOperationsAborted and not any(os.path.exists(p) for p in paths)


# Whether a deleted file there goes to the Recycle Bin: a fixed local drive.
# Removable drives and network shares have none, and Windows would delete for
# good.
def can_recycle(path: str) -> bool:
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) != 2 or drive[1] != ':':
        return False          # \\server\share
    DRIVE_FIXED = 3
    return ctypes.windll.kernel32.GetDriveTypeW(drive + '\\') == DRIVE_FIXED


def open_recycle_bin() -> None:
    subprocess.Popen(['explorer.exe', 'shell:RecycleBinFolder'])


# ── Start-menu apps ──────────────────────────────────────────────────────────

# {'calculator': 'C:\\...\\Calculator.lnk', ...}: the shortcuts in yours and
# everyone's Start menu, by lowercase name; uninstallers left out.
def start_menu_apps() -> Dict[str, str]:
    roots = [Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs',
             Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs']
    apps = {}
    for root in roots:
        if not root.is_dir():
            continue
        for link in root.rglob('*.lnk'):
            name = link.stem.strip().lower()
            if 'uninstall' in name or name in apps:
                continue
            apps[name] = str(link)
    return apps


# Starts an app from its Start-menu shortcut (a path start_menu_apps gave).
def start_app(shortcut: str) -> None:
    os.startfile(shortcut)


# Opens a file, a folder or a link with what Windows uses for it.
def open_path(target: str) -> None:
    os.startfile(target)


# Opens the folder holding `path` in Explorer with `path` selected: where an
# AI action put something.
def reveal(path: str) -> None:
    subprocess.Popen(['explorer.exe', f'/select,{os.path.normpath(path)}'])
