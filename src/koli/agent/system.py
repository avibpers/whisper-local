# agent/system.py
# System commands the AI writes from its own knowledge of Windows and macOS
# (PowerShell there, the shell or AppleScript on a Mac): the volume, the
# brightness, Wi-Fi, a settings page, "what's my battery". Three things live
# here: the danger list Koli checks on its own, without any AI (deleting,
# formatting, downloading and running code, the registry's machine keys, ...),
# how a command runs (no profile, no prompts, a time limit, output captured),
# and the name of the system for the writer. The reviewer model rates each
# command too; policy.level weighs both.

import platform
import re
import subprocess
import sys
from typing import List, Tuple

IS_WINDOWS = sys.platform == 'win32'
RUN_SECONDS = 20.0
MAX_COMMAND_CHARS = 2000
MAX_OUTPUT_CHARS = 600

# ── The danger list ──────────────────────────────────────────────────────────
# (pattern, why), matched case-insensitively against the whole command. Any
# hit means your password, whatever the reviewer says. Files are changed
# through Koli's file actions (with the Recycle Bin and Undo), so a command
# that writes, moves or deletes files is on the list too.

DANGERS: List[Tuple[str, str]] = [
    # Deleting, and changing files outside the file actions
    (r'\b(remove-item|rm|rmdir|rd|del|erase|clear-recyclebin|clear-content)\b', 'deletes files'),
    (r'\b(move-item|copy-item|rename-item|new-item|set-content|add-content|out-file|mv|cp)\b', 'changes files'),
    (r'(?<![=-])>{1,2}\s*[\w"\'$%~./\\]', 'writes to a file'),
    (r'\b(format-volume|clear-disk|initialize-disk|diskpart|diskutil|cipher\s+/w|format\s+[a-z]:)', 'erases a disk'),
    # Downloading files and running code. Reading a little text from the web
    # ("what's my public IP") is fine; see WEB_DANGERS for sending data out.
    (r'\bstart-bitstransfer\b|downloadfile|-outfile\b|\b(curl|wget)\b[^|;]*\s(-o|-O|--output)\b', 'downloads a file'),
    (r'\b(invoke-expression|iex)\b|-e(nc(odedcommand)?)?\s+[a-z0-9+/=]{16,}|frombase64string', 'runs hidden code'),
    (r'\b(winget|choco|msiexec|install-\w+|uninstall-\w+|pip|npm|brew)\b', 'installs or removes software'),
    # The system itself
    (r'\b(stop-process|taskkill|kill|pkill|killall)\b', 'closes programs'),
    (r'\b(shutdown|restart-computer|stop-computer|logoff|reboot|halt)\b', 'shuts down or restarts'),
    (r'\b(set-executionpolicy|set-mppreference|add-mppreference|bcdedit|csrutil|spctl)\b', 'changes security settings'),
    (r'\b(netsh\s+(adv)?firewall|set-netfirewall\w*|new-netfirewall\w*)', 'changes the firewall'),
    (r'\b(schtasks|register-scheduledtask|new-service|launchctl|crontab)\b|\bsc(\.exe)?\s+(create|delete|config)', 'runs something later or as a service'),
    (r'\b(set-acl|icacls|takeown|chmod|chown|attrib)\b', 'changes permissions'),
    (r'\b(sudo|runas)\b|-verb\s+runas|with administrator privileges', 'runs as administrator'),
    (r'(hklm:|hkey_local_machine|\breg(\.exe)?\s+(delete|add|import))', 'changes the machine registry'),
    (r'\b(remove-\w+|disable-\w+|clear-\w+)\b', 'removes or turns off something'),
    (r'\bdefaults\s+delete\b', 'removes settings'),
    # Ways around the names above
    # A program from a folder ("C:\x\a.exe", ".\a.exe"); a plain system one
    # by name (explorer.exe ms-settings:...) is like opening an app.
    (r'[\\/][^\s"\'|;]*\.(exe|com|scr|pif)\b', 'runs a program from a folder'),
    (r'\.(bat|cmd|ps1|psm1|vbs|vbe|js|jse|wsf|wsh|msi|msc|jar|hta|cpl|reg|lnk)\b', 'runs a script or installer'),
    (r'\b(cmd|powershell|pwsh|wscript|cscript|mshta|rundll32|regsvr32|wsl|bash|python|node)(\.exe)?\b', 'starts another shell'),
    (r'\bdelete\b', 'deletes something'),
    (r'\b(stop-service|set-service|restart-service|suspend-service)\b|\bnet\s+(stop|user|localgroup)\b|\bsc(\.exe)?\s+stop\b',
     'changes a service or account'),
    (r'\bwmic\b|\b(invoke-command|enter-pssession|new-pssession|invoke-wmimethod|invoke-cimmethod)\b', 'runs code another way'),
    (r'\[(system\.)?io\.(file|directory|fileinfo|directoryinfo)\]::', 'changes files'),
    (r'\[(system\.)?environment\]::setenvironmentvariable|\[microsoft\.win32\.registry', 'changes the system'),
    (r'dllimport', 'calls Windows internals'),
    (r'(hkcu:|hkey_current_user).*(\\run\b|\\runonce|\\policies|\\winlogon)', 'starts something with Windows'),
]
_DANGERS = [(re.compile(p, re.IGNORECASE), why) for p, why in DANGERS]

# Checked on the command as written (web links left in): a web request that
# sends something, or puts your data ($variables) into the address.
WEB_DANGERS: List[Tuple[str, str]] = [
    (r'-method\s+["\']?(post|put|patch|delete)|-body\b|\bcurl\b[^|;]*\s(-d|--data|-F|--form|-X\s*(post|put))\b',
     'sends data to the internet'),
    (r'\b(invoke-webrequest|iwr|invoke-restmethod|irm|curl|wget|downloadstring)\b[^|;]*\$', 'sends your data to the internet'),
]
_WEB_DANGERS = [(re.compile(p, re.IGNORECASE), why) for p, why in WEB_DANGERS]


# Why `command` needs your password, from the list alone (no AI): [] when
# nothing on it matches.
def dangers(command: str) -> List[str]:
    # Sending output nowhere ("2>$null", "2>&1") writes no file, and a web
    # link isn't a command ("youtube.com", ".../delete-account").
    command = re.sub(r'\d?>{1,2}\s*\$null\b|\d?>&\d', ' ', command or '', flags=re.IGNORECASE)
    found = [why for pattern, why in _WEB_DANGERS if pattern.search(command)]
    command = re.sub(r'https?://[^\s"\'|;)]+', ' ', command, flags=re.IGNORECASE)
    for pattern, why in _DANGERS:
        if pattern.search(command) and why not in found:
            found.append(why)
    return found


# ── The system, for the writer ───────────────────────────────────────────────

def describe_system() -> str:
    if IS_WINDOWS:
        return f"Windows {platform.release()} (version {platform.version()}); system() runs Windows PowerShell 5.1"
    if sys.platform == 'darwin':
        return f"macOS {platform.mac_ver()[0]}; system() runs zsh (use osascript for AppleScript)"
    return f"{platform.system()} {platform.release()}; system() runs sh"


# ── Running one ──────────────────────────────────────────────────────────────

# (ok, output): the command's printed output (or its error), cut to
# MAX_OUTPUT_CHARS. PowerShell starts with no profile and no prompts, prints
# UTF-8, and hides its progress bars; nothing gets a window.
def run(command: str, timeout: float = RUN_SECONDS) -> Tuple[bool, str]:
    if IS_WINDOWS:
        prefix = "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', prefix + command]
        flags = subprocess.CREATE_NO_WINDOW
    else:
        args, flags = ['/bin/zsh' if sys.platform == 'darwin' else '/bin/sh', '-c', command], 0
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"it took longer than {int(timeout)} s"
    except OSError as e:
        return False, str(e)
    out = done.stdout.decode('utf-8', errors='replace').strip()
    err = done.stderr.decode('utf-8', errors='replace').strip()
    if done.returncode != 0 or (err and not out):
        return False, _cut(err or out or f"it ended with code {done.returncode}")
    return True, _cut(out)


def _cut(text: str) -> str:
    text = ' '.join(text.split()) if len(text) > MAX_OUTPUT_CHARS else text
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS - 1] + '…'
