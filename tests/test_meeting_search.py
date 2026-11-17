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


# ── Pieces of a meeting ──────────────────────────────────────────────────────

class PiecesTests(unittest.TestCase):
    def meeting(self, n_turns=10, step=15.0):
        return {'id': 'm1', 'title': 'Pricing', 'started_at': '2026-11-16T17:51:57+02:00',
                'speakers': [{'id': 'you', 'name': 'You'}, {'id': 's1', 'name': 'Shahar'}],
                'turns': [{'start': i * step, 'speaker': 's1' if i % 2 else 'you', 'text': f'turn {i}'} for i in range(n_turns)],
                'screenshots': [{'at': 30.0, 'caption': 'The price table for enterprise'}]}

    def test_transcript_windows_overlap(self):
        from koli.meeting.pieces import pieces
        ps = [p for p in pieces(self.meeting(), None) if p['kind'] == 'transcript']
        self.assertEqual(ps[0]['at'], 0.0)
        self.assertIn('Shahar: turn 1', ps[0]['text'])
        self.assertEqual(ps[1]['at'], 60.0)          # 75 s window, next starts 20 s before its end (55 s → first turn at 60)
        self.assertIn('turn 4', ps[0]['text'])      # 60 s is inside the first window too

    def test_summary_items_plain_and_layout_skipped(self):
        from koli.meeting.pieces import pieces
        brief = {'tldr': 'We agreed the enterprise price', 'decisions': [{'id': 'D1', 'text': 'Enterprise costs 40 per seat', 'at': 120}],
                 'labels': {'tldr': 'סיכום'}, 'generator': 'codex via Koli', 'avi_vision': ['Koli answers every meeting question'],
                 'action_items': [{'task': 'ok'}]}
        ps = [p for p in pieces(self.meeting(), brief) if p['kind'] == 'summary']
        texts = [p['text'] for p in ps]
        self.assertIn('Enterprise costs 40 per seat', texts)
        self.assertEqual(next(p for p in ps if p['field'] == 'decisions')['at'], 120)
        self.assertIn('Koli answers every meeting question', texts)      # a template's own field
        self.assertFalse(any('סיכום' in t or 'codex' in t or '{' in t for t in texts))
        self.assertNotIn('ok', texts)                                     # under 15 characters

    def test_screenshot_caption_piece_and_label(self):
        from koli.meeting.pieces import embed_text, pieces
        m = self.meeting()
        shot = next(p for p in pieces(m, None) if p['kind'] == 'screenshot')
        self.assertEqual(shot['at'], 30.0)
        self.assertTrue(embed_text(m, shot).startswith('Meeting "Pricing" on 2026-11-16\n'))

    def test_empty_meeting_has_no_pieces(self):
        from koli.meeting.pieces import pieces
        self.assertEqual(pieces({'id': 'x', 'title': '', 'turns': [], 'speakers': []}, None), [])


# ── Word search ──────────────────────────────────────────────────────────────

class WordSearchTests(unittest.TestCase):
    # Ten meetings ("a".."d" plus six others), every one saying "what about the plans for today";
    # "a" also "the budget for next year, budget again", "b" "תקציב של השנה", "c" "budget" once,
    # "d" "מס״ב נפל". A query word found in no meeting still counts (lab rule), so a meeting must
    # hold it: no word hits.
    EXTRA = {'a': 'the budget for next year, budget again', 'b': 'תקציב של השנה', 'c': 'budget', 'd': 'מס״ב נפל'}

    def setUp(self):
        import datetime
        import tempfile
        from koli.meeting.store import MeetingStore, new_meeting
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))
        tz = datetime.timezone(datetime.timedelta(hours=3))
        for day, letter in enumerate('abcdefghij', start=1):
            m = new_meeting(letter, f'Meeting {letter}', {'app': 'x', 'kind': 'native', 'title': '', 'code': ''},
                            datetime.datetime(2026, 11, day, 10, 0, 0, tzinfo=tz))
            m['turns'].append({'start': 5.0, 'end': 9.0, 'speaker': 'you', 'text': 'what about the plans for today', 'lang': 'en'})
            if letter in self.EXTRA:
                m['turns'].append({'start': 20.0, 'end': 30.0, 'speaker': 'you', 'text': self.EXTRA[letter], 'lang': 'en'})
            self.store.save(m)

    def test_common_words_ignored_every_rare_word_needed(self):
        self.assertEqual([h['id'] for h in self.store.search('what about the budget')], ['a', 'c'])
        self.assertEqual(self.store.search('budget lunch'), [])    # "lunch" is in no meeting
        self.assertEqual(self.store.search('what about the plans'), [])   # only common words: no word hits

    def test_ranked_by_times_said(self):
        hits = self.store.search('budget')
        self.assertEqual(hits[0]['id'], 'a')
        self.assertGreater(hits[0]['score'], hits[1]['score'])

    def test_hit_says_where_with_snippet_and_second(self):
        hit = self.store.search('budget')[0]
        self.assertEqual((hit['where'], hit['at']), ('transcript', 20.0))
        self.assertIn('budget', hit['snippet'])
        self.assertEqual(hit['title'], 'Meeting a')

    def test_ties_newest_first(self):
        # "plans" and "today" are common; "year" is only in "a"; equal scores sort newest first.
        self.assertEqual([h['id'] for h in self.store.search('budget')][1:], ['c'])
        m = self.store.load('j')
        m['turns'].append({'start': 40.0, 'end': 41.0, 'speaker': 'you', 'text': 'budget', 'lang': 'en'})
        self.store.save(m)
        self.assertEqual([h['id'] for h in self.store.search('budget')], ['a', 'j', 'c'])

    def test_hebrew_prefix_both_ways(self):
        self.assertEqual(self.store.search('בתקציב')[0]['id'], 'b')  # the meeting says "תקציב"
        m = self.store.load('e')
        m['turns'].append({'start': 40.0, 'end': 41.0, 'speaker': 'you', 'text': 'בתקציב', 'lang': 'he'})
        self.store.save(m)                                          # now the meeting has the prefix, the query not
        self.assertEqual({h['id'] for h in self.store.search('תקציב')}, {'b', 'e'})

    def test_whole_words_only(self):
        self.assertEqual(self.store.search('udge'), [])

    def test_abbreviation_with_gershayim(self):
        self.assertEqual(self.store.search('מס״ב')[0]['id'], 'd')

    def test_meeting_filter(self):
        self.assertEqual({h['id'] for h in self.store.search('budget', meeting_id='c')}, {'c'})

    def test_limit_and_empty_query(self):
        self.assertEqual(len(self.store.search('budget', limit=1)), 1)
        self.assertEqual(self.store.search('   '), [])

    def test_changed_meeting_is_seen_again(self):
        # The cache follows the file's modification time: an edit shows up in the next search.
        self.assertEqual(self.store.search('lunch'), [])
        m = self.store.load('f')
        m['turns'].append({'start': 50.0, 'end': 51.0, 'speaker': 'you', 'text': 'lunch', 'lang': 'en'})
        self.store.save(m)
        self.assertEqual([h['id'] for h in self.store.search('lunch')], ['f'])
        self.store.delete('f')
        self.assertEqual(self.store.search('lunch'), [])

    def test_list_skips_chat_files(self):
        self.store.save_chat('a', [{'role': 'user', 'text': 'hi'}])
        with self.assertNoLogs('koli.meeting.store', level='WARNING'):
            self.assertEqual(len(self.store.list()), 10)


if __name__ == "__main__":
    unittest.main()
