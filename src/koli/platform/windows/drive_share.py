# platform/windows/drive_share.py
# Google's own Share window for a file Drive for desktop has synced: finds the
# drive letter it mounts for an account ("P:\ studio@… - Google Drive") and
# runs the file's "Share with Google Drive" entry from its right-click menu,
# which opens Drive for desktop's Share item window. No Google sign-in in Koli.

import ctypes
import logging
import string
from ctypes import wintypes
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SHARE_TEXTS = ('share with google drive', 'שיתוף עם google drive', 'שתף עם google drive')


def available() -> bool:
    return True


def _volume_label(root: str) -> str:
    name = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), name, 261, None, None, None, None, 0)
    return name.value if ok else ''


# The root of the drive Drive for desktop mounts for this account, or None.
def local_root(email: str) -> Optional[Path]:
    email = (email or '').lower()
    if not email:
        return None
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        if email in _volume_label(root).lower() and (Path(root) / 'My Drive').is_dir():
            return Path(root)
    return None


# Opens Drive for desktop's Share window for `path`. True when the entry ran.
def show_share(path) -> bool:
    try:
        import pythoncom
        import win32con
        import win32gui
        import win32gui_struct
        from win32com.shell import shell, shellcon
    except ImportError as e:
        logger.info(f"Share window unavailable: {e}")
        return False
    path = Path(path)
    pythoncom.CoInitialize()
    try:
        desktop = shell.SHGetDesktopFolder()
        _, folder_pidl, _ = desktop.ParseDisplayName(0, None, str(path.parent))
        folder = desktop.BindToObject(folder_pidl, None, shell.IID_IShellFolder)
        _, pidl, _ = folder.ParseDisplayName(0, None, path.name)
        menu = folder.GetUIObjectOf(0, [pidl], shell.IID_IContextMenu, 0)[1]
        hmenu = win32gui.CreatePopupMenu()
        first = 1
        menu.QueryContextMenu(hmenu, 0, first, 0x7FFF, shellcon.CMF_NORMAL)
        command = None
        for i in range(win32gui.GetMenuItemCount(hmenu)):
            buf, _ = win32gui_struct.EmptyMENUITEMINFO()
            win32gui.GetMenuItemInfo(hmenu, i, True, buf)
            item = win32gui_struct.UnpackMENUITEMINFO(buf)
            if item.text and item.text.replace('&', '').strip().lower() in SHARE_TEXTS:
                command = item.wID
                break
        win32gui.DestroyMenu(hmenu)
        if command is None:
            logger.info(f"No 'Share with Google Drive' entry for {path.name}")
            return False
        menu.InvokeCommand((0, 0, command - first, None, None, win32con.SW_SHOWNORMAL, 0, 0))
        return True
    except Exception as e:
        logger.warning(f"Share window didn't open for {path}: {e}")
        return False
    finally:
        pythoncom.CoUninitialize()
