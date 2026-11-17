# tests/test_meeting_search.py
# Meeting search by meaning: embeddings through Ollama, the pieces and their
# index, word search, merged results and Ask, all without a running model.
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ── Embeddings through Ollama ────────────────────────────────────────────────

class EmbedTests(unittest.TestCase):
    def test_embed_batches_and_normalizes(self):
        from koli import llm
        calls, payloads = [], []
        def fake(url, payload, headers=None, timeout=30):
            calls.append(payload['input']); payloads.append(payload)
            return {'embeddings': [[3.0, 4.0] for _ in payload['input']]}
        with mock.patch('koli.llm.transports.ollama.http.post_json', fake):
            out = llm.embed('qwen3-embedding:0.6b', ['a', 'b', 'c'], batch=2)
        self.assertEqual([len(c) for c in calls], [2, 1])
        self.assertEqual(payloads[0]['keep_alive'], '30m')      # stays loaded between questions
        np.testing.assert_allclose(out[0], [0.6, 0.8], rtol=1e-5)

    def test_embed_error_raises(self):
        from koli import llm
        from koli.llm.types import ProviderError
        with mock.patch('koli.llm.transports.ollama.http.post_json', lambda *a, **k: {'error': 'model not found'}):
            with self.assertRaises(ProviderError):
                llm.embed('missing', ['a'])

    def test_embed_empty_gives_empty_matrix(self):
        from koli import llm
        self.assertEqual(llm.embed('m', []).shape, (0, 0))

    def test_pull_reports_progress(self):
        from koli.llm.transports import ollama
        lines = [b'{"status":"pulling","total":200,"completed":100}\n', b'{"status":"success"}\n']
        class Resp(list):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        seen = []
        with mock.patch('koli.llm.transports.ollama.http.post_stream', lambda *a, **k: Resp(lines)):
            ollama.pull('http://x', 'm', lambda p, s: seen.append((p, s)))
        self.assertEqual(seen[0], (0.5, 'pulling'))


if __name__ == "__main__":
    unittest.main()
