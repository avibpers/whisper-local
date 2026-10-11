# platform/macos/browser_tabs.py
# Not implemented on macOS yet: no browser tab is ever a meeting source.
# Mirrors platform/windows/browser_tabs.py.

BROWSERS = ('Google Chrome', 'Microsoft Edge', 'Brave Browser')


def tabs(exes=BROWSERS) -> list:
    return []
