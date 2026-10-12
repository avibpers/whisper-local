# tests/test_meeting.py
# Meeting mode: the store, the session state machine, detection, chunking,
# speaker clustering and the transcriber, all without audio hardware.
import datetime
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

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


class FakeClock:
    def __init__(self, start):
        self.t = start
        self.timers = []   # (due, fn, alive)

    def now(self):
        return self.t

    def schedule(self, seconds, fn):
        entry = [self.t + datetime.timedelta(seconds=seconds), fn, True]
        self.timers.append(entry)
        return lambda: entry.__setitem__(2, False)

    def advance(self, seconds):
        self.t += datetime.timedelta(seconds=seconds)
        for entry in list(self.timers):
            if entry[2] and entry[0] <= self.t:
                entry[2] = False
                entry[1]()


def discord(mode='auto'):
    return {'key': 'discord.exe', 'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': '', 'mode': mode}


def meet(code='abc-defg-hij', mode='auto'):
    return {'key': f'chrome.exe|Meet - {code}', 'app': 'chrome.exe', 'kind': 'browser', 'title': f'Meet - {code}',
            'code': code, 'mode': mode}


def started_at(meeting):
    from koli.meeting.store import started_ts
    return started_ts(meeting)


class SessionTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))
        self.clock = FakeClock(at(30))
        self.captures = []
        self.events = []
        self.settings = {'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto',
                         'meeting.include_me': True}
        self.sm = SessionManager(self.store, start_capture=lambda m: self.captures.append(('start', m['id'])),
                                 stop_capture=lambda: self.captures.append(('stop',)),
                                 publish=lambda topic, payload: self.events.append((topic, payload)),
                                 settings=lambda path, default=None: self.settings.get(path, default),
                                 now=self.clock.now, schedule=self.clock.schedule)

    def live(self, source=None):
        source = source or discord()
        self.sm.on_sources([source])
        self.sm.on_speech(3.5)
        self.assertEqual(self.sm.state, 'live')
        return self.sm.current()

    def test_source_without_speech_does_not_start(self):
        self.sm.on_sources([discord()])
        self.sm.on_speech(1.0)
        self.assertEqual(self.sm.state, 'idle')
        self.assertEqual(self.captures, [])
        self.sm.on_sources([])          # the ping ended
        self.sm.on_sources([discord()])
        self.sm.on_speech(1.0)          # speech counts from this appearance only
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_speech(2.5)
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.captures, [('start', self.sm.current()['id'])])

    def test_auto_start_names_and_saves_the_meeting(self):
        m = self.live()
        self.assertEqual(m['title'], 'Discord')
        self.assertEqual(m['source']['app'], 'discord.exe')
        self.assertTrue(self.store.path(m['id']).exists())
        self.assertEqual(self.events[-1][0], 'meeting')
        self.assertEqual(self.events[-1][1]['state'], 'live')

    def test_ask_mode_offers_then_starts_on_accept(self):
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'offered')
        self.assertEqual(self.captures, [])
        self.sm.offer_accept()
        self.assertEqual(self.sm.state, 'live')

    def test_declined_offer_is_not_asked_again_until_source_goes_away(self):
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.sm.offer_decline()
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_sources([])
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'offered')

    def test_source_gone_pauses_and_same_source_resumes(self):
        m = self.live()
        self.sm.add_turn({'t0': started_at(m) + 5, 't1': started_at(m) + 8, 'stream': 'loopback', 'speaker': 's1',
                          'text': 'hi', 'lang': 'en'})
        self.sm.on_sources([])
        self.assertEqual(self.sm.state, 'paused')
        self.assertEqual(self.captures[-1], ('stop',))
        self.clock.advance(120)
        self.sm.on_sources([discord()])
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'resumed')
        self.assertEqual(self.sm.current()['gaps'], [{'start': 0.0, 'end': 120.0, 'reason': 'paused'}])

    def test_source_back_after_gap_starts_new_meeting(self):
        m = self.live()
        self.sm.on_sources([])
        self.clock.advance(10 * 60 + 1)
        self.assertEqual(self.sm.state, 'idle')
        saved = self.store.load(m['id'])
        self.assertIsNotNone(saved['ended_at'])
        self.clock.advance(5)
        self.sm.on_sources([discord()])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'new')

    def test_different_code_within_gap_starts_new_and_can_continue_previous(self):
        m = self.live(meet('abc-defg-hij'))
        self.sm.on_sources([])
        self.clock.advance(60)
        self.sm.on_sources([meet('zzz-zzzz-zzz')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'new')
        self.assertTrue(self.sm.state_payload()['can_continue'])
        base = started_at(self.sm.current())
        self.sm.add_turn({'t0': base + 1, 't1': base + 2, 'stream': 'mic', 'speaker': 'you', 'text': 'late', 'lang': 'en'})
        new_id = self.sm.current()['id']
        self.sm.continue_previous()
        self.assertEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.current()['turns'][-1]['text'], 'late')
        self.assertFalse(self.store.path(new_id).exists())

    def test_same_code_resumes_even_from_another_browser_window(self):
        m = self.live(meet('abc-defg-hij'))
        self.sm.on_sources([])
        self.clock.advance(30)
        self.sm.on_sources([meet('abc-defg-hij')])
        self.assertEqual(self.sm.current()['id'], m['id'])

    def test_resumed_meeting_can_be_split_into_a_new_one(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 1, 't1': base + 2, 'stream': 'mic', 'speaker': 'you', 'text': 'first', 'lang': 'en'})
        self.sm.on_sources([])
        self.clock.advance(60)
        self.sm.on_sources([discord()])
        self.sm.add_turn({'t0': base + 70, 't1': base + 72, 'stream': 'mic', 'speaker': 'you', 'text': 'second', 'lang': 'en'})
        self.sm.split_resumed()
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual([t['text'] for t in self.sm.current()['turns']], ['second'])
        self.assertEqual([t['text'] for t in self.store.load(m['id'])['turns']], ['first'])
        self.assertIsNotNone(self.store.load(m['id'])['ended_at'])

    def test_manual_start_without_source_and_end(self):
        self.sm.start()
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.sm.current()['source']['kind'], 'manual')
        self.assertEqual(self.sm.current()['title'], 'Meeting')
        self.sm.end()
        self.assertEqual(self.sm.state, 'idle')
        self.assertEqual(self.captures, [('start', self.store.list()[0]['id']), ('stop',)])
        self.assertEqual(self.events[-2][1]['state'], 'ended')
        self.assertEqual(self.events[-1][1]['state'], 'idle')

    def test_new_ends_current_and_starts_fresh_with_same_source(self):
        m = self.live()
        self.sm.new()
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.current()['source'], m['source'])
        self.assertIsNotNone(self.store.load(m['id'])['ended_at'])

    def test_pause_and_exclude_record_gaps(self):
        m = self.live()
        self.sm.pause()
        self.assertEqual(self.sm.state, 'paused')
        self.assertTrue(self.sm.state_payload()['paused_by_user'])
        self.clock.advance(10)
        self.sm.resume()
        self.assertEqual(self.sm.state, 'live')
        self.clock.advance(5)
        self.sm.set_excluded(True)
        self.clock.advance(20)
        self.sm.set_excluded(False)
        self.assertEqual(m['gaps'], [{'start': 0.0, 'end': 10.0, 'reason': 'paused'},
                                     {'start': 15.0, 'end': 35.0, 'reason': 'excluded'}])

    def test_user_pause_is_not_resumed_by_the_source(self):
        self.live()
        self.sm.pause()
        self.sm.on_sources([discord()])
        self.assertEqual(self.sm.state, 'paused')

    def test_turns_speakers_and_rename(self):
        m = self.live()
        base = started_at(m)
        self.sm.ensure_speaker({'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0], 'count': 1})
        self.sm.add_turn({'t0': base + 3.25, 't1': base + 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hi', 'lang': 'en'})
        self.assertEqual(m['turns'], [{'start': 3.25, 'end': 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hi', 'lang': 'en'}])
        self.assertEqual(self.events[-1], ('meeting_turn', {'id': m['id'], 'turn': m['turns'][0]}))
        self.sm.rename_speaker('s1', 'Ilona')
        self.assertEqual(m['speakers'][1]['name'], 'Ilona')
        self.assertEqual(self.sm.state_payload()['speakers'][1], {'id': 's1', 'name': 'Ilona', 'colour': 1})

    # The microphone hears the call's audio (a headset leaking, a desk mic):
    # the other side's words then arrive twice, once from the loopback and
    # once as "You" with near-identical text at the same seconds. A mic turn
    # that repeats an overlapping loopback turn is an echo and is dropped,
    # whichever of the two was transcribed first.
    def test_mic_echo_of_a_loopback_turn_is_dropped(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 24.7, 't1': base + 28.6, 'stream': 'loopback', 'speaker': 's1',
                          'text': 'This will be in builder in some way to show for people.', 'lang': 'en'})
        self.sm.add_turn({'t0': base + 24.8, 't1': base + 28.4, 'stream': 'mic', 'speaker': 'you',
                          'text': 'This will be a builder in some way to show it for people.', 'lang': 'en'})
        self.assertEqual([t['speaker'] for t in m['turns']], ['s1'])

    def test_mic_echo_transcribed_first_is_removed_when_the_loopback_turn_arrives(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 43.6, 't1': base + 48.8, 'stream': 'mic', 'speaker': 'you',
                          'text': 'And what we have now, do auto scroll to it. It will show full.', 'lang': 'en'})
        self.sm.add_turn({'t0': base + 42.9, 't1': base + 48.5, 'stream': 'loopback', 'speaker': 's1',
                          'text': 'And what we have now, do auto-scroll to drop-down list to show full.', 'lang': 'en'})
        self.assertEqual([t['speaker'] for t in m['turns']], ['s1'])
        self.assertEqual(self.events[-1], ('meeting_turn', {'id': m['id'], 'reload': True}))

    # From a real Hebrew meeting: the mic's copy is cut at other seconds than
    # the loopback, so it spans two loopback turns and reads differently from
    # each (character similarity 0.03 and 0.40). Its words, looked for in both
    # joined, are mostly there, so it is still the call's echo.
    def test_mic_copy_spanning_two_loopback_turns_is_dropped(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 16.4, 't1': base + 31.4, 'stream': 'loopback', 'speaker': 's2',
                          'text': 'יש הבדל בין, אתה יושב מול קלוד ואתה באיטרציות מול העובד ברמה שאתה נותן לו את הפרומפט, עושה את העבודה.',
                          'lang': 'he'})
        self.sm.add_turn({'t0': base + 31.4, 't1': base + 36.1, 'stream': 'loopback', 'speaker': 's2',
                          'text': 'אתה מסתכל עליו רבע שעה, הוא חוזר, שואל אותך, ואתה כל הזמן בהרבה אינטראקציות איתו.',
                          'lang': 'he'})
        self.sm.add_turn({'t0': base + 29.5, 't1': base + 36.0, 'stream': 'mic', 'speaker': 'you',
                          'text': 'ואתה באיטרציות לא לא עובד לרמה שאתה נותן אותו פה, אתה עושה את העבודה, '
                                  'אתה מסתכל עליו רבע שעה, הוא חוזר, שואל אותך, ואתה כל הזמן',
                          'lang': 'he'})
        self.assertEqual([t['stream'] for t in m['turns']], ['loopback', 'loopback'])

    # Your own sentence while someone else talks shares a word or two at most.
    def test_your_own_words_over_someone_else_are_kept(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 240.0, 't1': base + 251.0, 'stream': 'loopback', 'speaker': 's2',
                          'text': 'אז אני לא יודע מה ויקטור עושה כרגע, לא חוץ מהעמודי ההורדה שתוכננו לעשות והוא מתחיל, אבל',
                          'lang': 'he'})
        self.sm.add_turn({'t0': base + 243.5, 't1': base + 251.0, 'stream': 'mic', 'speaker': 'you',
                          'text': "ויקטור עובד על הדאונלוד פאג' אולי הוא לא עשה קומיט ולכן סם לא יודע",
                          'lang': 'he'})
        self.assertEqual([t['stream'] for t in m['turns']], ['loopback', 'mic'])

    # A one-word "yes" from you is kept even when the other side said "yes"
    # somewhere in a long turn at the same time.
    def test_a_short_word_of_yours_is_not_taken_for_an_echo(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 50.0, 't1': base + 65.0, 'stream': 'loopback', 'speaker': 's2',
                          'text': 'כן, אז מה שאני אומר זה שצריך לבנות את התהליך מחדש לפני שמתחילים', 'lang': 'he'})
        self.sm.add_turn({'t0': base + 55.0, 't1': base + 55.6, 'stream': 'mic', 'speaker': 'you',
                          'text': 'כן.', 'lang': 'he'})
        self.assertEqual([t['stream'] for t in m['turns']], ['loopback', 'mic'])

    def test_talking_over_each_other_keeps_both_turns(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 10.5, 't1': base + 11.5, 'stream': 'loopback', 'speaker': 's1',
                          'text': 'We chat.', 'lang': 'en'})
        self.sm.add_turn({'t0': base + 10.6, 't1': base + 14.7, 'stream': 'mic', 'speaker': 'you',
                          'text': 'With Jumbo form, what we said.', 'lang': 'en'})
        self.assertEqual([t['speaker'] for t in m['turns']], ['s1', 'you'])

    def test_repeating_the_other_side_later_is_not_an_echo(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 10.0, 't1': base + 13.0, 'stream': 'loopback', 'speaker': 's1',
                          'text': 'Move it to next week.', 'lang': 'en'})
        self.sm.add_turn({'t0': base + 20.0, 't1': base + 23.0, 'stream': 'mic', 'speaker': 'you',
                          'text': 'Move it to next week.', 'lang': 'en'})
        self.assertEqual(len(m['turns']), 2)

    def test_payload_shape(self):
        p = self.sm.state_payload()
        self.assertEqual(p['state'], 'idle')
        self.live()
        self.sm.set_behind(12.5)
        p = self.sm.state_payload()
        for key in ('state', 'id', 'title', 'source', 'started_at', 'elapsed_s', 'paused_by_user', 'excluded', 'behind_s',
                    'speakers', 'notice', 'turns', 'can_continue', 'can_split', 'offer'):
            self.assertIn(key, p)
        self.assertEqual(p['behind_s'], 12.5)


RULES = [
    {'id': 'discord', 'match': ['discord.exe'], 'meeting': 'default'},
    {'id': 'zoom', 'match': ['zoom.exe'], 'meeting': 'ask'},
    {'id': 'meet', 'match': ['chrome.exe', 'msedge.exe', 'brave.exe'], 'window_title': ['Meet - '], 'meeting': 'default'},
    {'id': 'teams-web', 'match': ['chrome.exe', 'msedge.exe'], 'window_title': ['Microsoft Teams'], 'meeting': 'off'},
    {'id': 'terminals', 'match': ['cmd.exe', 'WindowsTerminal.exe'], 'auto_send': False},
]


class DetectorTests(unittest.TestCase):
    def test_native_app_with_active_session_is_a_source(self):
        from koli.meeting.detector import find_sources
        sources = find_sources([{'pid': 1, 'exe': 'discord.exe', 'state': 1}], [], RULES, 'auto')
        self.assertEqual(sources, [{'key': 'discord.exe', 'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': '',
                                    'mode': 'auto', 'device': ''}])

    def test_inactive_session_and_unlisted_app_are_ignored(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 1, 'exe': 'discord.exe', 'state': 0}, {'pid': 2, 'exe': 'spotify.exe', 'state': 1},
                    {'pid': 3, 'exe': 'cmd.exe', 'state': 1}]
        self.assertEqual(find_sources(sessions, [], RULES, 'auto'), [])

    def test_rule_mode_wins_over_default_and_off_drops(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 1, 'exe': 'zoom.exe', 'state': 1}, {'pid': 2, 'exe': 'chrome.exe', 'state': 1}]
        tabs = [{'exe': 'chrome.exe', 'title': 'Microsoft Teams - Audio playing', 'media': 'Audio playing', 'hwnd': 9}]
        sources = find_sources(sessions, tabs, RULES, 'auto')
        self.assertEqual([(s['app'], s['mode']) for s in sources], [('zoom.exe', 'ask')])

    def test_browser_tab_behind_others_with_code(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 2, 'exe': 'chrome.exe', 'state': 1}]
        tabs = [{'exe': 'chrome.exe', 'title': 'Inbox - Gmail', 'media': '', 'hwnd': 9},
                {'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': 'Camera and microphone in use', 'hwnd': 9}]
        sources = find_sources(sessions, tabs, RULES, 'auto')
        self.assertEqual(sources, [{'key': 'chrome.exe|Meet - abc-defg-hij', 'app': 'chrome.exe', 'kind': 'browser',
                                    'title': 'Meet - abc-defg-hij', 'code': 'abc-defg-hij', 'mode': 'auto', 'device': ''}])

    def test_browser_tab_without_audio_session_is_not_a_source(self):
        from koli.meeting.detector import find_sources
        tabs = [{'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': '', 'hwnd': 9}]
        self.assertEqual(find_sources([{'pid': 2, 'exe': 'chrome.exe', 'state': 0}], tabs, RULES, 'auto'), [])

    def test_strip_media_suffix(self):
        from koli.meeting.detector import strip_media
        self.assertEqual(strip_media('Meet - abc-defg-hij - Microphone in use'), ('Meet - abc-defg-hij', 'Microphone in use'))
        self.assertEqual(strip_media('Inbox - Gmail'), ('Inbox - Gmail', ''))
        self.assertEqual(strip_media('Tab - Audio muted'), ('Tab', 'Audio muted'))

    def test_detector_tick_reports_every_time(self):
        from koli.meeting.detector import MeetingDetector
        seen = []
        d = MeetingDetector(on_sources=seen.append, rules_provider=lambda: RULES, mode_provider=lambda: 'auto',
                            sessions_fn=lambda: [{'pid': 1, 'exe': 'discord.exe', 'state': 1}], tabs_fn=lambda: [])
        d.tick()
        d.tick()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0]['app'], 'discord.exe')

    def test_detector_survives_probe_errors(self):
        from koli.meeting.detector import MeetingDetector
        seen = []

        def boom():
            raise RuntimeError('COM failed')
        d = MeetingDetector(on_sources=seen.append, rules_provider=lambda: RULES, mode_provider=lambda: 'auto',
                            sessions_fn=boom, tabs_fn=lambda: [])
        d.tick()
        self.assertEqual(seen, [[]])

    # Loaded by path: the macOS package itself needs fcntl, which Windows lacks.
    def test_platform_stubs_import_everywhere(self):
        import importlib.util
        for name, fn in (('audio_sessions', 'active'), ('browser_tabs', 'tabs')):
            path = ROOT / 'src' / 'koli' / 'platform' / 'macos' / f'{name}.py'
            spec = importlib.util.spec_from_file_location(f'stub_{name}', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(getattr(module, fn)(), [])


class CaptureTests(unittest.TestCase):
    # A fake VAD: a frame is speech when its mean absolute level is above 0.1.
    @staticmethod
    def prob(frame):
        return 0.9 if float(np.abs(frame).mean()) > 0.1 else 0.05

    def make(self, **kw):
        from koli.meeting.capture import Chunker
        self.chunks = []
        self.speech = []
        return Chunker('loopback', self.prob, self.chunks.append, on_speech=self.speech.append, **kw)

    @staticmethod
    def seconds(level, s, rate=16000):
        return np.full(int(s * rate), level, dtype=np.float32)

    def test_cuts_at_a_pause_and_keeps_wall_clock_times(self):
        c = self.make()
        t = 1000.0
        for block in (self.seconds(0.5, 1.0), self.seconds(0.0, 0.8)):
            t += len(block) / 16000
            c.feed(block, t)
        self.assertEqual(len(self.chunks), 1)
        ch = self.chunks[0]
        self.assertEqual(ch['stream'], 'loopback')
        self.assertAlmostEqual(ch['t0'], 1000.0, places=2)
        self.assertLessEqual(ch['t1'], 1001.5)       # trailing silence trimmed
        self.assertGreaterEqual(ch['t1'], 1001.0)
        self.assertAlmostEqual(ch['speech_s'], 1.0, places=1)
        self.assertGreater(sum(self.speech), 0.9)     # on_speech saw about a second

    def test_cuts_at_max_length_while_still_speaking(self):
        c = self.make(max_s=2.0)
        t = 0.0
        for _ in range(5):
            block = self.seconds(0.5, 0.5)
            t += 0.5
            c.feed(block, t)
        self.assertEqual(len(self.chunks), 1)
        self.assertAlmostEqual(len(self.chunks[0]['audio']) / 16000, 2.0, places=1)

    def test_drops_too_short_speech_and_pure_silence(self):
        c = self.make()
        c.feed(self.seconds(0.5, 0.1), 0.1)
        c.feed(self.seconds(0.0, 1.0), 1.1)
        c.feed(self.seconds(0.0, 3.0), 4.1)
        self.assertEqual(self.chunks, [])

    def test_flush_emits_the_open_chunk(self):
        c = self.make()
        c.feed(self.seconds(0.5, 0.7), 0.7)
        self.assertEqual(self.chunks, [])
        c.flush()
        self.assertEqual(len(self.chunks), 1)

    def test_loopback_stream_reports_after_retries(self):
        from koli.meeting.capture import LoopbackStream
        errors = []
        attempts = []

        def open_fn():
            attempts.append(1)
            raise RuntimeError('no loopback device')
        s = LoopbackStream(on_block=lambda a, t: None, on_error=errors.append, retries=2, open_fn=open_fn, retry_wait=0.0)
        s.start()
        s._thread.join(2.0)
        self.assertEqual(len(attempts), 3)
        self.assertIn('no loopback device', errors[0])

    def test_mic_tap_respects_allow(self):
        from koli.meeting.capture import MicTap

        class Recorder:
            def __init__(self):
                self.taps = []

            def add_tap(self, fn):
                self.taps.append(fn)

            def remove_tap(self, fn):
                self.taps.remove(fn)
        rec = Recorder()
        got = []
        allowed = {'ok': True}
        tap = MicTap(lambda: rec, lambda audio, when: got.append(audio), lambda: allowed['ok'])
        tap.start()
        rec.taps[0](np.zeros(160, dtype=np.float32), 1.0)
        allowed['ok'] = False
        rec.taps[0](np.zeros(160, dtype=np.float32), 2.0)
        self.assertEqual(len(got), 1)
        tap.stop()
        self.assertEqual(rec.taps, [])

    def test_recorder_calls_taps_with_16k_mono(self):
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec._taps = []
        rec._recording_rate = 48000
        rec._needs_resampling_cached = True
        rec.logger = logging.getLogger('test')
        got = []
        rec.add_tap(lambda audio, when: got.append(audio))
        rec._notify_taps(np.zeros((4800, 1), dtype=np.float32))
        self.assertEqual(got[0].shape, (1600,))
        self.assertEqual(got[0].dtype, np.float32)


class SpeakerTests(unittest.TestCase):
    @staticmethod
    def unit(*xs):
        v = np.array(xs, dtype=np.float32)
        return v / np.linalg.norm(v)

    def test_same_voice_joins_far_voice_is_new(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0, 0), 'a2': self.unit(0.95, 0.1, 0), 'b': self.unit(0, 1, 0)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio])
        sid, spk = t.label('a')
        self.assertEqual(sid, 's1')
        self.assertEqual(spk['name'], 'Speaker 1')
        self.assertEqual(spk['colour'], 1)
        sid2, spk2 = t.label('a2')
        self.assertEqual(sid2, 's1')
        self.assertEqual(spk2['count'], 2)
        sid3, spk3 = t.label('b')
        self.assertEqual(sid3, 's2')
        self.assertEqual(spk3['colour'], 2)

    def test_centroid_moves_toward_new_samples(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0), 'a2': self.unit(0.8, 0.6)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio], similarity=0.5)
        t.label('a')
        _, spk = t.label('a2')
        c = np.array(spk['centroid'])
        self.assertGreater(c[1], 0.2)
        self.assertAlmostEqual(float(np.linalg.norm(c)), 1.0, places=4)

    def test_load_restores_speakers_and_numbering(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=lambda audio: self.unit(0, 0, 1))
        t.load([{'id': 'you', 'name': 'You', 'colour': 0},
                {'id': 's1', 'name': 'Ilona', 'colour': 1, 'centroid': [1.0, 0.0, 0.0], 'count': 4},
                {'id': 's2', 'name': 'Speaker 2', 'colour': 2, 'centroid': [0.0, 1.0, 0.0], 'count': 2}])
        sid, spk = t.label('x')
        self.assertEqual(sid, 's3')
        self.assertEqual(spk['colour'], 3)
        self.assertEqual([s['id'] for s in t.export()], ['s1', 's2', 's3'])

    def test_no_embedder_means_unknown(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=None)
        self.assertEqual(t.label(np.zeros(16000)), (SpeakerTracker.UNKNOWN, None))
        t2 = SpeakerTracker(embed=lambda audio: None)
        self.assertEqual(t2.label(np.zeros(16000)), (SpeakerTracker.UNKNOWN, None))

    # A short chunk's voiceprint is unreliable (a real call turned one person
    # into twelve "speakers" from 1-2 s chunks), so it never opens a new
    # speaker: it joins the best match when there is any resemblance, else
    # whoever spoke last on the loopback.
    def test_short_chunk_never_creates_a_speaker(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0, 0, 0), 'a_short': self.unit(0.5, 0.86, 0, 0), 'far': self.unit(0, 0, 1, 0),
                'e': self.unit(0, 0, 0, 1)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio], similarity=0.30)
        self.assertEqual(t.label('far', speech_s=1.0)[0], 's1', 'the first voice is kept even when short')
        self.assertEqual(t.label('a', speech_s=4.0)[0], 's2')
        self.assertEqual(t.label('a_short', speech_s=1.0)[0], 's2', 'some resemblance to a: joins a')
        sid, entry = t.label('e', speech_s=1.0)
        self.assertEqual(sid, 's2', 'far from everyone but short: stays with the last speaker')
        self.assertIsNone(entry, 'a continuity join leaves the voiceprint alone')
        self.assertEqual(t.label('e', speech_s=4.0)[0], 's3', 'long and far: a new speaker')

    # Near the threshold the speaker who just spoke wins (turns are sticky).
    def test_last_speaker_is_sticky_near_the_threshold(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0, 0), 'b': self.unit(0, 1, 0), 'near_a': self.unit(0.25, 0.97, 0),
                'weak': self.unit(0.25, 0.0, 0.97)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio], similarity=0.30)
        t.label('a', speech_s=4.0)
        t.label('b', speech_s=4.0)
        self.assertEqual(t.label('near_a', speech_s=4.0)[0], 's2', 'a clear better match wins')
        t.label('a', speech_s=4.0)
        # 0.25 to the last speaker (a), 0.0 to b: under the threshold but within the sticky margin of a
        self.assertEqual(t.label('weak', speech_s=4.0)[0], 's1')

    def test_default_voiceprint_model_is_the_voxceleb_one(self):
        import yaml
        from koli.config_manager import _resolve_platform_values
        from koli.settings import schema
        d = _resolve_platform_values(yaml.safe_load(
            (ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8')))['meeting']
        self.assertEqual(d['speaker_model'], 'eres2net-en')
        self.assertEqual(d['speaker_models']['eres2net-en']['files']['model'],
                         '3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx')
        self.assertIn('eres2net-en', [c[0] for c in schema.get('meeting.speaker_model').choices])

    def test_colours_wrap_after_seven(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=lambda audio: audio, similarity=0.99)
        labels = []
        for i in range(9):
            v = np.zeros(9, dtype=np.float32)
            v[i] = 1.0
            labels.append(t.label(v))
        self.assertEqual([spk['colour'] for _, spk in labels][:8], [1, 2, 3, 4, 5, 6, 7, 1])

    def test_registry_knows_speaker_models(self):
        from koli.model_registry import ModelRegistry
        r = ModelRegistry(speaker_models_config={'eres2net': {
            'source': 'csukuangfj/speaker-embedding-models', 'label': 'ERes2Net',
            'files': {'model': '3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx'}}})
        self.assertIn('eres2net', r.speaker_models)
        self.assertFalse(r.is_speaker_model_cached('nope'))


class FakeEngine:
    def __init__(self, answers, initial_prompt=None):
        self.answers = list(answers)
        self.calls = []
        self.prompt_used = []
        self.last_info = {}
        self.initial_prompt = initial_prompt

    def transcribe_audio(self, audio, language=None, use_prompt=True):
        self.calls.append((len(audio), language))
        self.prompt_used.append(use_prompt)
        text = self.answers.pop(0) if self.answers else None
        self.last_info = {'language': 'he' if text and 'שלום' in text else 'en'}
        return text


class TranscriberTests(unittest.TestCase):
    def run_chunks(self, chunks, answers, busy=None, tracker=None, language='auto'):
        from koli.meeting.transcriber import MeetingTranscriber
        from koli.meeting.speakers import SpeakerTracker
        engine = FakeEngine(answers)
        self.turns, self.speakers, self.pending, self.behind = [], [], [], []
        busy_state = busy or {'busy': False}
        t = MeetingTranscriber(engine_provider=lambda: engine, is_busy=lambda: busy_state['busy'],
                               tracker=tracker or SpeakerTracker(embed=lambda a: np.array([1.0, 0.0])),
                               language_provider=lambda: language,
                               on_turn=self.turns.append, on_speaker=self.speakers.append,
                               on_pending=self.pending.append, on_behind=self.behind.append, poll_s=0.01)
        t.start()
        for c in chunks:
            t.submit(c)
        deadline = time.time() + 3
        while t.queued_seconds() > 0 or t.working:
            if time.time() > deadline:
                break
            time.sleep(0.01)
        t.stop()
        return engine

    @staticmethod
    def chunk(stream, seconds=1.0, t0=100.0):
        return {'audio': np.zeros(int(16000 * seconds), dtype=np.float32), 'stream': stream, 't0': t0,
                't1': t0 + seconds, 'speech_s': seconds}

    def test_mic_turn_is_you_and_loopback_turn_is_labelled(self):
        engine = self.run_chunks([self.chunk('mic'), self.chunk('loopback', t0=105)], ['hello', 'שלום'])
        self.assertEqual([(t['speaker'], t['text'], t['lang']) for t in self.turns],
                         [('you', 'hello', 'en'), ('s1', 'שלום', 'he')])
        self.assertEqual(self.speakers[0]['id'], 's1')
        self.assertEqual(engine.calls[0][1], 'auto')
        self.assertEqual(self.pending[0], 'mic')
        self.assertIsNone(self.pending[-1])

    # The dictation prompt is a style hint for the app in front; meetings never
    # send it, and a turn that only repeats it is Whisper's echo, not speech.
    def test_meeting_turns_skip_the_dictation_prompt(self):
        from koli.meeting.transcriber import MeetingTranscriber
        from koli.meeting.speakers import SpeakerTracker
        engine = FakeEngine(['Friendly tone.', 'שלום לכולם'], initial_prompt='Friendly tone.')
        turns = []
        t = MeetingTranscriber(engine_provider=lambda: engine, is_busy=lambda: False,
                               tracker=SpeakerTracker(embed=lambda a: np.array([1.0, 0.0])),
                               language_provider=lambda: 'auto', on_turn=turns.append, on_speaker=lambda s: None,
                               on_pending=lambda s: None, on_behind=lambda s: None, poll_s=0.01)
        t.start()
        t.submit(self.chunk('mic'))
        t.submit(self.chunk('mic', t0=102))
        deadline = time.time() + 3
        while (t.queued_seconds() > 0 or t.working) and time.time() < deadline:
            time.sleep(0.01)
        t.stop()
        self.assertEqual(engine.prompt_used, [False, False])
        self.assertEqual([turn['text'] for turn in turns], ['שלום לכולם'])

    def test_empty_transcription_makes_no_turn(self):
        self.run_chunks([self.chunk('loopback')], [None])
        self.assertEqual(self.turns, [])
        self.assertEqual(self.speakers, [])
        self.assertIsNone(self.pending[-1])

    def test_waits_while_dictation_is_busy(self):
        busy = {'busy': True}
        from koli.meeting.transcriber import MeetingTranscriber
        from koli.meeting.speakers import SpeakerTracker
        engine = FakeEngine(['late'])
        turns = []
        t = MeetingTranscriber(engine_provider=lambda: engine, is_busy=lambda: busy['busy'], tracker=SpeakerTracker(),
                               language_provider=lambda: 'en', on_turn=turns.append, on_speaker=lambda s: None,
                               on_pending=lambda s: None, on_behind=lambda s: None, poll_s=0.01)
        t.start()
        t.submit(self.chunk('mic'))
        time.sleep(0.1)
        self.assertEqual(engine.calls, [])
        busy['busy'] = False
        time.sleep(0.3)
        t.stop()
        self.assertEqual([x['text'] for x in turns], ['late'])
        self.assertEqual(engine.calls[0][1], 'en')

    def test_behind_reports_queued_seconds(self):
        self.run_chunks([self.chunk('mic', 2.0), self.chunk('mic', 3.0)], ['a', 'b'])
        self.assertIn(5.0, self.behind)
        self.assertEqual(self.behind[-1], 0.0)

    def test_unknown_speaker_when_no_model(self):
        from koli.meeting.speakers import SpeakerTracker
        self.run_chunks([self.chunk('loopback')], ['x'], tracker=SpeakerTracker(embed=None))
        self.assertEqual(self.turns[0]['speaker'], '?')


class EngineLanguageTests(unittest.TestCase):
    def test_route_honours_a_per_call_language(self):
        from koli.whisper_engine import WhisperEngine
        e = WhisperEngine.__new__(WhisperEngine)
        e.model, e.model_key, e.language, e.initial_prompt = 'M', 'small', 'en', None
        e.language_model = None
        self.assertEqual(e._route(np.zeros(10))[2], 'en')
        self.assertEqual(e._route(np.zeros(10), language='he')[2], 'he')
        self.assertIsNone(e._route(np.zeros(10), language='auto')[2])


class MeetingSettingsTests(unittest.TestCase):
    def defaults(self):
        import yaml
        from koli.config_manager import _resolve_platform_values
        return _resolve_platform_values(yaml.safe_load(
            (ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8')))

    def test_defaults_match_the_spec(self):
        d = self.defaults()['meeting']
        self.assertTrue(d['enabled'])
        self.assertEqual(d['detect'], 'auto')
        self.assertTrue(d['include_me'])
        self.assertEqual(d['gap_minutes'], 10)
        self.assertEqual(d['start_speech_seconds'], 3)
        self.assertEqual(d['language'], 'auto')
        self.assertEqual(d['model'], '')
        self.assertEqual(d['speaker_model'], 'eres2net-en')
        self.assertIn('eres2net-en', d['speaker_models'])
        self.assertEqual(d['panel']['opacity'], 85)
        self.assertEqual(d['panel']['position'], 'right-edge')
        self.assertEqual((d['panel']['width'], d['panel']['height']), (380, 420))
        self.assertTrue(d['panel']['hide_from_sharing'])
        self.assertFalse(self.defaults()['overlay']['hide_from_sharing'])
        self.assertEqual(self.defaults()['hotkey']['meeting_hotkey'], '')

    def test_schema_section_and_simple_question(self):
        from koli.settings import schema, simple_mode
        self.assertIn('meetings', [s['id'] for s in schema.SECTIONS])
        self.assertEqual(schema.get('meeting.detect').choices,
                         [['auto', 'Automatically'], ['ask', 'Ask me first'], ['off', 'Never']])
        self.assertEqual(schema.get('meeting.panel.custom_x').mode, schema.HIDDEN)
        self.assertEqual(simple_mode.to_patch('calls', 'ask'), {'meeting.detect': 'ask'})
        self.assertEqual(simple_mode.from_settings(lambda p: {'meeting.detect': 'off'}.get(p))['calls'], 'off')

    def test_appliers_route_meeting_keys(self):
        from koli.settings.appliers import _PREFIX_HANDLERS, _LISTENER_KEYS, Appliers
        self.assertIn('hotkey.meeting_hotkey', _LISTENER_KEYS)
        self.assertTrue(any(prefix == 'meeting.' and fn is Appliers.meeting for prefix, fn in _PREFIX_HANDLERS))


class FakeStateManager:
    def __init__(self):
        self.recording = False
        self.processing = False
        self.whisper_engine = FakeEngine(['hello'])
        self.app_rules = type('R', (), {'rules': RULES})()
        self.audio_recorder = type('Rec', (), {'add_tap': lambda s, f: None, 'remove_tap': lambda s, f: None})()

    def is_idle(self):
        return not (self.recording or self.processing)

    def is_transcription_recording(self):
        return self.recording


class FakeConfig:
    def __init__(self, values):
        self.values = values

    def get_path(self, path, default=None):
        return self.values.get(path, default)


class ServiceTests(unittest.TestCase):
    def make(self, enabled=True):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        values = {'meeting.enabled': enabled, 'meeting.detect': 'auto', 'meeting.include_me': True,
                  'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto',
                  'meeting.model': '', 'meeting.speaker_model': 'eres2net'}
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig(values)
        ctx.model_registry = None
        self.events = []
        fake_loopback = type('L', (), {'start': lambda s: None, 'stop': lambda s: None})
        svc = MeetingService(ctx, store=MeetingStore(Path(self.tmp.name)),
                             publish=lambda t, p: self.events.append((t, p)),
                             loopback_factory=lambda on_block, on_error, device=None: fake_loopback(),
                             embedder_factory=lambda: None, loopback_available=lambda: True,
                             sessions_fn=lambda: [])
        self.addCleanup(svc.shutdown)
        return svc

    def test_manual_start_and_end_round_trip(self):
        svc = self.make()
        self.assertEqual(svc.state()['state'], 'idle')
        svc.start()
        self.assertEqual(svc.state()['state'], 'live')
        svc.end()
        self.assertEqual(svc.state()['state'], 'idle')
        heads = svc.list()
        self.assertEqual(len(heads), 1)
        self.assertEqual(svc.get(heads[0]['id'])['turns'], [])
        self.assertIn('Meeting', svc.export(heads[0]['id'], 'txt'))
        self.assertTrue(svc.delete(heads[0]['id']))

    def test_toggle_starts_then_ends(self):
        svc = self.make()
        svc.toggle()
        self.assertEqual(svc.state()['state'], 'live')
        svc.toggle()
        self.assertEqual(svc.state()['state'], 'idle')

    def test_detector_runs_only_when_enabled(self):
        svc = self.make(enabled=False)
        self.assertIsNone(svc.detector)
        svc.apply_setting('meeting.enabled', True)
        self.assertIsNotNone(svc.detector)
        svc.apply_setting('meeting.enabled', False)
        self.assertIsNone(svc.detector)

    def test_mic_blocks_are_dropped_while_dictating_or_excluded(self):
        svc = self.make()
        svc.start()
        self.assertTrue(svc._mic_allowed())
        svc.ctx.state_manager.recording = True
        self.assertFalse(svc._mic_allowed())
        svc.ctx.state_manager.recording = False
        svc.set_excluded(True)
        self.assertFalse(svc._mic_allowed())
        svc.set_excluded(False)
        svc.ctx.config_manager.values['meeting.include_me'] = False
        self.assertFalse(svc._mic_allowed())

    def test_events_topics_registered(self):
        from koli.ui.events import TOPICS
        self.assertIn('meeting', TOPICS)
        self.assertIn('meeting_turn', TOPICS)

    def test_loopback_extra_is_installable(self):
        from koli.services.system import EXTRAS
        self.assertEqual(EXTRAS['loopback'][0], 'soundcard')


class PanelPlacementTests(unittest.TestCase):
    def test_panel_docks_right_lower_half_and_clamps(self):
        from koli.ui.floating_bar import panel_origin
        area = (0, 0, 1920, 1040)
        x, y = panel_origin(area, (380, 420), 1.0, 'right-edge', None)
        self.assertEqual(x, 1920 - 380 - 6)
        self.assertGreater(y, 1040 // 2 - 1)
        self.assertLessEqual(y + 420, 1040 - 6)
        x, y = panel_origin(area, (380, 420), 1.0, 'custom', (1900, 1000))
        self.assertEqual((x, y), (1920 - 380, 1040 - 420))
        x, y = panel_origin(area, (380, 420), 1.0, 'top-left', None)
        self.assertEqual((x, y), (6, 6))


class FakeLoop:
    # A loopback stream the test drives by hand: blocks go in through on_block.
    made = []

    def __init__(self, on_block, on_error, device=None):
        self.on_block = on_block
        self.on_error = on_error
        self.device = device
        self.started = False
        self.stopped = False
        FakeLoop.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class ReviewFixTests(unittest.TestCase):
    # The chunker's callbacks run outside its lock: one that re-enters the
    # chunker (or takes another lock) must not deadlock the audio thread.
    def test_chunker_callbacks_run_outside_its_lock(self):
        import threading
        from koli.meeting.capture import Chunker
        chunks = []
        holder = {}

        def on_chunk(chunk):
            chunks.append(chunk)
            holder['c'].flush()
        c = Chunker('mic', lambda f: 0.9 if float(np.abs(f).mean()) > 0.1 else 0.0, on_chunk)
        holder['c'] = c

        def run():
            c.feed(np.full(16000, 0.5, dtype=np.float32), 1.0)
            c.feed(np.zeros(16000, dtype=np.float32), 2.0)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(2.0)
        self.assertFalse(t.is_alive(), 'feed deadlocked on its own lock')
        self.assertEqual(len(chunks), 1)

    # The session calls start/stop capture outside its lock, so a capture
    # callback (or anything it triggers on another thread) can read the state.
    def test_session_capture_callbacks_run_outside_its_lock(self):
        import threading
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        seen = []
        holder = {}

        def start_capture(m):
            t = threading.Thread(target=lambda: seen.append(holder['sm'].state_payload()['state']), daemon=True)
            t.start()
            t.join(1.0)
            seen.append('alive' if t.is_alive() else 'finished')
        clock = FakeClock(at(30))
        sm = SessionManager(MeetingStore(Path(tmp.name)), start_capture=start_capture, stop_capture=lambda: None,
                            publish=lambda t, p: None, settings=lambda p, d=None: d, now=clock.now, schedule=clock.schedule)
        holder['sm'] = sm
        sm.start()
        self.assertEqual(seen, ['live', 'finished'])

    def _session(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MeetingStore(Path(tmp.name))
        clock = FakeClock(at(30))
        self.reloads = []
        sm = SessionManager(store, start_capture=lambda m: None, stop_capture=lambda: None, publish=lambda t, p: None,
                            settings=lambda p, d=None: {'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3}.get(p, d),
                            now=clock.now, schedule=clock.schedule, on_speakers=self.reloads.append)
        return sm, store, clock

    # A chunk transcribed after its meeting ended still lands in that
    # meeting's file (nothing is dropped), never in the next meeting.
    def test_turn_of_an_ended_meeting_lands_in_its_file(self):
        sm, store, clock = self._session()
        sm.start()
        m = sm.current()
        base = started_at(m)
        sm.end()
        sm.add_turn({'meeting_id': m['id'], 't0': base + 5, 't1': base + 8, 'stream': 'mic', 'speaker': 'you',
                     'text': 'last words', 'lang': 'en'})
        saved = store.load(m['id'])
        self.assertEqual(saved['turns'][-1]['text'], 'last words')
        self.assertEqual(saved['turns'][-1]['start'], 5.0)

    def test_turn_of_the_old_meeting_never_lands_in_the_new_one(self):
        sm, store, clock = self._session()
        sm.start()
        m = sm.current()
        base = started_at(m)
        sm.new()
        sm.add_turn({'meeting_id': m['id'], 't0': base + 1, 't1': base + 2, 'stream': 'mic', 'speaker': 'you',
                     'text': 'old', 'lang': 'en'})
        self.assertEqual(sm.current()['turns'], [])
        self.assertEqual(store.load(m['id'])['turns'][-1]['text'], 'old')

    # A Meet tab sitting in the lobby must not start a meeting because YouTube
    # plays in another tab: when any tab of that browser carries a media
    # state, only tabs with one count.
    def test_lobby_tab_does_not_count_while_another_tab_plays(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 2, 'exe': 'chrome.exe', 'state': 1}]
        lobby = {'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': '', 'hwnd': 9}
        youtube = {'exe': 'chrome.exe', 'title': 'Cat videos - YouTube', 'media': 'Audio playing', 'hwnd': 9}
        self.assertEqual(find_sources(sessions, [lobby, youtube], RULES, 'auto'), [])
        in_call = {**lobby, 'media': 'Microphone in use'}
        self.assertEqual([s['code'] for s in find_sources(sessions, [in_call, youtube], RULES, 'auto')], ['abc-defg-hij'])
        # A browser whose tabs report no media state at all still counts by title.
        self.assertEqual(len(find_sources(sessions, [lobby], RULES, 'auto')), 1)

    # Continuing the previous meeting (and splitting a resumed one) tells the
    # speaker tracker which speakers it is now working with.
    def test_speakers_reload_after_continue_previous_and_split(self):
        sm, store, clock = self._session()
        sm.on_sources([meet('abc-defg-hij')])
        sm.on_speech(4.0)
        sm.ensure_speaker({'id': 's1', 'name': 'Ilona', 'colour': 1, 'centroid': [1.0], 'count': 2})
        sm.on_sources([])
        clock.advance(60)
        sm.on_sources([meet('zzz-zzzz-zzz')])
        sm.on_speech(4.0)
        sm.continue_previous()
        self.assertEqual([s['name'] for s in self.reloads[-1]], ['You', 'Ilona'])
        sm.split_resumed()
        self.assertEqual([s['name'] for s in self.reloads[-1]], ['You', 'Ilona'])

    def test_meeting_events_are_not_coalesced(self):
        from koli.ui.host import COALESCE
        self.assertNotIn('meeting', COALESCE)

    def test_meeting_model_setting_is_hidden(self):
        from koli.settings import schema
        self.assertEqual(schema.get('meeting.model').mode, schema.HIDDEN)

    def _service(self, **kw):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        values = {'meeting.enabled': False, 'meeting.detect': 'auto', 'meeting.include_me': True,
                  'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto',
                  'meeting.model': '', 'meeting.speaker_model': 'eres2net'}
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig(values)
        ctx.model_registry = None
        FakeLoop.made = []
        self.probs = []

        def prob_factory():
            self.probs.append(1)
            return lambda frame: 0.9
        args = dict(store=MeetingStore(Path(tmp.name)), publish=lambda t, p: None, loopback_factory=FakeLoop,
                    embedder_factory=lambda: None, speech_prob_factory=prob_factory, loopback_available=lambda: True,
                    sessions_fn=lambda: [])
        args.update(kw)
        svc = MeetingService(ctx, **args)
        self.addCleanup(svc.shutdown)
        return svc

    # Detection end to end: a source appears, the idle listener hears speech,
    # the meeting starts, and the meeting's own streams take over.
    def test_sources_then_speech_start_a_meeting_automatically(self):
        svc = self._service()
        svc._on_sources([discord()])
        self.assertEqual(len(FakeLoop.made), 1, 'an idle listener opens while a source waits for speech')
        idle = FakeLoop.made[0]
        for i in range(40):
            idle.on_block(np.full(1600, 0.5, dtype=np.float32), 100.0 + i * 0.1)
        self.assertEqual(svc.state()['state'], 'live')
        self.assertTrue(idle.stopped)
        self.assertEqual(len(FakeLoop.made), 2)
        self.assertTrue(FakeLoop.made[1].started)
        self.assertGreaterEqual(len(self.probs), 3, 'each stream gets its own voice detector')
        svc._on_sources([])
        self.assertEqual(svc.state()['state'], 'paused')
        self.assertTrue(FakeLoop.made[1].stopped)

    def test_idle_listener_closes_when_the_source_goes_away(self):
        svc = self._service()
        svc._on_sources([discord()])
        svc._on_sources([])
        self.assertTrue(FakeLoop.made[0].stopped)
        self.assertEqual(svc.state()['state'], 'idle')

    # Without the loopback package nothing starts, and the problem says why.
    def test_missing_loopback_package_refuses_to_start(self):
        problems = []
        svc = self._service(loopback_available=lambda: False, report=problems.append)
        svc.start()
        self.assertEqual(svc.state()['state'], 'idle')
        self.assertIn('loopback', problems[0].lower())
        svc.apply_setting('meeting.enabled', True)
        self.assertIsNone(svc.detector)

    # Ending a meeting keeps what is still queued; it drains into the file.
    def test_end_does_not_drop_queued_chunks(self):
        svc = self._service()
        svc.start()
        svc.ctx.state_manager.processing = True      # the engine is busy, so the chunk stays queued
        svc.transcriber.submit({'audio': np.zeros(16000, dtype=np.float32), 'stream': 'mic', 't0': 1.0, 't1': 2.0,
                                'speech_s': 1.0, 'meeting_id': svc.state()['id']})
        svc.end()
        self.assertGreater(svc.transcriber.queued_seconds(), 0)


class DeviceRoutingTests(unittest.TestCase):
    # Sessions are read from every output device, each tagged with the device
    # it plays on, so a call on the headset is seen even when the monitor is
    # the Windows default.
    def test_sessions_carry_their_output_device(self):
        import importlib.util
        path = ROOT / 'src' / 'koli' / 'platform' / 'windows' / 'audio_sessions.py'
        spec = importlib.util.spec_from_file_location('win_audio_sessions', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class Proc:
            def __init__(self, name, pid):
                self._name, self.pid = name, pid

            def name(self):
                return self._name

        class Session:
            def __init__(self, name, pid, state):
                self.Process = Proc(name, pid) if name else None
                self.State = state
        devices = [('Speakers (Logitech USB Headset)', 'dev-1', [Session('Discord.exe', 10, 1), Session(None, 0, 0)]),
                   ('LG ULTRAWIDE', 'dev-2', [Session('Spotify.exe', 20, 0)])]
        out = mod.active(devices_fn=lambda: devices)
        self.assertEqual(out, [{'pid': 10, 'exe': 'discord.exe', 'state': 1, 'device': 'Speakers (Logitech USB Headset)',
                                'device_id': 'dev-1'},
                               {'pid': 20, 'exe': 'spotify.exe', 'state': 0, 'device': 'LG ULTRAWIDE', 'device_id': 'dev-2'}])

    def test_source_names_the_device_its_sound_plays_on(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 1, 'exe': 'discord.exe', 'state': 0, 'device': 'LG ULTRAWIDE', 'device_id': 'd2'},
                    {'pid': 1, 'exe': 'discord.exe', 'state': 1, 'device': 'Speakers (Logitech USB Headset)', 'device_id': 'd1'}]
        sources = find_sources(sessions, [], RULES, 'auto')
        self.assertEqual(sources[0]['device'], 'Speakers (Logitech USB Headset)')

    def test_meeting_keeps_the_source_device(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = FakeClock(at(30))
        sm = SessionManager(MeetingStore(Path(tmp.name)), start_capture=lambda m: None, stop_capture=lambda: None,
                            publish=lambda t, p: None, settings=lambda p, d=None: d, now=clock.now, schedule=clock.schedule)
        sm.start({**discord(), 'device': 'Speakers (Logitech USB Headset)'})
        self.assertEqual(sm.current()['source']['device'], 'Speakers (Logitech USB Headset)')

    def _service(self, sessions=None, enabled=False):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        values = {'meeting.enabled': enabled, 'meeting.detect': 'auto', 'meeting.include_me': True,
                  'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto',
                  'meeting.model': '', 'meeting.speaker_model': 'eres2net'}
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig(values)
        ctx.model_registry = None
        FakeLoop.made = []
        svc = MeetingService(ctx, store=MeetingStore(Path(tmp.name)), publish=lambda t, p: None, loopback_factory=FakeLoop,
                             embedder_factory=lambda: None, speech_prob_factory=lambda: (lambda f: 0.9),
                             loopback_available=lambda: True, sessions_fn=lambda: list(sessions or []))
        self.addCleanup(svc.shutdown)
        return svc

    # The idle listener and the meeting's loopback open on the device the
    # source plays on, not on the Windows default.
    def test_listener_and_meeting_follow_the_source_device(self):
        svc = self._service()
        src = {**discord(), 'device': 'Speakers (Logitech USB Headset)'}
        svc._on_sources([src])
        self.assertEqual(FakeLoop.made[0].device, 'Speakers (Logitech USB Headset)')
        for i in range(40):
            FakeLoop.made[0].on_block(np.full(1600, 0.5, dtype=np.float32), 100.0 + i * 0.1)
        self.assertEqual(svc.state()['state'], 'live')
        self.assertEqual([l.device for l in FakeLoop.made[1:]], ['Speakers (Logitech USB Headset)'])

    # A meeting started by hand listens on every device that has sound, and
    # on the default output when none does.
    def test_manual_start_listens_on_every_device_with_sound(self):
        sessions = [{'pid': 1, 'exe': 'discord.exe', 'state': 1, 'device': 'Headset', 'device_id': 'd1'},
                    {'pid': 2, 'exe': 'spotify.exe', 'state': 1, 'device': 'Monitor', 'device_id': 'd2'},
                    {'pid': 3, 'exe': 'x.exe', 'state': 0, 'device': 'Broadcast', 'device_id': 'd3'}]
        svc = self._service(sessions=sessions)
        svc.start()
        self.assertEqual(sorted(l.device for l in FakeLoop.made), ['Headset', 'Monitor'])
        svc.end()
        self.assertTrue(all(l.stopped for l in FakeLoop.made))
        quiet = self._service(sessions=[])
        quiet.start()
        self.assertEqual([l.device for l in FakeLoop.made], [None])

    # Installing the loopback package while the app runs turns detection on.
    def test_package_install_starts_the_detector(self):
        available = {'ok': False}
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig({'meeting.enabled': True, 'meeting.detect': 'auto'})
        ctx.model_registry = None
        svc = MeetingService(ctx, store=MeetingStore(Path(tmp.name)), publish=lambda t, p: None, loopback_factory=FakeLoop,
                             embedder_factory=lambda: None, loopback_available=lambda: available['ok'],
                             report=lambda m: None)
        self.addCleanup(svc.shutdown)
        self.assertIsNone(svc.detector)
        available['ok'] = True
        svc.package_installed()
        self.assertIsNotNone(svc.detector)


class StartupLoadTests(unittest.TestCase):
    # Meeting detection waits for the app window: probing Chrome and opening
    # a loopback while WebView2 initialises delayed the main window past
    # pywebview's 10 s limit, which then never created the bubble and panel.
    def test_service_waits_for_the_ui_before_detecting(self):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig({'meeting.enabled': True, 'meeting.detect': 'auto'})
        ctx.model_registry = None
        svc = MeetingService(ctx, store=MeetingStore(Path(tmp.name)), publish=lambda t, p: None, loopback_factory=FakeLoop,
                             embedder_factory=lambda: None, loopback_available=lambda: True, sessions_fn=lambda: [],
                             autostart=False)
        self.addCleanup(svc.shutdown)
        self.assertIsNone(svc.detector)
        svc.ui_ready()
        self.assertIsNotNone(svc.detector)

    # A detector tick that lands during shutdown must not open a listener
    # that nothing will ever close (seen in the log: "Listening to the output
    # of" after "Meeting detection off").
    def test_nothing_opens_after_shutdown(self):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig({'meeting.enabled': False, 'meeting.detect': 'auto'})
        ctx.model_registry = None
        FakeLoop.made = []
        svc = MeetingService(ctx, store=MeetingStore(Path(tmp.name)), publish=lambda t, p: None, loopback_factory=FakeLoop,
                             embedder_factory=lambda: None, loopback_available=lambda: True, sessions_fn=lambda: [])
        svc.shutdown()
        svc._on_sources([discord()])
        self.assertEqual(FakeLoop.made, [])
        svc.start()
        self.assertEqual(svc.state()['state'], 'idle')

    # Chrome's tab names say "Microphone recording" and "Camera and
    # microphone recording" (seen in a real Meet call), not "in use".
    def test_chrome_recording_suffixes_are_media_states(self):
        from koli.meeting.detector import strip_media
        self.assertEqual(strip_media('Meet - tum-xdrg-hkg - Camera and microphone recording'),
                         ('Meet - tum-xdrg-hkg', 'Camera and microphone recording'))
        self.assertEqual(strip_media('Meet - tum-xdrg-hkg - Microphone recording')[1], 'Microphone recording')
        self.assertEqual(strip_media('Meet - tum-xdrg-hkg - Camera recording')[1], 'Camera recording')

    # The same tab reported by several windows or strips is one source.
    def test_duplicate_tabs_make_one_source(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 2, 'exe': 'chrome.exe', 'state': 1, 'device': 'Headset', 'device_id': 'd1'}]
        tab = {'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': 'Microphone recording', 'hwnd': 9}
        sources = find_sources(sessions, [tab, dict(tab), {**tab, 'hwnd': 10}], RULES, 'auto')
        self.assertEqual(len(sources), 1)

    # Between full scans (every 15 s) only the windows that held a tab in a
    # call are probed again, so twelve Chrome windows don't cost a second
    # every few seconds.
    def test_tab_probe_rescans_only_hot_windows_between_full_scans(self):
        import importlib.util
        path = ROOT / 'src' / 'koli' / 'platform' / 'windows' / 'browser_tabs.py'
        spec = importlib.util.spec_from_file_location('win_browser_tabs', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        probed = []
        names = {1: ['Inbox - Gmail'], 2: ['Meet - abc-defg-hij - Microphone recording'], 3: ['Docs']}
        clock = {'t': 1000.0}
        mod._browser_windows = lambda exes: [(1, 'chrome.exe'), (2, 'chrome.exe'), (3, 'chrome.exe')]
        mod._uia = lambda: object()
        mod._tab_names = lambda uia, hwnd: (probed.append(hwnd) or names[hwnd])
        mod._monotonic = lambda: clock['t']
        mod._reset_cache()
        from koli.meeting.detector import strip_media
        probe = lambda: mod.tabs(strip=strip_media)
        first = probe()
        self.assertEqual(sorted(probed), [1, 2, 3])
        self.assertEqual([t['hwnd'] for t in first if t['media']], [2])
        clock['t'] += 4.0
        probed.clear()
        second = probe()
        self.assertEqual(probed, [2], 'only the window with the call is probed again')
        self.assertEqual([t['title'] for t in second], ['Meet - abc-defg-hij'])
        clock['t'] += 16.0
        probed.clear()
        probe()
        self.assertEqual(sorted(probed), [1, 2, 3], 'a full scan after 15 s')


class FakeInputStream:
    # Stands in for sounddevice.InputStream: the test pushes blocks into the
    # callback the way the audio thread would.
    opened = []

    def __init__(self, device, samplerate, callback):
        self.device, self.samplerate, self.callback = device, samplerate, callback
        self.started = False
        self.closed = False
        FakeInputStream.opened.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class MeetingMicTests(unittest.TestCase):
    def test_setting_exists_with_same_as_dictation_default(self):
        import yaml
        from koli.config_manager import _resolve_platform_values
        from koli.settings import schema
        defaults = _resolve_platform_values(yaml.safe_load(
            (ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8')))
        self.assertEqual(defaults['meeting']['input_device'], '')
        entry = schema.get('meeting.input_device')
        self.assertEqual(entry.control, 'microphone')
        self.assertEqual(schema.coerce(entry, ''), '')
        self.assertEqual(schema.coerce(entry, 'default'), 'default')
        self.assertEqual(schema.coerce(entry, '24'), 24)

    # A separate microphone for meetings: opened at its own rate, handed on
    # as 16 kHz mono, and silenced by allow() like the tap.
    def test_mic_stream_feeds_16k_mono_and_honours_allow(self):
        from koli.meeting.capture import MicStream
        FakeInputStream.opened = []
        got = []
        allowed = {'ok': True}
        opened = lambda device, rate, callback: FakeInputStream(device, rate, callback)
        stream = MicStream(24, lambda audio, when: got.append(audio), lambda: allowed['ok'],
                           on_error=lambda m: None, open_fn=opened, device_rate=lambda d: 48000)
        stream.start()
        s = FakeInputStream.opened[0]
        self.assertEqual((s.device, s.samplerate), (24, 48000))
        s.callback(np.zeros((4800, 1), dtype=np.float32), 4800, None, None)
        self.assertEqual(got[0].shape, (1600,))
        allowed['ok'] = False
        s.callback(np.zeros((4800, 1), dtype=np.float32), 4800, None, None)
        self.assertEqual(len(got), 1)
        stream.stop()
        self.assertTrue(s.closed)

    def test_mic_stream_reports_when_the_device_cannot_open(self):
        from koli.meeting.capture import MicStream
        errors = []

        def boom(device, rate, callback):
            raise RuntimeError('Error opening InputStream: Invalid device')
        stream = MicStream(99, lambda a, t: None, lambda: True, on_error=errors.append, open_fn=boom,
                           device_rate=lambda d: 48000)
        self.assertFalse(stream.start())
        self.assertIn('Invalid device', errors[0])

    def _service(self, values, mic_factory=None):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = {'meeting.enabled': False, 'meeting.detect': 'auto', 'meeting.include_me': True, 'meeting.gap_minutes': 10,
                'meeting.start_speech_seconds': 3, 'meeting.language': 'auto', 'meeting.model': '',
                'meeting.speaker_model': 'eres2net', 'meeting.input_device': ''}
        base.update(values)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        self.taps = []
        ctx.state_manager.audio_recorder = type('Rec', (), {'add_tap': lambda s, f: self.taps.append(f),
                                                            'remove_tap': lambda s, f: self.taps.remove(f)})()
        ctx.config_manager = FakeConfig(base)
        ctx.model_registry = None
        FakeLoop.made = []
        self.problems = []
        svc = MeetingService(ctx, store=MeetingStore(Path(tmp.name)), publish=lambda t, p: None, loopback_factory=FakeLoop,
                             embedder_factory=lambda: None, speech_prob_factory=lambda: (lambda f: 0.9),
                             loopback_available=lambda: True, sessions_fn=lambda: [], report=self.problems.append,
                             mic_stream_factory=mic_factory)
        self.addCleanup(svc.shutdown)
        return svc

    def test_same_as_dictation_taps_the_recorder(self):
        svc = self._service({'meeting.input_device': ''})
        svc.start()
        self.assertEqual(len(self.taps), 1)

    def test_a_separate_device_opens_its_own_stream(self):
        made = []

        class FakeMic:
            def __init__(self, device, on_block, allow, on_error):
                self.device = device
                made.append(self)
                self.stopped = False

            def start(self):
                return True

            def stop(self):
                self.stopped = True
        svc = self._service({'meeting.input_device': 24}, mic_factory=FakeMic)
        svc.start()
        self.assertEqual([m.device for m in made], [24])
        self.assertEqual(self.taps, [])
        svc.end()
        self.assertTrue(made[0].stopped)

    # The chosen device failing to open must not cost your side of the
    # meeting: the dictation microphone is tapped instead, and you are told.
    def test_unopenable_device_falls_back_to_the_tap(self):
        class BrokenMic:
            def __init__(self, device, on_block, allow, on_error):
                self.on_error = on_error

            def start(self):
                self.on_error('Error opening InputStream: Invalid device')
                return False

            def stop(self):
                pass
        svc = self._service({'meeting.input_device': 'default'}, mic_factory=BrokenMic)
        svc.start()
        self.assertEqual(len(self.taps), 1)
        self.assertIn('Invalid device', self.problems[0])


# A 4-person Meet came out as "You" and "Speaker 1": each 15 s chunk got one
# voiceprint, an average of everyone in it. The splitter finds the voices
# inside a chunk; these are its pure parts, without the model.
class SplitByVoiceTests(unittest.TestCase):
    VOICES = [(0.0, 4.7, 1), (5.7, 10.8, 0), (11.9, 16.6, 1)]

    @staticmethod
    def piece(start, end, text):
        return {'start': start, 'end': end, 'text': text}

    def test_each_sentence_goes_to_the_voice_speaking_then(self):
        from koli.meeting.diarize import parts_by_voice
        parts = parts_by_voice([self.piece(0.0, 4.6, ' Move the release.'), self.piece(5.6, 10.9, ' I disagree.'),
                                self.piece(11.8, 16.7, ' Fine, behind a flag.')], self.VOICES)
        self.assertEqual([(p['voice'], p['text']) for p in parts],
                         [(1, 'Move the release.'), (0, 'I disagree.'), (1, 'Fine, behind a flag.')])
        self.assertEqual((parts[1]['start'], parts[1]['end']), (5.6, 10.9))

    def test_sentences_of_one_voice_in_a_row_are_one_part(self):
        from koli.meeting.diarize import parts_by_voice
        parts = parts_by_voice([self.piece(0.0, 2.0, ' One.'), self.piece(2.0, 4.5, ' Two.'),
                                self.piece(5.8, 10.0, ' Three.')], self.VOICES)
        self.assertEqual([(p['voice'], p['text']) for p in parts], [(1, 'One. Two.'), (0, 'Three.')])

    # A half-second "yes" isn't a turn of its own; it stays with its neighbour.
    def test_a_very_short_part_joins_its_neighbour(self):
        from koli.meeting.diarize import parts_by_voice
        parts = parts_by_voice([self.piece(0.0, 4.6, ' Ready?'), self.piece(5.8, 6.3, ' Yes.'),
                                self.piece(11.9, 16.5, ' Then go.')], self.VOICES)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['text'], 'Ready? Yes. Then go.')

    def test_a_piece_in_a_gap_goes_to_the_nearest_voice(self):
        from koli.meeting.diarize import voice_of
        self.assertEqual(voice_of(self.piece(10.9, 11.5, 'hm'), self.VOICES), 0)
        self.assertIsNone(voice_of(self.piece(1, 2, 'x'), []))

    def test_a_voice_audio_is_all_its_stretches_joined(self):
        from koli.meeting.diarize import voice_audio, voice_seconds
        audio = np.arange(17 * 16000, dtype=np.float32)
        got = voice_audio(audio, self.VOICES, 1)
        self.assertEqual(len(got), int(4.7 * 16000) + int(16.6 * 16000) - int(11.9 * 16000))
        self.assertAlmostEqual(voice_seconds(self.VOICES, 1), 4.7 + 4.7, places=5)


# Whisper makes sentences out of noise. Numbers from a real Hebrew Meet.
class BelievableTests(unittest.TestCase):
    @staticmethod
    def info(*pieces):
        return {'segments': [{'text': t, 'start': 0.0, 'end': 1.0, 'no_speech_prob': 0.0, 'avg_logprob': lp,
                              'compression_ratio': 1.2} for t, lp in pieces]}

    def test_a_short_clip_whisper_wasnt_clearly_sure_of_is_dropped(self):
        from koli.meeting.transcriber import believable_pieces
        self.assertEqual(believable_pieces("We'll see you next time.", self.info(("We'll see you next time.", -1.10)), 1.0), [])
        self.assertEqual(believable_pieces('PR in Chile.', self.info(('PR in Chile.', -0.45)), 1.2), [])

    def test_real_short_speech_is_kept(self):
        from koli.meeting.transcriber import believable_pieces
        kept = believable_pieces('כל פעם לפני שעולה גרסה.', self.info(('כל פעם לפני שעולה גרסה.', -0.09)), 1.95)
        self.assertEqual([p['text'] for p in kept], ['כל פעם לפני שעולה גרסה.'])

    def test_a_long_clip_loses_only_its_unsure_pieces(self):
        from koli.meeting.transcriber import believable_pieces
        kept = believable_pieces('x', self.info(('אני באמצע פגישה', -0.28), (' Piano Intro Bye. Tracey', -2.48)), 15.0)
        self.assertEqual([p['text'] for p in kept], ['אני באמצע פגישה'])

    def test_without_piece_numbers_the_text_is_one_piece(self):
        from koli.meeting.transcriber import believable_pieces
        self.assertEqual(believable_pieces('hello', {}, 3.0), [{'text': 'hello', 'start': 0.0, 'end': 3.0}])


# Every voice heard on a call is saved with a permanent id; meetings point to
# it, so one person can be followed through every meeting, named or not.
class VoiceBookTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.voices import VoiceBook
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'voices.json'
        self.book = VoiceBook(self.path)

    def test_a_kept_voice_has_an_id_and_is_found_after_a_restart(self):
        from koli.meeting.voices import VoiceBook
        vid = self.book.keep([1.0, 0.0, 0.0], 4, 'm1')
        self.assertRegex(vid, r'^v-[0-9a-f]{12}$')
        again = VoiceBook(self.path)
        self.assertEqual(again.match([0.9, 0.2, 0.0]), {'id': vid, 'name': ''})
        self.assertIsNone(again.match([0.0, 1.0, 0.0]), 'another voice is not this one')
        self.assertEqual(again.get(vid)['meetings'], ['m1'])

    def test_keeping_the_same_voice_again_refines_it(self):
        vid = self.book.keep([1.0, 0.0], 3, 'm1')
        self.assertEqual(self.book.keep([0.95, 0.31], 2, 'm2'), vid, 'it matches, so it is the same voice')
        v = self.book.get(vid)
        self.assertEqual((v['count'], v['meetings']), (5, ['m1', 'm2']))

    # A meeting resumed and ended again must not count its audio twice.
    def test_the_same_meeting_counts_once(self):
        vid = self.book.keep([1.0, 0.0], 3, 'm1')
        self.book.keep([1.0, 0.0], 3, 'm1', voice_id=vid)
        self.assertEqual(self.book.get(vid)['count'], 3)

    def test_a_voice_already_in_the_meeting_is_skipped(self):
        vid = self.book.keep([1.0, 0.0], 1, 'm1')
        self.assertIsNone(self.book.match([1.0, 0.0], taken=[vid]))

    # A speaker's first chunk is a noisy sample: it needs a closer match.
    def test_a_first_chunk_needs_a_closer_match(self):
        vid = self.book.keep([1.0, 0.0], 5, 'm1')
        near = [0.55, 0.835]                                      # cosine 0.55
        self.assertEqual(self.book.match(near)['id'], vid)
        self.assertIsNone(self.book.match(near, first=True))

    def test_a_name_is_a_label_on_the_id(self):
        vid = self.book.keep([1.0, 0.0], 2, 'm1')
        self.book.set_name(vid, 'Eran')
        self.assertEqual(self.book.match([1.0, 0.0]), {'id': vid, 'name': 'Eran'})
        self.book.set_name(vid, 'Speaker 3')
        self.assertEqual(self.book.get(vid)['name'], '', "Koli's own names are never kept")

    def test_merge_and_forget(self):
        a = self.book.keep([1.0, 0.0], 3, 'm1')
        b = self.book.keep([0.0, 1.0], 1, 'm2')
        self.book.merge(b, a)
        self.assertEqual([v['id'] for v in self.book.all()], [a])
        self.assertEqual(self.book.get(a)['meetings'], ['m1', 'm2'])
        self.assertTrue(self.book.forget(a))
        self.assertFalse(self.book.forget(a))
        self.assertEqual(self.book.all(), [])


class TrackerVoicesTests(unittest.TestCase):
    @staticmethod
    def unit(*xs):
        v = np.array(xs, dtype=np.float32)
        return v / np.linalg.norm(v)

    def book(self):
        from koli.meeting.voices import VoiceBook
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return VoiceBook(Path(tmp.name) / 'voices.json')

    # Two voices the splitter heard in one chunk are two people, even when
    # their voiceprints come out close.
    def test_two_voices_of_one_chunk_are_never_one_speaker(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0, 0), 'b': self.unit(0.6, 0.8, 0)}   # cosine 0.6: would join, yet under SAME_VOICE
        t = SpeakerTracker(embed=lambda audio: vecs[audio], similarity=0.30)
        first, _ = t.label('a', speech_s=4.0)
        second, _ = t.label('b', speech_s=4.0, exclude=[first])
        self.assertNotEqual(first, second)
        without = SpeakerTracker(embed=lambda audio: vecs[audio], similarity=0.30)
        without.label('a', speech_s=4.0)
        self.assertEqual(without.label('b', speech_s=4.0)[0], 's1', 'without the rule it would have joined')

    def test_a_known_voice_is_linked_by_id_and_named_when_it_has_a_name(self):
        from koli.meeting.speakers import SpeakerTracker
        book = self.book()
        eran = book.keep(self.unit(1, 0, 0), 5, 'm0')
        book.set_name(eran, 'Eran')
        unnamed = book.keep(self.unit(0, 0, 1), 5, 'm0')
        vecs = {'eran': self.unit(0.95, 0.1, 0), 'other': self.unit(0, 0.1, 0.95)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio], voices=book)
        sid, entry = t.label('eran', speech_s=4.0)
        self.assertEqual((entry['name'], entry['voice'], entry['voice_id']), ('Eran', 'Eran', eran))
        _, entry = t.label('other', speech_s=4.0)
        self.assertEqual((entry['name'], entry['voice_id']), ('Speaker 2', unnamed))
        self.assertNotIn('voice', entry, 'no name to take')

    # The first chunk is often too short to tell; once the voiceprint firms
    # up and matches, the speaker is linked (and named).
    def test_a_speaker_is_linked_once_its_voice_is_firm(self):
        from koli.meeting.speakers import SpeakerTracker
        book = self.book()
        shachar = book.keep(self.unit(0, 1, 0), 5, 'm0')
        book.set_name(shachar, 'Shachar')
        vecs = {'first': self.unit(0.8, 0.6, 0), 'more': self.unit(0, 1, 0)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio], voices=book, similarity=0.30)
        _, entry = t.label('first', speech_s=4.0)
        self.assertEqual(entry['name'], 'Speaker 1', '0.6 is not enough for a first chunk')
        self.assertNotIn('voice_id', entry)
        t.label('more', speech_s=4.0)
        _, entry = t.label('more', speech_s=4.0)
        self.assertEqual((entry['name'], entry['voice_id']), ('Shachar', shachar))

    def test_a_name_you_gave_is_never_replaced(self):
        from koli.meeting.speakers import SpeakerTracker
        book = self.book()
        vid = book.keep(self.unit(0, 1, 0), 5, 'm0')
        book.set_name(vid, 'Shachar')
        t = SpeakerTracker(embed=lambda audio: self.unit(0, 1, 0), similarity=0.30)
        t.label('x', speech_s=4.0)
        t.rename('s1', 'Moshe')
        t.voices = book
        for _ in range(3):
            _, entry = t.label('x', speech_s=4.0)
        self.assertEqual((entry['name'], entry['voice_id']), ('Moshe', vid))


# A real Meet: the splitter cut Speaker 1 in two inside a chunk, and "two
# voices of one chunk are two people" made "Speaker 3", who never spoke
# (voiceprints 0.86 alike; the real people were 0.12-0.30 apart).
class FalseSplitTests(unittest.TestCase):
    @staticmethod
    def unit(*xs):
        v = np.array(xs, dtype=np.float32)
        return v / np.linalg.norm(v)

    def test_a_part_that_is_clearly_the_other_voice_is_not_a_new_speaker(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0, 0), 'a_cut': self.unit(0.86, 0.51, 0), 'b': self.unit(0.3, 0.95, 0)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio], similarity=0.30)
        first, _ = t.label('a', speech_s=4.0)
        self.assertEqual(t.label('a_cut', speech_s=4.0, exclude=[first])[0], first)
        self.assertNotEqual(t.label('b', speech_s=4.0, exclude=[first])[0], first, 'a different voice still splits')

    def test_merging_two_labels_moves_the_turns_and_joins_the_voiceprints(self):
        from koli.meeting.session import merge_speaker_into
        m = {'speakers': [{'id': 'you', 'name': 'You', 'colour': 0},
                          {'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0, 0.0], 'count': 3},
                          {'id': 's3', 'name': 'Speaker 3', 'colour': 3, 'centroid': [0.0, 1.0], 'count': 1}],
             'turns': [{'speaker': 's1'}, {'speaker': 's3'}, {'speaker': 'you'}]}
        self.assertTrue(merge_speaker_into(m, 's3', 's1'))
        self.assertEqual([t['speaker'] for t in m['turns']], ['s1', 's1', 'you'])
        self.assertEqual([s['id'] for s in m['speakers']], ['you', 's1'])
        self.assertEqual(m['speakers'][1]['count'], 4)
        self.assertGreater(m['speakers'][1]['centroid'][0], m['speakers'][1]['centroid'][1])
        self.assertFalse(merge_speaker_into(m, 'you', 's1'), 'You is never merged')

    # Renaming "Speaker 3" to Speaker 1's name merges them, in a saved meeting too.
    def test_renaming_to_a_name_in_the_meeting_merges_a_saved_meeting(self):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore, new_meeting
        from koli.meeting.voices import VoiceBook
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig({'meeting.enabled': False})
        ctx.model_registry = None
        store = MeetingStore(Path(tmp.name) / 'meetings')
        m = new_meeting('m1', 'Meeting', {'key': 'manual', 'app': '', 'kind': 'manual'}, at(0))
        m['speakers'] += [{'id': 's1', 'name': 'Eran', 'colour': 1, 'centroid': [1.0, 0.0], 'count': 5},
                          {'id': 's3', 'name': 'Speaker 3', 'colour': 3, 'centroid': [0.9, 0.1], 'count': 1}]
        m['turns'] = [{'start': 0, 'end': 1, 'stream': 'loopback', 'speaker': 's3', 'text': 'x', 'lang': 'en'}]
        store.save(m)
        svc = MeetingService(ctx, store=store, publish=lambda t, p: None, embedder_factory=lambda: None,
                             loopback_available=lambda: True, sessions_fn=lambda: [],
                             voices=VoiceBook(Path(tmp.name) / 'voices.json'))
        self.addCleanup(svc.shutdown)
        svc.rename_saved_speaker('m1', 's3', 'eran')
        saved = store.load('m1')
        self.assertEqual([s['id'] for s in saved['speakers'] if s['id'] != 'you'], ['s1'])
        self.assertEqual(saved['turns'][0]['speaker'], 's1')


class SessionVoiceNameTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sm = SessionManager(MeetingStore(Path(self.tmp.name)), start_capture=lambda m: None,
                                 stop_capture=lambda: None, publish=lambda t, p: None,
                                 settings=lambda p, d=None: d)
        self.sm.start()

    def test_a_remembered_name_replaces_only_speaker_n(self):
        self.sm.ensure_speaker({'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0], 'count': 1})
        self.sm.ensure_speaker({'id': 's2', 'name': 'Speaker 2', 'colour': 2, 'centroid': [0.0], 'count': 1})
        self.sm.rename_speaker('s2', 'Moshe')
        self.sm.ensure_speaker({'id': 's1', 'name': 'Eran', 'colour': 1, 'centroid': [1.0], 'count': 2, 'voice': 'Eran'})
        self.sm.ensure_speaker({'id': 's2', 'name': 'Eran', 'colour': 2, 'centroid': [0.0], 'count': 2, 'voice': 'Eran'})
        names = {s['id']: s['name'] for s in self.sm.meeting['speakers']}
        self.assertEqual((names['s1'], names['s2']), ('Eran', 'Moshe'))
        self.assertNotIn('voice', self.sm.meeting['speakers'][1], 'the marker is never stored')


# One loopback chunk with two voices becomes two turns, two speakers.
class TranscriberSplitTests(unittest.TestCase):
    def test_a_chunk_with_two_voices_becomes_two_turns(self):
        from koli.meeting.transcriber import MeetingTranscriber
        from koli.meeting.speakers import SpeakerTracker
        audio = np.zeros(16000 * 12, dtype=np.float32)
        audio[16000 * 6:] = 1.0      # the second voice's stretch, so the embedder can tell them apart
        engine = FakeEngine(['Move it. I disagree.'])
        engine.transcribe_audio_info = None
        original = engine.transcribe_audio

        def transcribe(a, language=None, use_prompt=True):
            text = original(a, language, use_prompt)
            engine.last_info = {'language': 'en', 'segments': [
                {'text': ' Move it.', 'start': 0.2, 'end': 5.5, 'no_speech_prob': 0.0, 'avg_logprob': -0.1, 'compression_ratio': 1.0},
                {'text': ' I disagree.', 'start': 6.3, 'end': 11.5, 'no_speech_prob': 0.0, 'avg_logprob': -0.1, 'compression_ratio': 1.0}]}
            return text
        engine.transcribe_audio = transcribe
        embed = lambda a: np.array([1.0, 0.0]) if float(np.mean(a)) < 0.5 else np.array([0.0, 1.0])
        turns, speakers = [], []
        t = MeetingTranscriber(engine_provider=lambda: engine, is_busy=lambda: False,
                               tracker=SpeakerTracker(embed=embed), language_provider=lambda: 'auto',
                               on_turn=turns.append, on_speaker=speakers.append, on_pending=lambda s: None,
                               on_behind=lambda s: None, poll_s=0.01)
        t.split = lambda a: [(0.0, 5.8, 0), (6.0, 12.0, 1)]
        t.start()
        t.submit({'audio': audio, 'stream': 'loopback', 't0': 100.0, 't1': 112.0, 'speech_s': 11.0})
        deadline = time.time() + 3
        while (t.queued_seconds() > 0 or t.working or len(turns) < 2) and time.time() < deadline:
            time.sleep(0.01)
        t.stop()
        self.assertEqual([(x['speaker'], x['text']) for x in turns], [('s1', 'Move it.'), ('s2', 'I disagree.')])
        self.assertEqual((turns[1]['t0'], turns[1]['t1']), (106.3, 111.5))


# Headphones leak the call into the headset's microphone (38 dB under it in a
# real Meet); Whisper turned that into "You" sentences nobody said. A leak is
# far quieter than the call and rises and falls with it; your voice isn't.
class LeakTests(unittest.TestCase):
    @staticmethod
    def call_and_steps(seconds=10.0, seed=1):
        from koli.meeting.transcriber import STEP_S
        rng = np.random.default_rng(seed)
        n = int(seconds / STEP_S)
        # Speech-like loudness: words and pauses, around -18 dB.
        call_db = np.where(rng.random(n) < 0.6, -18 + rng.normal(0, 3, n), -55 + rng.normal(0, 2, n))
        times = 1000.0 + np.arange(n) * STEP_S
        return list(zip(times.tolist(), call_db.tolist())), call_db

    def test_the_call_heard_faintly_by_the_mic_is_a_leak(self):
        from koli.meeting.transcriber import is_leak, STEP_S
        call, call_db = self.call_and_steps()
        mic = (np.roll(call_db, 1) - 38).astype(np.float32)   # 38 dB under, 50 ms late
        self.assertTrue(is_leak(mic, 1000.0, call))

    def test_your_own_voice_is_not_a_leak_even_when_quiet(self):
        from koli.meeting.transcriber import is_leak
        call, _ = self.call_and_steps(seed=1)
        _, own = self.call_and_steps(seed=2)          # speech of its own, not following the call
        self.assertFalse(is_leak((own - 33).astype(np.float32), 1000.0, call), 'a very low mic, own words')
        self.assertFalse(is_leak((own + 5).astype(np.float32), 1000.0, call), 'talking over the call')

    def test_without_call_audio_nothing_is_a_leak(self):
        from koli.meeting.transcriber import is_leak
        _, own = self.call_and_steps()
        self.assertFalse(is_leak(own.astype(np.float32), 1000.0, []))

    def test_loudness_steps_are_50_ms(self):
        from koli.meeting.transcriber import loudness_steps
        steps = loudness_steps(np.full(16000, 0.1, dtype=np.float32))
        self.assertEqual(len(steps), 20)
        self.assertAlmostEqual(float(steps[0]), -20.0, places=1)


class MicMuteTests(unittest.TestCase):
    def make(self):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        from koli.meeting.voices import VoiceBook
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig({'meeting.enabled': False})
        ctx.model_registry = None
        svc = MeetingService(ctx, store=MeetingStore(Path(tmp.name) / 'meetings'), publish=lambda t, p: None,
                             embedder_factory=lambda: None, loopback_available=lambda: True, sessions_fn=lambda: [],
                             voices=VoiceBook(Path(tmp.name) / 'voices.json'))
        self.addCleanup(svc.shutdown)
        return svc

    # One key mutes the microphone in Windows (for the meeting app too) and
    # leaves you out of the meeting; again undoes both.
    def test_mute_toggles_the_microphone_and_exclude_me_together(self):
        import unittest.mock as mock
        svc = self.make()
        state = {'muted': False}
        with mock.patch.object(svc, '_mic_name', return_value='Microphone (Logitech USB Headset)'), \
                mock.patch('koli.platform.media.mic_muted', side_effect=lambda n: state['muted']), \
                mock.patch('koli.platform.media.set_mic_muted',
                           side_effect=lambda n, on: state.update(muted=on) or True):
            self.assertTrue(svc.toggle_mic_mute())
            self.assertTrue(state['muted'])
            self.assertTrue(svc.session.excluded)
            self.assertFalse(svc.toggle_mic_mute())
            self.assertFalse(svc.session.excluded)

    def test_an_unknown_microphone_changes_nothing(self):
        import unittest.mock as mock
        svc = self.make()
        with mock.patch.object(svc, '_mic_name', return_value=None):
            self.assertIsNone(svc.toggle_mic_mute())
        self.assertFalse(svc.session.excluded)


# meeting.keep_audio: off by default; on, each chunk is a WAV beside the
# meeting, and deleting the meeting deletes it.
class KeepAudioTests(unittest.TestCase):
    def test_kept_audio_is_a_wav_per_chunk_and_goes_with_the_meeting(self):
        import wave
        from koli.meeting.store import MeetingStore, new_meeting
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MeetingStore(Path(tmp.name))
        store.save(new_meeting('m1', 'Meeting', {'key': 'manual', 'app': '', 'kind': 'manual'}, at(0)))
        path = store.save_audio('m1', {'t0': 1700000000.5, 'stream': 'loopback',
                                       'audio': np.full(16000, 0.25, dtype=np.float32)})
        self.assertEqual(path.name, '1700000000.50-loopback.wav')
        with wave.open(str(path)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (16000, 1, 16000))
        self.assertEqual([h['id'] for h in store.list()], ['m1'], 'the audio folder is not a meeting')
        self.assertTrue(store.delete('m1'))
        self.assertFalse(store.audio_dir('m1').exists())

    def test_the_service_keeps_audio_only_when_asked(self):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        from koli.meeting.voices import VoiceBook
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        values = {'meeting.enabled': False, 'meeting.keep_audio': False}
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig(values)
        ctx.model_registry = None
        store = MeetingStore(Path(tmp.name) / 'meetings')
        svc = MeetingService(ctx, store=store, publish=lambda t, p: None, embedder_factory=lambda: None,
                             loopback_available=lambda: True, sessions_fn=lambda: [],
                             voices=VoiceBook(Path(tmp.name) / 'voices.json'))
        self.addCleanup(svc.shutdown)
        chunk = {'meeting_id': 'm1', 't0': 5.0, 'stream': 'mic', 'audio': np.zeros(1600, dtype=np.float32)}
        svc._keep_audio(chunk)
        self.assertFalse(store.audio_dir('m1').exists())
        values['meeting.keep_audio'] = True
        svc._keep_audio(chunk)
        self.assertEqual([p.name for p in store.audio_dir('m1').iterdir()], ['5.00-mic.wav'])


class ServiceVoicesTests(unittest.TestCase):
    def make(self):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        from koli.meeting.voices import VoiceBook
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig({'meeting.enabled': False, 'meeting.include_me': True, 'meeting.language': 'auto',
                                         'meeting.speaker_model': 'eres2net', 'meeting.segmentation_model': ''})
        ctx.model_registry = None
        self.book = VoiceBook(Path(tmp.name) / 'voices.json')
        self.store = MeetingStore(Path(tmp.name) / 'meetings')
        fake_loopback = type('L', (), {'start': lambda s: None, 'stop': lambda s: None})
        svc = MeetingService(ctx, store=self.store, publish=lambda t, p: None,
                             loopback_factory=lambda on_block, on_error, device=None: fake_loopback(),
                             embedder_factory=lambda: None, loopback_available=lambda: True, sessions_fn=lambda: [],
                             voices=self.book)
        self.addCleanup(svc.shutdown)
        return svc

    # Three chunks of one voice, one of another (too thin to keep).
    def speak(self, svc, vec, times):
        svc.tracker.embed = lambda audio: np.asarray(vec, dtype=np.float32)
        for _ in range(times):
            sid, entry = svc.tracker.label(np.zeros(16000 * 4), speech_s=4.0)
            if entry:
                svc.session.ensure_speaker(entry)
        return sid

    # When a meeting ends every voice in it is saved with an id, the meeting
    # file points to it, and the next meeting links the same voice to it.
    def test_ending_a_meeting_saves_its_voices_for_the_next_one(self):
        svc = self.make()
        svc.start()
        meeting_id = svc.session.meeting['id']
        self.speak(svc, [1.0, 0.0, 0.0], 3)
        self.speak(svc, [0.0, 1.0, 0.0], 1)
        svc.end()
        saved = [s for s in self.store.load(meeting_id)['speakers'] if s['id'] != 'you']
        self.assertRegex(saved[0]['voice_id'], r'^v-[0-9a-f]{12}$')
        self.assertNotIn('voice_id', saved[1], 'one chunk is too thin to keep')
        self.assertEqual([v['id'] for v in svc.voices_list()], [saved[0]['voice_id']])
        svc.start()
        sid = self.speak(svc, [0.97, 0.2, 0.0], 1)
        linked = next(s for s in svc.session.meeting['speakers'] if s['id'] == sid)
        self.assertEqual(linked['voice_id'], saved[0]['voice_id'])

    # Two labels that are one voice (the splitter cut a person in two) are
    # merged before the voices are saved.
    def test_labels_that_are_one_voice_merge_when_the_meeting_ends(self):
        svc = self.make()
        svc.start()
        meeting_id = svc.session.meeting['id']
        m = svc.session.meeting
        m['speakers'] += [{'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0, 0.0], 'count': 9},
                          {'id': 's3', 'name': 'Speaker 3', 'colour': 3, 'centroid': [0.86, 0.51], 'count': 3}]
        m['turns'] = [{'start': 0, 'end': 1, 'stream': 'loopback', 'speaker': 's3', 'text': 'x', 'lang': 'en'}]
        svc.end()
        saved = self.store.load(meeting_id)
        self.assertEqual([s['id'] for s in saved['speakers'] if s['id'] != 'you'], ['s1'])
        self.assertEqual(saved['turns'][0]['speaker'], 's1')
        self.assertEqual(len(svc.voices_list()), 1)

    def test_naming_a_speaker_names_its_voice(self):
        svc = self.make()
        svc.start()
        sid = self.speak(svc, [1.0, 0.0, 0.0], 2)
        svc.rename_speaker(sid, 'Eran')
        voices = svc.voices_list()
        self.assertEqual([v['name'] for v in voices], ['Eran'])
        self.assertEqual(self.book.match([0.9, 0.1, 0.0])['name'], 'Eran')
        self.assertTrue(svc.forget_voice(voices[0]['id']))
        self.assertEqual(svc.voices_list(), [])

    def test_voices_are_not_saved_when_turned_off(self):
        svc = self.make()
        svc.ctx.config_manager.values['meeting.remember_voices'] = False
        svc.start()
        self.speak(svc, [1.0, 0.0, 0.0], 3)
        svc.end()
        self.assertEqual(svc.voices_list(), [])


if __name__ == '__main__':
    unittest.main()
