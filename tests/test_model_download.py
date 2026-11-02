# tests/test_model_download.py
# A model is on disk only when its files are in a finished snapshot (a
# cancelled download leaves the folder behind), and a download that stops
# growing ends with an error instead of a frozen progress bar.

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli.model_registry import ModelRegistry  # noqa: E402
from koli.services import models  # noqa: E402


class FinishedSnapshotTests(unittest.TestCase):
    def test_only_a_snapshot_with_the_model_files_counts(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'models--Systran--faster-whisper-base'
            self.assertFalse(ModelRegistry._has_finished_snapshot(str(folder)))
            # A download just started: the folder and an incomplete blob.
            (folder / 'blobs').mkdir(parents=True)
            (folder / 'blobs' / 'abc.incomplete').write_bytes(b'x')
            self.assertFalse(ModelRegistry._has_finished_snapshot(str(folder)))
            snap = folder / 'snapshots' / 'rev1'
            snap.mkdir(parents=True)
            (snap / 'config.json').write_text('{}')
            self.assertFalse(ModelRegistry._has_finished_snapshot(str(folder)))
            (snap / 'model.bin').write_bytes(b'x')
            self.assertTrue(ModelRegistry._has_finished_snapshot(str(folder)))


class StallTests(unittest.TestCase):
    def test_a_download_that_stops_growing_fails(self):
        registry = mock.Mock()
        registry.is_model_cached.return_value = False
        release = []

        def hang(*_a, **_k):
            while not release:
                time.sleep(0.05)
            return 'path'

        fake_hub = mock.Mock(snapshot_download=hang)
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.dict(sys.modules, {'huggingface_hub': fake_hub}), \
             mock.patch.object(models, 'repo_for', return_value='Systran/x'), \
             mock.patch.object(models, '_expected_bytes', return_value=None), \
             mock.patch.object(models, 'cache_path', return_value=Path(folder)), \
             mock.patch.object(models, 'STALL_SECONDS', 0.3):
            try:
                with self.assertRaisesRegex(RuntimeError, 'stalled'):
                    models.ensure_downloaded(registry, 'base')
            finally:
                release.append(True)

    def test_without_a_folder_to_watch_nothing_reads_as_stalled(self):
        registry = mock.Mock()
        registry.is_model_cached.return_value = False
        calls = []

        def slow(*_a, **_k):
            time.sleep(0.6)
            calls.append(True)
            return 'path'
        with mock.patch.dict(sys.modules, {'huggingface_hub': mock.Mock(snapshot_download=slow)}), \
             mock.patch.object(models, 'repo_for', return_value='Systran/x'), \
             mock.patch.object(models, '_expected_bytes', return_value=None), \
             mock.patch.object(models, 'cache_path', return_value=None), \
             mock.patch.object(models, 'STALL_SECONDS', 0.2):
            self.assertEqual(models.ensure_downloaded(registry, 'base'), 'path')
        self.assertEqual(calls, [True])


if __name__ == '__main__':
    unittest.main()
