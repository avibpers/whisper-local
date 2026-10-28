# tests/__init__.py
# Every test run counts its AI and transcription calls (usage/ledger.py) in a
# throwaway folder, never in the real usage.json of the settings folder.
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_USAGE_DIR = tempfile.mkdtemp(prefix='koli-usage-test-')
atexit.register(shutil.rmtree, _USAGE_DIR, True)

from koli.usage import ledger  # noqa: E402

ledger.path = lambda: Path(_USAGE_DIR) / 'usage.json'
