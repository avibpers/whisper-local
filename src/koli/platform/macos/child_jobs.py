# platform/macos/child_jobs.py
# Mirror of platform/windows/child_jobs.py. Not built for macOS yet: nothing
# ties a child process to Koli's life here, so the caller closes it on
# shutdown only.


def attach(pid: int) -> bool:
    return False
