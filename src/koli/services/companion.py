# services/companion.py
# Koli in the meeting, running: follows the live meeting's new lines, keeps
# the notes up to date with the brain (meeting/companion.py) on one worker
# thread, makes the cards the panel shows (an answer, a catch-up, a question
# someone asked), keeps the meeting's tasks, and owns the guest that sits in
# a Google Meet call (meeting/guest.py). The panel hears 'meeting_companion'.
# Design: docs/design/meet-participant.md.

import collections
import logging
import queue
import threading
import time
from typing import Callable, Optional

from ..meeting import companion as brain
from ..meeting.companion import CompanionError
from ..meeting.store import started_ts
from ..meeting.summarize import meeting_language
from ..usage.ledger import for_meeting

logger = logging.getLogger(__name__)

TOPIC = 'meeting_companion'
MAX_CARDS = 6
ANSWER_WINDOW_S = 180      # the talk an answer is drawn from
TASK_WINDOW_S = 90         # the talk a new task is found in
SAY_GAP_S = 2.0            # the least time between two answers of hers
SAY_SHOWN_S = 60           # how long what she said stays on her board
IDLE_S = 1.0               # how often the worker listens for a pause in what is being said to her
READ_EVERY = 5             # every so many idle looks, whether the notes are due
PAUSE_S = 1.2              # a caption unchanged this long: the speaker paused, she may answer
STREAM_EVERY_S = 0.4       # her tile is redrawn this often while her words come in
TALK_KEEP = 12             # lines of her talk with the call that the model sees again
THINKING = '…'             # on her tile from the moment she starts on an answer
NO_MODEL = "Choose an AI model in Settings › AI first"
NO_MEETING = "No meeting is running"
GUEST_BUSY = ('installing', 'starting', 'joining', 'asking', 'in_call', 'refused')


class CompanionService:
    def __init__(self, meetings, setting: Callable, publish: Callable, complete: Callable, parse: Optional[Callable],
                 ai_ready: Callable[[], bool], subscribe: Optional[Callable] = None,
                 guest_factory: Optional[Callable] = None, copy: Optional[Callable[[str], None]] = None,
                 now: Callable[[], float] = time.time, start_worker: bool = True):
        self._meetings = meetings          # the meeting service: its session and its Meet feed
        self._setting = setting
        self._publish = publish
        self._complete = complete
        self._parse = parse
        self._ai_ready = ai_ready
        self._guest_factory = guest_factory or self._make_guest
        self._copy = copy or _copy_text
        self._now = now
        self._lock = threading.RLock()
        self._jobs = queue.Queue()
        self._read_queued = False
        self._closed = False
        self._busy = 0                     # buttons pressed and not yet answered
        self._guest = None
        self._guest_code = ''              # the Meet call she sits in
        self._guest_state = {'state': 'off', 'detail': '', 'progress': None}
        self._auto_tried = ''              # the meeting she already joined (or was taken out of) by herself
        self._card_seq = 0
        self._forget('')
        self._unsubscribe = []             # how to stop hearing the meeting's events
        if subscribe is not None:
            self._unsubscribe = [subscribe('meeting', self.on_event), subscribe('meeting_turn', self.on_event)]
        if start_worker:
            threading.Thread(target=self._loop, daemon=True, name='meeting-companion').start()

    # Everything known about one meeting; a new meeting starts clean.
    def _forget(self, meeting_id: str):
        self._id = meeting_id
        # Counts every fresh start of the notes: an answer of the model that
        # was asked before one is thrown away.
        self._generation = getattr(self, '_generation', 0) + 1
        self._notes = brain.empty_notes()
        self._cards = []
        self._read = 0                     # turns the notes already hold
        self._heard = 0                    # turns already checked for her name
        self._updated_at = float('-inf')
        self._asked = set()                # questions already shown as a card
        self._say = ''                     # what she last said to the call, while it stays on her board
        self._say_until = 0.0
        self._said_at = float('-inf')
        self._talk = collections.deque(maxlen=TALK_KEEP)   # her talk with the call: [{'who', 'text'}], her answers too
        self._answered = {}                # caption block id -> its words she already took in
        # She is (or was) in this meeting's call: from then on her notes are made
        # only of what the call heard, since they are on her tile for everyone.
        self._public = False

    # ── Settings ─────────────────────────────────────────────────────────────

    def _mode(self) -> str:
        mode = str(self._setting('meeting.companion.mode', 'off') or 'off')
        return mode if mode in brain.MODES else 'off'

    def _name(self) -> str:
        return str(self._setting('meeting.companion.name', 'Koli') or 'Koli').strip()[:40] or 'Koli'

    # Whether the call heard a turn of yours: your microphone was on in Meet
    # for all of it (the extension reports the button; the Meet feed keeps
    # it). True, False, or None while that can't be known yet. Not a Meet
    # call: nobody heard it there.
    def _heard_fn(self, m: dict) -> Callable:
        code = self._code(m)
        if not code:
            return lambda turn: False
        base, feed = started_ts(m), self._meetings.meet
        return lambda turn: feed.heard_state(code, base + turn.get('start', 0), base + turn.get('end', 0))

    # The lines from turn `first` on that the call heard, and the turn to go
    # on from next time. A line of yours that can't be told yet stops the
    # reading there: it and what follows are read once it is known.
    def _public_rows(self, m: dict, first: int) -> tuple:
        heard = []
        for row in brain.lines(m, first=first, heard=self._heard_fn(m)):
            if row['heard'] is None:
                return heard, row['index']
            if row['heard']:
                heard.append(row)
        return heard, len(m.get('turns', []))

    # A setting of hers changed: the panel shows it, and a mode switched on
    # in the middle of a meeting reads what was said so far.
    def apply_setting(self, path: str, value):
        if path.startswith('meeting.companion.'):
            if path == 'meeting.companion.join_call' and value:
                self._jobs.put(('auto_join',))
            self._queue_read()
            self._publish_state()

    # ── What comes in ────────────────────────────────────────────────────────

    # The meeting's events. A turn is published under the session's lock, so
    # nothing is done here but leaving a note for the worker.
    def on_event(self, topic: str, payload):
        if self._closed:
            return
        payload = payload if isinstance(payload, dict) else {}
        if topic == 'meeting':
            self._jobs.put(('meeting', payload.get('state'), payload.get('id') or ''))
        elif topic == 'meeting_turn' and 'pending' not in payload:
            self._queue_read()

    def _queue_read(self):
        with self._lock:
            if self._read_queued:
                return
            self._read_queued = True
        self._jobs.put(('read',))

    # ── The worker ───────────────────────────────────────────────────────────

    def _loop(self):
        idle = 0
        while not self._closed:
            try:
                self._run(self._jobs.get(timeout=IDLE_S))
            except queue.Empty:
                idle += 1
                if idle % READ_EVERY == 0:
                    self._run(('read',))
            self._run(('listen',))

    # Runs one waiting job; False when there was none. For tests.
    def step(self) -> bool:
        try:
            job = self._jobs.get_nowait()
        except queue.Empty:
            return False
        self._run(job)
        return True

    def _run(self, job: tuple):
        kind = job[0]
        user = kind in ('now', 'answer', 'catch_up', 'task')
        try:
            if kind == 'meeting':
                self._meeting_changed(job[1], job[2])
            elif kind == 'auto_join':
                self._auto_join()
            elif kind == 'listen':
                self.listen()
            elif kind == 'read':
                with self._lock:
                    self._read_queued = False
                self._read_new()
            elif user:
                self._button(kind, *job[1:])
        except CompanionError as e:
            self._error(str(e))
        except Exception as e:
            logger.warning(f"Koli in the meeting: {kind} failed: {e}", exc_info=True)
            self._error(f"Koli could not do that: {e}")
        finally:
            if user:
                with self._lock:
                    self._busy = max(0, self._busy - 1)
                self._publish_state()

    # The live meeting, when it is the one being followed.
    def _meeting(self) -> Optional[dict]:
        m = getattr(self._meetings.session, 'meeting', None)
        return m if m and m.get('id') == self._id else None

    def _live(self) -> dict:
        m = self._meeting()
        if not m or self._meetings.session.state not in ('live', 'paused'):
            raise ValueError(NO_MEETING)
        return m

    @staticmethod
    def _elapsed(m: dict) -> float:
        return max((t.get('end', 0) for t in m.get('turns', [])), default=0.0)

    def _meeting_changed(self, state, meeting_id: str):
        if state in ('live', 'paused') and meeting_id:
            if meeting_id != self._id:
                # Continue previous and Start new instead change the meeting, not the call:
                # she stays in it, and only her notes start over.
                same_call = self._guest is not None and self._guest_code == self._code(getattr(self._meetings.session, 'meeting', None))
                if not same_call:
                    self._leave_call()
                self._forget(meeting_id)
                if same_call:
                    self._public = True
                    self._auto_tried = meeting_id
                    self._show_board(self._meeting())
                self._publish_state()
            self._auto_join()
            self._queue_read()
        elif state in ('ended', 'idle') and self._id and meeting_id in ('', self._id):
            self._leave_call()
            self._forget('')
            self._publish_state()

    # New lines arrived (or it is time to look): her name, then the notes.
    def _read_new(self):
        m = self._meeting()
        if not m:
            return
        self._expire_say()
        if self._meetings.session.state != 'live':
            return
        mode = self._mode()
        turns = len(m.get('turns', []))
        self._heard = min(self._heard, turns)       # the list can get shorter (echo removal, a split)
        # Only words the call heard: your own aside to her with the microphone
        # muted in Meet must not get an answer in front of everyone.
        new, self._heard = self._public_rows(m, self._heard)
        if mode == 'active':
            # A finished caption of Meet's was already heard while it was open (listen).
            to_her = [row for row in new if not row['captioned'] and self._to_her(m, row['text'], row['you'])]
            if to_her:
                self._reply(m, self._owner(m) or 'You' if to_her[-1]['you'] else to_her[-1]['name'], to_her[-1]['text'])
        if mode != 'off':
            self._update(m, mode, force=False)

    # Brings the notes up to date when enough was said and enough time passed
    # (or `force`). The notes only change when the AI's answer could be read.
    def _update(self, m: dict, mode: str, force: bool):
        turns = len(m.get('turns', []))
        self._read = min(self._read, turns)
        if self._public:
            rows, upto = self._public_rows(m, self._read)
        else:
            rows, upto = brain.lines(m, first=self._read), turns
        if not rows:
            self._read = upto          # lines the call didn't hear are passed over
            return
        seconds = float(self._setting('meeting.companion.update_seconds', 45) or 45)
        if not force and (brain.words(rows) < brain.MIN_NEW_WORDS or self._now() - self._updated_at < seconds):
            return
        if not self._ai_ready():
            if force:
                raise CompanionError(NO_MODEL)
            return
        self._updated_at = self._now()          # a failed call waits its turn too
        generation = self._generation
        with for_meeting(m['id'], 'companion'):
            notes = brain.update(self._notes, rows, mode, meeting_language(m), self._complete, self._parse,
                                 owner=self._owner(m))
        # While the model was answering she was brought into the call, or the
        # meeting changed: these notes were made for other eyes, or another meeting.
        if generation != self._generation:
            return
        self._notes = notes
        self._read = upto
        ask = notes.get('ask')
        if ask:
            key = (ask['who'].lower(), ask['question'].lower())
            if key not in self._asked:
                self._asked.add(key)
                who = ask['who'] or 'Someone'
                self._card('ask', f"{who} asked you: {ask['question']}" if ask['for_you'] else f"{who} asked: {ask['question']}",
                           ask['answer'], ask['points'])
        self._show_board(m)
        self._publish_state()

    # ── The buttons ──────────────────────────────────────────────────────────

    def _press(self, kind: str, *args):
        self._live()
        with self._lock:
            self._busy += 1
        self._jobs.put((kind, *args))
        self._publish_state()

    # "What now?": the notes brought up to this second.
    def now_(self):
        self._press('now')

    # "What should I answer?"
    def answer(self):
        self._press('answer')

    # "Catch me up": the last `minutes`.
    def catch_up(self, minutes: float = 5):
        self._press('catch_up', max(1.0, min(30.0, float(minutes or 5))))

    # "+ Task": the task being talked about just now.
    def task(self):
        self._press('task')

    def _button(self, kind: str, *args):
        m = self._live()
        if not self._ai_ready():
            raise CompanionError(NO_MODEL)
        language = meeting_language(m)
        elapsed = self._elapsed(m)
        if kind == 'now':
            mode = self._mode()
            self._update(m, mode if mode != 'off' else 'insights', force=True)
            return
        with for_meeting(m['id'], 'companion'):
            if kind == 'answer':
                rows = brain.lines(m, since=elapsed - ANSWER_WINDOW_S)
                got = brain.answer(self._notes, rows, language, self._complete, self._parse, owner=self._owner(m))
                self._card('answer', got['question'] or 'What you could say', got['answer'], got['points'])
            elif kind == 'catch_up':
                minutes = args[0]
                got = brain.catch_up(brain.lines(m, since=elapsed - minutes * 60), language, self._complete, self._parse)
                self._card('catch_up', f"The last {minutes:g} minutes", '\n'.join(got['lines']), [])
            elif kind == 'task':
                got = brain.task_from_talk(brain.lines(m, since=elapsed - TASK_WINDOW_S), language, self._complete, self._parse)
                self._add_task(m, got['task'], got['owner'], 'you')

    # ── Tasks ────────────────────────────────────────────────────────────────

    def _add_task(self, m: dict, task: str, owner, source: str):
        tasks = list(m.get('tasks') or [])
        if any(t['task'].lower() == task.lower() for t in tasks):
            return
        tasks.append({'task': task, 'owner': owner, 'at': self._elapsed(m), 'source': source})
        self._meetings.session.set_tasks(m['id'], tasks)
        self._publish_state()

    # A task Koli noted by herself becomes one of the meeting's.
    def task_take(self, index: int):
        m = self._live()
        noted = self._notes.get('tasks') or []
        if not isinstance(index, int) or not 0 <= index < len(noted):
            raise ValueError("That task is no longer in the notes")
        self._add_task(m, noted[index]['task'], noted[index]['owner'], 'koli')

    def task_remove(self, task_id: str):
        m = self._live()
        self._meetings.session.set_tasks(m['id'], [t for t in m.get('tasks') or [] if t.get('id') != task_id])
        self._publish_state()

    # ── Cards ────────────────────────────────────────────────────────────────

    def _card(self, kind: str, title: str, text: str, points: list):
        with self._lock:
            self._card_seq += 1
            card = {'id': f'c{self._card_seq}', 'kind': kind, 'title': title, 'text': text, 'points': list(points or []),
                    'at': self._now()}
            self._cards = ([card] + self._cards)[:MAX_CARDS]
        self._publish_state()

    # A failure the user should see, once: the same words twice in a row are one card.
    def _error(self, text: str):
        with self._lock:
            repeated = bool(self._cards) and self._cards[0]['kind'] == 'error' and self._cards[0]['text'] == text
        if not repeated:
            self._card('error', "That didn't work", text, [])

    def dismiss(self, card_id: str):
        with self._lock:
            self._cards = [c for c in self._cards if c['id'] != card_id]
        self._publish_state()

    # The panel never has the focus, so it can't reach the clipboard itself.
    def copy(self, text: str):
        self._copy(str(text or ''))

    # ── Koli in the call ─────────────────────────────────────────────────────

    def _make_guest(self, on_state, name, announce):
        from ..meeting.guest import MeetGuest
        return MeetGuest(on_state=on_state, name=name, announce=announce)

    def _code(self, m: Optional[dict]) -> str:
        return str(((m or {}).get('source') or {}).get('code') or '')

    # Your first name, as Meet knows you: who her board says she is with.
    def _owner(self, m: Optional[dict]) -> str:
        call = self._meetings.meet.call(self._code(m)) if self._code(m) else None
        return str((call or {}).get('self_name') or '').split(' ')[0]

    def _announcement(self, m: dict, owner: str) -> str:
        if not self._setting('meeting.companion.announce', True):
            return ''
        name = self._name()
        if meeting_language(m) == 'he' and m.get('turns'):
            return (f"שלום, אני {name}, עוזרת ה-AI של {owner}. אני עוקבת אחרי הפגישה ומציגה סיכום על המסך שלי." if owner
                    else f"שלום, אני {name}, עוזרת AI. אני עוקבת אחרי הפגישה ומציגה סיכום על המסך שלי.")
        return (f"Hi, I'm {name}, {owner}'s AI assistant. I follow the meeting and show notes on my tile." if owner
                else f"Hi, I'm {name}, an AI assistant. I follow the meeting and show notes on my tile.")

    # "Bring Koli in": she joins this meeting's Google Meet call as a guest.
    def join(self):
        m = self._live()
        code = self._code(m)
        if not code:
            raise ValueError("Koli can only join a Google Meet call you joined with the Koli extension")
        with self._lock:
            # Her state as the panel has it: a second press while she is on her way does nothing.
            if self._guest is not None and self._guest_state['state'] in GUEST_BUSY:
                return
            owner = self._owner(m)
            self._go_public()
            cell = {}
            guest = self._guest_factory(lambda state, detail='', progress=None: self._on_guest_state(cell.get('guest'), state, detail, progress),
                                        self._name(), self._announcement(m, owner))
            cell['guest'] = guest
            self._guest, self._guest_code = guest, code
            self._guest_state = {'state': 'starting', 'detail': "Starting Koli's browser…", 'progress': None}
            self._auto_tried = m['id']
            guest.show(self._view(m))
            guest.join(code, owner)
        self._publish_state()

    # Her notes are about to be on a tile everyone sees. Notes made so far may
    # hold what only Koli's own microphone heard, so they start over, from
    # what the call heard, and stay that way for the rest of this meeting.
    def _go_public(self):
        if self._public:
            return
        self._public = True
        self._generation += 1
        self._notes = brain.empty_notes()
        self._read = 0
        self._updated_at = float('-inf')
        self._queue_read()

    # "Take Koli out". She won't come back to this meeting by herself.
    def leave(self):
        if self._id:
            self._auto_tried = self._id
        self._leave_call()
        self._publish_state()

    def _leave_call(self):
        with self._lock:
            guest, self._guest, self._guest_code = self._guest, None, ''
        if guest is not None:
            try:
                guest.leave()
            except Exception as e:
                logger.debug(f"Taking Koli out of the call failed: {e}")
        self._guest_state = {'state': 'off', 'detail': '', 'progress': None}

    # With meeting.companion.join_call she comes in by herself, once per meeting.
    def _auto_join(self):
        m = self._meeting()
        if (m and self._setting('meeting.companion.join_call', False) and self._auto_tried != m['id']
                and self._code(m) and self._guest is None):
            try:
                self.join()
            except ValueError as e:
                logger.info(f"Koli did not join the call: {e}")

    # What a guest says about herself. Only the guest in use is heard: one
    # that was taken out may still report a failure a moment later.
    def _on_guest_state(self, guest, state: str, detail: str = '', progress=None):
        if guest is not self._guest:
            return
        self._guest_state = {'state': state, 'detail': detail, 'progress': progress}
        if state == 'in_call':
            self._show_board(self._meeting())
        self._publish_state()

    # Her name while she is in the call or on her way: the extension measures
    # the tile with this name, and the Meet feed leaves her out of the people.
    def guest_name(self) -> str:
        guest = self._guest
        return self._name() if guest is not None and self._guest_state['state'] in GUEST_BUSY else ''

    # The size of her tile on your screen, from the extension.
    def tile(self, width, height):
        guest = self._guest
        if guest is not None:
            guest.set_tile(width, height)

    # The one view of the notes that goes to the call's screen.
    def _view(self, m: Optional[dict]) -> dict:
        return brain.public_view(self._notes, self._owner(m), self._name(), self._say)

    def _show_board(self, m: Optional[dict]):
        guest = self._guest
        if guest is not None:
            guest.show(self._view(m))

    def _expire_say(self):
        if self._say and self._now() >= self._say_until:
            self._say = ''
            self._show_board(self._meeting())

    # A card's words written in the call's chat, by your click.
    def say(self, card_id: str):
        card = next((c for c in self._cards if c['id'] == card_id), None)
        if card is None:
            raise ValueError("That card is gone")
        # Only something to say: a catch-up is for you, and an error may hold details.
        if card['kind'] not in ('ask', 'answer') or not card['text']:
            raise ValueError("That card isn't something to say to the call")
        guest = self._guest
        if guest is None or not guest.in_call:
            raise ValueError(f"{self._name()} is not in the call")
        if not guest.say(card['text']):
            raise ValueError("The message was not sent")

    # ── Talking with her in the call ─────────────────────────────────────────
    # In Active mode, while she is in the call, she answers on her tile: a
    # line that speaks to her by name, and when you are alone with her in the
    # call, anything you say. With meeting.companion.chat the answer is
    # written in the chat too.

    # Just you and her in the call (Meet's people list has nobody else).
    def _alone(self, m: dict) -> bool:
        call = self._meetings.meet.call(self._code(m)) if self._code(m) else None
        people = (call or {}).get('people') or []
        return any(p.get('self') for p in people) and not any(not p.get('self') for p in people)

    def _to_her(self, m: dict, text: str, yours: bool) -> bool:
        return brain.spoken_to(text, self._name()) or (yours and self._alone(m))

    def _can_talk(self) -> bool:
        guest = self._guest
        return (self._mode() == 'active' and guest is not None and guest.in_call
                and self._meetings.session.state == 'live')

    # Meet closes a caption about 8 seconds after the speaker stops, and only
    # then is it a line of the transcript. She looks at the captions still
    # open instead, every second: words to her that haven't changed for
    # PAUSE_S are answered now. A caption that grows later is answered for
    # its new words. (Captions are heard by the call by their nature.)
    def listen(self):
        m = self._meeting()
        if not m or not self._can_talk() or not self._code(m):
            return
        now = self._now()
        for block in self._meetings.meet.open_captions(self._code(m))[-2:]:
            text = ' '.join(str(block.get('text') or '').split())
            taken = self._answered.get(block['id'], '')
            if not text or text == taken or now - block['t1'] < PAUSE_S:
                continue
            fresh = text[len(taken):].strip() if taken and text.startswith(taken) else text
            self._answered[block['id']] = text
            if fresh and self._to_her(m, fresh, bool(block.get('self'))):
                self._reply(m, (self._owner(m) or 'You') if block.get('self') else block['name'], fresh)
        for block_id in list(self._answered)[:-20]:
            del self._answered[block_id]

    # Her answer to `text`, said by `who`: three dots on her tile at once,
    # then her words as the model writes them, then the whole answer for
    # SAY_SHOWN_S. Drawn only from what the call heard.
    def _reply(self, m: dict, who: str, text: str):
        guest = self._guest
        if not self._can_talk() or self._now() - self._said_at < SAY_GAP_S or not self._ai_ready():
            return
        self._said_at = self._now()
        self._say, self._say_until = THINKING, self._now() + SAY_SHOWN_S
        self._show_board(m)
        shown = {'text': '', 'at': float('-inf')}

        def on_text(piece):
            shown['text'] += str(piece or '')
            if self._now() - shown['at'] >= STREAM_EVERY_S and brain.tidy_say(shown['text']):
                shown['at'] = self._now()
                self._say = brain.tidy_say(shown['text'])
                self._show_board(m)
        rows = brain.lines(m, since=self._elapsed(m) - ANSWER_WINDOW_S, heard=self._heard_fn(m), heard_only=True)
        try:
            with for_meeting(m['id'], 'companion'):
                said = brain.reply(self._notes, rows, list(self._talk), who, text, meeting_language(m), self._complete,
                                   on_text=on_text, owner=self._owner(m))
        except CompanionError:
            self._say = ''
            self._show_board(m)
            raise
        self._say, self._say_until = said, self._now() + SAY_SHOWN_S
        self._show_board(m)
        self._talk.append({'who': who, 'text': text})
        self._talk.append({'who': self._name(), 'text': said})
        if self._setting('meeting.companion.chat', False):
            guest.say(said)
        self._card('said', f"{self._name()} answered {who}", said, [])

    # ── What the panel gets ──────────────────────────────────────────────────

    def state(self) -> dict:
        m = self._meeting()
        with self._lock:
            notes = {k: v for k, v in self._notes.items() if k != 'ask'}
            return {'id': self._id, 'mode': self._mode(), 'ready': bool(self._ai_ready()), 'busy': self._busy > 0,
                    'notes': notes, 'cards': [dict(c) for c in self._cards],
                    'tasks': [dict(t) for t in (m or {}).get('tasks') or []],
                    'guest': {**self._guest_state, 'can_join': bool(self._code(m)), 'name': self._name()}}

    def _publish_state(self):
        try:
            self._publish(TOPIC, self.state())
        except Exception as e:
            logger.debug(f"Publishing Koli's state failed: {e}")

    def shutdown(self):
        self._closed = True
        for stop in self._unsubscribe:
            if callable(stop):
                stop()
        self._unsubscribe = []
        self._leave_call()


def _copy_text(text: str):
    import pyperclip
    pyperclip.copy(text)


__all__ = ['CompanionService', 'TOPIC']
