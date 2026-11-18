# tests/test_meeting_search.py
# Meeting search by meaning: embeddings through Ollama, the pieces and their
# index, word search, merged results and Ask, all without a running model.
import sys
import time
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
        # Only common words: every one of them is needed, so the meetings holding them all are word hits.
        self.assertEqual(len(self.store.search('what about the plans')), 10)

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

    def test_only_common_words_still_find_meetings(self):
        # "plans" is in every meeting: common, yet the only word asked, so it is needed rather than dropped.
        self.assertEqual(len(self.store.search('plans')), 10)
        self.assertEqual(self.store.search('plans lunch'), [])          # "lunch" is in no meeting

    def test_one_meeting_needs_every_query_word_even_common_ones(self):
        self.assertEqual([h['id'] for h in self.store.search('plans', meeting_id='e')], ['e'])
        self.assertEqual([h['id'] for h in self.store.search('plans budget', meeting_id='c')], ['c'])
        self.assertEqual(self.store.search('plans budget', meeting_id='e'), [])   # e never says budget

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
        meeting = {'id': meeting_id, 'title': title, 'started_at': '2026-11-16T10:00:00+02:00',
                   'speakers': [{'id': 'you', 'name': 'You'}],
                   'turns': [{'start': i * 30.0, 'end': i * 30.0 + 20, 'speaker': 'you', 'text': t} for i, t in enumerate(lines)],
                   'screenshots': []}
        self.store.save(meeting)           # a search only trusts an index whose meeting file exists
        return meeting

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
        self.index.update(self.m, None)
        real_unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if path.name.endswith('.index.npz'):
                raise PermissionError('in use')
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', locked), self.assertLogs('koli.meeting.store', 'WARNING'):
            self.assertTrue(self.store.delete('a'))
            self.assertFalse((self.folder / 'a.json').exists())
            self.assertTrue((self.folder / 'a.index.npz').exists())         # could not be removed...
            hits = self.index.search(self.embed(['budget'])[0])
            self.assertTrue(hits and all(h['id'] == 'b' for h in hits))    # ...but never found again
            self.assertEqual(self.index.search(self.embed(['budget'])[0], meeting_id='a'), [])

    def test_orphan_index_is_removed_by_a_search(self):
        self.index.update(self.m, None)
        (self.folder / 'a.json').unlink()
        self.index.search(self.embed(['budget'])[0])
        self.assertFalse((self.folder / 'a.index.npz').exists())

    def test_concurrent_updates_keep_the_newer_state(self):
        import threading
        gate = threading.Event()
        started = threading.Event()
        calls = []

        def gated_embed(texts):
            calls.append(list(texts))
            if len(calls) == 1:                         # the first (older) update stalls in the model
                started.set()
                gate.wait(10)
            return self.embed(texts)

        from koli.meeting.index import MeetingIndex
        index = MeetingIndex(self.folder, 'fake-model', gated_embed)
        old_state = self.meeting('a', 'Budget talk', ['the budget for next year'])
        new_state = self.meeting('a', 'Budget talk', ['the budget for next year', 'a later sentence added'])
        first = threading.Thread(target=index.update, args=(old_state, None))
        first.start()
        self.assertTrue(started.wait(5))
        second = threading.Thread(target=index.update, args=(new_state, None))
        second.start()
        gate.set()
        first.join(10)
        second.join(10)
        texts = ' '.join(h['text'] for h in index.search(self.embed(['sentence'])[0], meeting_id='a'))
        self.assertIn('later sentence', texts)          # the older update did not win

    def test_query_text(self):
        self.assertEqual(self.index.query_text('what did we decide?'),
                         'Instruct: Given a question about past meetings, retrieve the passages that answer it\nQuery: what did we decide?')

    def test_store_delete_removes_index(self):
        self.store.save(self.m)
        self.index.update(self.m, None)
        self.store.delete(self.m['id'])
        self.assertFalse((self.folder / f"{self.m['id']}.index.npz").exists())


# ── Merging words and meaning ────────────────────────────────────────────────

def w(meeting_id):
    """A word hit (one per meeting) as MeetingStore.search returns it."""
    return {'id': meeting_id, 'title': f'T-{meeting_id}', 'started_at': '2026-11-01T10:00:00+03:00',
            'score': 1.0, 'where': 'transcript', 'snippet': f'word {meeting_id}', 'at': 5}


def mh(meeting_id, score, at=None, text=None, kind='transcript'):
    """A meaning hit (a piece) as MeetingIndex.search returns it."""
    return {'kind': kind, 'field': 'transcript', 'at': at, 'text': text or f'piece {meeting_id} {at}',
            'id': meeting_id, 'title': f'T-{meeting_id}', 'started_at': '2026-11-01T10:00:00+03:00',
            'score': score}


class MergeTests(unittest.TestCase):
    def test_found_by_both_ranks_above_either(self):
        from koli.meeting.find import merge
        words = [w('x'), w('both')]; meaning = [mh('y', 0.9), mh('both', 0.8)]
        out = merge(words, meaning)
        self.assertEqual(out[0]['id'], 'both')
        self.assertEqual(out[0]['by'], ['words', 'meaning'])

    def test_places_capped_meaning_first(self):
        from koli.meeting.find import merge
        out = merge([w('a')], [mh('a', 0.9, at=10), mh('a', 0.8, at=20), mh('a', 0.7, at=30), mh('a', 0.6, at=40)], places=3)
        self.assertEqual([p['at'] for p in out[0]['places']], [10, 20, 30])

    def test_words_only_when_no_meaning(self):
        from koli.meeting.find import merge
        self.assertEqual([r['id'] for r in merge([w('a'), w('b')], [])], ['a', 'b'])

    def test_meaning_rank_is_best_piece_per_meeting(self):
        # Meeting "a" has two pieces ahead of "b"'s: still one rank each, a first, b second.
        from koli.meeting.find import merge
        out = merge([], [mh('a', 0.9, at=1), mh('a', 0.8, at=2), mh('b', 0.7, at=3)])
        self.assertEqual([r['id'] for r in out], ['a', 'b'])
        self.assertEqual([r['by'] for r in out], [['meaning'], ['meaning']])

    def test_word_place_added_when_new_and_skipped_when_same(self):
        from koli.meeting.find import merge
        # Word hit at 5/transcript is new next to a meaning piece at 10: added after it.
        out = merge([w('a')], [mh('a', 0.9, at=10)])
        self.assertEqual([p['at'] for p in out[0]['places']], [10, 5])
        # Same second and place as the meaning piece: not repeated.
        out = merge([w('a')], [mh('a', 0.9, at=5)])
        self.assertEqual([p['at'] for p in out[0]['places']], [5])

    def test_summary_word_place_kept_when_its_snippet_differs(self):
        from koli.meeting.find import merge
        word = {**w('a'), 'where': 'summary', 'at': None, 'snippet': 'the budget was approved'}
        out = merge([word], [mh('a', 0.9, kind='summary', text='hiring two people')])
        self.assertEqual([p['snippet'] for p in out[0]['places']], ['hiring two people', 'the budget was approved'])
        out = merge([word], [mh('a', 0.9, kind='summary', text='the budget was approved')])
        self.assertEqual(len(out[0]['places']), 1)                  # the same place and text: shown once

    def test_meaning_kind_and_text_become_where_and_snippet(self):
        from koli.meeting.find import merge
        out = merge([], [mh('a', 0.9, at=7, text='line one\nline two', kind='summary')])
        self.assertEqual(out[0]['places'], [{'where': 'summary', 'snippet': 'line one · line two', 'at': 7}])

    def test_snippet_capped_at_160_characters(self):
        from koli.meeting.find import merge
        out = merge([], [mh('a', 0.9, at=1, text='ab\n' + 'x' * 400)])
        snippet = out[0]['places'][0]['snippet']
        self.assertEqual(len(snippet), 160)
        self.assertTrue(snippet.startswith('ab · x'))
        self.assertNotIn('\n', snippet)

    def test_limit_and_title_from_whichever_list_has_it(self):
        from koli.meeting.find import merge
        out = merge([w('a')], [mh('b', 0.9, at=1)], limit=1)
        self.assertEqual(len(out), 1)
        out = merge([w('a')], [mh('b', 0.9, at=1)])
        self.assertEqual({r['id']: r['title'] for r in out}, {'a': 'T-a', 'b': 'T-b'})


# ── The search service and its indexer ───────────────────────────────────────

class SearchServiceTests(unittest.TestCase):
    # Three meetings in a temp folder ("a" talks about the budget); a fake model that hashes words
    # into unit vectors and counts its calls; settings and "a meeting is running" are plain attributes.
    def setUp(self):
        import tempfile
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))
        self.settings = {'meeting.search.meaning': True, 'meeting.search.model': 'fake-model'}
        self.running = False
        self.embed_calls = 0
        self.published = []
        for letter, line in (('a', 'the budget for next year'), ('b', 'we need to hire two people'),
                             ('c', 'the office moves in spring')):
            self.store.save({'id': letter, 'title': f'Meeting {letter}', 'started_at': '2026-11-16T10:00:00+02:00',
                             'speakers': [{'id': 'you', 'name': 'You'}],
                             'turns': [{'start': 0.0, 'end': 20.0, 'speaker': 'you', 'text': line}],
                             'screenshots': []})

    def fake_embed(self, model, texts):
        self.embed_calls += 1
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in text.lower().split():
                out[row, sum(map(ord, word)) % 64] += 1.0
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)

    def service(self, embed=None, tasks=None):
        from koli.services.meeting_search import MeetingSearch
        svc = MeetingSearch(self.store, setting=lambda path, default=None: self.settings.get(path, default),
                            is_running=lambda: self.running,
                            publish=lambda topic, payload: self.published.append((topic, payload)),
                            embed=embed or self.fake_embed, tasks=tasks)
        self.store.on_change = svc.changed
        self.addCleanup(svc.stop)
        return svc

    def raising(self, error):
        def embed(model, texts):
            raise error
        return embed

    def sleeping(self, seconds):
        def embed(model, texts):
            time.sleep(seconds)
            return self.fake_embed(model, texts)
        return embed

    def test_search_without_model_returns_words_and_reason(self):
        from koli.llm.types import ProviderError
        svc = self.service(embed=self.raising(ProviderError('model "qwen3-embedding:0.6b" not found')))
        out = svc.search('budget')
        self.assertEqual(out['meaning'], 'no_model')
        self.assertEqual(out['results'][0]['by'], ['words'])

    def test_missing_model_as_http_404_is_no_model(self):
        from koli.llm.http import HttpError
        svc = self.service(embed=self.raising(HttpError(404, '{"error":"model not found, try pulling it first"}')))
        self.assertEqual(svc.search('budget')['meaning'], 'no_model')

    def test_ollama_not_running_is_no_ollama(self):
        from koli.llm.http import NetworkError
        svc = self.service(embed=self.raising(NetworkError('[WinError 10061] connection refused')))
        out = svc.search('budget')
        self.assertEqual(out['meaning'], 'no_ollama')
        self.assertEqual(out['results'][0]['by'], ['words'])

    def test_slow_model_returns_loading(self):
        svc = self.service(embed=self.sleeping(3.0))
        t = time.monotonic()
        out = svc.search('budget')
        self.assertLess(time.monotonic() - t, 2.5)
        self.assertEqual(out['meaning'], 'loading')
        self.assertEqual(out['results'][0]['by'], ['words'])

    def test_meaning_results_join_the_words(self):
        svc = self.service()
        svc.run_all()
        out = svc.search('budget')
        self.assertEqual(out['meaning'], 'on')
        self.assertEqual(out['results'][0]['id'], 'a')
        self.assertEqual(out['results'][0]['by'], ['words', 'meaning'])

    def test_meaning_while_backfill_is_indexing(self):
        svc = self.service()
        svc.run_all()
        self.store.save({'id': 'd', 'title': 'Meeting d', 'started_at': '2026-11-16T10:00:00+02:00', 'speakers': [],
                         'turns': [{'start': 0.0, 'end': 5.0, 'speaker': 'you', 'text': 'budget again'}],
                         'screenshots': []})
        out = svc.search('budget')
        self.assertEqual(out['meaning'], 'indexing')
        self.assertTrue(any(r['by'] == ['words', 'meaning'] for r in out['results']))

    def test_one_meeting_filter_shows_ten_places(self):
        svc = self.service()
        self.store.save({'id': 'a', 'title': 'Meeting a', 'started_at': '2026-11-16T10:00:00+02:00',
                         'speakers': [{'id': 'you', 'name': 'You'}],
                         'turns': [{'start': i * 80.0, 'end': i * 80.0 + 20, 'speaker': 'you',
                                    'text': f'budget point {i}'} for i in range(8)], 'screenshots': []})
        svc.run_all()
        self.assertEqual(len(svc.search('budget', meeting_id='a')['results'][0]['places']), 8)
        self.assertEqual(len(svc.search('budget')['results'][0]['places']), 3)

    def test_indexer_waits_while_meeting_runs(self):
        self.running = True
        svc = self.service()
        svc.changed('a')
        svc.run_pending(now=time.time() + 11)
        self.assertEqual(self.embed_calls, 0)
        self.running = False
        svc.run_pending(now=time.time() + 11)
        self.assertGreater(self.embed_calls, 0)

    def test_changes_are_debounced_ten_seconds(self):
        svc = self.service()
        svc.changed('a')
        svc.run_pending(now=time.time() + 5)
        self.assertEqual(self.embed_calls, 0)
        self.assertIn('a', svc.pending())

    def test_store_change_queues_meeting(self):
        svc = self.service()
        self.store.save_brief('a', {'tldr': 'We agreed the enterprise price'})
        self.assertIn('a', svc.pending())

    def test_store_calls_listener_for_save_brief_and_delete_and_survives_its_failure(self):
        seen = []
        self.store.on_change = seen.append
        self.store.save_brief('a', {'tldr': 'x'})
        self.store.delete('b')
        self.assertEqual(seen, ['a', 'b'])

        def broken(meeting_id):
            raise RuntimeError('listener bug')
        self.store.on_change = broken
        self.store.save_brief('a', {'tldr': 'y'})          # must not raise
        self.assertTrue(self.store.brief_path('a').exists())

    def test_retitling_brief_notifies_after_it_is_written(self):
        seen = []
        self.store.on_change = lambda meeting_id: seen.append(self.store.brief_path(meeting_id).exists())
        self.store.save_brief('a', {'title': 'Budget cut agreed'})
        self.assertTrue(seen and all(seen))

    def test_deleted_meeting_loses_its_index_without_embedding(self):
        svc = self.service()
        svc.run_all()
        self.assertTrue((Path(self.tmp.name) / 'b.index.npz').exists())
        calls = self.embed_calls
        self.store.delete('b')
        svc.run_pending(now=time.time() + 11)
        self.assertEqual(self.embed_calls, calls)
        self.assertNotIn('b', svc.pending())
        self.assertFalse((Path(self.tmp.name) / 'b.index.npz').exists())

    def test_model_change_reindexes(self):
        svc = self.service()
        svc.run_all()
        self.settings['meeting.search.model'] = 'other'
        svc.run_pending(now=time.time())
        self.assertEqual(set(svc.pending()), {'a', 'b', 'c'})

    def test_search_after_model_change_queues_and_never_mixes_models(self):
        svc = self.service()
        svc.run_all()
        self.settings['meeting.search.model'] = 'other'
        out = svc.search('budget')
        self.assertEqual(set(svc.pending()), {'a', 'b', 'c'})
        self.assertEqual(out['results'][0]['by'], ['words'])      # the old model's vectors are not used

    def test_meaning_off_setting(self):
        self.settings['meeting.search.meaning'] = False
        svc = self.service()
        self.assertEqual(svc.search('budget')['meaning'], 'off')
        svc.run_all()
        self.assertEqual(self.embed_calls, 0)

    def test_failing_model_keeps_meetings_queued_and_waits_before_retrying(self):
        from koli.llm.http import NetworkError
        svc = self.service(embed=self.raising(NetworkError('refused')))
        svc.changed('a')
        svc.run_pending(now=time.time() + 11)
        self.assertIn('a', svc.pending())
        self.assertEqual(svc.status()['meaning'], 'no_ollama')
        calls_before_wait = self.embed_calls
        svc.run_pending(now=time.time() + 12)                # still inside the retry wait
        self.assertEqual(self.embed_calls, calls_before_wait)
        self.assertIn('a', svc.pending())

    def test_one_broken_meeting_does_not_stall_the_rest_or_blame_ollama(self):
        def embed(model, texts):
            if any('Meeting a' in text for text in texts):
                raise ValueError('cannot read this meeting')
            return self.fake_embed(model, texts)
        svc = self.service(embed=embed)
        for meeting_id in 'abc':
            svc.changed(meeting_id)
        now = time.time() + 11
        for _ in range(3):
            svc.run_pending(now=now)
        self.assertEqual(svc.pending(), ['a'])                  # b and c got their turn; a waits its retry
        self.assertEqual(svc.status()['indexed'], 2)
        self.assertEqual(svc.status()['meaning'], 'on')         # a failed meeting is not waited for; not "no_ollama"
        svc.run_pending(now=now + 61)                           # second try
        self.assertEqual(svc.pending(), ['a'])
        svc.run_pending(now=now + 122)                          # third try: given up, the status settles
        self.assertEqual(svc.pending(), [])
        self.assertEqual(svc.status()['meaning'], 'on')
        svc.changed('a')                                        # new content is worth a new try
        self.assertEqual(svc.pending(), ['a'])

    def test_ollama_down_is_never_given_up_on(self):
        from koli.llm.http import NetworkError
        svc = self.service(embed=self.raising(NetworkError('refused')))
        svc.changed('a')
        for step in range(1, 6):
            svc.run_pending(now=time.time() + 11 + step * 61)
        self.assertEqual(svc.pending(), ['a'])

    def test_busy_ollama_never_gives_up_a_meeting(self):
        from koli.llm.http import HttpError
        from koli.llm.types import ProviderError
        busy = [True]
        errors = [HttpError(503, 'server busy'), ProviderError('overloaded', status=429)]

        def embed(model, texts):
            if busy[0]:
                errors.reverse()
                raise errors[0]
            return self.fake_embed(model, texts)
        svc = self.service(embed=embed)
        for meeting_id in 'abc':
            svc.changed(meeting_id)
        now = time.time() + 11
        for step in range(8):                                  # many passes, each a model error
            svc.run_pending(now=now + step * 61)
        self.assertEqual(svc.pending(), ['a', 'b', 'c'])       # nobody given up: the server was at fault
        busy[0] = False
        for step in range(8, 14):
            svc.run_pending(now=now + step * 61)
        self.assertEqual(svc.pending(), [])
        self.assertEqual(svc.status(), {'meaning': 'on', 'indexed': 3, 'total': 3, 'model': 'fake-model'})

    def test_busy_ollama_shows_loading_and_never_blocks_searches(self):
        from koli.llm.http import HttpError
        busy = [True]

        def embed(model, texts):
            if busy[0] and any(text.startswith('Meeting') for text in texts):   # pieces fail, a query answers
                raise HttpError(503, 'server busy')
            return self.fake_embed(model, texts)
        svc = self.service(embed=embed)
        svc.changed('a')
        svc.run_pending(now=time.time() + 11)
        self.assertEqual(svc.status()['meaning'], 'loading')
        self.assertNotIn(svc.search('budget')['meaning'], ('no_ollama', 'no_model'))

    def test_meetings_own_model_error_is_given_up_and_never_blocks_searches(self):
        from koli.llm.types import ProviderError

        def embed(model, texts):
            if any('Meeting a' in text for text in texts):
                raise ProviderError('input too long')            # Ollama answered: this meeting is the problem
            return self.fake_embed(model, texts)
        svc = self.service(embed=embed)
        for meeting_id in 'abc':
            svc.changed(meeting_id)
        now = time.time() + 11
        for _ in range(3):
            svc.run_pending(now=now)
        self.assertEqual(svc.pending(), ['a'])
        self.assertEqual(svc.status()['meaning'], 'on')         # not "needs Ollama running"
        out = svc.search('hire people')
        self.assertEqual(out['meaning'], 'on')
        self.assertIn('meaning', out['results'][0]['by'])       # meaning still answers for the other meetings
        svc.run_pending(now=now + 61)
        svc.run_pending(now=now + 122)                          # third try: given up
        self.assertEqual(svc.pending(), [])

    def test_bad_request_is_the_meetings_own_error(self):
        from koli.llm.http import HttpError
        from koli.services.meeting_search import failure_kind, problem_of
        self.assertEqual(failure_kind(HttpError(400, 'input too large')), 'own')
        self.assertIsNone(problem_of(HttpError(400, 'input too large')))
        self.assertEqual(failure_kind(ValueError('embed returned 3 vectors for 4 texts')), 'own')
        self.assertEqual(problem_of(HttpError(500, 'boom')), 'loading')
        self.assertEqual(problem_of(HttpError(404, 'model not found')), 'no_model')

    # Meeting "a" again, with one 75 s piece per turn: `pieces` of them.
    def long_meeting(self, pieces=12):
        self.store.save({'id': 'a', 'title': 'Meeting a', 'started_at': '2026-11-16T10:00:00+02:00',
                         'speakers': [{'id': 'you', 'name': 'You'}],
                         'turns': [{'start': i * 80.0, 'end': i * 80.0 + 20, 'speaker': 'you',
                                    'text': f'budget point {i}'} for i in range(pieces)], 'screenshots': []})

    def test_timeout_retries_the_meeting_in_small_batches(self):
        from koli.llm.http import TimedOut
        sizes = []

        def slow_for_big_batches(model, texts):
            sizes.append(len(texts))
            if len(texts) > 8:
                raise TimedOut('timed out')
            return self.fake_embed(model, texts)
        self.long_meeting()
        svc = self.service(embed=slow_for_big_batches)
        svc.run_all()
        self.assertEqual(svc.status(), {'meaning': 'on', 'indexed': 3, 'total': 3, 'model': 'fake-model'})
        first = sizes.index(12)                                 # tried whole first, then in eights
        self.assertEqual(sizes[first + 1:first + 3], [8, 4])

    def test_timeout_every_time_shows_loading_and_is_given_up(self):
        from koli.llm.http import TimedOut
        self.long_meeting()
        svc = self.service(embed=self.raising(TimedOut('timed out')))
        svc.changed('a')
        now = time.time() + 11
        svc.run_pending(now=now)
        self.assertEqual(svc.status()['meaning'], 'loading')    # slow, not "needs Ollama running"
        self.assertIn('a', svc.pending())
        svc.run_pending(now=now + 61)
        svc.run_pending(now=now + 122)                          # third timed-out try: given up
        self.assertEqual(svc.pending(), [])
        self.assertEqual(svc.status()['meaning'], 'on')

    def test_searches_between_timeouts_do_not_reset_the_meetings_tries(self):
        from koli.llm.http import TimedOut

        def embed(model, texts):
            if len(texts) > 1:                                  # every piece batch times out; a query answers
                raise TimedOut('timed out')
            return self.fake_embed(model, texts)
        self.long_meeting()
        svc = self.service(embed=embed)
        svc.changed('a')
        now = time.time() + 11
        for step in range(3):
            svc.run_pending(now=now + step * 61)
            self.assertEqual(svc.search('budget')['meaning'] in ('on', 'indexing'), True)
        self.assertEqual(svc.pending(), [])                     # given up after three timed-out tries

    def test_many_searches_on_a_slow_model_make_one_model_call(self):
        calls = []

        def slow(model, texts):
            calls.append(texts)
            time.sleep(0.6)
            return self.fake_embed(model, texts)
        svc = self.service(embed=slow)
        with mock.patch('koli.services.meeting_search.QUERY_WAIT_S', 0.05):
            states = [svc.search('budget')['meaning'] for _ in range(5)]
        self.assertEqual(states, ['loading'] * 5)
        time.sleep(0.8)
        self.assertEqual(len(calls), 1)

    def test_late_error_shows_on_the_next_search_then_it_tries_again(self):
        from koli.llm.http import NetworkError
        outcomes = [NetworkError('refused'), None]

        def embed(model, texts):
            time.sleep(0.3)
            outcome = outcomes.pop(0)
            if outcome:
                raise outcome
            return self.fake_embed(model, texts)
        svc = self.service(embed=embed)
        with mock.patch('koli.services.meeting_search.QUERY_WAIT_S', 0.05):
            self.assertEqual(svc.search('budget')['meaning'], 'loading')
            time.sleep(0.5)
            self.assertEqual(svc.status()['meaning'], 'no_ollama')       # recorded when the call failed
            self.assertEqual(svc.search('budget')['meaning'], 'no_ollama')
        self.assertEqual(svc.search('budget')['meaning'], 'no_ollama')   # known problem: no new call for RETRY_S
        with mock.patch.object(svc, '_clock', return_value=time.time() + 61):
            self.assertEqual(svc.search('budget')['meaning'], 'on')      # the rest is over: it tries again

    def test_without_ollama_searches_answer_at_once(self):
        from koli.llm.http import NetworkError
        calls = []

        def refusing(model, texts):
            calls.append(texts)
            time.sleep(0.5)                                   # Windows takes a while to refuse localhost
            raise NetworkError('[WinError 10061] connection refused')
        svc = self.service(embed=refusing)
        self.assertEqual(svc.search('budget')['meaning'], 'no_ollama')
        for query in ('budget', 'hire', 'office', 'spring', 'people'):
            started = time.monotonic()
            out = svc.search(query)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(out['meaning'], 'no_ollama')
        self.assertEqual(len(calls), 1)

    def test_another_question_never_gets_the_in_flight_vector(self):
        seen = []

        def embed(model, texts):
            seen.append(texts[0])
            time.sleep(0.3)
            return self.fake_embed(model, texts)
        svc = self.service(embed=embed)
        with mock.patch('koli.services.meeting_search.QUERY_WAIT_S', 0.05):
            svc.search('budget')                                          # loading, call 1 in flight
        out = svc.search('hire')                                          # waits for call 1, then asks its own
        self.assertEqual(out['meaning'], 'on')
        self.assertEqual(len(seen), 2)
        self.assertIn('Query: hire', seen[1])

    def test_stop_then_start_runs_one_indexer(self):
        svc = self.service()
        svc.start()
        first = svc._thread
        svc.stop()
        svc.start()
        second = svc._thread
        self.assertIsNot(first, second)
        first.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertTrue(second.is_alive())
        svc.stop()

    def test_status_counts_indexed_meetings_and_is_published(self):
        svc = self.service()
        self.assertEqual((svc.status()['indexed'], svc.status()['total']), (0, 3))
        svc.run_all()
        status = svc.status()
        self.assertEqual((status['indexed'], status['total'], status['meaning'], status['model']),
                         (3, 3, 'on', 'fake-model'))
        self.assertTrue(self.published)
        self.assertEqual(self.published[-1][0], 'meeting_search')
        self.assertEqual(self.published[-1][1]['indexed'], 3)

    def test_download_pulls_with_steady_progress_then_queues_everything(self):
        pulled, progress = [], []

        class Reporter:
            def report(self, fraction=None, detail=None):
                progress.append(fraction)

            def check_cancelled(self):
                pass

        class Tasks:
            def run(self, label, fn, kind='general', on_done=None, ref=None):
                self.label, self.kind, self.ref = label, kind, ref
                fn(Reporter())
                return type('T', (), {'id': 7, 'status': 'done'})()

        def pull(address, model, on_progress, timeout=3600):
            pulled.append((address, model))
            for fraction in (0.0, 0.4, 0.0, 0.2, 1.0):     # size-less lines report 0.0 between layers
                on_progress(fraction, 'pulling')
        tasks = Tasks()
        svc = self.service(tasks=tasks)
        with mock.patch('koli.llm.transports.ollama.pull', pull):
            self.assertEqual(svc.download(), 7)
        self.assertEqual(pulled, [('http://localhost:11434', 'fake-model')])
        self.assertEqual((tasks.label, tasks.kind, tasks.ref),
                         ('Downloading the meaning model', 'model', 'meeting-search'))
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(set(svc.pending()), {'a', 'b', 'c'})

    def test_hits_for_caps_pieces_per_meeting(self):
        svc = self.service()
        svc.run_all()
        hits = svc.hits_for('budget', None, per_meeting=1, total=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(len({h['id'] for h in hits}), 2)

    def test_hits_for_is_empty_when_meaning_is_unavailable(self):
        from koli.llm.http import NetworkError
        svc = self.service(embed=self.raising(NetworkError('refused')))
        self.assertEqual(svc.hits_for('budget', None, 4, 30), [])

    def test_start_scans_and_indexes_in_the_background_then_stops(self):
        svc = self.service()
        svc.start()
        deadline = time.time() + 10
        while time.time() < deadline and svc.status()['indexed'] < 3:
            time.sleep(0.05)
        svc.stop()
        self.assertEqual(svc.status()['indexed'], 3)


# ── Settings and the meeting service's wiring ────────────────────────────────

class SearchSettingsAndWiringTests(unittest.TestCase):
    def test_settings_are_in_meetings_search_group_with_defaults(self):
        from ruamel.yaml import YAML
        from koli.settings import schema
        for path in ('meeting.search.meaning', 'meeting.search.model'):
            self.assertEqual((schema.get(path).section, schema.get(path).group), ('meetings', 'Search'), path)
        defaults = YAML(typ='safe').load((ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8'))
        self.assertEqual(defaults['meeting']['search'], {'meaning': True, 'model': 'qwen3-embedding:0.6b'})

    def test_service_wires_the_store_to_the_finder_and_keeps_usage_on_results(self):
        import tempfile
        from koli.meeting.store import MeetingStore
        from koli.services.meetings import MeetingService
        from tests.test_meeting import FakeConfig, FakeLoop, FakeStateManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig({'meeting.enabled': False, 'meeting.search.meaning': False})
        ctx.model_registry = None
        store = MeetingStore(Path(tmp.name))
        svc = MeetingService(ctx, store=store, publish=lambda t, p: None, loopback_factory=FakeLoop,
                             embedder_factory=lambda: None, loopback_available=lambda: True,
                             sessions_fn=lambda: [], autostart=False)
        self.addCleanup(svc.shutdown)
        store.save({'id': 'a', 'title': 'Budget talk', 'started_at': '2026-11-16T10:00:00+02:00',
                    'speakers': [], 'turns': [{'start': 0.0, 'end': 5.0, 'speaker': 'you', 'text': 'budget'}],
                    'screenshots': []})
        self.assertEqual(svc.finder.pending(), ['a'])        # the store told the finder
        out = svc.search('budget')
        self.assertEqual(out['meaning'], 'off')
        self.assertEqual([r['id'] for r in out['results']], ['a'])
        self.assertIn('usage', out['results'][0])


class AskTests(unittest.TestCase):
    # Ask all meetings with a scripted AI: the first reply is the plan, the second the answer.
    def setUp(self):
        import json
        import tempfile
        from types import SimpleNamespace
        self.json, self.reply_type = json, SimpleNamespace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hits_for_calls = []
        self.messages_seen = []
        self.replies = [{'task': 'pricing', 'search': ['pricing'], 'open': []},
                        {'reply': 'Pricing was set [1].', 'refs': [{'id': 'm1', 'at': 30}]}]
        self.heads = [{'id': 'm1', 'title': 'Pricing call', 'source': 'zoom', 'started_at': '2026-11-01T10:00:00+02:00',
                       'speakers': ['Shahar', 'Dana'], 'turns': 2, 'brief': True},
                      {'id': 'm2', 'title': 'Planning', 'source': 'meet', 'started_at': '2026-11-02T10:00:00+02:00',
                       'speakers': ['You'], 'turns': 1, 'brief': False},
                      {'id': 'long', 'title': 'Long one', 'source': 'meet', 'started_at': '2026-11-03T10:00:00+02:00',
                       'speakers': ['You'], 'turns': 1216, 'brief': True}]
        self.meetings = {
            'm1': self.meeting('m1', 'Pricing call', 2),
            'm2': self.meeting('m2', 'Planning', 1),
            'long': self.meeting('long', 'Long one', 1216)}

    @staticmethod
    def meeting(meeting_id, title, turns):
        return {'id': meeting_id, 'title': title, 'source': 'meet', 'started_at': '2026-11-01T10:00:00+02:00',
                'speakers': [{'id': 'sp1', 'name': 'Shahar'}],
                'turns': [{'start': 15.0 * (i + 1), 'end': 15.0 * (i + 1) + 10, 'speaker': 'sp1',
                           'text': f'turn {i + 1} ' + 'word ' * 12} for i in range(turns)],
                'screenshots': []}

    def load(self, meeting_id):
        return self.meetings[meeting_id]

    def hits_for(self, question, meeting_id, per_meeting, total):
        self.hits_for_calls.append((question, meeting_id, per_meeting, total))
        who = meeting_id or 'm1'
        return [{'kind': 'transcript', 'field': 'transcript', 'at': 30 + i, 'text': f'piece {i} ' + 'x' * 200,
                 'id': who, 'title': f'T-{who}', 'started_at': '2026-11-01T10:00:00+02:00', 'score': 1.0 - i / 100}
                for i in range(total)]

    def complete(self, feature, msgs, json=False):
        self.messages_seen.append((feature, msgs))
        return self.reply_type(ok=True, text=self.json.dumps(self.replies.pop(0)), error='')

    def service(self):
        from koli.services.meeting_search import MeetingSearch
        from koli.meeting.store import MeetingStore
        store = MeetingStore(Path(self.tmp.name) / 'meetings')
        for meeting in self.meetings.values():
            store.save(meeting)
        return MeetingSearch(store, setting=lambda path, default=None: False, is_running=lambda: False,
                             publish=lambda *a: None, embed=lambda model, texts: None,
                             complete=self.complete, parse=self.json.loads, ai_ready=lambda: True,
                             ask_path=Path(self.tmp.name) / 'meetings_ask.json')

    def test_plan_drops_unknown_ids(self):
        from koli.meeting.ask import clean_plan
        self.assertEqual(clean_plan({'task': 't', 'search': ['a', 'b', 'c', 'd'], 'open': ['m1', 'nope']}, {'m1'}),
                         {'task': 't', 'search': ['a', 'b', 'c'], 'open': ['m1']})

    def test_meeting_list_has_one_line_each(self):
        from koli.meeting.ask import meeting_list
        lines = meeting_list(self.heads).splitlines()
        self.assertEqual(lines[0], 'm1 | 2026-11-01 10:00 | Pricing call | zoom | Shahar, Dana | 2 | summary')
        self.assertTrue(lines[1].endswith('| 1 | no summary'))

    def test_open_meeting_goes_in_whole(self):
        from koli.meeting.ask import material
        text, given = material({'search': [], 'open': ['m1']}, 'q', self.load, self.hits_for)
        self.assertIn('0:15 Shahar: turn 1', text)
        self.assertIn('[Pricing call | 2026-11-01 | m1 | full transcript]', text)
        self.assertIn('m1', given)
        self.assertEqual(given['m1'], 40.0)             # the last turn's end

    def test_open_meeting_skips_turns_without_a_numeric_start(self):
        from koli.meeting.ask import material
        self.meetings['m1']['turns'] += [{'speaker': 'sp1', 'text': 'missing'},
                                         {'start': None, 'speaker': 'sp1', 'text': 'empty'},
                                         {'start': 'soon', 'speaker': 'sp1', 'text': 'worded'}]
        text, given = material({'search': [], 'open': ['m1']}, 'q', self.load, self.hits_for)
        self.assertIn('0:15 Shahar: turn 1', text)
        for skipped in ('missing', 'empty', 'worded'):
            self.assertNotIn(skipped, text)

    def test_material_uses_pieces_when_meeting_too_long(self):
        from koli.meeting.ask import MAX_MATERIAL, material
        text, given = material({'search': [], 'open': ['long', 'm1', 'm2']}, 'q', self.load, self.hits_for)
        self.assertLessEqual(len(text), MAX_MATERIAL)
        self.assertEqual(self.hits_for_calls[-1][1], 'long')            # restricted to that meeting
        self.assertNotIn('Long one | 2026-11-01 | long | full transcript', text)
        self.assertIn('T-long | 2026-11-01 | long | transcript 0:30', text)   # its pieces instead
        self.assertIn('0:15 Shahar: turn 1', text)                      # the short ones still went in whole
        self.assertEqual(set(given), {'long', 'm1', 'm2'})

    def test_search_pieces_come_after_opened_and_skip_opened(self):
        from koli.meeting.ask import material
        text, given = material({'search': ['pricing'], 'open': ['m2']}, 'q', self.load, self.hits_for)
        self.assertLess(text.index('m2 | full transcript'), text.index('m1 | transcript 0:30'))
        self.assertEqual(self.hits_for_calls[-1], ('pricing', None, 4, 30))
        self.assertEqual(text.count('m1 | transcript'), 4)              # at most 4 pieces of one meeting
        self.assertEqual(set(given), {'m1', 'm2'})

    def test_answer_drops_unknown_refs_and_markers(self):
        from koli.meeting.ask import clean_answer
        out = clean_answer({'reply': 'Pricing [1], budget [2].', 'refs': [{'id': 'm1', 'at': 30}, {'id': 'ghost', 'at': 5}]},
                           {'m1': 600.0})
        self.assertEqual(out['reply'], 'Pricing [1], budget.')
        self.assertEqual([r['id'] for r in out['refs']], ['m1'])

    def test_kept_refs_keep_their_numbers(self):
        from koli.meeting.ask import clean_answer
        out = clean_answer({'reply': 'A [1] B [2] C [3]', 'refs': [{'id': 'ghost', 'at': 1}, {'id': 'm1', 'at': 5},
                                                                   {'id': 'm1', 'at': 'x'}]},
                           {'m1': 600.0}, {'m1': 'Pricing call'})
        self.assertEqual(out['reply'], 'A B [2] C')
        self.assertEqual(out['refs'], [{'n': 2, 'id': 'm1', 'title': 'Pricing call', 'at': 5}])

    def test_ref_past_the_end_dropped(self):
        from koli.meeting.ask import clean_answer
        self.assertEqual(clean_answer({'reply': 'x [1]', 'refs': [{'id': 'm1', 'at': 9999}]}, {'m1': 600.0})['refs'], [])
        self.assertEqual(clean_answer({'reply': 'x [1]', 'refs': [{'id': 'm1', 'at': -3}]}, {'m1': 600.0})['refs'], [])

    def test_ask_records_cost_without_meeting(self):
        from koli.meeting.ask import ask
        with mock.patch('koli.meeting.ask.for_meeting') as fm:
            ask('where is pricing?', [], self.heads, self.load, self.hits_for, self.complete, self.json.loads)
        fm.assert_called_with(None, 'meeting_ask')

    def test_ask_plans_then_answers_with_checked_refs(self):
        from koli.meeting.ask import ask
        out = ask('where is pricing?', [], self.heads, self.load, self.hits_for, self.complete, self.json.loads)
        self.assertEqual(out['reply'], 'Pricing was set [1].')
        self.assertEqual(out['refs'], [{'n': 1, 'id': 'm1', 'title': 'Pricing call', 'at': 30}])
        self.assertEqual([feature for feature, _ in self.messages_seen], ['meetings', 'meetings'])
        self.assertIn('Pricing call', self.messages_seen[0][1][-1]['content'])      # the planner saw the list

    def test_ask_without_plan_searches_the_question(self):
        from koli.meeting.ask import ask
        self.replies[0] = {'task': '', 'search': [], 'open': ['nope']}
        ask('where is pricing?', [], self.heads, self.load, self.hits_for, self.complete, self.json.loads)
        self.assertEqual(self.hits_for_calls[0][0], 'where is pricing?')

    def test_ask_sends_only_the_last_questions(self):
        from koli.meeting.ask import ask, HISTORY
        history = [{'role': 'user' if i % 2 == 0 else 'assistant', 'text': f'h{i}'} for i in range(60)]
        ask('q', history, self.heads, self.load, self.hits_for, self.complete, self.json.loads)
        sent = [m['content'] for m in self.messages_seen[0][1] if m['content'].startswith('h')]
        self.assertEqual(len(sent), 2 * HISTORY)
        self.assertEqual(sent[-1], 'h59')

    def test_ask_error_raises_summary_error(self):
        from koli.meeting.ask import ask
        from koli.meeting.summarize import SummaryError
        failing = lambda feature, msgs, json=False: self.reply_type(ok=False, text='', error='no model')
        with self.assertRaises(SummaryError):
            ask('q', [], self.heads, self.load, self.hits_for, failing, self.json.loads)
        with self.assertRaises(SummaryError):
            ask('  ', [], self.heads, self.load, self.hits_for, self.complete, self.json.loads)

    def test_history_saved_and_cleared(self):
        self.replies[0] = {'task': 'pricing', 'search': [], 'open': ['m1']}     # real search is off: open it whole
        svc = self.service(); svc.ask('where is pricing?')
        self.assertEqual([m['role'] for m in svc.ask_history()], ['user', 'assistant'])
        self.assertEqual(svc.ask_history()[1]['refs'][0]['id'], 'm1')
        svc.ask_clear(); self.assertEqual(svc.ask_history(), [])

    def test_prose_answer_is_kept_when_parser_gives_none(self):
        from koli.meeting.ask import ask
        self.complete = lambda feature, msgs, json=False: self.reply_type(ok=True, text='Plain words, no JSON.', error='')
        out = ask('q', [], self.heads, self.load, self.hits_for, self.complete, lambda text: None)
        self.assertEqual(out, {'reply': 'Plain words, no JSON.', 'refs': []})

    def test_ref_with_list_id_is_dropped_not_fatal(self):
        from koli.meeting.ask import clean_answer
        out = clean_answer({'reply': 'x [1] y [2]', 'refs': [{'id': ['m1'], 'at': 5}, {'id': {'a': 1}, 'at': 5}]},
                           {'m1': 600.0})
        self.assertEqual(out, {'reply': 'x y', 'refs': []})

    def test_meeting_list_stays_under_its_character_budget(self):
        from koli.meeting.ask import LIST_MAX_CHARS, meeting_list
        heads = [{'id': f'id{i:04d}', 'title': 'T' * 400, 'source': 'meet', 'started_at': '2026-11-01T10:00:00+02:00',
                  'speakers': ['Name' * 100], 'turns': 5, 'brief': True} for i in range(500)]
        listing = meeting_list(heads)
        self.assertLessEqual(len(listing), LIST_MAX_CHARS)
        lines = listing.splitlines()
        self.assertTrue(lines[0].startswith('id0000'))                  # newest first; the oldest fell off
        self.assertLess(len(lines), 500)
        self.assertNotIn('T' * 81, listing)

    def test_ref_title_comes_from_all_heads(self):
        from koli.meeting.ask import ask
        heads = [{**self.heads[1], 'id': f'x{i}', 'title': 'F' * 400} for i in range(300)] + self.heads
        out = ask('where is pricing?', [], heads, self.load, self.hits_for, self.complete, self.json.loads)
        self.assertEqual(out['refs'][0]['title'], 'Pricing call')       # m1 is past the listing's budget

    def test_material_says_when_search_is_unavailable(self):
        from koli.meeting.ask import material
        text, _ = material({'search': ['pricing'], 'open': ['m1']}, 'q', self.load, lambda *a: [], lambda: True)
        self.assertIn('Search by meaning is unavailable right now', text)
        self.assertIn('0:15 Shahar: turn 1', text)
        text, _ = material({'search': ['pricing'], 'open': []}, 'q', self.load, lambda *a: [], lambda: False)
        self.assertEqual(text, '')

    def test_service_reports_unavailable_meaning_to_the_model(self):
        self.replies[0] = {'task': 'pricing', 'search': ['pricing'], 'open': []}
        svc = self.service()                                            # meaning is off in these settings
        svc.ask('where is pricing?')
        self.assertIn('Search by meaning is unavailable', self.messages_seen[1][1][-1]['content'])

    def test_ask_needs_a_model(self):
        svc = self.service()
        svc._ai_ready = lambda: False
        with self.assertRaises(ValueError):
            svc.ask('q')


if __name__ == "__main__":
    unittest.main()
