# platform/macos/session.py
# Whether the Mac is locked, so the wake word stops listening on the lock
# screen (see the Windows mirror: platform/windows/session.py). The login
# session's own dictionary says so. When it can't be asked, the Mac counts as
# locked: the wake word must never run commands on a locked computer.

import logging

logger = logging.getLogger(__name__)
_warned = False


def is_locked() -> bool:
    global _warned
    try:
        import Quartz
        info = Quartz.CGSessionCopyCurrentDictionary()
        if info is None:          # no login session on screen (fast user switch, login window)
            return True
        return bool(info.get('CGSSessionScreenIsLocked', False)) or not bool(info.get('kCGSSessionOnConsoleKey', True))
    except Exception as e:
        if not _warned:
            _warned = True
            logger.warning(f"Can't tell whether the Mac is locked ({e}); the wake word won't listen")
        return True
