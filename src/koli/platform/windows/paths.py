# platform/windows/paths.py
# Resolves the per-user config directory (%APPDATA%\koli).
# macOS mirror: platform/macos/paths.py (~/.koli).
import os
from pathlib import Path

def get_app_data_path():
    return Path(os.getenv('APPDATA')) / 'koli'

def open_file(path):
    os.startfile(path)
