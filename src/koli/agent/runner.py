# agent/runner.py
# One AI action, start to finish: the writer's script, the code check and
# rehearsal, the reviewer, the danger level, your approval when needed, the
# real run, the log, and the offer to keep a repeated plan as a command. Every
# step tells the bubble what is happening (the 'agent' event). Runs in the
# caller's thread: the wake word's worker or the command pipeline, which keeps
# Koli from listening to anything else meanwhile.

import logging
import os
import threading
import time
from typing import Callable, List, Optional

from . import actions, planner, policy, sandbox
from .approval import Approval
from .journal import REPEATS_TO_OFFER, Journal, signature

logger = logging.getLogger(__name__)

APPROVE_TRIES = 2
CLICK_WAIT_S = 30.0
# A request this soon after an action may follow on from it ("call it
# Invoices" after "make a new folder"): the writer is told about it, and
# last() returns what it made.
FOLLOW_ON_S = 300.0

# Questions Koli may ask you in one request (the writer's, and "did you mean
# this folder?"): enough to settle it, never a conversation that won't end.
MAX_QUESTIONS = 2
YES = {'yes', 'yeah', 'yep', 'yup', 'sure', 'right', 'correct', 'exactly', 'ok', 'okay', 'that one', 'yes please',
       'כן', 'נכון', 'בדיוק', 'בטח', 'אוקיי', 'כן כן', 'כן בדיוק'}

# What handle() reports back.
RAN, NOT_UNDERSTOOD, FAILED, CANCELLED, NOTHING = 'ran', 'not_understood', 'failed', 'cancelled', 'nothing'


# Whether a spoken answer is a yes ("Yes.", "yeah, that one", "כן").
def is_yes(answer: str) -> bool:
    from ..utils import normalize_spoken
    words = normalize_spoken(answer or '')
    return words in YES or (words.split()[:1] or [''])[0] in YES and not words.startswith(('yes but', 'כן אבל'))


class AgentRunner:
    # `complete(feature, messages, json=True)` → llm.Result; `publish(payload)`
    # sends an 'agent' event; `commands()` → your fixed command phrases;
    # `run_fixed(phrase)` runs one of them; `enabled()` reads the setting;
    # `hear(stop)` listens for your answer to a question → text or None.
    def __init__(self, complete: Callable, publish: Callable[[dict], None], approval: Approval,
                 backend: actions.Backend = None, journal: Journal = None, folders: policy.Folders = None,
                 commands: Callable[[], List[str]] = lambda: [], run_fixed: Callable[[str], bool] = lambda p: False,
                 enabled: Callable[[], bool] = lambda: True, hear: Callable = None):
        self.hear = hear
        self._questions = 0
        self.complete = complete
        self.publish = publish
        self.approval = approval
        self.backend = backend or actions.Backend()
        self.journal = journal or Journal()
        self.folders = folders or policy.Folders.from_platform()
        self.commands = commands
        self.run_fixed = run_fixed
        self.enabled = enabled
        self._cancel = threading.Event()
        self._click = threading.Event()
        self.busy = False

    # ── From the bubble ──

    def cancel(self):
        self._cancel.set()

    def approve_click(self):
        self._click.set()

    # ── A spoken request ──

    def handle(self, sentence: str) -> str:
        self._cancel.clear()
        self.busy = True
        try:
            return self._handle(sentence)
        except Exception as e:
            logger.error(f"AI action failed: {e}", exc_info=True)
            self._say('failed', f"That didn't work: {e}")
            return FAILED
        finally:
            self.busy = False

    # The writer may ask you something first ("Only today's pictures, or
    # all of them?"); your answer goes back to it with the request. The
    # rehearsal may ask too, about a folder name it didn't find. At most
    # MAX_QUESTIONS in one request.
    def _handle(self, sentence: str) -> str:
        self._questions = 0
        self._say('thinking', 'Thinking…', heard=sentence)
        previous = self._previous()
        conversation: List[tuple] = []
        while True:
            started = time.monotonic()
            result = self.complete('commands', planner.writer_messages(
                sentence, self._folder_names(), self.commands(), self._recent(),
                previous=self._brief(*previous) if previous else None, conversation=conversation), json=True)
            if self._cancel.is_set():
                return self._cancelled()
            if not result.ok:
                self._say('failed', result.error or 'The AI model gave no answer')
                return FAILED
            plan = planner.parse_plan(result.text)
            logger.info(f"AI action: plan written in {time.monotonic() - started:.1f}s")
            if plan and plan['question'] and not plan['understood']:
                if self._questions >= MAX_QUESTIONS:
                    plan['reply'] = plan['reply'] or f"I still don't know: {plan['question']}"
                else:
                    answer = self._ask(plan['question'])
                    if answer is None:
                        return CANCELLED
                    conversation.append((plan['question'], answer))
                    continue
            if not plan or not plan['understood']:
                # The model's own words when it gave any ("I can turn the volume
                # up, but not by exactly 20%"): Koli talking to you. "Sorry, I
                # didn't understand" only when it said nothing.
                # Kept in the history too ("couldn't do" in Activity), so what the
                # AI can't do yet is there to fix.
                reply = (plan or {}).get('reply') or ''
                self._say('not_understood', reply or "Sorry, I didn't understand", heard=sentence, reply=bool(reply))
                self.journal.add({'sentence': sentence, 'summary': reply, 'lines': [], 'results': [],
                                  'outcome': NOT_UNDERSTOOD})
                return NOT_UNDERSTOOD
            outcome = self._run_script(plan['script'], sentence, plan.get('summary') or '',
                                       plan.get('command_name') or '', review=True, previous=previous,
                                       conversation=conversation)
            if isinstance(outcome, tuple):
                # A folder question answered with something other than yes
                # ("no, the Pictures one"): the writer hears it and tries again.
                conversation.append(outcome)
                continue
            return outcome

    # A saved AI command: the same checks, no writer.
    def run_saved(self, script: str, phrase: str) -> str:
        self._cancel.clear()
        self._questions = 0
        self.busy = True
        try:
            outcome = self._run_script(script, phrase, '', phrase, review=False, saved=True)
            if isinstance(outcome, tuple):      # no writer to hear a different answer
                return self._cancelled()
            return outcome
        except Exception as e:
            logger.error(f"Saved AI command failed: {e}", exc_info=True)
            self._say('failed', f"That didn't work: {e}")
            return FAILED
        finally:
            self.busy = False

    # The script rehearsed. When it got nowhere because a folder name wasn't
    # found and a real one is close ("API" for "avi"), Koli asks; a yes
    # rehearses again with that name. Returns the recorder, an outcome when
    # it ends here, or (question, answer) for the writer when you answered
    # something else.
    def _rehearse(self, script: str, previous: Optional[tuple]):
        aliases = {}
        while True:
            recorder = actions.Recorder(self.backend, command_exists=lambda p: p in self.commands(),
                                        previous=previous[1] if previous else (), aliases=aliases)
            error = None
            try:
                sandbox.rehearse(script, recorder)
            except sandbox.ScriptRejected as e:
                logger.warning(f"AI action: script rejected ({e})")
                self._say('failed', "I couldn't do that safely", detail=str(e))
                return FAILED
            except actions.ActionError as e:
                error = e
            stuck = error is not None or not recorder.ops
            miss = next((m for m in recorder.missing if m['name'].lower() not in aliases), None) if stuck else None
            if miss and self._questions < MAX_QUESTIONS:
                question = (f"There's no folder “{miss['name']}” in {actions._short(miss['where'])}. "
                            f"Did you mean “{miss['closest']}”?")
                answer = self._ask(question)
                if answer is None:
                    return CANCELLED
                if is_yes(answer):
                    aliases[miss['name'].lower()] = miss['closest']
                    continue
                return question, answer
            if error is not None:
                self._say('failed', str(error))
                return FAILED
            return recorder

    # `previous`: (run, places) of the action this one may follow on from;
    # `conversation`: the questions asked and your answers, for the history.
    def _run_script(self, script: str, sentence: str, summary: str, name: str, review: bool, saved: bool = False,
                    previous: Optional[tuple] = None, conversation: Optional[list] = None):
        recorder = self._rehearse(script, previous)
        if not isinstance(recorder, actions.Recorder):
            return recorder
        ops, said, notes = recorder.ops, recorder.said, recorder.notes
        logger.info(f"AI action: rehearsal gave {len(ops)} operation(s), {len(notes)} note(s)")
        if not ops:
            # The script's say() is written before anything runs, so it may
            # claim what didn't happen ("Created New folder" for one that was
            # already there): what Koli found comes first.
            if notes:
                self._say('nothing', f"Nothing to do: {notes[0]}", lines=notes)
                return NOTHING
            if said:
                self._say('done', said[-1], lines=[])
                return RAN
            self._say('nothing', 'Nothing to do: nothing matched')
            return NOTHING
        lines = actions.describe(ops)
        # It used last(): one action with the previous one, for Undo.
        follows = previous[0]['id'] if previous and any(t[0] == 'last' for t in recorder.trace) else None
        mismatch = ''
        commands = [op for op in ops if op['op'] == 'system']
        # A saved command skips the writer, not the reviewer's rating of its
        # system commands: what they do is judged every time they run.
        if (review and policy.changes_anything(ops)) or commands:
            self._say('checking', 'Checking…', lines=lines)
            answer = self.complete('commands', planner.reviewer_messages(
                sentence, lines, ops, previous=previous[0].get('sentence') or '' if follows else ''), json=True)
            if answer.ok:
                matches, reason, ratings = planner.parse_review(answer.text)
                if not matches:
                    mismatch = reason or 'the plan may not be what you asked'
                # Each system command gets the reviewer's plain words and risk;
                # one it skipped stays unrated, so the policy asks for the password.
                for op, rating in zip(commands, ratings):
                    op.update({k: v for k, v in rating.items() if k != 'why' and v}, risk_reason=rating['why'])
                if commands:
                    lines = actions.describe(ops)
                    logger.info("AI action: system command(s) rated " + ', '.join(op.get('risk') or 'unrated' for op in commands))
            else:
                logger.info(f"AI action: reviewer unavailable ({answer.error}); the policy decides")
        if self._cancel.is_set():
            return self._cancelled()
        level, reasons = policy.level(ops, self.folders, mismatch)
        if level == policy.PASSWORD:
            logger.info(f"AI action needs approval: {'; '.join(reasons)}")
            # The exact commands are on the bubble (on hover) for you to read.
            if not self._approved(lines, reasons, detail='\n'.join(op['command'] for op in commands)):
                return CANCELLED
        self._say('working', 'Working…', lines=lines)
        results = actions.replay(ops, self.backend, run_command=self.run_fixed)
        done = sum(1 for r in results if r['status'] == 'done')
        skipped = [r['detail'] for r in results if r['status'] == 'skipped']
        failed = [r['detail'] for r in results if r['status'] == 'failed']
        sig = signature(recorder.trace)
        run = self.journal.add({
            'sentence': sentence, 'script': script, 'summary': summary or '; '.join(lines), 'lines': lines,
            'command_name': name, 'signature': sig, 'ops': ops, 'results': results, 'saved_command': saved,
            'follows': follows, 'conversation': [list(qa) for qa in conversation or []],
            'outcome': 'done' if done and not failed else 'failed' if not done else 'partial'})
        undoable = any(r.get('undo') and r['status'] == 'done' and r['undo']['op'] != 'restore' for r in results)
        # What was really done, from the results, not the script's say():
        # "Moved 3 items from ~\Downloads to ~\Pictures".
        done_lines = actions.describe([op for op, r in zip(ops, results) if r['status'] == 'done'],
                                      limit=100, past=True)
        text = (done_lines[0] if len(done_lines) == 1 else f"{done_lines[0]} + {len(done_lines) - 1} more"
                ) if done_lines else 'Nothing was done'
        # What a system command printed is the answer: alone for a question
        # ("Your IP address is 192.168.1.100"), after the line for a change.
        printed = [(op, r['output']) for op, r in zip(ops, results) if r.get('output')]
        if printed:
            op, output = printed[0]
            text = output if op.get('risk') == 'read' and len(ops) == 1 else f"{text}: {output}"
        show = actions.place_of(results) is not None
        if failed:
            text = f"{text} · {len(failed)} failed: {failed[0]}"
        elif skipped:
            text = f"{text} · {len(skipped)} skipped ({skipped[0]})"
        # A follow-up means nothing without the action before it: never a command.
        offer = None if saved or follows else self.journal.offer_for(sig)
        if offer is not None:
            # The offer takes the place of the done bubble, so it carries Undo too.
            phrase = name or sentence[:40]
            self._say('offer_save', f"{text} · You've asked this {REPEATS_TO_OFFER} times. Save it as “{phrase}”?",
                      run_id=run['id'], phrase=phrase, undo=undoable, show=show, lines=done_lines)
        else:
            self._say('done' if done else 'failed', text, lines=done_lines or lines, run_id=run['id'],
                      undo=undoable, show=show, reply=said[-1] if said else '')
        return RAN if done else FAILED

    # ── Approval ──

    def _approved(self, lines: List[str], reasons: List[str], detail: str = '') -> bool:
        why = '; '.join(reasons)
        if not self.approval.phrase():
            self._say('needs_password', 'Set an approval password first (Commands › AI actions). Nothing was done.',
                      lines=lines, reasons=reasons, detail=detail)
            return False
        if not self.approval.can_listen():
            self._click.clear()
            self._say('approve', 'Press Approve to go ahead', lines=lines, reasons=reasons, needs='click', detail=detail,
                      note="Your voice isn't recorded on this microphone, so the password can't be checked")
            if not self._wait(self._click, CLICK_WAIT_S):
                self._cancelled()
                return False
            return True
        last = ''
        for attempt in range(APPROVE_TRIES):
            self._say('approve', 'Say your password' if attempt == 0 else 'Try again: say your password',
                      lines=lines, reasons=reasons, needs='password', detail=detail)
            ok, wrong = self.approval.ask_once(self._cancel)
            if self._cancel.is_set():
                self._cancelled()
                return False
            if ok:
                logger.info(f"AI action approved ({why})")
                return True
            logger.info(f"AI action: password refused ({wrong})")
            last = wrong
        self._say('failed', f"Not approved: {last}. Nothing was done.", lines=lines)
        return False

    # ── Questions ──

    # Shows the question and listens for your answer right away (no wake
    # word). None, with the bubble saying so, when you cancel or say nothing.
    def _ask(self, question: str) -> Optional[str]:
        self._questions += 1
        self._say('ask', question, needs='voice')
        logger.info("AI action: asked you a question")
        answer = (self.hear(self._cancel) or '').strip() if self.hear else ''
        if self._cancel.is_set():
            self._cancelled()
            return None
        if not answer:
            self._say('cancelled', 'No answer heard. Nothing was done.')
            return None
        self._say('thinking', 'Thinking…', heard=answer)
        return answer

    def _wait(self, event: threading.Event, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if event.wait(0.1):
                return True
            if self._cancel.is_set():
                return False
        return False

    # ── Undo and saving ──

    def undo(self, run_id: Optional[str] = None) -> str:
        run = self.journal.get(run_id) if run_id else self.journal.last_undoable()
        if run is None or run.get('undone'):
            self._say('failed', 'Nothing to undo')
            return FAILED
        # The run with its follow-ups and what they followed, newest first:
        # the rename goes back before the folder it renamed is removed.
        chain = [r for r in self.journal.chain(run['id']) if not r.get('undone')] or [run]
        by_hand = []
        for step in chain:
            by_hand += actions.undo(step.get('results') or [], self.backend)[1]
            self.journal.update(step['id'], undone=True)
        text = f"Undone: {chain[-1].get('summary') or 'the last action'}"
        if len(chain) > 1:
            text += f" and the {len(chain) - 1} step{'s' if len(chain) > 2 else ''} after it"
        if by_hand:
            text += f" · restore {', '.join(by_hand[:3])} from the Recycle Bin"
        self._say('undone', text)
        return RAN

    # The bubble's Show: Explorer at what a run made or moved, that item
    # selected. Only places from the run's own results, never a page's path.
    def show(self, run_id: str) -> bool:
        run = self.journal.get(run_id)
        place = actions.place_of(run.get('results')) if run else None
        if place is None:
            return False
        self.backend.reveal(place)
        return True

    def decline(self, run_id: str):
        run = self.journal.get(run_id)
        if run:
            self.journal.mark(run['signature'], 'declined')

    def saved(self, run_id: str):
        run = self.journal.get(run_id)
        if run:
            self.journal.mark(run['signature'], 'saved')

    # ── Helpers ──

    def _cancelled(self) -> str:
        self._say('cancelled', 'Cancelled. Nothing was done.')
        return CANCELLED

    def _say(self, phase: str, text: str, **extra):
        try:
            self.publish({'phase': phase, 'text': text, **extra})
        except Exception as e:
            logger.debug(f"AI action event not sent: {e}")
        if phase in ('done', 'failed', 'not_understood', 'nothing', 'undone', 'cancelled', 'needs_password'):
            logger.info(f"AI action {phase}: {text}")

    # The known folders you have, by name only: a full path would send your
    # Windows user name to the model.
    def _folder_names(self) -> list:
        names = []
        for name in actions.KNOWN_FOLDERS:
            try:
                if self.backend.known_folder(name):
                    names.append(name)
            except Exception:
                continue
        return names

    # (run, places) for the action just before, when a new request may follow
    # on from it: recent, not undone, and something it made is still there.
    def _previous(self) -> Optional[tuple]:
        runs = [r for r in self.journal.runs(10) if r.get('outcome') != NOT_UNDERSTOOD]
        if not runs:
            return None
        run = runs[-1]
        if run.get('undone') or time.time() - run.get('at', 0) > FOLLOW_ON_S:
            return None
        places = actions.places_of(run.get('results'))
        return (run, places) if places else None

    # What the writer hears about it: what you said and what it did, and how
    # many files and folders last() returns. No names.
    @staticmethod
    def _brief(run: dict, places: list) -> dict:
        folders = sum(1 for p in places if os.path.isdir(p))
        files = len(places) - folders
        parts = [f"{n} {kind}{'s' if n != 1 else ''}" for n, kind in ((folders, 'folder'), (files, 'file')) if n]
        return {'said': run.get('sentence') or '', 'did': run.get('summary') or '',
                'seconds_ago': int(time.time() - run.get('at', 0)), 'last_returns': ' and '.join(parts)}

    def _recent(self) -> list:
        return [{'said': r.get('sentence'), 'did': r.get('summary')} for r in self.journal.runs(5)]
