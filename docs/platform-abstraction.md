## Structure

```
platform/
├── __init__.py        # sets `IS_MACOS` / `IS_WINDOWS` and imports
└── {macos,windows}/
    ├── assets/        # platform-specific assets
    └── *.py           # modules (mirrored API)
```

Module Contract:
- Mirrored with identical API (no-ops OK)
- Imported in `__init__.py`
- No-op stubs are valid when a platform doesn't need the functionality 

## App window helpers

`ui/win32.py` holds the Windows calls the app window and floating bar need
(a bar that never takes focus, rounded corners, a dark title bar, DPI and the
work area). It lives next to the UI rather than in `platform/` because only
the app window uses it, and every function is a safe no-op on other systems.
The macOS port of the app window (a non-activating `NSPanel` for the bar) will
move these behind a mirrored `platform/*/windows.py`.

## Usage

```python
from .platform import keyboard, hotkeys, app, paths, icons  # prefer
from .platform import IS_MACOS, IS_WINDOWS  # sparingly
```
