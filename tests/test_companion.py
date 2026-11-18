# tests/test_companion.py
# Koli in the meeting: the brain that keeps notes while a meeting runs
# (src/koli/meeting/companion.py), the tasks kept in the meeting file, the
# service that follows the live meeting (src/koli/services/companion.py),
# and what the Meet feed knows about Koli's own tile.

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from koli.meeting import companion  # noqa: E402
from koli.meeting.companion import CompanionError  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeResult:
    def __init__(self, text='', error=''):
        self.text, self.error = text, error
        self.ok = bool(text) and not error


class FakeAI:
    """Stands in for llm.complete: answers in turn, remembers what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, feature, messages, json=False, **kwargs):
        self.calls.append({'feature': feature, 'messages': messages, 'json': json})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, FakeResult):
            return answer
        return FakeResult(answer if isinstance(answer, str) else __import__('json').dumps(answer, ensure_ascii=False))

    def said(self, index=-1) -> str:
        return '\n'.join(m['content'] for m in self.calls[index]['messages'])


def meeting(turns, speakers=None):
    return {'id': 'm1', 'title': 'Weekly', 'started_at': '2026-11-18T10:00:00+02:00', 'ended_at': None,
            'speakers': speakers or [{'id': 'you', 'name': 'You', 'colour': 0}, {'id': 's1', 'name': 'Dana', 'colour': 1},
                                     {'id': 's2', 'name': 'Eran', 'colour': 2}],
            'turns': [{'start': start, 'end': start + 2, 'stream': 'mic' if who == 'you' else 'loopback',
                       'speaker': who, 'text': text, 'lang': 'en'} for start, who, text in turns]}


NOTES = {'topic': 'Pricing for the Pro plan', 'summary': ['The team compared two prices.'],
         'decisions': ['Ship on Thursday'], 'tasks': [{'task': 'Send the QA list', 'owner': 'Eran'}],
         'questions': ['Who writes the guide?'], 'ask': None}


# ── Lines ────────────────────────────────────────────────────────────────────

class LinesTests(unittest.TestCase):
    def test_names_are_resolved_and_the_owner_is_you(self):
        m = meeting([(5, 'you', 'I think so'), (65, 's1', 'Hi there')])
        rows = companion.lines(m)
        self.assertEqual(rows[0], {'at': 5, 'name': 'You', 'you': True, 'text': 'I think so', 'heard': False, 'index': 0})
        self.assertEqual(rows[1]['name'], 'Dana')
        self.assertFalse(rows[1]['you'])

    def test_a_renamed_speaker_has_the_new_name(self):
        m = meeting([(5, 's1', 'Hi')])
        m['speakers'][1]['name'] = 'Dana Levi'
        self.assertEqual(companion.lines(m)[0]['name'], 'Dana Levi')

    def test_an_unknown_speaker_keeps_its_id(self):
        m = meeting([(5, '?', 'Hello')])
        self.assertEqual(companion.lines(m)[0]['name'], 'Speaker ?')

    def test_from_an_index_and_since_a_time(self):
        m = meeting([(5, 's1', 'one'), (65, 's1', 'two'), (125, 's2', 'three')])
        self.assertEqual([r['text'] for r in companion.lines(m, first=1)], ['two', 'three'])
        self.assertEqual([r['text'] for r in companion.lines(m, since=100)], ['three'])
        self.assertEqual(companion.lines(m, first=9), [])

    def test_heard_only_leaves_out_your_words_the_call_did_not_hear(self):
        # Koli's own Whisper hears your microphone even while you are muted in Meet.
        m = meeting([(5, 'you', 'said while muted'), (9, 's1', 'said by Dana'), (12, 'you', 'said in the call')])
        m['turns'][2]['heard'] = True
        self.assertEqual([r['text'] for r in companion.lines(m, heard_only=True)], ['said by Dana', 'said in the call'])
        self.assertEqual(len(companion.lines(m)), 3)
        self.assertEqual([r['heard'] for r in companion.lines(m)], [False, True, True])

    def test_a_turn_with_no_mark_is_asked_about(self):
        m = meeting([(5, 'you', 'on the air'), (9, 'you', 'muted'), (12, 'you', 'too soon to tell'), (15, 's1', 'Dana')])
        answers = {'on the air': True, 'muted': False, 'too soon to tell': None}
        rows = companion.lines(m, heard=lambda turn: answers[turn['text']])
        self.assertEqual([(r['index'], r['heard']) for r in rows], [(0, True), (1, False), (2, None), (3, True)])
        self.assertEqual([r['text'] for r in companion.lines(m, heard=lambda turn: answers[turn['text']], heard_only=True)],
                         ['on the air', 'Dana'])

    def test_empty_text_is_left_out(self):
        m = meeting([(5, 's1', '  '), (9, 's1', 'ok')])
        self.assertEqual([r['text'] for r in companion.lines(m)], ['ok'])

    def test_transcript_format(self):
        rows = companion.lines(meeting([(65, 's1', 'hi'), (3725, 'you', 'yes')]))
        self.assertEqual(companion.transcript(rows), '[1:05] Dana: hi\n[1:02:05] You: yes')

    def test_transcript_keeps_the_newest_lines_under_the_limit(self):
        rows = companion.lines(meeting([(i * 10, 's1', f'line number {i} ' + 'word ' * 20) for i in range(200)]))
        text = companion.transcript(rows, limit=1000)
        self.assertLessEqual(len(text), 1000)
        self.assertIn('line number 199', text)
        self.assertNotIn('line number 0 ', text)

    def test_words(self):
        rows = companion.lines(meeting([(5, 's1', 'one two three'), (9, 'you', 'four')]))
        self.assertEqual(companion.words(rows), 4)


# ── Notes ────────────────────────────────────────────────────────────────────

class CleanNotesTests(unittest.TestCase):
    def test_only_known_fields_within_their_limits(self):
        raw = {'topic': ' Pricing ', 'summary': ['a', 'b', 'c', 'd'], 'decisions': [f'd{i}' for i in range(20)],
               'tasks': [{'task': f't{i}', 'owner': 'Eran', 'secret': 1} for i in range(20)],
               'questions': [f'q{i}' for i in range(20)], 'ask': None, 'private': 'x', 'answer_for_you': 'y'}
        notes = companion.clean_notes(raw, 'insights')
        self.assertEqual(set(notes), {'topic', 'summary', 'decisions', 'tasks', 'questions', 'ask'})
        self.assertEqual(notes['topic'], 'Pricing')
        self.assertEqual(len(notes['summary']), companion.MAX_SUMMARY)
        self.assertEqual(len(notes['decisions']), companion.MAX_DECISIONS)
        self.assertEqual(len(notes['tasks']), companion.MAX_TASKS)
        self.assertEqual(len(notes['questions']), companion.MAX_QUESTIONS)
        self.assertEqual(notes['tasks'][0], {'task': 't0', 'owner': 'Eran'})

    def test_tasks_given_as_words_become_tasks(self):
        notes = companion.clean_notes({'tasks': ['Send the list', {'task': '', 'owner': 'x'}, 7]}, 'insights')
        self.assertEqual(notes['tasks'], [{'task': 'Send the list', 'owner': None}])

    def test_ask_only_in_active(self):
        ask = {'who': 'Eran', 'question': 'When can we ship?', 'for_you': True, 'answer': 'Monday.', 'points': ['QA ends Thursday']}
        self.assertIsNone(companion.clean_notes({'ask': ask}, 'insights')['ask'])
        cleaned = companion.clean_notes({'ask': ask}, 'active')['ask']
        self.assertEqual(cleaned['who'], 'Eran')
        self.assertEqual(cleaned['answer'], 'Monday.')
        self.assertIs(cleaned['for_you'], True)

    def test_an_ask_without_a_question_is_none(self):
        self.assertIsNone(companion.clean_notes({'ask': {'who': 'Eran', 'question': ''}}, 'active')['ask'])
        self.assertIsNone(companion.clean_notes({'ask': 'when?'}, 'active')['ask'])

    def test_rubbish_becomes_empty_notes(self):
        for raw in (None, [], 'text', 7, {'topic': ['a'], 'decisions': 'x', 'tasks': {'a': 1}}):
            notes = companion.clean_notes(raw, 'active')
            self.assertEqual(notes['decisions'], [])
            self.assertEqual(notes['tasks'], [])
            self.assertIsInstance(notes['topic'], str)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.rows = companion.lines(meeting([(65, 's1', 'So the price is twenty dollars'), (70, 'you', 'Agreed')]))

    def test_the_model_gets_the_old_notes_and_only_the_new_lines(self):
        ai = FakeAI({**NOTES, 'topic': 'The price'})
        notes = companion.update(NOTES, self.rows, 'insights', 'en', ai)
        self.assertEqual(notes['topic'], 'The price')
        self.assertEqual(ai.calls[0]['feature'], 'companion')
        self.assertTrue(ai.calls[0]['json'])
        said = ai.said()
        self.assertIn('Pricing for the Pro plan', said)                 # the notes so far
        self.assertIn('[1:05] Dana: So the price is twenty dollars', said)
        self.assertIn('English', said)

    def test_insights_does_not_ask_for_answers(self):
        ai = FakeAI(NOTES)
        companion.update(NOTES, self.rows, 'insights', 'en', ai)
        self.assertNotIn('"ask"', ai.calls[0]['messages'][0]['content'])
        ai = FakeAI(NOTES)
        companion.update(NOTES, self.rows, 'active', 'en', ai)
        self.assertIn('"ask"', ai.calls[0]['messages'][0]['content'])

    def test_hebrew_is_asked_for_by_name(self):
        ai = FakeAI(NOTES)
        companion.update(NOTES, self.rows, 'insights', 'he', ai)
        self.assertIn('Hebrew', ai.said())

    def test_an_answer_that_is_not_json_raises_and_changes_nothing(self):
        before = json.dumps(NOTES, sort_keys=True)
        for bad in ('Sure! Here are the notes.', '[1, 2]', '{"topic": ', FakeResult(error='The model is busy')):
            with self.assertRaises(CompanionError):
                companion.update(NOTES, self.rows, 'insights', 'en', FakeAI(bad))
        self.assertEqual(json.dumps(NOTES, sort_keys=True), before)

    def test_the_models_error_is_the_message(self):
        with self.assertRaises(CompanionError) as raised:
            companion.update(NOTES, self.rows, 'insights', 'en', FakeAI(FakeResult(error='No AI model chosen. Pick one in Settings › AI.')))
        self.assertIn('No AI model chosen', str(raised.exception))

    def test_json_inside_a_code_fence_is_read(self):
        ai = FakeAI('```json\n' + json.dumps({**NOTES, 'topic': 'Fenced'}) + '\n```')
        self.assertEqual(companion.update(NOTES, self.rows, 'insights', 'en', ai)['topic'], 'Fenced')

    def test_a_json_answer_that_is_not_the_notes_raises(self):
        # {} or {"error": ...} once emptied the notes, and with them the board in the call.
        for bad in ({}, {'error': 'rate limited'}, {'notes': NOTES}, {'summary': ['x']}):
            with self.assertRaises(CompanionError):
                companion.update(NOTES, self.rows, 'insights', 'en', FakeAI(bad))

    def test_fields_the_model_left_out_keep_their_value(self):
        notes = companion.update(NOTES, self.rows, 'insights', 'en', FakeAI({'topic': 'The price'}))
        self.assertEqual(notes['topic'], 'The price')
        self.assertEqual(notes['decisions'], NOTES['decisions'])
        self.assertEqual(notes['tasks'], NOTES['tasks'])
        self.assertEqual(notes['questions'], NOTES['questions'])

    def test_an_answer_that_cannot_be_read_is_asked_for_once_more(self):
        # Seen with a small local model on 2026-11-18: one answer in a few is not the notes.
        ai = FakeAI('Here are your notes!', {**NOTES, 'topic': 'Second try'})
        notes = companion.update(NOTES, self.rows, 'insights', 'en', ai)
        self.assertEqual(notes['topic'], 'Second try')
        self.assertEqual(len(ai.calls), 2)

    def test_two_answers_that_cannot_be_read_are_enough(self):
        ai = FakeAI({'error': 'x'})
        with self.assertRaises(CompanionError):
            companion.update(NOTES, self.rows, 'insights', 'en', ai)
        self.assertEqual(len(ai.calls), 2)

    def test_a_model_that_fails_is_not_asked_again(self):
        ai = FakeAI(FakeResult(error='No AI model chosen. Pick one in Settings › AI.'))
        with self.assertRaises(CompanionError):
            companion.update(NOTES, self.rows, 'insights', 'en', ai)
        self.assertEqual(len(ai.calls), 1)

    def test_the_model_is_told_who_you_are(self):
        # "Noa, when can we ship?" is a question to "You" only if the model knows You is Noa.
        ai = FakeAI(NOTES)
        companion.update(NOTES, self.rows, 'active', 'en', ai, owner='Noa')
        self.assertIn('"You" is Noa', ai.calls[0]['messages'][0]['content'])
        ai = FakeAI(NOTES)
        companion.update(NOTES, self.rows, 'active', 'en', ai)
        self.assertNotIn('"You" is ', ai.calls[0]['messages'][0]['content'])

    def test_the_field_names_are_asked_for_as_they_are(self):
        ai = FakeAI(NOTES)
        companion.update(NOTES, self.rows, 'insights', 'he', ai)
        self.assertIn('field names exactly as written', ai.calls[0]['messages'][0]['content'])

    def test_no_new_lines_no_call(self):
        ai = FakeAI(NOTES)
        self.assertEqual(companion.update(NOTES, [], 'insights', 'en', ai), NOTES)
        self.assertEqual(ai.calls, [])


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.rows = companion.lines(meeting([(65, 's2', 'When can we ship the Pro plan?'), (70, 's1', 'QA ends on Thursday')]))

    def test_answer(self):
        ai = FakeAI({'question': 'When can we ship?', 'answer': 'Monday is realistic.', 'points': ['QA ends Thursday', 2, '']})
        card = companion.answer(NOTES, self.rows, 'en', ai)
        self.assertEqual(card, {'question': 'When can we ship?', 'answer': 'Monday is realistic.', 'points': ['QA ends Thursday']})
        self.assertIn('[1:05] Eran: When can we ship the Pro plan?', ai.said())

    def test_answer_may_use_past_meetings_only_when_given(self):
        ai = FakeAI({'question': 'q', 'answer': 'a', 'points': []})
        companion.answer(NOTES, self.rows, 'en', ai)
        self.assertNotIn('earlier meetings', ai.said().lower())
        companion.answer(NOTES, self.rows, 'en', ai, past='12 Oct: we chose Thursday releases')
        self.assertIn('12 Oct: we chose Thursday releases', ai.said())

    def test_an_answer_is_asked_for_once_more_when_it_has_no_words(self):
        ai = FakeAI({'question': 'q', 'answer': ''}, {'question': 'q', 'answer': 'Monday.', 'points': []})
        self.assertEqual(companion.answer(NOTES, self.rows, 'en', ai)['answer'], 'Monday.')
        self.assertEqual(len(ai.calls), 2)

    def test_an_answer_with_no_words_raises(self):
        with self.assertRaises(CompanionError):
            companion.answer(NOTES, self.rows, 'en', FakeAI({'question': 'q', 'answer': '', 'points': []}))

    def test_catch_up(self):
        ai = FakeAI({'lines': ['They compared prices.', 'Thursday was chosen.', 'a', 'b', 'c']})
        self.assertEqual(companion.catch_up(self.rows, 'en', ai), {'lines': ['They compared prices.', 'Thursday was chosen.', 'a']})

    def test_catch_up_with_nothing_said(self):
        with self.assertRaises(CompanionError) as raised:
            companion.catch_up([], 'en', FakeAI({'lines': ['x']}))
        self.assertIn('Nothing was said', str(raised.exception))

    def test_task_from_talk(self):
        ai = FakeAI({'task': 'Send the QA list by Wednesday', 'owner': 'Eran'})
        self.assertEqual(companion.task_from_talk(self.rows, 'en', ai), {'task': 'Send the QA list by Wednesday', 'owner': 'Eran'})

    def test_task_from_talk_without_a_task_raises(self):
        with self.assertRaises(CompanionError):
            companion.task_from_talk(self.rows, 'en', FakeAI({'task': '', 'owner': None}))

    def test_public_answer_gets_only_the_public_part_of_the_notes(self):
        ai = FakeAI({'answer': 'Thursday.'})
        notes = {**NOTES, 'summary': ['SUMMARY LINE FOR THE OWNER'],
                 'ask': {'who': 'Eran', 'question': 'q', 'for_you': True, 'answer': 'ANSWER FOR THE OWNER', 'points': []}}
        companion.public_answer(notes, self.rows, 'Koli, what did we decide?', 'en', ai)
        said = ai.said()
        self.assertNotIn('SUMMARY LINE FOR THE OWNER', said)
        self.assertNotIn('ANSWER FOR THE OWNER', said)
        self.assertIn('Ship on Thursday', said)                 # a decision is on the board anyway

    def test_public_answer_is_from_this_meeting_only_and_short(self):
        ai = FakeAI({'answer': 'We decided to ship on Thursday. ' * 40})
        text = companion.public_answer(NOTES, self.rows, 'Koli, what did we decide?', 'en', ai)
        self.assertLessEqual(len(text), companion.SAY_MAX)
        said = ai.said()
        self.assertIn('everyone in the call', said)
        self.assertIn('this meeting only', said)
        self.assertIn('Koli, what did we decide?', said)


# ── What others may see ──────────────────────────────────────────────────────

class PublicViewTests(unittest.TestCase):
    def test_exactly_the_public_fields(self):
        view = companion.public_view(NOTES, 'Noa', name='Koli')
        self.assertEqual(tuple(view), companion.PUBLIC_FIELDS)
        self.assertEqual(companion.PUBLIC_FIELDS, ('title', 'subtitle', 'topic', 'decisions', 'tasks', 'questions', 'say'))
        self.assertEqual(view['title'], 'Koli')
        self.assertEqual(view['subtitle'], 'AI notes · with Noa')
        self.assertEqual(view['topic'], 'Pricing for the Pro plan')
        self.assertEqual(view['tasks'], ['Send the QA list (Eran)'])

    def test_what_is_for_the_owner_never_gets_in(self):
        notes = {**NOTES, 'ask': {'who': 'Eran', 'question': 'When?', 'for_you': True, 'answer': 'SECRET ANSWER', 'points': ['SECRET POINT']},
                 'summary': ['SECRET SUMMARY LINE'], 'private': 'SECRET FIELD'}
        text = json.dumps(companion.public_view(notes, 'Noa'), ensure_ascii=False)
        for secret in ('SECRET ANSWER', 'SECRET POINT', 'SECRET SUMMARY LINE', 'SECRET FIELD'):
            self.assertNotIn(secret, text)

    def test_no_owner_no_with(self):
        self.assertEqual(companion.public_view(NOTES, '')['subtitle'], 'AI notes')

    def test_hebrew_notes_get_a_hebrew_subtitle(self):
        notes = {**NOTES, 'topic': 'תמחור תוכנית Pro'}
        self.assertEqual(companion.public_view(notes, 'נועה')['subtitle'], 'סיכום AI · עם נועה')

    def test_empty_notes_say_she_listens(self):
        view = companion.public_view(companion.empty_notes(), 'Noa')
        self.assertEqual(view['topic'], '')
        self.assertEqual(view['decisions'], [])

    def test_say_is_passed_and_cut(self):
        view = companion.public_view(NOTES, 'Noa', say='x' * 1000)
        self.assertEqual(len(view['say']), companion.SAY_MAX)


class SpokenToTests(unittest.TestCase):
    def test_her_name_and_a_question(self):
        self.assertTrue(companion.spoken_to('Koli, what did we decide?', 'Koli'))
        self.assertTrue(companion.spoken_to('What do you think, Koli?', 'Koli'))
        self.assertTrue(companion.spoken_to('קולי מה החלטנו?', 'Koli'))
        self.assertTrue(companion.spoken_to('Coley can you list the tasks', 'Koli'))

    def test_her_name_inside_another_word_is_not_her(self):
        self.assertFalse(companion.spoken_to('the colour is nice?', 'Koli'))
        self.assertFalse(companion.spoken_to('broccoli is what we ordered?', 'Koli'))
        self.assertFalse(companion.spoken_to('אני שומע את הקוליות שלך?', 'Koli'))

    def test_her_name_with_no_question_is_not_a_question(self):
        self.assertFalse(companion.spoken_to('I told Koli about it yesterday.', 'Koli'))

    def test_another_name_she_was_given(self):
        self.assertTrue(companion.spoken_to('Nova, when do we ship?', 'Nova'))
        self.assertFalse(companion.spoken_to('Koli, when do we ship?', 'Nova'))


# ── Tasks in the meeting file ────────────────────────────────────────────────

class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        from tests.test_meeting import FakeClock, at
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))
        self.clock = FakeClock(at(30))
        self.events = []
        settings = {'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.countdown_seconds': 0,
                    'meeting.language': 'auto', 'meeting.include_me': True}
        self.sm = SessionManager(self.store, start_capture=lambda m: None, stop_capture=lambda: None,
                                 publish=lambda topic, payload: self.events.append((topic, payload)),
                                 settings=lambda path, default=None: settings.get(path, default),
                                 now=self.clock.now, schedule=self.clock.schedule)

    def live(self, source):
        self.sm.on_sources([source])
        self.sm.on_speech(3.5)
        self.assertEqual(self.sm.state, 'live')
        return self.sm.current()

    def gone(self):
        self.sm.on_sources([])
        self.clock.advance(2)
        self.sm.on_sources([])

    def test_tasks_on_the_live_meeting_are_kept_and_saved(self):
        from tests.test_meeting import zoom
        m = self.live(zoom())
        self.events.clear()
        ok = self.sm.set_tasks(m['id'], [{'id': 'T1', 'task': 'Send the QA list', 'owner': 'Eran', 'at': 65.4, 'source': 'you'}])
        self.assertTrue(ok)
        self.assertEqual(self.sm.current()['tasks'], [{'id': 'T1', 'task': 'Send the QA list', 'owner': 'Eran', 'at': 65.4, 'source': 'you'}])
        self.assertIn('meeting', [topic for topic, _ in self.events])
        self.sm.end()
        self.assertEqual(self.store.load(m['id'])['tasks'][0]['task'], 'Send the QA list')

    def test_tasks_are_tidied(self):
        from tests.test_meeting import zoom
        m = self.live(zoom())
        self.sm.set_tasks(m['id'], [{'task': '  Write   the guide ' + 'x' * 400, 'owner': '', 'source': 'someone'},
                                    {'task': '', 'owner': 'Dana'}, 'not a task',
                                    {'id': 'T9', 'task': 'Call the customer', 'owner': 'D' * 80, 'at': 'soon', 'source': 'koli'}])
        tasks = self.sm.current()['tasks']
        self.assertEqual(len(tasks), 2)
        self.assertEqual(len(tasks[0]['task']), 200)
        self.assertTrue(tasks[0]['task'].startswith('Write the guide'))
        self.assertIsNone(tasks[0]['owner'])
        self.assertEqual(tasks[0]['source'], 'you')
        self.assertTrue(tasks[0]['id'])
        self.assertNotEqual(tasks[0]['id'], tasks[1]['id'])
        self.assertEqual(tasks[1], {'id': 'T9', 'task': 'Call the customer', 'owner': 'D' * 40, 'at': None, 'source': 'koli'})

    def test_tasks_on_a_saved_meeting(self):
        from tests.test_meeting import zoom
        m = self.live(zoom())
        self.sm.end()
        self.assertTrue(self.sm.set_tasks(m['id'], [{'task': 'Late task'}]))
        self.assertEqual(self.store.load(m['id'])['tasks'][0]['task'], 'Late task')
        self.assertFalse(self.sm.set_tasks('no-such-meeting', [{'task': 'x'}]))

    def test_continue_previous_moves_the_tasks_with_their_times(self):
        from tests.test_meeting import meet, started_at
        first = self.live(meet('abc-defg-hij'))
        self.sm.set_tasks(first['id'], [{'id': 'A', 'task': 'First task', 'at': 10}])
        self.gone()
        self.clock.advance(60)
        self.sm.on_sources([meet('zzz-zzzz-zzz')])
        self.sm.on_speech(4.0)
        second = self.sm.current()
        self.assertNotEqual(second['id'], first['id'])
        self.sm.set_tasks(second['id'], [{'id': 'B', 'task': 'Second task', 'at': 10}, {'id': 'C', 'task': 'No time'}])
        self.sm.continue_previous()
        cur = self.sm.current()
        self.assertEqual(cur['id'], first['id'])
        offset = started_at(second) - started_at(first)
        self.assertEqual([(t['id'], t['at']) for t in cur['tasks']], [('A', 10.0), ('B', round(offset + 10, 2)), ('C', None)])

    def test_continue_previous_never_leaves_two_tasks_with_one_id(self):
        from tests.test_meeting import meet
        first = self.live(meet('abc-defg-hij'))
        self.sm.set_tasks(first['id'], [{'task': 'First task', 'at': 10}])
        self.gone()
        self.clock.advance(60)
        self.sm.on_sources([meet('zzz-zzzz-zzz')])
        self.sm.on_speech(4.0)
        second = self.sm.current()
        self.sm.set_tasks(second['id'], [{'task': 'Second task', 'at': 10}])
        self.sm.continue_previous()
        tasks = self.sm.current()['tasks']
        self.assertEqual([t['task'] for t in tasks], ['First task', 'Second task'])
        self.assertEqual(len({t['id'] for t in tasks}), 2)

    def test_start_new_instead_splits_the_tasks_at_the_resume(self):
        from tests.test_meeting import started_at, zoom
        m = self.live(zoom())
        base = started_at(m)
        self.sm.add_turn({'t0': base + 1, 't1': base + 2, 'stream': 'mic', 'speaker': 'you', 'text': 'first', 'lang': 'en'})
        self.sm.set_tasks(m['id'], [{'id': 'A', 'task': 'Before', 'at': 1}, {'id': 'N', 'task': 'No time'}])
        self.gone()
        self.clock.advance(58)          # resumed 60 s in
        self.sm.on_sources([zoom()])
        self.sm.add_turn({'t0': base + 70, 't1': base + 72, 'stream': 'mic', 'speaker': 'you', 'text': 'second', 'lang': 'en'})
        self.sm.set_tasks(m['id'], [{'id': 'A', 'task': 'Before', 'at': 1}, {'id': 'N', 'task': 'No time'},
                                    {'id': 'B', 'task': 'After', 'at': 70}])
        self.sm.split_resumed()
        fresh = self.sm.current()
        self.assertNotEqual(fresh['id'], m['id'])
        self.assertEqual([(t['id'], t['at']) for t in fresh['tasks']], [('B', 10.0)])
        self.assertEqual([t['id'] for t in self.store.load(m['id'])['tasks']], ['A', 'N'])

    def test_the_summary_reads_the_tasks(self):
        from koli.meeting.summarize import meeting_text
        m = meeting([(65, 's1', 'I will send the list')])
        self.assertNotIn('Tasks noted during the meeting', meeting_text(m))
        m['tasks'] = [{'id': 'T1', 'task': 'Send the QA list', 'owner': 'Eran', 'at': 65, 'source': 'you'},
                      {'id': 'T2', 'task': 'Book the room', 'owner': None, 'at': None, 'source': 'koli'}]
        text = meeting_text(m)
        self.assertIn('Tasks noted during the meeting (include each in the action items):', text)
        self.assertIn('- [1:05] Send the QA list (Eran)', text)
        self.assertIn('- Book the room', text)
        self.assertLess(text.index('Tasks noted during the meeting'), text.index('Transcript, one turn per line'))


# ── The service ──────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self):
        self.meeting = None
        self.state = 'idle'

    def set_tasks(self, meeting_id, tasks):
        from koli.meeting.session import clean_tasks
        if self.meeting and self.meeting['id'] == meeting_id:
            self.meeting['tasks'] = clean_tasks(tasks)
            return True
        return False


class FakeMeet:
    def __init__(self):
        self.calls = {}
        self.states = {}       # epoch second a line began -> True, False or None (not known yet)

    def call(self, code):
        return self.calls.get(code)

    # What the test said about a line of the owner's, by its words; False when it said nothing.
    def heard_state(self, code, t0, t1):
        return self.states.get(round(t0, 2), False)


class FakeMeetings:
    def __init__(self):
        self.session = FakeSession()
        self.meet = FakeMeet()


class FakeGuest:
    def __init__(self, on_state, name='Koli', announce=''):
        self.on_state, self.name, self.announce = on_state, name, announce
        self.state = 'off'
        self.joined, self.said, self.views, self.tiles = [], [], [], []
        self.left = 0

    @property
    def in_call(self):
        return self.state == 'in_call'

    def join(self, code, owner=''):
        self.joined.append((code, owner))
        self.state = 'in_call'
        self.on_state('in_call', f'{self.name} is in the call', None)

    def leave(self):
        self.left += 1
        self.state = 'off'
        self.on_state('off', '', None)

    def show(self, view):
        self.views.append(view)

    def say(self, text):
        if not self.in_call:
            return False
        self.said.append(text)
        return True

    def set_tile(self, width, height):
        self.tiles.append((width, height))


class SlowGuest(FakeGuest):
    """A guest still on her way: join() returns and she has said nothing yet."""

    def join(self, code, owner=''):
        self.joined.append((code, owner))


LONG = 'we should look at the price of the Pro plan again because customers told us that twenty dollars is too much for small teams and for startups too'


class ServiceCase(unittest.TestCase):
    def make(self, *answers, ready=True, **settings):
        from koli.services.companion import CompanionService
        self.settings = {'meeting.companion.mode': 'insights', 'meeting.companion.update_seconds': 45,
                         'meeting.companion.join_call': False, 'meeting.companion.name': 'Koli',
                         'meeting.companion.announce': True, 'meeting.companion.chat': False}
        self.settings.update({f'meeting.companion.{k}': v for k, v in settings.items()})
        self.meetings = FakeMeetings()
        self.events, self.copied, self.guests = [], [], []
        self.t = 1000.0
        self.ai = FakeAI(*(answers or (NOTES,)))

        def guest_factory(on_state, name, announce):
            guest = FakeGuest(on_state, name, announce)
            self.guests.append(guest)
            return guest
        self.svc = CompanionService(self.meetings, setting=lambda path, default=None: self.settings.get(path, default),
                                    publish=lambda topic, payload: self.events.append((topic, payload)),
                                    complete=self.ai, parse=None, ai_ready=lambda: ready, guest_factory=guest_factory,
                                    copy=self.copied.append, now=lambda: self.t, start_worker=False)
        return self.svc

    def start(self, code='abc-defg-hij'):
        m = meeting([])
        m['source'] = {'key': f'meet|{code}', 'app': 'chrome.exe', 'kind': 'browser', 'title': '', 'code': code, 'device': ''}
        self.meetings.session.meeting = m
        self.meetings.session.state = 'live'
        self.meetings.meet.calls[code] = {'code': code, 'self_name': 'Noa Levi', 'people': []}
        self.svc.on_event('meeting', {'id': 'm1', 'state': 'live'})
        self.drain()
        return m

    def turn(self, start, who, text, heard=False):
        turn = {'start': start, 'end': start + 2, 'stream': 'mic' if who == 'you' else 'loopback', 'speaker': who,
                'text': text, 'lang': 'en'}
        if heard:
            turn['heard'] = True
        self.meetings.session.meeting['turns'].append(turn)
        self.svc.on_event('meeting_turn', {'id': 'm1', 'turn': turn})

    def drain(self):
        while self.svc.step():
            pass

    def cards(self, kind=None):
        return [c for c in self.svc.state()['cards'] if kind is None or c['kind'] == kind]


class ServiceNotesTests(ServiceCase):
    def test_a_new_line_only_queues_work(self):
        self.make()
        self.start()
        self.turn(5, 's1', LONG)
        self.assertEqual(self.ai.calls, [])          # the caller holds the session's lock: nothing runs there
        self.drain()
        self.assertEqual(len(self.ai.calls), 1)

    def test_notes_are_updated_and_published(self):
        self.make({**NOTES, 'topic': 'The price'})
        self.start()
        self.turn(5, 's1', LONG)
        self.drain()
        state = self.svc.state()
        self.assertEqual(state['id'], 'm1')
        self.assertEqual(state['mode'], 'insights')
        self.assertEqual(state['notes']['topic'], 'The price')
        self.assertFalse(state['busy'])
        topic, payload = self.events[-1]
        self.assertEqual(topic, 'meeting_companion')
        self.assertEqual(payload['notes']['topic'], 'The price')

    def test_too_few_new_words_wait(self):
        self.make()
        self.start()
        self.turn(5, 's1', 'hello everyone')
        self.drain()
        self.assertEqual(self.ai.calls, [])

    def test_the_next_update_waits_for_its_time(self):
        self.make()
        self.start()
        self.turn(5, 's1', LONG)
        self.drain()
        self.turn(20, 's2', LONG)
        self.drain()
        self.assertEqual(len(self.ai.calls), 1)       # enough words, too soon
        self.t += 44
        self.turn(30, 's1', 'and one more thing')
        self.drain()
        self.assertEqual(len(self.ai.calls), 1)
        self.t += 2
        self.turn(40, 's1', 'that is all')
        self.drain()
        self.assertEqual(len(self.ai.calls), 2)
        self.assertIn('[0:20] Eran:', self.ai.said())       # the lines since the last update
        self.assertNotIn('[0:05] Dana:', self.ai.said().split('New lines:')[1])

    def test_off_makes_no_ai_call(self):
        self.make(mode='off')
        self.start()
        self.turn(5, 's1', LONG)
        self.turn(20, 's2', 'Koli, what did we decide?')
        self.drain()
        self.assertEqual(self.ai.calls, [])
        self.assertEqual(self.svc.state()['mode'], 'off')

    def test_switching_on_in_the_middle_reads_the_meeting_so_far(self):
        self.make(mode='off')
        self.start()
        self.turn(5, 's1', LONG)
        self.drain()
        self.settings['meeting.companion.mode'] = 'insights'
        self.svc.apply_setting('meeting.companion.mode', 'insights')
        self.drain()
        self.assertEqual(len(self.ai.calls), 1)
        self.assertIn('[0:05] Dana:', self.ai.said())
        self.assertEqual(self.svc.state()['mode'], 'insights')

    def test_a_paused_meeting_is_not_read(self):
        self.make()
        self.start()
        self.meetings.session.state = 'paused'
        self.turn(5, 's1', LONG)
        self.drain()
        self.assertEqual(self.ai.calls, [])

    def test_a_shorter_turn_list_after_a_reload_is_no_error(self):
        self.make()
        m = self.start()
        self.turn(5, 's1', LONG)
        self.turn(20, 's2', LONG)
        self.drain()
        del m['turns'][1:]                              # echo removal dropped a turn
        self.svc.on_event('meeting_turn', {'id': 'm1', 'reload': True})
        self.drain()
        self.t += 60
        self.turn(30, 's1', LONG)
        self.drain()
        self.assertEqual(len(self.ai.calls), 2)
        self.assertEqual(self.cards('error'), [])

    def test_another_meetings_turn_is_ignored(self):
        self.make()
        self.start()
        self.svc.on_event('meeting_turn', {'id': 'other', 'turn': {'start': 1, 'end': 2, 'speaker': 's1', 'text': LONG}})
        self.drain()
        self.assertEqual(self.ai.calls, [])

    def test_an_ai_failure_is_a_card_once_and_the_notes_stay(self):
        self.make(NOTES, FakeResult(error='The model is busy'))
        self.start()
        self.turn(5, 's1', LONG)
        self.drain()
        for start in (60, 120):
            self.t += 60
            self.turn(start, 's1', LONG)
            self.drain()
        errors = self.cards('error')
        self.assertEqual(len(errors), 1)
        self.assertIn('The model is busy', errors[0]['text'])
        self.assertEqual(self.svc.state()['notes']['topic'], NOTES['topic'])
        self.assertFalse(self.svc.state()['busy'])

    def test_a_question_becomes_a_card_once_in_active(self):
        ask = {'who': 'Eran', 'question': 'When can we ship?', 'for_you': True, 'answer': 'Monday is realistic.', 'points': ['QA ends Thursday']}
        self.make({**NOTES, 'ask': ask}, mode='active')
        self.start()
        self.turn(5, 's2', LONG + ' when can we ship?')
        self.drain()
        self.t += 60
        self.turn(70, 's1', LONG)
        self.drain()
        cards = self.cards('ask')
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['title'], 'Eran asked you: When can we ship?')
        self.assertEqual(cards[0]['text'], 'Monday is realistic.')
        self.assertEqual(cards[0]['points'], ['QA ends Thursday'])

    def test_the_meeting_ending_clears_the_notes_and_takes_her_out(self):
        self.make()
        self.start()
        self.turn(5, 's1', LONG)
        self.drain()
        self.svc.join()
        self.meetings.session.state = 'idle'
        self.meetings.session.meeting = None
        self.svc.on_event('meeting', {'id': 'm1', 'state': 'ended'})
        self.svc.on_event('meeting', {'state': 'idle'})
        self.drain()
        self.assertEqual(self.guests[0].left, 1)
        state = self.svc.state()
        self.assertEqual(state['id'], '')
        self.assertEqual(state['notes']['topic'], '')
        self.assertEqual(state['cards'], [])

    def test_a_new_meeting_starts_with_empty_notes(self):
        self.make()
        self.start()
        self.turn(5, 's1', LONG)
        self.drain()
        fresh = meeting([])
        fresh['id'] = 'm2'
        self.meetings.session.meeting = fresh
        self.svc.on_event('meeting', {'id': 'm2', 'state': 'live'})
        self.drain()
        self.assertEqual(self.svc.state()['id'], 'm2')
        self.assertEqual(self.svc.state()['notes']['topic'], '')


class ServiceActionTests(ServiceCase):
    def test_what_should_i_answer_makes_a_card(self):
        self.make(NOTES, {'question': 'When can we ship?', 'answer': 'Monday is realistic.', 'points': ['QA ends Thursday']})
        self.start()
        self.turn(5, 's2', LONG)
        self.drain()
        self.svc.answer()
        self.assertTrue(self.svc.state()['busy'])
        self.drain()
        card = self.cards('answer')[0]
        self.assertEqual((card['title'], card['text'], card['points']),
                         ('When can we ship?', 'Monday is realistic.', ['QA ends Thursday']))
        self.assertFalse(self.svc.state()['busy'])

    def test_catch_me_up_makes_a_card_from_the_last_minutes(self):
        self.make(NOTES, {'lines': ['They compared prices.', 'Thursday was chosen.']})
        self.start()
        self.turn(5, 's2', LONG)
        self.turn(900, 's1', 'So we ship on Thursday')
        self.drain()
        self.svc.catch_up(5)
        self.drain()
        card = self.cards('catch_up')[0]
        self.assertEqual(card['text'], 'They compared prices.\nThursday was chosen.')
        said = self.ai.said()
        self.assertIn('[15:00] Dana: So we ship on Thursday', said)
        self.assertNotIn('[0:05]', said)                # older than five minutes

    def test_without_a_model_the_buttons_say_so(self):
        self.make(ready=False)
        self.start()
        self.turn(5, 's2', LONG)
        self.drain()
        self.svc.answer()
        self.drain()
        self.assertEqual(self.ai.calls, [])
        self.assertIn('Choose an AI model', self.cards('error')[0]['text'])
        self.assertFalse(self.svc.state()['ready'])

    def test_add_task_takes_the_task_being_talked_about(self):
        self.make(NOTES, {'task': 'Send the QA list by Wednesday', 'owner': 'Eran'})
        self.start()
        self.turn(5, 's2', LONG)
        self.turn(100, 's2', 'I will send the QA list by Wednesday')
        self.drain()
        self.svc.task()
        self.drain()
        tasks = self.svc.state()['tasks']
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]['task'], tasks[0]['owner'], tasks[0]['source'], tasks[0]['at']),
                         ('Send the QA list by Wednesday', 'Eran', 'you', 102.0))
        self.assertEqual(self.meetings.session.meeting['tasks'], tasks)

    def test_a_task_koli_noted_can_be_taken_once(self):
        self.make()
        self.start()
        self.turn(5, 's2', LONG)
        self.drain()
        self.svc.task_take(0)
        self.svc.task_take(0)
        tasks = self.svc.state()['tasks']
        self.assertEqual([(t['task'], t['owner'], t['source']) for t in tasks], [('Send the QA list', 'Eran', 'koli')])
        with self.assertRaises(ValueError):
            self.svc.task_take(7)

    def test_a_task_can_be_removed(self):
        self.make()
        self.start()
        self.turn(5, 's2', LONG)
        self.drain()
        self.svc.task_take(0)
        self.svc.task_remove(self.svc.state()['tasks'][0]['id'])
        self.assertEqual(self.svc.state()['tasks'], [])

    def test_cards_can_be_dismissed_and_are_at_most_six(self):
        self.make(NOTES, {'lines': ['one']})
        self.start()
        self.turn(5, 's2', LONG)
        self.drain()
        for _ in range(8):
            self.svc.catch_up(5)
            self.drain()
        cards = self.cards()
        self.assertEqual(len(cards), 6)
        self.svc.dismiss(cards[0]['id'])
        self.assertEqual(len(self.cards()), 5)
        self.assertNotIn(cards[0]['id'], [c['id'] for c in self.cards()])

    def test_copy_goes_through_the_app(self):
        self.make()
        self.svc.copy('Monday is realistic.')
        self.assertEqual(self.copied, ['Monday is realistic.'])

    def test_buttons_outside_a_meeting_say_so(self):
        self.make()
        for action in (self.svc.answer, self.svc.catch_up, self.svc.task, self.svc.now_, self.svc.join):
            with self.assertRaises(ValueError):
                action()


class ServiceGuestTests(ServiceCase):
    def test_bring_her_in_uses_the_calls_code_and_your_first_name(self):
        self.make()
        self.start()
        self.assertEqual(self.svc.guest_name(), '')
        self.svc.join()
        guest = self.guests[0]
        self.assertEqual(guest.joined, [('abc-defg-hij', 'Noa')])
        self.assertIn('Koli', guest.announce)
        self.assertIn('Noa', guest.announce)
        self.assertEqual(self.svc.guest_name(), 'Koli')
        self.assertEqual(self.svc.state()['guest']['state'], 'in_call')
        self.svc.leave()
        self.assertEqual(self.svc.guest_name(), '')
        self.assertEqual(self.svc.state()['guest']['state'], 'off')

    def test_no_announcement_when_it_is_off(self):
        self.make(announce=False)
        self.start()
        self.svc.join()
        self.assertEqual(self.guests[0].announce, '')

    def test_a_meeting_that_is_not_a_meet_call_cannot_have_her(self):
        self.make()
        self.start(code='')
        self.assertFalse(self.svc.state()['guest']['can_join'])
        with self.assertRaises(ValueError) as raised:
            self.svc.join()
        self.assertIn('Google Meet', str(raised.exception))

    def test_she_joins_by_herself_only_when_set_and_only_once(self):
        self.make(join_call=True)
        self.start()
        self.assertEqual(len(self.guests), 1)
        self.assertEqual(self.guests[0].joined, [('abc-defg-hij', 'Noa')])
        self.svc.leave()                                     # you took her out
        self.svc.on_event('meeting', {'id': 'm1', 'state': 'live'})
        self.drain()
        self.assertEqual(len(self.guests[0].joined), 1)
        self.assertEqual(len(self.guests), 1)

    def test_she_does_not_join_by_herself_by_default(self):
        self.make()
        self.start()
        self.assertEqual(self.guests, [])

    def test_the_notes_are_asked_for_with_your_name(self):
        self.make(mode='active')
        self.start()
        self.turn(5, 's2', LONG)
        self.drain()
        self.assertIn('"You" is Noa', self.ai.calls[0]['messages'][0]['content'])

    def test_the_board_gets_only_the_public_view(self):
        ask = {'who': 'Eran', 'question': 'When?', 'for_you': True, 'answer': 'SECRET ANSWER', 'points': []}
        self.make({**NOTES, 'ask': ask}, mode='active')
        self.start()
        self.svc.join()
        self.turn(5, 's2', LONG)
        self.drain()
        view = self.guests[0].views[-1]
        self.assertEqual(tuple(view), companion.PUBLIC_FIELDS)
        self.assertEqual(view['topic'], NOTES['topic'])
        self.assertEqual(view['subtitle'], 'AI notes · with Noa')
        self.assertNotIn('SECRET ANSWER', json.dumps(view))

    def test_send_to_chat_needs_her_in_the_call_and_sends_the_cards_text(self):
        self.make(NOTES, {'question': 'When can we ship?', 'answer': 'Monday is realistic.', 'points': []})
        self.start()
        self.turn(5, 's2', LONG)
        self.drain()
        self.svc.answer()
        self.drain()
        card = self.cards('answer')[0]
        with self.assertRaises(ValueError):
            self.svc.say(card['id'])
        self.svc.join()
        self.svc.say(card['id'])
        self.assertEqual(self.guests[0].said, ['Monday is realistic.'])
        with self.assertRaises(ValueError):
            self.svc.say('no-such-card')

    def test_spoken_to_by_name_she_answers_in_the_chat_when_allowed(self):
        self.make({'answer': 'We decided to ship on Thursday.'}, mode='active', chat=True)
        self.start()
        self.svc.join()
        self.turn(5, 's2', 'Koli, what did we decide?')
        self.drain()
        self.assertEqual(self.guests[0].said, ['We decided to ship on Thursday.'])
        self.assertEqual(self.guests[0].views[-1]['say'], 'We decided to ship on Thursday.')
        self.assertEqual(len(self.cards('said')), 1)
        self.t += 5
        self.turn(9, 's1', 'Koli, and who does it?')
        self.drain()
        self.assertEqual(len(self.guests[0].said), 1)        # not twice within 20 seconds
        self.t += 30
        self.turn(40, 's1', 'Koli, and who does it?')
        self.drain()
        self.assertEqual(len(self.guests[0].said), 2)

    def test_she_stays_quiet_unless_active_and_allowed_and_in_the_call(self):
        for settings, joined in (({'mode': 'active', 'chat': False}, True), ({'mode': 'insights', 'chat': True}, True),
                                 ({'mode': 'active', 'chat': True}, False)):
            self.make(NOTES, {'answer': 'x'}, **settings)
            self.start()
            if joined:
                self.svc.join()
            self.turn(5, 's2', 'Koli, what did we decide?')
            self.drain()
            self.assertEqual([g.said for g in self.guests], [[]] if joined else [])

    def test_what_she_said_leaves_the_board_after_a_minute(self):
        self.make({'answer': 'Thursday.'}, NOTES, mode='active', chat=True)
        self.start()
        self.svc.join()
        self.turn(5, 's2', 'Koli, what did we decide?')
        self.drain()
        self.t += 61
        self.turn(70, 's1', LONG)
        self.drain()
        self.assertEqual(self.guests[0].views[-1]['say'], '')

    def test_only_an_answer_can_be_sent_to_the_chat(self):
        self.make(NOTES, {'lines': ['They compared prices.']})
        self.start()
        self.turn(5, 's2', LONG)
        self.drain()
        self.svc.catch_up(5)
        self.drain()
        self.svc.join()
        with self.assertRaises(ValueError):
            self.svc.say(self.cards('catch_up')[0]['id'])
        self.assertEqual(self.guests[0].said, [])

    def test_two_presses_bring_one_koli(self):
        self.make()
        self.start()
        self.svc._guest_factory = lambda on_state, name, announce: self.guests.append(SlowGuest(on_state, name, announce)) or self.guests[-1]
        self.svc.join()
        self.svc.join()
        self.assertEqual(len(self.guests), 1)
        self.assertEqual(self.svc.state()['guest']['state'], 'starting')

    def test_joining_by_herself_can_be_switched_on_in_the_middle(self):
        self.make()
        self.start()
        self.assertEqual(self.guests, [])
        self.settings['meeting.companion.join_call'] = True
        self.svc.apply_setting('meeting.companion.join_call', True)
        self.drain()
        self.assertEqual(len(self.guests), 1)
        self.assertEqual(self.guests[0].joined, [('abc-defg-hij', 'Noa')])

    def another_meeting(self, meeting_id, code):
        fresh = meeting([])
        fresh['id'] = meeting_id
        fresh['source'] = {'key': f'meet|{code}', 'app': 'chrome.exe', 'kind': 'browser', 'title': '', 'code': code, 'device': ''}
        self.meetings.session.meeting = fresh
        self.meetings.meet.calls[code] = {'code': code, 'self_name': 'Noa Levi', 'people': []}
        self.svc.on_event('meeting', {'id': meeting_id, 'state': 'live'})
        self.drain()

    def test_she_stays_in_the_call_when_the_meeting_is_continued_or_split(self):
        # Continue previous and Start new instead change the meeting's id, not the call.
        self.make()
        self.start()
        self.svc.join()
        self.another_meeting('m2', 'abc-defg-hij')
        self.assertEqual(self.guests[0].left, 0)
        self.assertEqual(self.svc.guest_name(), 'Koli')
        self.assertEqual(self.svc.state()['guest']['state'], 'in_call')
        self.assertEqual(self.svc.state()['id'], 'm2')
        self.assertEqual(len(self.guests), 1)

    def test_she_leaves_when_the_next_meeting_is_another_call(self):
        self.make()
        self.start()
        self.svc.join()
        self.another_meeting('m2', 'zzz-zzzz-zzz')
        self.assertEqual(self.guests[0].left, 1)
        self.assertEqual(self.svc.guest_name(), '')

    def test_a_guest_that_was_replaced_cannot_change_the_panel(self):
        self.make()
        self.start()
        self.svc.join()
        first = self.guests[0]
        self.svc.leave()
        self.svc.join()
        first.on_state('failed', 'Koli could not join: the browser is closed', None)
        self.assertEqual(self.svc.state()['guest']['state'], 'in_call')

    def test_in_the_call_her_notes_are_made_only_of_what_the_call_heard(self):
        self.make()
        self.start()
        self.turn(5, 'you', 'PRIVATE ' + LONG)                  # Whisper heard it; Meet may not have
        self.drain()
        self.assertIn('PRIVATE', self.ai.said())                 # alone with you, she may use it
        self.svc.join()
        self.drain()
        self.assertEqual(self.svc.state()['notes']['topic'], '')  # notes that held it are not shown to the call
        self.assertEqual(self.guests[0].views[-1]['topic'], '')
        calls = len(self.ai.calls)
        self.t += 60
        self.turn(70, 's1', 'HEARD ' + LONG)
        self.turn(80, 'you', 'CAPTIONED ' + LONG, heard=True)
        self.turn(90, 'you', 'MUTED ' + LONG)
        self.drain()
        self.assertEqual(len(self.ai.calls), calls + 1)
        said = self.ai.said()
        self.assertIn('HEARD', said)
        self.assertIn('CAPTIONED', said)
        self.assertNotIn('MUTED', said)
        self.assertNotIn('PRIVATE', said)

    def said_at(self, start, state):
        # The feed's answer for the owner's line that began `start` seconds into the meeting.
        from koli.meeting.store import started_ts
        self.meetings.meet.states[round(started_ts(self.meetings.session.meeting) + start, 2)] = state

    def test_a_line_not_yet_known_to_be_heard_holds_back_what_follows(self):
        # The extension confirms the microphone was on a few seconds after the words.
        self.make()
        self.start()
        self.svc.join()
        self.turn(5, 's1', 'FIRST ' + LONG)
        self.said_at(20, None)
        self.turn(20, 'you', 'MINE ' + LONG)
        self.turn(30, 's2', 'THIRD ' + LONG)
        self.drain()
        said = self.ai.said()
        self.assertIn('FIRST', said)
        self.assertNotIn('MINE', said)
        self.assertNotIn('THIRD', said)
        self.said_at(20, True)
        self.t += 60
        self.turn(90, 's1', 'and so on')
        self.drain()
        said = self.ai.said().split('New lines:')[1]
        self.assertIn('MINE', said)
        self.assertIn('THIRD', said)
        self.assertNotIn('FIRST', said)

    def test_a_line_the_call_did_not_hear_is_passed_over(self):
        self.make()
        self.start()
        self.svc.join()
        self.said_at(5, False)
        self.turn(5, 'you', 'MUTED ' + LONG)
        self.turn(20, 's2', 'HEARD ' + LONG)
        self.drain()
        said = self.ai.said()
        self.assertIn('HEARD', said)
        self.assertNotIn('MUTED', said)

    def test_words_to_her_wait_until_it_is_known_the_call_heard_them(self):
        self.make({'answer': 'Thursday.'}, mode='active', chat=True)
        self.start()
        self.svc.join()
        self.said_at(5, None)
        self.turn(5, 'you', 'Koli, what did we decide?')
        self.drain()
        self.assertEqual(self.guests[0].said, [])
        self.said_at(5, True)
        self.turn(9, 's1', 'hmm')
        self.drain()
        self.assertEqual(self.guests[0].said, ['Thursday.'])

    def test_notes_still_being_written_when_she_is_brought_in_are_thrown_away(self):
        # The model is answering about your private words while you press Bring Koli in.
        import json as _json
        self.make()
        self.start()
        pressed = []

        def slow_model(feature, messages, json=False, **kwargs):
            if not pressed:
                pressed.append(True)
                self.svc.join()
            return FakeResult(_json.dumps({**NOTES, 'topic': 'PRIVATE TOPIC'}))
        self.svc._complete = slow_model
        self.turn(5, 'you', 'PRIVATE ' + LONG)
        self.drain()
        self.assertEqual(self.svc.state()['notes']['topic'], '')
        self.assertNotIn('PRIVATE TOPIC', _json.dumps(self.guests[0].views))

    def test_your_own_words_to_her_are_answered_in_the_call_only_if_the_call_heard_them(self):
        self.make({'answer': 'Thursday.'}, mode='active', chat=True)
        self.start()
        self.svc.join()
        self.turn(5, 'you', 'Koli, what did we decide?')         # said with the microphone muted in Meet
        self.drain()
        self.assertEqual(self.guests[0].said, [])
        self.t += 30
        self.turn(40, 'you', 'Koli, what did we decide?', heard=True)
        self.drain()
        self.assertEqual(self.guests[0].said, ['Thursday.'])

    def test_her_answer_to_the_call_is_drawn_only_from_what_the_call_heard(self):
        self.make({'answer': 'Thursday.'}, mode='active', chat=True)
        self.start()
        self.svc.join()
        self.turn(5, 'you', 'MUTED aside about the budget')
        self.turn(9, 's2', 'Koli, what did we decide?')
        self.drain()
        self.assertEqual(self.guests[0].said, ['Thursday.'])
        self.assertNotIn('MUTED', self.ai.said())

    def test_she_stays_careful_when_the_meeting_is_continued_in_the_same_call(self):
        self.make()
        self.start()
        self.svc.join()
        self.another_meeting('m2', 'abc-defg-hij')
        turn = {'start': 5, 'end': 7, 'stream': 'mic', 'speaker': 'you', 'text': 'MUTED ' + LONG, 'lang': 'en'}
        self.meetings.session.meeting['turns'].append(turn)
        self.svc.on_event('meeting_turn', {'id': 'm2', 'turn': turn})
        calls = len(self.ai.calls)
        self.drain()
        self.assertEqual(len(self.ai.calls), calls)              # nothing the call heard: nothing to note

    def test_the_tile_goes_to_the_guest(self):
        self.make()
        self.start()
        self.svc.join()
        self.svc.tile(258, 305)
        self.assertEqual(self.guests[0].tiles, [(258, 305)])

    def test_shutdown_takes_her_out(self):
        self.make()
        self.start()
        self.svc.join()
        self.svc.shutdown()
        self.assertEqual(self.guests[0].left, 1)


# ── In the app: the event bus and the worker thread ──────────────────────────

class ServiceInTheAppTests(ServiceCase):
    def wait_for(self, condition, seconds=5.0):
        import time
        end = time.time() + seconds
        while time.time() < end:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def make_live(self):
        from koli.services.companion import CompanionService
        from koli.ui.events import EventBus
        self.make()
        self.bus = EventBus()
        self.svc = CompanionService(self.meetings, setting=lambda path, default=None: self.settings.get(path, default),
                                    publish=self.bus.publish, complete=self.ai, parse=None, ai_ready=lambda: True,
                                    subscribe=self.bus.subscribe, guest_factory=lambda *a: FakeGuest(*a),
                                    copy=self.copied.append, now=lambda: self.t)
        self.addCleanup(self.svc.shutdown)
        self.published = []
        self.bus.subscribe('meeting_companion', lambda topic, payload: self.published.append(payload))

    def go_live(self):
        m = meeting([])
        m['source'] = {'code': 'abc-defg-hij'}
        self.meetings.session.meeting = m
        self.meetings.session.state = 'live'
        self.bus.publish('meeting', {'id': 'm1', 'state': 'live'})
        return m

    def test_events_on_the_bus_reach_her_and_her_own_thread_does_the_work(self):
        self.make_live()
        m = self.go_live()
        self.assertTrue(self.wait_for(lambda: self.svc.state()['id'] == 'm1'))
        turn = {'start': 5, 'end': 7, 'stream': 'loopback', 'speaker': 's1', 'text': LONG, 'lang': 'en'}
        m['turns'].append(turn)
        self.bus.publish('meeting_turn', {'id': 'm1', 'turn': turn})
        self.assertTrue(self.wait_for(lambda: self.svc.state()['notes']['topic'] == NOTES['topic']))
        self.assertTrue(any(p['notes']['topic'] == NOTES['topic'] for p in self.published))

    def test_after_shutdown_the_bus_no_longer_reaches_her(self):
        self.make_live()
        self.go_live()
        self.assertTrue(self.wait_for(lambda: self.svc.state()['id'] == 'm1'))
        self.svc.shutdown()
        waiting = self.svc._jobs.qsize()
        self.bus.publish('meeting', {'id': 'm9', 'state': 'live'})
        self.assertEqual(self.svc._jobs.qsize(), waiting)


# ── What the Meet feed knows about Koli's own tile ───────────────────────────

class MeetFeedGuestTests(unittest.TestCase):
    def status(self, **more):
        return {'type': 'status', 'code': 'abc-defg-hij', 'joined': True, 'captions': True, 'title': 'Weekly',
                'self_name': 'Noa Levi', 'at_ms': 1000_000,
                'people': [{'id': '1', 'name': 'Noa Levi', 'self': True}, {'id': '2', 'name': 'Dana'},
                           {'id': '3', 'name': 'Koli'}], **more}

    def test_koli_is_not_one_of_the_people(self):
        from koli.meeting.meet_feed import MeetFeed
        feed = MeetFeed(now=lambda: 1000.0, guest_name=lambda: 'koli')
        feed.apply([self.status()])
        self.assertEqual([p['name'] for p in feed.call('abc-defg-hij')['people']], ['Noa Levi', 'Dana'])

    def test_without_a_guest_nobody_is_left_out(self):
        from koli.meeting.meet_feed import MeetFeed
        feed = MeetFeed(now=lambda: 1000.0)
        feed.apply([self.status()])
        self.assertEqual(len(feed.call('abc-defg-hij')['people']), 3)

    def test_her_arrival_alone_does_not_change_the_people(self):
        from koli.meeting.meet_feed import MeetFeed
        name = ['']
        feed = MeetFeed(now=lambda: 1000.0, guest_name=lambda: name[0])
        first = self.status()
        first['people'] = first['people'][:2]
        feed.apply([first])
        name[0] = 'Koli'
        result = feed.apply([self.status()])
        self.assertEqual(result['changed'], [])

    def test_she_stays_out_of_the_people_after_she_left(self):
        # The extension keeps listing people who left; she must not turn into an attendee then.
        from koli.meeting.meet_feed import MeetFeed
        name = ['Koli']
        feed = MeetFeed(now=lambda: 1000.0, guest_name=lambda: name[0])
        feed.apply([self.status()])
        name[0] = ''
        feed.apply([self.status(title='Weekly sync')])
        self.assertEqual([p['name'] for p in feed.call('abc-defg-hij')['people']], ['Noa Levi', 'Dana'])

    def muted_feed(self, reports, now=1000.0):
        from koli.meeting.meet_feed import MeetFeed
        self.now = now
        feed = MeetFeed(now=lambda: self.now)
        for at, muted in reports:
            more = {} if muted == 'absent' else {'muted': muted}
            feed.apply([self.status(at_ms=at * 1000, **more)])
        return feed

    def test_heard_needs_the_microphone_on_the_whole_time(self):
        feed = self.muted_feed([(100, False), (200, True), (300, False), (400, False)])
        code = 'abc-defg-hij'
        self.assertIs(feed.heard_state(code, 110, 120), True)
        self.assertIs(feed.heard_state(code, 210, 220), False)         # muted
        self.assertIs(feed.heard_state(code, 190, 205), False)         # muted before it ended
        self.assertIs(feed.heard_state(code, 310, 320), True)          # on again
        self.assertIs(feed.heard_state(code, 50, 60), False)           # before the first report: unknown
        self.assertIs(feed.heard_state('zzz-zzzz-zzz', 110, 120), False)

    def test_a_mute_counts_from_a_moment_before_it_was_noticed(self):
        # The page is read once a second: the mute may be that old when it is reported.
        feed = self.muted_feed([(100, False), (200, True), (250, True)])
        self.assertIs(feed.heard_state('abc-defg-hij', 197, 199.5), False)
        self.assertIs(feed.heard_state('abc-defg-hij', 190, 198), True)

    def test_not_decided_until_a_report_dated_after_the_line_ended(self):
        # A mute in the line's last second may not have been reported yet.
        feed = self.muted_feed([(100, False)], now=104.0)
        self.assertIsNone(feed.heard_state('abc-defg-hij', 101, 103))
        feed.apply([self.status(at_ms=105_000, muted=False)])
        self.assertIs(feed.heard_state('abc-defg-hij', 101, 103), True)

    def test_a_mute_reported_after_the_line_makes_it_unheard(self):
        feed = self.muted_feed([(100, False)], now=104.0)
        feed.apply([self.status(at_ms=104_000, muted=True)])          # muted at about 102.5, inside the line
        self.assertIs(feed.heard_state('abc-defg-hij', 101, 103), False)

    def test_with_no_report_coming_the_wait_ends_as_unheard(self):
        # The Meet tab was closed, or the extension stopped: an old "microphone on" proves nothing.
        feed = self.muted_feed([(100, False)], now=104.0)
        self.now = 130.0
        self.assertIs(feed.heard_state('abc-defg-hij', 101, 103), False)

    def test_a_status_without_the_microphone_or_out_of_the_call_is_unknown(self):
        feed = self.muted_feed([(100, False), (150, 'absent'), (200, False), (260, False)])
        self.assertIs(feed.heard_state('abc-defg-hij', 110, 120), True)
        self.assertIs(feed.heard_state('abc-defg-hij', 160, 170), False)       # the button was not there
        self.assertIs(feed.heard_state('abc-defg-hij', 140, 155), False)       # it went missing during the line
        self.assertIs(feed.heard_state('abc-defg-hij', 210, 220), True)
        left = self.status(at_ms=300_000, muted=False)
        left['joined'] = False
        feed.apply([left, self.status(at_ms=400_000, muted=False)])
        self.assertIs(feed.heard_state('abc-defg-hij', 310, 320), False)       # you were not in the call

    def test_no_mute_state_at_all_is_unheard(self):
        feed = self.muted_feed([(100, 'absent'), (130, 'absent')])
        self.assertIs(feed.heard_state('abc-defg-hij', 110, 120), False)

    def test_her_tile_is_kept(self):
        from koli.meeting.meet_feed import MeetFeed
        feed = MeetFeed(now=lambda: 1000.0, guest_name=lambda: 'Koli')
        feed.apply([self.status(guest_tile=[258.4, 305])])
        self.assertEqual(feed.call('abc-defg-hij')['guest_tile'], [258, 305])
        feed.apply([self.status()])                      # her tile left the screen
        self.assertIsNone(feed.call('abc-defg-hij')['guest_tile'])

    def test_a_tile_that_is_not_two_numbers_is_none(self):
        from koli.meeting.meet_feed import MeetFeed
        feed = MeetFeed(now=lambda: 1000.0, guest_name=lambda: 'Koli')
        for bad in ('big', [1], [0, 5], [10, 'x'], {'w': 1}):
            feed.apply([self.status(guest_tile=bad)])
            self.assertIsNone(feed.call('abc-defg-hij')['guest_tile'])


# ── Wired into the app ───────────────────────────────────────────────────────

class WiringTests(unittest.TestCase):
    def make(self, **settings):
        from koli.meeting.store import MeetingStore
        from koli.services.meetings import MeetingService
        from tests.test_meeting import FakeConfig, FakeStateManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        self.values = {'meeting.enabled': False, 'meeting.include_me': True, 'meeting.language': 'auto',
                       'meeting.detect': 'auto', 'meeting.google_meet': 'captions', 'meeting.speaker_model': 'eres2net',
                       'meeting.segmentation_model': '', 'meeting.start_speech_seconds': 3, 'meeting.countdown_seconds': 0,
                       **{f'meeting.companion.{k}': v for k, v in settings.items()}}
        ctx.config_manager = FakeConfig(self.values)
        ctx.model_registry = None
        fake_loopback = type('L', (), {'start': lambda s: None, 'stop': lambda s: None})
        self.events = []
        svc = MeetingService(ctx, store=MeetingStore(Path(tmp.name) / 'meetings'),
                             publish=lambda topic, payload: self.events.append((topic, payload)),
                             loopback_factory=lambda on_block, on_error, device=None: fake_loopback(),
                             embedder_factory=lambda: None, loopback_available=lambda: True, sessions_fn=lambda: [],
                             call_people_fn=lambda exe: {})
        self.addCleanup(svc.shutdown)
        return svc

    def in_a_call(self, svc):
        guest = FakeGuest(lambda *a: None)
        guest.state = 'in_call'
        svc.companion._guest = guest
        svc.companion._guest_state = {'state': 'in_call', 'detail': '', 'progress': None}
        return guest

    def status(self, **more):
        return {'type': 'status', 'code': 'abc-defg-hij', 'joined': True, 'captions': True, 'title': '',
                'self_name': 'Noa Levi', 'at_ms': 1000_000,
                'people': [{'id': '1', 'name': 'Noa Levi', 'self': True}, {'id': '2', 'name': 'Dana'},
                           {'id': '3', 'name': 'Koli'}], **more}

    def test_the_meeting_service_has_her(self):
        from koli.services.companion import CompanionService
        svc = self.make()
        self.assertIsInstance(svc.companion, CompanionService)
        state = svc.companion.state()
        self.assertEqual((state['mode'], state['id'], state['guest']['state']), ('off', '', 'off'))

    def test_the_extension_is_told_her_name_only_while_she_is_in_a_call(self):
        svc = self.make()
        self.assertEqual(svc._meet_status()['guest_name'], '')
        self.in_a_call(svc)
        self.assertEqual(svc._meet_status()['guest_name'], 'Koli')

    def test_she_is_left_out_of_the_calls_people(self):
        svc = self.make()
        self.in_a_call(svc)
        svc._meet_events([self.status()])
        self.assertEqual([p['name'] for p in svc.meet.call('abc-defg-hij')['people']], ['Noa Levi', 'Dana'])

    def test_a_turn_from_meets_captions_says_so(self):
        import time
        svc = self.make()
        svc._meet_events([self.status()])
        svc._on_sources([])
        self.assertEqual(svc.session.state, 'live')
        now = time.time()
        mine = {'type': 'caption', 'code': 'abc-defg-hij', 'id': 'b1', 'name': 'You', 'self': True, 'text': 'Hello all',
                'first_ms': now * 1000, 'last_ms': (now + 2) * 1000, 'final': True}
        svc._meet_events([mine])
        turns = svc.session.meeting['turns']
        self.assertEqual([(t['speaker'], t['text'], t.get('heard')) for t in turns], [('you', 'Hello all', True)])

    def test_in_the_app_a_line_of_yours_is_heard_when_meet_says_your_microphone_was_on(self):
        import time
        svc = self.make()
        self.values['meeting.google_meet'] = 'names'      # no captions as the transcript: Koli's own Whisper
        now = time.time()
        svc._meet_events([self.status(muted=False, at_ms=(now - 60) * 1000)])
        svc._on_sources([])
        self.assertEqual(svc.session.state, 'live')
        m = svc.session.meeting

        def mine(t0, text):
            svc.session.add_turn({'t0': t0, 't1': t0 + 2, 'stream': 'mic', 'speaker': 'you', 'text': text, 'lang': 'en',
                                  'meeting_id': m['id']})
        mine(now + 10, 'microphone on')
        svc._meet_events([self.status(muted=True, at_ms=(now + 20) * 1000)])
        mine(now + 30, 'muted in Meet')
        svc._meet_events([self.status(muted=False, at_ms=(now + 40) * 1000)])
        mine(now + 50, 'on again')
        svc._meet_events([self.status(muted=False, at_ms=(now + 60) * 1000)])   # the heartbeat after the last line
        rows, upto = svc.companion._public_rows(m, 0)
        self.assertEqual([r['text'] for r in rows], ['microphone on', 'on again'])
        self.assertEqual(upto, 3)
        self.assertNotIn('heard', m['turns'][0])            # nothing is written on the turn itself

    def test_her_tile_reaches_her(self):
        svc = self.make()
        guest = self.in_a_call(svc)
        svc._meet_events([self.status(guest_tile=[258, 305])])
        self.assertEqual(guest.tiles, [(258, 305)])

    def test_her_settings_reach_her(self):
        svc = self.make()
        self.values['meeting.companion.mode'] = 'insights'
        svc.apply_setting('meeting.companion.mode', 'insights')
        self.assertEqual(svc.companion.state()['mode'], 'insights')
        self.assertIn('meeting_companion', [topic for topic, _ in self.events])

    def test_shutdown_takes_her_out_of_the_call(self):
        svc = self.make()
        guest = self.in_a_call(svc)
        svc.shutdown()
        self.assertEqual(guest.left, 1)

    def test_a_live_meeting_is_followed(self):
        svc = self.make(mode='insights')
        svc.start()
        svc.companion.on_event('meeting', {'id': svc.session.meeting['id'], 'state': 'live'})
        while svc.companion.step():
            pass
        self.assertEqual(svc.companion.state()['id'], svc.session.meeting['id'])

    def test_every_setting_of_hers_has_a_default_and_a_schema_entry(self):
        from ruamel.yaml import YAML
        from koli.settings import schema
        defaults = YAML(typ='safe').load((ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8'))
        hers = defaults['meeting']['companion']
        self.assertEqual(hers, {'mode': 'off', 'update_seconds': 45, 'join_call': False, 'name': 'Koli', 'announce': True,
                                'chat': False})
        self.assertEqual(defaults['ai']['companion'], {'model': '', 'timeout': 20})
        for key in hers:
            self.assertIsNotNone(schema.get(f'meeting.companion.{key}'), key)
        self.assertEqual([c[0] for c in schema.get('meeting.companion.mode').choices], ['off', 'insights', 'active'])
        self.assertIsNotNone(schema.get('ai.companion.model'))
        self.assertIsNotNone(schema.get('ai.companion.timeout'))

    def test_the_panel_is_sent_her_events(self):
        from koli.ui import host
        self.assertIn('meeting_companion', host.PANEL_TOPICS)

    def test_the_bridge_calls_answer_on_an_idle_app(self):
        import types
        from koli.ui.bridge import Bridge
        svc = self.make()
        stub = types.SimpleNamespace(_companion=svc.companion)
        self.assertIs(Bridge.companion_state(stub)['ok'], True)
        self.assertEqual(Bridge.companion_dismiss(stub, 'c1'), {'ok': True, 'data': None})
        self.assertEqual(Bridge.companion_leave(stub), {'ok': True, 'data': None})
        for name in ('companion_now', 'companion_answer', 'companion_catch_up', 'companion_task', 'companion_join'):
            self.assertEqual(getattr(Bridge, name)(stub), {'ok': False, 'error': 'No meeting is running'}, name)
        for name, argument in (('companion_task_take', 0), ('companion_task_remove', 'x'), ('companion_say', 'x')):
            self.assertIs(getattr(Bridge, name)(stub, argument)['ok'], False, name)


if __name__ == '__main__':
    unittest.main()
