# platform/macos/paths.py
# Resolves the per-user config directory (~/.koli).
# Windows mirror: platform/windows/paths.py (%APPDATA%\koli).
import subprocess
from pathlib import Path

def get_app_data_path():
    return Path.home() / '.koli'

def open_file(path):
    subprocess.run(['open', str(path)])
