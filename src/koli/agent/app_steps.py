# agent/app_steps.py
# Doing something inside another app the way a person would: look at it, do a
# step, look again. Each round Koli reads the app's controls (numbered) and
# takes a picture of its window, side by side (agent/desktop.py see()); the AI
# answers with one step or a few on the same screen ("click 12"), or that it
# is done, or stuck. Every step passes the policy (danger words, closing an
# app, the AI's own "danger" ask for your password) and is kept for Undo. What
# worked goes to the app memory (agent/app_memory.py): the next similar
# request starts with it in front of the AI, and the very same request is
# first replayed by name without asking the AI at all.

import logging
import re
import threading
import time
from typing import Callable, List, Optional

from . import actions, app_memory, desktop, planner, policy

logger = logging.getLogger(__name__)

MAX_ROUNDS = 8               # looks at the app in one request
TIME_LIMIT_S = 120.0         # one request, all rounds
# What run() reports, the runner's outcomes plus NOT_HERE: the AI says the
# request isn't about working in this app (the script writer takes it).
RAN, FAILED, CANCELLED, NOT_HERE = 'ran', 'failed', 'cancelled', 'not_here'

# Words of a request that don't help find a control out of view.
_COMMON = {'the', 'and', 'on', 'in', 'to', 'of', 'a', 'my', 'it', 'is', 'for', 'with', 'please', 'את', 'של', 'על', 'ב'}


class AppSteps:
    # `complete(feature, messages, json=True)` → llm.Result; `say(phase, text,
    # **extra)` → the bubble; `approve(lines, reasons, detail)` → True to go
    # ahead (it tells the bubble itself when not); `pictures()` → whether the
    # AI may see the app's window; `cancel`: set when you cancel.
    def __init__(self, complete: Callable, backend: actions.Backend, memory: app_memory.AppMemory,
                 say: Callable, approve: Callable, cancel: threading.Event,
                 pictures: Callable[[], bool] = lambda: True, clock: Callable[[], float] = time.monotonic):
        self.complete = complete
        self.backend = backend
        self.memory = memory
        self.say = say
        self.approve = approve
        self.cancel = cancel
        self.pictures = pictures
        self.clock = clock

    # One request in `app` (its running name). Returns {'outcome', 'text',
    # 'ops', 'results', 'told'}: the steps done with their results (Undo, the
    # history), the sentence for the bubble, and `told` when the bubble has
    # already said how it ended (a refused password).
    def run(self, sentence: str, app: str) -> dict:
        self.sentence, self.app = sentence, app
        self.ops: List[dict] = []
        self.results: List[dict] = []
        self.trail: List[str] = []          # "Click “Daily Mix 6” in Spotify: done", for the next round
        recipe = self.memory.recipe(app, sentence)
        if recipe:
            ended = self._replay(recipe)
            if ended is not None:
                return ended
            logger.info("AI action: the remembered steps didn't all happen; looking at the app")
        worked_before = [app_memory.describe(entry) for entry in self.memory.similar(app, sentence)]
        words = [w for w in re.sub(r'[^\w]+', ' ', sentence.lower()).split()
                 if w not in _COMMON and w not in app.lower()]
        deadline = self.clock() + TIME_LIMIT_S
        window, idle = None, 0
        for round_no in range(1, MAX_ROUNDS + 1):
            if self.cancel.is_set():
                return self._end(CANCELLED, '')
            if self.clock() > deadline:
                return self._end(FAILED, f"It took longer than {int(TIME_LIMIT_S)} seconds")
            self.say('thinking', f"Looking at {app}…" if round_no == 1 else f"Looking at {app} again…")
            try:
                view = desktop.see(self.backend.desktop, app, words, picture=self.pictures(), window=window)
            except desktop.DesktopError as e:
                return self._end(FAILED, str(e))
            window = view.get('window') or window
            started = self.clock()
            answer = self.complete('commands', planner.step_messages(
                sentence, view, self.trail, worked_before, round_no, MAX_ROUNDS), json=True)
            if self.cancel.is_set():
                return self._end(CANCELLED, '')
            if not answer.ok:
                return self._end(FAILED, answer.error or 'The AI model gave no answer')
            step = planner.parse_step(answer.text)
            logger.info(f"AI action: round {round_no} in {app} ({len(view['controls'])} controls"
                        f"{', a picture' if view.get('image') else ''}) answered in {self.clock() - started:.1f}s")
            if step is None:
                return self._end(FAILED, "The AI's answer couldn't be read")
            if step['not_here'] and not self.ops:
                return {'outcome': NOT_HERE}
            if not step['steps']:
                if step['done']:
                    return self._end(RAN, step['done'])
                return self._end(FAILED, step['stuck'] or 'The AI gave no step', learned=bool(step['stuck']))
            before = len(self.ops)
            ran_all = self._round(view, step)
            if ran_all == CANCELLED:
                return self._end(CANCELLED, '', told=True)
            if ran_all and step['final']:
                return self._end(RAN, step['final'])
            # Two rounds in a row where nothing was even tried (a control
            # number that isn't in the list): it won't get better.
            idle = idle + 1 if len(self.ops) == before else 0
            if idle >= 2:
                return self._end(FAILED, f"The AI couldn't find what to press in {app}")
        return self._end(FAILED, f"Not finished after {MAX_ROUNDS} looks at {app}")

    # ── A round's steps ──

    # Runs the steps in order. True when all ran and the screen was looked at
    # for each (no click came before another step); CANCELLED when you said no.
    def _round(self, view: dict, step: dict):
        by_number = {control['n']: control for control in view['controls']}
        steps = step['steps']
        for i, wanted in enumerate(steps):
            if wanted['action'] == 'close_app':
                op = self._op(view, None, wanted)
            elif wanted['control'] not in by_number:
                self.trail.append(f"control {wanted['control']} isn't in the list: nothing was done")
                return False
            else:
                op = self._op(view, by_number[wanted['control']], wanted)
            status = self._do(op, step['doing'])
            if status == CANCELLED:
                return CANCELLED
            if status != 'done':
                return False
            # A click may open another screen: the steps planned after it
            # were for this one, so Koli looks again first.
            if op['action'] in ('click', 'close_app') and i < len(steps) - 1:
                self.trail.append('the steps after that click were left for after a new look')
                return False
        return True

    # The step as Koli's own op (the same shape as a script's ui step, so the
    # bubble's lines, the policy and Undo treat both alike).
    def _op(self, view: dict, control: Optional[dict], wanted: dict) -> dict:
        op = {'op': 'ui', 'action': wanted['action'], 'app': self.app, 'seen': True, 'live': True,
              'risk': wanted['risk']}
        if control is None:
            op.update(name=self.app, role='', ref='')
            return op
        op.update(name=control['name'], role=control['role'], ref=control['ref'])
        if view.get('window'):
            op['window'] = view['window']
        if wanted['action'] == 'set_text':
            old = control.get('value')
            op.update(text=wanted['text'], old_value=old if isinstance(old, str) else '',
                      old_length=len(old) if isinstance(old, str) else 0)
        elif wanted['action'] == 'select':
            op['option'] = wanted['option']
        elif wanted['action'] in ('check', 'uncheck'):
            states = control.get('states') or []
            op['was_checked'] = 'checked' in states if control['role'] in actions.TOGGLE_ROLES else None
        return op

    # One step for real, after the policy. Returns the status ('done',
    # 'skipped', 'failed') or CANCELLED.
    def _do(self, op: dict, doing: str = '') -> str:
        line = actions.ui_line(op)
        reasons = policy.ui_reasons(op)
        if reasons and not self.approve([line], reasons, line):
            return CANCELLED
        if self.cancel.is_set():
            return CANCELLED
        self.say('working', doing or line, lines=[line])
        status, detail = desktop.perform(op, self.backend.desktop)
        entry = {'op': 'ui', 'status': status, 'detail': detail, 'undo': None}
        if status == 'done':
            entry['undo'] = actions._ui_undo(op)
        self.ops.append(op)
        self.results.append(entry)
        self.trail.append(f"{line}: {status}{f' ({detail})' if detail else ''}")
        logger.info(f"AI action: {line}: {status}{f' ({detail})' if detail else ''}")
        return status

    # ── From memory ──

    # The very same request done before: its steps again, by label, with no
    # AI call. The end when they all happen; None when one didn't (the loop
    # goes on from there, the trail saying what happened).
    def _replay(self, recipe: dict) -> Optional[dict]:
        self.say('working', 'Doing it the way that worked last time…')
        for step in recipe.get('steps') or []:
            op = {'op': 'ui', 'action': step.get('action') or 'click', 'app': self.app,
                  'name': step.get('name') or '', 'role': step.get('role') or '', 'ref': '',
                  'seen': True, 'live': True, 'risk': step.get('risk') or 'change'}
            if op['action'] == 'set_text':
                op['text'] = step.get('text') or ''
                op.update(self._old_text(op))
            elif op['action'] == 'select':
                op['option'] = step.get('option') or ''
            status = self._do(op)
            if status == CANCELLED:
                return self._end(CANCELLED, '', told=True)
            if status != 'done':
                return None
        logger.info(f"AI action: done from memory in {self.app}")
        return self._end(RAN, recipe.get('answer') or '')

    # What a field holds now, for Undo, when a remembered step writes in it.
    def _old_text(self, op: dict) -> dict:
        try:
            found = desktop.find_control(self.backend.desktop, self.app, op['name'], op['role'] or None)
        except desktop.DesktopError:
            return {}
        if len(found) != 1:
            return {}
        old = found[0].get('value')
        old = old if isinstance(old, str) else ''
        return {'old_value': old, 'old_length': len(old)}

    # ── The end ──

    # What run() returns. What worked is remembered, and where the AI itself
    # said it got stuck (`learned`); a network error, a timeout or a closed
    # window is no lesson about the app, and would replace a recipe that worked.
    def _end(self, outcome: str, text: str, told: bool = False, learned: bool = False) -> dict:
        done = [op for op, r in zip(self.ops, self.results) if r['status'] == 'done']
        if (outcome == RAN and done) or (outcome == FAILED and learned):
            steps = [{k: op[k] for k in ('action', 'name', 'role', 'text', 'option', 'risk') if op.get(k)}
                     for op in done]
            self.memory.add(self.app, self.sentence, steps, worked=outcome == RAN,
                            stuck=text if outcome == FAILED else '', answer=text if outcome == RAN else '')
        return {'outcome': outcome, 'text': text, 'ops': self.ops, 'results': self.results, 'told': told}
