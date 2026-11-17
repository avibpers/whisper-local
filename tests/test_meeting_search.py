# tests/test_meeting_search.py
# Meeting search by meaning: embeddings through Ollama, the pieces and their
# index, word search, merged results and Ask, all without a running model.
import sys
import unittest
from collections import Counter
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

    def test_few_meetings_still_searchable(self):
        # With 1 to 3 meetings every word is in over 30% of them: nothing is "common" until more than 3 hold it.
        for letter in 'defghij':
            self.store.delete(letter)
        self.assertEqual([h['id'] for h in self.store.search('budget')], ['a', 'c'])      # 3 meetings
        self.store.delete('b')
        self.store.delete('c')
        self.assertEqual([h['id'] for h in self.store.search('budget')], ['a'])           # 1 meeting

    def test_snippet_centres_on_the_whole_word(self):
        m = self.store.load('a')
        m['turns'].insert(0, {'start': 1.0, 'end': 2.0, 'speaker': 'you', 'text': 'x' * 100 + ' budgeting is not it, ' + 'y' * 100 + ' the budget', 'lang': 'en'})
        self.store.save(m)
        hit = self.store.search('budget')[0]
        self.assertEqual(hit['at'], 1.0)
        self.assertIn('the budget', hit['snippet'])
        self.assertNotIn('budgeting', hit['snippet'])

    def test_unreadable_meeting_file_is_skipped(self):
        (self.store.folder / 'zzz.json').write_text('[1, 2]', encoding='utf-8')     # not a meeting
        with self.assertLogs('koli.meeting.store', level='WARNING'):
            self.assertEqual(self.store.search('budget')[0]['id'], 'a')

    def test_overlapping_searches_do_not_crash(self):
        import threading
        errors = []
        def run():
            try:
                for _ in range(20):
                    self.store.search('budget')
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=run) for _ in range(6)]
        for t in threads: t.start()
        self.store.delete('j')
        for t in threads: t.join()
        self.assertEqual(errors, [])


# ── Index ────────────────────────────────────────────────────────────────────

class IndexTests(unittest.TestCase):
    # Two meetings: "a" talks about the budget, "b" about hiring and the budget; a fake embedder
    # hashes words into 64 unit-length dimensions and remembers every text it was asked for.
    def setUp(self):
        import tempfile
        from koli.meeting.index import MeetingIndex
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.store = MeetingStore(self.folder)
        self.embedded_texts = []
        self.m = self.meeting('a', 'Budget talk', ['the budget for next year', 'we cut the budget'])
        self.other = self.meeting('b', 'Hiring plan', ['we need to hire two people', 'the budget allows one hire'] * 3)
        self.index = MeetingIndex(self.folder, 'fake-model', self.embed)
        self.index.update(self.other, None)

    def meeting(self, meeting_id, title, lines):
        return {'id': meeting_id, 'title': title, 'started_at': '2026-11-16T10:00:00+02:00',
                'speakers': [{'id': 'you', 'name': 'You'}],
                'turns': [{'start': i * 30.0, 'end': i * 30.0 + 20, 'speaker': 'you', 'text': t} for i, t in enumerate(lines)],
                'screenshots': []}

    def embed(self, texts):
        self.embedded_texts.extend(texts)
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in text.lower().split():
                out[row, sum(map(ord, word)) % 64] += 1.0
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)

    def test_update_reuses_unchanged_pieces(self):
        n1 = self.index.update(self.m, None)
        self.m['turns'].append({'start': 900.0, 'end': 910.0, 'speaker': 'you', 'text': 'new topic'})
        n2 = self.index.update(self.m, None)
        self.assertGreater(n1, 0)
        self.assertEqual(n2, 1)

    def test_retitle_reembeds_everything(self):
        n1 = self.index.update(self.m, None)
        self.m['title'] = 'Another title'
        self.assertEqual(self.index.update(self.m, None), n1)

    def test_other_model_rebuilds(self):
        from koli.meeting.index import MeetingIndex
        self.index.update(self.m, None)
        other = MeetingIndex(self.folder, 'other-model', self.embed)
        self.assertFalse(other.is_current(self.m['id'], 0))
        self.assertEqual(other.search(self.embed(['budget'])[0]), [])   # never mixes models
        self.assertGreater(other.update(self.m, None), 0)                # nothing reused from the old model

    def test_file_has_no_pickle(self):
        self.index.update(self.m, None)
        with np.load(self.folder / f"{self.m['id']}.index.npz", allow_pickle=False) as data:
            self.assertEqual(data['vectors'].dtype, np.float16)
            self.assertEqual(str(data['model']), 'fake-model')
            data['rows']
        self.assertEqual(list(self.folder.glob('*.tmp*')), [])           # the temp file is gone

    def test_search_filter_and_cap(self):
        from koli.meeting.index import cap
        self.index.update(self.m, None)
        hits = cap(self.index.search(self.embed(['budget'])[0]), per_meeting=2, total=30)
        self.assertLessEqual(max(Counter(h['id'] for h in hits).values()), 2)
        filtered = self.index.search(self.embed(['budget'])[0], meeting_id='b')
        self.assertTrue(filtered and all(h['id'] == 'b' for h in filtered))

    def test_search_rows_ranked_with_meeting_fields(self):
        self.index.update(self.m, None)
        hits = self.index.search(self.embed(['we cut the budget'])[0])
        self.assertEqual(hits[0]['id'], 'a')
        self.assertEqual(hits[0]['title'], 'Budget talk')
        self.assertEqual(hits[0]['started_at'], '2026-11-16T10:00:00+02:00')
        self.assertEqual(hits[0]['kind'], 'transcript')
        scores = [h['score'] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(len(self.index.search(self.embed(['budget'])[0], top=1)), 1)

    def test_search_sees_a_rewritten_file(self):
        self.index.update(self.m, None)
        query = self.embed(['budget'])[0]
        before = len(self.index.search(query))
        self.m['turns'].append({'start': 900.0, 'end': 910.0, 'speaker': 'you', 'text': 'budget again'})
        self.index.update(self.m, None)
        self.assertEqual(len(self.index.search(query)), before + 1)
        self.index.remove('a')
        self.assertTrue(all(h['id'] == 'b' for h in self.index.search(query)))

    def test_is_current_follows_file_time(self):
        import os
        self.index.update(self.m, None)
        os.utime(self.folder / 'a.index.npz', (1000.0, 1000.0))
        fresh = type(self.index)(self.folder, 'fake-model', self.embed)
        self.assertTrue(fresh.is_current('a', 999.0))
        self.assertFalse(fresh.is_current('a', 1001.0))
        self.assertFalse(fresh.is_current('missing', 0))

    def test_empty_meeting_gets_an_empty_index(self):
        empty = self.meeting('e', 'Silence', [])
        self.assertEqual(self.index.update(empty, None), 0)
        self.assertTrue(self.index.is_current('e', 0))
        self.assertTrue(all(h['id'] != 'e' for h in self.index.search(self.embed(['budget'])[0])))

    def test_turn_without_start_is_skipped(self):
        from koli.meeting.pieces import pieces
        m = self.meeting('x', 'T', ['one two three'])
        m['turns'].append({'speaker': 'you', 'text': 'no start'})
        m['turns'].append({'start': 'soon', 'speaker': 'you', 'text': 'bad start'})
        self.assertEqual(len(pieces(m, None)), 1)

    def test_search_does_not_wait_for_a_slow_embed(self):
        import threading
        release = threading.Event()
        started = threading.Event()

        def slow_embed(texts):
            started.set()
            release.wait(10)
            return self.embed(texts)

        from koli.meeting.index import MeetingIndex
        slow = MeetingIndex(self.folder, 'fake-model', slow_embed)
        worker = threading.Thread(target=slow.update, args=(self.m, None))
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(5))
        finished = []
        searcher = threading.Thread(target=lambda: finished.append(slow.search(self.embed(['budget'])[0])))
        searcher.start()
        searcher.join(2)
        self.assertFalse(searcher.is_alive())          # answered while the embed was still running
        self.assertTrue(finished and all(h['id'] == 'b' for h in finished[0]))

    def test_damaged_file_is_skipped_then_rebuilt(self):
        (self.folder / 'a.index.npz').write_bytes(b'not an npz at all')
        self.assertFalse(self.index.is_current('a', 0))
        self.assertTrue(all(h['id'] == 'b' for h in self.index.search(self.embed(['budget'])[0])))
        self.assertGreater(self.index.update(self.m, None), 0)
        self.assertTrue(self.index.is_current('a', 0))

    def test_embed_row_count_mismatch_raises(self):
        from koli.meeting.index import MeetingIndex
        short = MeetingIndex(self.folder, 'fake-model', lambda texts: self.embed(texts)[:-1])
        with self.assertRaises(ValueError):
            short.update(self.m, None)
        self.assertFalse((self.folder / 'a.index.npz').exists())

    def test_delete_survives_a_locked_index_file(self):
        self.store.save(self.m)
        self.index.update(self.m, None)
        real_unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if path.name.endswith('.index.npz'):
                raise PermissionError('in use')
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', locked):
            self.assertTrue(self.store.delete('a'))
        self.assertFalse((self.folder / 'a.json').exists())

    def test_query_text(self):
        self.assertEqual(self.index.query_text('what did we decide?'),
                         'Instruct: Given a question about past meetings, retrieve the passages that answer it\nQuery: what did we decide?')

    def test_store_delete_removes_index(self):
        self.store.save(self.m)
        self.index.update(self.m, None)
        self.store.delete(self.m['id'])
        self.assertFalse((self.folder / f"{self.m['id']}.index.npz").exists())


if __name__ == "__main__":
    unittest.main()
