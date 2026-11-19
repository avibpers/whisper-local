# tests/test_guest.py
# Koli as a guest in a Google Meet call (src/koli/meeting/guest.py), against a
# page that answers each probe from a script: the join in the order that was
# measured, refusals and retries, "Ask to join", the dialog after Join, the
# chat, the board, and leaving.

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from koli.meeting import guest as guest_module  # noqa: E402
from koli.meeting.guest import MeetGuest, frame_size  # noqa: E402

NAME_BOX = {'x': 758, 'y': 244}
JOIN = {'x': 836, 'y': 401, 'text': 'Join now'}
ASK = {'x': 836, 'y': 401, 'text': 'Ask to join'}
IN_CALL = {'in_call': True, 'refused': False, 'asking': False, 'denied': False}
WAITING = {'in_call': False, 'refused': False, 'asking': False, 'denied': False}
REFUSED = {'in_call': False, 'refused': True, 'asking': False, 'denied': False}
ASKING = {'in_call': False, 'refused': False, 'asking': True, 'denied': False}
DENIED = {'in_call': False, 'refused': False, 'asking': False, 'denied': True}


def probe_of(expression: str) -> str:
    found = re.search(r'/\*probe:([\w:]+)\*/', expression)
    return found.group(1) if found else ''


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakePage:
    """Answers each probe from `script`: a list is used one item per call
    (the last one repeats), anything else is the answer every time."""

    def __init__(self, script, log):
        self.script = dict(script)
        self.log = log
        self.expressions = []
        self.closed = False

    def evaluate(self, expression, timeout=30.0):
        name = probe_of(expression)
        self.expressions.append(expression)
        self.log.append(('eval', name))
        if self.closed:
            raise RuntimeError('the browser is closed')
        answer = self.script.get(name)
        if callable(answer):
            answer = answer()
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else (answer[0] if answer else None)
        return answer

    def send(self, method, params=None, timeout=30.0):
        if self.closed:
            raise RuntimeError('the browser is closed')
        self.log.append(('send', method, dict(params or {})))
        return {}

    def navigate(self, url):
        self.log.append(('navigate', url))

    def add_script(self, source):
        self.log.append(('script', source))


class FakeSession:
    def __init__(self, script, log):
        self.page = FakePage(script, log)
        self.log = log

    def open(self):
        self.log.append(('open',))

    def hide(self):
        self.log.append(('hide',))

    def close(self):
        self.page.closed = True
        self.log.append(('close',))


class FakeInstaller:
    def __init__(self, installed=True, supported=True):
        self.installed = installed
        self.is_supported = supported
        self.ensured = 0

    def supported(self):
        return self.is_supported

    def executable(self):
        return Path('C:/koli/browser/chrome.exe') if self.installed else None

    def ensure(self, reporter=None):
        self.ensured += 1
        reporter.report(0.5, "Downloading Koli's browser: 100 of 215 MB")
        reporter.check_cancelled()
        self.installed = True
        return Path('C:/koli/browser/chrome.exe')


HAPPY = {'state': [WAITING, WAITING, IN_CALL], 'name_box': NAME_BOX, 'button:got_it': None, 'button:join': JOIN,
         'button:dialog_join': None, 'button:chat': {'x': 996, 'y': 621, 'text': ''}, 'chat_box': True,
         'sending': [[]], 'board': 1, 'resize': '960x720', 'name_value': 'Koli'}


class GuestCase(unittest.TestCase):
    def make(self, script=None, installer=None, announce='', sessions=None):
        self.log = []
        self.states = []
        self.clock = FakeClock()
        self.sessions = []
        scripts = list(sessions) if sessions else None

        def session_factory():
            script_now = scripts.pop(0) if scripts else dict(HAPPY if script is None else script)
            session = FakeSession({k: (list(v) if isinstance(v, list) else v) for k, v in script_now.items()}, self.log)
            self.sessions.append(session)
            return session
        self.guest = MeetGuest(on_state=lambda state, detail='', progress=None: self.states.append((state, detail, progress)),
                               name='Koli', session_factory=session_factory, installer=installer or FakeInstaller(),
                               sleep=self.clock.sleep, now=self.clock.now, announce=announce)
        return self.guest

    def state_names(self):
        return [s[0] for s in self.states]

    def typed(self):
        return ''.join(e[2]['text'] for e in self.log
                       if e[0] == 'send' and e[1] == 'Input.dispatchKeyEvent' and e[2].get('type') == 'keyDown' and e[2].get('text'))

    def presses(self):
        return [(e[2]['x'], e[2]['y']) for e in self.log
                if e[0] == 'send' and e[1] == 'Input.dispatchMouseEvent' and e[2]['type'] == 'mousePressed']

    def kinds(self):
        return [e[0] if e[0] != 'eval' else f'eval:{e[1]}' for e in self.log]

    # Times Meet's shortcut for the hand was pressed: Ctrl+Alt+H.
    def hand_keys(self):
        return len([e for e in self.log if e[0] == 'send' and e[1] == 'Input.dispatchKeyEvent'
                    and e[2].get('type') == 'rawKeyDown' and e[2].get('code') == 'KeyH' and e[2].get('modifiers') == 3])


# ── The join ─────────────────────────────────────────────────────────────────

class JoinTests(GuestCase):
    def test_the_join_goes_in_the_order_that_was_measured(self):
        guest = self.make()
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'in_call')
        self.assertTrue(guest.in_call)
        self.assertEqual(self.state_names()[:2], ['starting', 'joining'])
        self.assertEqual(self.state_names()[-1], 'in_call')
        kinds = self.kinds()
        order = [kinds.index('open'), kinds.index('script'), kinds.index('navigate'), kinds.index('eval:name_box'),
                 kinds.index('eval:button:join'), kinds.index('hide')]
        self.assertEqual(order, sorted(order))
        self.assertIn(('navigate', 'https://meet.google.com/abc-defg-hij?hl=en'), self.log)
        self.assertEqual(self.typed(), 'Koli')
        self.assertEqual(self.presses()[:2], [(758, 244), (836, 401)])

    def test_continue_as_guest_is_pressed_when_meet_offers_a_sign_in_first(self):
        # Seen on 2026-11-18: instead of the name box, "Continue as guest" and
        # "Sign in with Google". The name box comes after the first is pressed.
        script = {**HAPPY, 'name_box': [None, None, NAME_BOX], 'button:guest': [{'x': 836, 'y': 226, 'text': 'Continue as guest'}, None]}
        guest = self.make(script)
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(self.presses()[:3], [(836, 226), (758, 244), (836, 401)])
        self.assertEqual(self.typed(), 'Koli')

    def test_the_name_box_is_found_again_once_the_page_settled(self):
        # Measured: the box moves while Meet's page finishes loading.
        guest = self.make({**HAPPY, 'name_box': [{'x': 742, 'y': 446}, {'x': 742, 'y': 186}]})
        guest.join_now('abc-defg-hij', 'Noa')
        self.assertEqual(self.presses()[0], (742, 186))

    def test_a_name_that_did_not_land_is_typed_again(self):
        guest = self.make({**HAPPY, 'name_value': ['', 'Koli']})
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(self.typed(), 'KoliKoli')
        self.assertEqual(self.presses()[:3], [(758, 244), (758, 244), (836, 401)])

    def test_a_name_that_never_lands_stops_the_join(self):
        guest = self.make({**HAPPY, 'name_value': ''})
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'failed')
        self.assertIn('type her name', self.states[-1][1])
        self.assertNotIn((836, 401), self.presses())          # Join is never pressed with no name

    def test_continue_as_guest_is_pressed_once(self):
        script = {**HAPPY, 'name_box': [None, None, None, NAME_BOX], 'button:guest': {'x': 836, 'y': 226, 'text': 'Continue as guest'}}
        guest = self.make(script)
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(self.presses().count((836, 226)), 1)

    def test_the_board_script_carries_the_first_view(self):
        guest = self.make()
        guest.show({'title': 'Koli', 'subtitle': 'AI notes · with Noa', 'topic': 'Pricing', 'decisions': [], 'tasks': [],
                    'questions': [], 'say': ''})
        guest.join_now('abc-defg-hij', 'Noa')
        source = next(e[1] for e in self.log if e[0] == 'script')
        self.assertIn('AI notes · with Noa', source)
        self.assertIn('getUserMedia', source)
        self.assertNotIn('/*CONFIG*/{ width', source)   # the placeholder was filled in

    def test_the_name_is_never_pasted(self):
        guest = self.make()
        guest.join_now('abc-defg-hij', 'Noa')
        hide = self.kinds().index('hide')
        before_join = [e for e in self.log[:self.kinds().index('eval:button:join')] if e[0] == 'send']
        self.assertNotIn('Input.insertText', [e[1] for e in before_join])
        self.assertGreater(hide, 0)

    def test_the_pauses_before_join_are_the_measured_ones(self):
        guest = self.make()
        guest.join_now('abc-defg-hij', 'Noa')
        for pause in (guest_module.SETTLE_S, guest_module.AFTER_CLICK_S, guest_module.AFTER_TYPING_S):
            self.assertIn(pause, self.clock.sleeps)
        self.assertEqual((guest_module.SETTLE_S, guest_module.AFTER_CLICK_S, guest_module.AFTER_TYPING_S), (3.0, 1.0, 2.0))

    def test_a_sign_in_hint_over_the_name_box_is_dismissed_first(self):
        guest = self.make({**HAPPY, 'button:got_it': [{'x': 1012, 'y': 195, 'text': 'Got it'}, None]})
        guest.join_now('abc-defg-hij', 'Noa')
        self.assertEqual(self.presses()[:3], [(1012, 195), (758, 244), (836, 401)])

    def test_a_dialog_after_join_is_answered(self):
        script = {**HAPPY, 'state': [WAITING, IN_CALL], 'button:dialog_join': [{'x': 636, 'y': 374, 'text': 'Join now'}, None]}
        guest = self.make(script)
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertIn((636, 374), self.presses())

    def test_being_in_the_call_behind_a_dialog_does_not_count(self):
        # Measured: with "Gemini is taking notes" up, the call's buttons are
        # there but nobody sees Koli until Join now is pressed.
        script = {**HAPPY, 'state': [WAITING, IN_CALL], 'button:dialog_join': [{'x': 636, 'y': 374, 'text': 'Join now'}, None]}
        guest = self.make(script)
        guest.join_now('abc-defg-hij', 'Noa')
        kinds = self.kinds()
        self.assertLess(kinds.index('eval:button:dialog_join'), kinds.index('hide'))
        self.assertIn((636, 374), self.presses())

    def test_ask_to_join_waits_to_be_admitted(self):
        script = {**HAPPY, 'state': [WAITING, WAITING, ASKING, ASKING, IN_CALL], 'button:join': ASK}
        guest = self.make(script)
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        names = self.state_names()
        self.assertIn('asking', names)
        self.assertLess(names.index('asking'), names.index('in_call'))
        asking = next(s for s in self.states if s[0] == 'asking')
        self.assertIn('Admit her', asking[1])

    def test_nobody_admits_her(self):
        script = {**HAPPY, 'state': [WAITING, WAITING, ASKING], 'button:join': ASK}
        guest = self.make(script)
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'failed')
        self.assertIn("wasn't let in", self.states[-1][1])
        self.assertEqual(self.kinds().count('close'), 1)      # asking is not retried: someone would be asked three times

    def test_being_turned_away_is_not_retried(self):
        script = {**HAPPY, 'state': [WAITING, WAITING, ASKING, DENIED], 'button:join': ASK}
        guest = self.make(script)
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'failed')
        self.assertEqual(self.kinds().count('close'), 1)


# ── Refusals and failures ────────────────────────────────────────────────────

class RefusalTests(GuestCase):
    def test_a_refusal_at_load_is_retried_then_given_up(self):
        guest = self.make({**HAPPY, 'state': REFUSED, 'name_box': None})
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'failed')
        self.assertIn('Meet refused the join', self.states[-1][1])
        self.assertEqual(self.state_names().count('refused'), guest_module.RETRIES - 1)
        self.assertEqual(self.kinds().count('open'), guest_module.RETRIES)
        self.assertEqual(self.kinds().count('close'), guest_module.RETRIES)
        self.assertEqual(self.clock.sleeps.count(guest_module.RETRY_WAIT_S), guest_module.RETRIES - 1)

    def test_a_refusal_after_join_then_a_good_second_try(self):
        refused_after = {**HAPPY, 'state': [WAITING, WAITING, REFUSED]}
        guest = self.make(sessions=[refused_after, HAPPY])
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        names = self.state_names()
        self.assertIn('refused', names)
        self.assertEqual(names[-1], 'in_call')
        self.assertEqual(self.kinds().count('open'), 2)

    def test_no_name_box_says_what_was_looked_for(self):
        guest = self.make({**HAPPY, 'state': WAITING, 'name_box': None})
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'failed')
        self.assertIn('name box', self.states[-1][1])
        self.assertEqual(self.kinds().count('open'), 1)       # Meet's page changed: trying again changes nothing
        self.assertEqual(self.kinds().count('close'), 1)

    def test_no_join_button_says_so(self):
        guest = self.make({**HAPPY, 'button:join': None})
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertIn('Join button', self.states[-1][1])

    def test_a_browser_that_breaks_is_a_failure_not_a_crash(self):
        guest = self.make()

        def broken():
            raise RuntimeError('the browser did not start')
        guest._session_factory = broken
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'failed')
        self.assertIn('the browser did not start', self.states[-1][1])

    def test_a_browser_that_does_not_answer_is_started_again(self):
        # Seen once in ten starts on 2026-11-18: a call to the fresh browser got no answer.
        from koli.browser.cdp import CdpError
        guest = self.make()
        factory = guest._session_factory
        opened = []

        def flaky():
            session = factory()
            if not opened:
                def no_answer():
                    self.log.append(('open',))
                    raise CdpError('Browser.grantPermissions: no answer in 10 s')
                session.open = no_answer
            opened.append(session)
            return session
        guest._session_factory = flaky
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(self.kinds().count('open'), 2)
        self.assertEqual(self.kinds().count('close'), 1)      # the one that did not answer was closed
        self.assertIn('did not answer', ' '.join(s[1] for s in self.states))

    def test_a_browser_that_never_answers_gives_up(self):
        from koli.browser.cdp import CdpError
        guest = self.make()
        factory = guest._session_factory

        def dead():
            session = factory()

            def no_answer():
                raise CdpError('Browser.getVersion: no answer in 10 s')
            session.open = no_answer
            return session
        guest._session_factory = dead
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'failed')
        self.assertEqual(self.kinds().count('close'), guest_module.RETRIES)

    def test_taking_her_out_in_the_middle_of_the_join_ends_off_not_failed(self):
        # The closed browser makes the next step fail; that is the leave, not a failure.
        guest = self.make()

        def join_button():
            guest.leave()
            return JOIN
        guest = self.make({**HAPPY, 'button:join': join_button})
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'off')
        self.assertNotIn('failed', self.state_names())
        self.assertNotIn('in_call', self.state_names())

    def test_a_bad_code_never_opens_the_browser(self):
        guest = self.make()
        self.assertFalse(guest.join_now('not a code; rm -rf', 'Noa'))
        self.assertEqual(guest.state, 'failed')
        self.assertNotIn('open', self.kinds())


# ── Getting the browser ──────────────────────────────────────────────────────

class InstallTests(GuestCase):
    def test_not_on_this_system(self):
        guest = self.make(installer=FakeInstaller(supported=False))
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'unsupported')
        self.assertNotIn('open', self.kinds())

    def test_the_browser_is_fetched_first_with_progress(self):
        installer = FakeInstaller(installed=False)
        guest = self.make(installer=installer)
        self.assertTrue(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(installer.ensured, 1)
        installing = [s for s in self.states if s[0] == 'installing']
        self.assertTrue(installing)
        self.assertEqual(installing[-1][2], 0.5)
        self.assertLess(self.state_names().index('installing'), self.state_names().index('starting'))

    def test_leaving_while_the_browser_downloads_stops_it(self):
        installer = FakeInstaller(installed=False)
        guest = self.make(installer=installer)
        original = installer.ensure

        def ensure(reporter=None):
            guest.leave()
            return original(reporter)
        installer.ensure = ensure
        self.assertFalse(guest.join_now('abc-defg-hij', 'Noa'))
        self.assertEqual(guest.state, 'off')
        self.assertNotIn('open', self.kinds())


# ── In the call ──────────────────────────────────────────────────────────────

class InCallTests(GuestCase):
    def joined(self, script=None, announce=''):
        guest = self.make(script, announce=announce)
        guest.join_now('abc-defg-hij', 'Noa')
        self.log.clear()
        return guest

    def test_the_chat_is_opened_before_the_page_is_hidden(self):
        guest = self.make()
        guest.join_now('abc-defg-hij', 'Noa')
        kinds = self.kinds()
        self.assertLess(kinds.index('eval:button:chat'), kinds.index('hide'))

    def test_she_mutes_her_own_microphone_once_she_is_in(self):
        # Her microphone sends silence; muted, Meet shows it and nobody wonders.
        guest = self.make({**HAPPY, 'button:mic': {'x': 310, 'y': 621, 'muted': False}})
        guest.join_now('abc-defg-hij', 'Noa')
        self.assertIn((310, 621), self.presses())
        kinds = self.kinds()
        self.assertLess(kinds.index('eval:button:mic'), kinds.index('hide'))
        self.assertLess(kinds.index('eval:button:join'), kinds.index('eval:button:mic'))   # after the join, not before

    def test_a_microphone_already_muted_is_left_alone(self):
        guest = self.make({**HAPPY, 'button:mic': {'x': 310, 'y': 621, 'muted': True}})
        guest.join_now('abc-defg-hij', 'Noa')
        self.assertNotIn((310, 621), self.presses())

    def test_she_says_who_she_is_once_when_asked_to(self):
        guest = self.make(announce="Hi, I'm Koli, Noa's AI assistant.")
        guest.join_now('abc-defg-hij', 'Noa')
        inserted = [e[2]['text'] for e in self.log if e[0] == 'send' and e[1] == 'Input.insertText']
        self.assertEqual(inserted, ["Hi, I'm Koli, Noa's AI assistant."])
        self.assertLess(self.kinds().index('eval:chat_box'), self.kinds().index('hide'))

    def test_no_announcement_no_chat_message(self):
        guest = self.make()
        guest.join_now('abc-defg-hij', 'Noa')
        self.assertEqual([e for e in self.log if e[0] == 'send' and e[1] == 'Input.insertText'], [])

    def test_say_writes_one_chat_message_cut_to_the_limit(self):
        guest = self.joined()
        self.assertTrue(guest.say('x' * 900))
        inserted = [e[2]['text'] for e in self.log if e[0] == 'send' and e[1] == 'Input.insertText']
        self.assertEqual(len(inserted), 1)
        self.assertEqual(len(inserted[0]), 400)
        keys = [e[2]['key'] for e in self.log if e[0] == 'send' and e[1] == 'Input.dispatchKeyEvent' and e[2]['type'] == 'keyDown']
        self.assertEqual(keys, ['Enter'])

    def test_say_does_nothing_outside_a_call(self):
        guest = self.make()
        self.assertFalse(guest.say('hello'))
        self.assertEqual(self.log, [])

    def test_say_without_a_chat_box_says_no(self):
        guest = self.joined({**HAPPY, 'chat_box': [True, False]})
        self.assertFalse(guest.say('hello'))

    def test_the_board_gets_the_view_as_json(self):
        guest = self.joined()
        view = {'title': 'Koli', 'subtitle': 'AI notes · with Noa', 'topic': 'תמחור', 'decisions': ['a'], 'tasks': [],
                'questions': [], 'say': ''}
        guest.show(view)
        board = [x for x in self.sessions[-1].page.expressions if probe_of(x) == 'board'][-1]
        sent = json.loads(board[board.index('('):].strip()[1:-1])
        self.assertEqual(sent['topic'], 'תמחור')
        self.assertEqual(sent['decisions'], ['a'])
        self.assertIs(sent['dense'], False)

    def test_while_she_says_something_her_board_is_redrawn_until_it_rests(self):
        # A hidden page draws once a second by itself; the app drives it ten times a second (measured).
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'frame': [True, True, False]})
        self.assertFalse(guest.moving)
        guest.show({'title': 'Koli', 'subtitle': '', 'topic': 'x', 'decisions': [], 'tasks': [], 'questions': [],
                    'say': 'In 1948.', 'say_kind': 'answer', 'say_to': 'Noa'})
        self.assertTrue(guest.moving)
        self.assertTrue(guest.animate())
        self.assertTrue(guest.animate())
        self.assertFalse(guest.animate())                       # the board says it has come to rest
        self.assertFalse(guest.moving)
        self.assertEqual(self.kinds().count('eval:frame'), 3)

    def test_her_notes_alone_need_no_extra_frames(self):
        guest = self.joined()
        guest.show({'title': 'Koli', 'subtitle': '', 'topic': 'x', 'decisions': [], 'tasks': [], 'questions': [], 'say': ''})
        self.assertFalse(guest.moving)

    def test_a_page_that_fails_a_frame_stops_the_redrawing(self):
        guest = self.joined()
        guest.show({'title': 'Koli', 'say': 'In 1948.'})

        def broken(expression, timeout=30.0):
            raise RuntimeError('the browser is closed')
        self.sessions[-1].page.evaluate = broken
        self.assertFalse(guest.animate())
        self.assertFalse(guest.moving)

    def test_a_missed_frame_is_made_up_at_the_next_look_at_the_call(self):
        # Chrome stops a page for some seconds once, soon after it is hidden (measured): a frame
        # that was not answered must not leave the rest of her words to the page's slow timer.
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'frame': [True]})
        guest.show({'title': 'Koli', 'say': 'In 1948.'})
        page = self.sessions[-1].page
        answering = page.evaluate

        def stalled(expression, timeout=30.0):
            raise TimeoutError('no answer')
        page.evaluate = stalled
        self.assertFalse(guest.animate())
        page.evaluate = answering
        guest.tick()
        self.assertTrue(guest.moving)
        self.assertTrue(guest.animate())

    def test_a_look_at_the_call_starts_no_frames_for_her_notes(self):
        guest = self.joined()
        guest.show({'title': 'Koli', 'say': ''})
        guest.tick()
        self.assertFalse(guest.moving)

    # ── Her hand ─────────────────────────────────────────────────────────────
    # Meet's own way to make people look at someone: the hand on her tile, a
    # sound and a line for everyone. Measured in a real call (2026-11-19): a
    # guest on a hidden page has the button and the page says "Your hand is
    # raised"; the button itself looks the same. She uses Meet's shortcut,
    # Ctrl+Alt+H: a pointer crossing a hidden page took over a minute (its
    # moves wait for the page's frames, about one a second), a key is at once.

    HAND_AT = {'x': 678, 'y': 565, 'text': ''}

    def test_she_raises_her_hand_and_lowers_it_after_a_while(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'button:hand': self.HAND_AT})
        self.assertFalse(guest.hand_up)
        guest.hand(True)
        guest.keep_hand()
        self.assertEqual(self.hand_keys(), 1)
        self.assertEqual(self.presses(), [])                    # no pointer on a hidden page
        self.assertTrue(guest.hand_up)
        guest.keep_hand()
        self.assertEqual(self.hand_keys(), 1)                   # once is enough
        self.clock.t += 21                                      # a raised hand is for a moment, not for the meeting
        guest.keep_hand()
        self.assertEqual(self.hand_keys(), 2)
        self.assertFalse(guest.hand_up)

    def test_her_hand_goes_down_when_her_words_go(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'button:hand': self.HAND_AT})
        guest.hand(True)
        guest.keep_hand()
        guest.hand(False)
        guest.keep_hand()
        self.assertEqual(self.hand_keys(), 2)
        self.assertFalse(guest.hand_up)

    def test_a_hand_that_was_never_raised_is_not_lowered(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'button:hand': self.HAND_AT})
        guest.hand(False)
        guest.keep_hand()
        self.assertEqual(self.hand_keys(), 0)

    def test_a_call_without_the_button_is_left_alone(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'button:hand': None})
        guest.hand(True)
        guest.keep_hand()
        guest.keep_hand()
        self.assertEqual(self.hand_keys(), 0)
        self.assertEqual(self.kinds().count('eval:button:hand'), 1)     # not looked for at every turn of the loop

    def test_a_press_that_did_not_take_is_made_again(self):
        # The button toggles and looks the same either way: the page's own words say which it is.
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'button:hand': self.HAND_AT, 'hand_up': [False, True]})
        guest.hand(True)
        guest.keep_hand()
        guest.tick()                                            # the page: no hand is raised
        self.assertFalse(guest.hand_up)
        guest.keep_hand()
        self.assertEqual(self.hand_keys(), 2)
        guest.tick()                                            # now it is
        self.assertTrue(guest.hand_up)
        guest.keep_hand()
        self.assertEqual(self.hand_keys(), 2)

    def test_leaving_forgets_the_hand(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'button:hand': self.HAND_AT})
        guest.hand(True)
        guest.keep_hand()
        guest.leave()
        self.assertFalse(guest.hand_up)

    def test_she_knows_whether_meet_asks_for_her_picture(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'sending': [[{'active': True, 'width': 480, 'height': 270},
                                                                  {'active': False, 'width': 0, 'height': 0}], []]})
        self.assertIsNone(guest.watched())                      # not looked yet
        guest.tick()
        self.assertEqual(guest.watched(), 270)
        guest.tick()
        self.assertEqual(guest.watched(), 0)                    # Meet asks for no picture of her

    def test_the_same_view_is_not_sent_twice(self):
        guest = self.joined()
        view = {'title': 'Koli', 'subtitle': '', 'topic': 'x', 'decisions': [], 'tasks': [], 'questions': [], 'say': ''}
        guest.show(view)
        guest.show(dict(view))
        guest.tick()
        self.assertEqual(self.kinds().count('eval:board'), 1)

    def test_a_small_tile_makes_the_board_dense(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'sending': [[{'active': True, 'width': 480, 'height': 270}]]})
        guest.show({'title': 'Koli', 'subtitle': '', 'topic': 'x', 'decisions': [], 'tasks': [], 'questions': [], 'say': ''})
        guest.tick()
        boards = [x for x in self.sessions[-1].page.expressions if probe_of(x) == 'board']
        self.assertIn('"dense": true', boards[-1])

    def test_a_large_tile_keeps_the_full_board(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL, 'sending': [[{'active': True, 'width': 480, 'height': 270},
                                                                    {'active': True, 'width': 960, 'height': 540}]]})
        guest.show({'title': 'Koli', 'subtitle': '', 'topic': 'x', 'decisions': [], 'tasks': [], 'questions': [], 'say': ''})
        guest.tick()
        boards = [x for x in self.sessions[-1].page.expressions if probe_of(x) == 'board']
        self.assertNotIn('"dense": true', boards[-1])

    def test_the_frame_takes_the_shape_of_your_tile(self):
        guest = self.joined({**HAPPY, 'state': IN_CALL})
        guest.set_tile(258, 305)
        guest.tick()
        resize = [x for x in self.sessions[-1].page.expressions if probe_of(x) == 'resize'][-1]
        self.assertIn('resize(608, 720)', resize)
        guest.set_tile(259, 306)             # the same shape: nothing more is sent
        guest.tick()
        self.assertEqual(self.kinds().count('eval:resize'), 1)

    def test_being_out_of_the_call_is_noticed(self):
        guest = self.joined({**HAPPY, 'state': [WAITING, WAITING, IN_CALL, WAITING]})
        guest.tick()
        self.assertEqual(guest.state, 'off')
        self.assertIn('no longer in the call', self.states[-1][1])
        self.assertIn('close', self.kinds())

    def test_leave_closes_the_browser_and_says_off(self):
        guest = self.joined()
        guest.leave()
        self.assertEqual(guest.state, 'off')
        self.assertIn('close', self.kinds())
        self.assertFalse(guest.in_call)

    def test_leave_twice_is_quiet(self):
        guest = self.joined()
        guest.leave()
        self.log.clear()
        guest.leave()
        self.assertEqual(self.log, [])

    def test_a_page_that_stops_answering_ends_the_visit(self):
        guest = self.joined()

        def broken(expression, timeout=30.0):
            raise RuntimeError('the browser is closed')
        self.sessions[-1].page.evaluate = broken
        guest.tick()
        self.assertEqual(guest.state, 'off')


# ── The frame ────────────────────────────────────────────────────────────────

class FrameTests(unittest.TestCase):
    def test_a_tall_tile(self):
        self.assertEqual(frame_size(258, 305), (608, 720))

    def test_a_wide_tile(self):
        self.assertEqual(frame_size(1280, 720), (1280, 720))

    def test_no_tile_known(self):
        self.assertEqual(frame_size(0, 0), (960, 720))
        self.assertEqual(frame_size(None, None), (960, 720))

    def test_the_shape_is_held_between_three_to_four_and_sixteen_to_nine(self):
        self.assertEqual(frame_size(100, 900), (544, 720))      # 0.75 at most tall
        self.assertEqual(frame_size(3000, 300), (1280, 720))    # 16:9 at most wide


if __name__ == '__main__':
    unittest.main()
