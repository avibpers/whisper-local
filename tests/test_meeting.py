# tests/test_meeting.py
# Meeting mode: the store, the session state machine, detection, chunking,
# speaker clustering and the transcriber, all without audio hardware.
import datetime
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

TZ = datetime.timezone(datetime.timedelta(hours=3))


def at(minute, second=0):
    return datetime.datetime(2026, 10, 11, 14, minute, second, tzinfo=TZ)


class StoreTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))

    def test_new_meeting_has_you_and_no_turns(self):
        from koli.meeting.store import new_id, new_meeting
        m = new_meeting(new_id(at(30, 12)), 'Discord', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        self.assertEqual(m['id'], '2026-10-11-143012')
        self.assertEqual(m['speakers'], [{'id': 'you', 'name': 'You', 'colour': 0}])
        self.assertEqual(m['turns'], [])
        self.assertEqual(m['started_at'], '2026-10-11T14:30:12+03:00')
        self.assertIsNone(m['ended_at'])

    def test_save_load_list_delete(self):
        from koli.meeting.store import new_meeting
        m = new_meeting('2026-10-11-143012', 'Discord', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        m['turns'].append({'start': 3.2, 'end': 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hello', 'lang': 'en'})
        m['speakers'].append({'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0, 0.0], 'count': 1})
        self.store.save(m)
        self.assertEqual(self.store.load(m['id'])['turns'][0]['text'], 'hello')
        heads = self.store.list()
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]['id'], m['id'])
        self.assertEqual(heads[0]['turns'], 1)
        self.assertEqual(heads[0]['speakers'], ['You', 'Speaker 1'])
        self.assertNotIn('centroid', str(heads[0]))
        self.assertTrue(self.store.delete(m['id']))
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.delete(m['id']))

    def test_list_is_newest_first_and_skips_broken_files(self):
        from koli.meeting.store import new_meeting
        for mid in ('2026-10-10-090000', '2026-10-11-090000'):
            self.store.save(new_meeting(mid, 'x', {'app': 'zoom.exe', 'kind': 'native', 'title': '', 'code': ''}, at(0)))
        (Path(self.tmp.name) / '2026-10-12-090000.json').write_text('{not json', encoding='utf-8')
        self.assertEqual([h['id'] for h in self.store.list()], ['2026-10-11-090000', '2026-10-10-090000'])

    def test_export_formats(self):
        from koli.meeting.store import export_text, new_meeting
        m = new_meeting('2026-10-11-143012', 'Team sync', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        m['speakers'].append({'id': 's1', 'name': 'Ilona', 'colour': 1})
        m['turns'] += [{'start': 3.0, 'end': 5.0, 'stream': 'loopback', 'speaker': 's1', 'text': 'Hi', 'lang': 'en'},
                       {'start': 6.0, 'end': 8.0, 'stream': 'mic', 'speaker': 'you', 'text': 'Hello', 'lang': 'en'}]
        txt = export_text(m, 'txt')
        self.assertIn('[00:03] Ilona: Hi', txt)
        self.assertIn('[00:06] You: Hello', txt)
        md = export_text(m, 'md')
        self.assertTrue(md.startswith('# Team sync'))
        self.assertIn('**Ilona**', md)
        with self.assertRaises(ValueError):
            export_text(m, 'pdf')

    def test_throttled_saver_writes_once_until_flushed(self):
        from koli.meeting.store import ThrottledSaver, new_meeting
        m = new_meeting('2026-10-11-143012', 'x', {'app': 'zoom.exe', 'kind': 'native', 'title': '', 'code': ''}, at(0))
        saver = ThrottledSaver(self.store, interval=60.0)
        saver.mark(m)
        self.assertTrue(self.store.path(m['id']).exists())   # the first mark writes at once
        m['turns'].append({'start': 1.0, 'end': 2.0, 'stream': 'mic', 'speaker': 'you', 'text': 'a', 'lang': 'en'})
        saver.mark(m)
        self.assertEqual(self.store.load(m['id'])['turns'], [])   # the next one waits
        saver.flush()
        self.assertEqual(len(self.store.load(m['id'])['turns']), 1)


if __name__ == '__main__':
    unittest.main()
