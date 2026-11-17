# services/meeting_search.py
# Searching meetings by words and by meaning, and the background indexer that keeps each meeting's
# meaning index up to date. Never makes the caller wait on the model: words come back at once, and
# the meaning part says why it is missing (docs/design/meeting-search.md).

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..llm.http import TimedOut
from ..meeting import ask as meeting_ask
from ..meeting.find import merge
from ..meeting.index import MeetingIndex, cap
from ..meeting.store import BRIEF_SUFFIX, CHAT_SUFFIX

logger = logging.getLogger(__name__)

# ── Timings and defaults ─────────────────────────────────────────────────────

QUERY_WAIT_S = 2.0        # a search waits this long for the model (cold load 3.5 s, warm 0.02 s)
ASK_WAIT_S = 30.0         # "Ask all meetings" may wait for a cold model: it takes seconds anyway
DEBOUNCE_S = 10.0         # a meeting's changes are indexed this long after the last one
RETRY_S = 60.0            # after a failed embed (Ollama off, model missing) the indexer waits this long
MAX_ATTEMPTS = 3         # an indexing error that is the meeting's own: given up after this many tries
SMALL_BATCH = 8           # pieces per model call when a meeting's whole batch timed out (slow CPU-only Ollama)
TICK_S = 1.0              # the indexer thread looks for work this often, one meeting at a time
BLOCKING = ('no_ollama', 'no_model')   # problems a search answers at once while the indexer rests
DEFAULT_MODEL = 'qwen3-embedding:0.6b'
DEFAULT_ADDRESS = 'http://localhost:11434'
ASK_FILE = 'meetings_ask.json'     # the Ask conversation, in the user data folder


# The embedding call the app uses; imported when first needed so building the service never loads it.
def _default_embed(model: str, texts: list):
    from .. import llm
    return llm.embed(model, texts)


# What a failed model call says: 'outage' (Ollama not reachable), 'missing' (the model isn't
# downloaded), 'busy' (a server error or "too many requests": try later), 'timeout' (reached but too
# slow), or 'own': Ollama answered, and the input was the problem (a bad request, an unusable answer,
# any other error), so only the meeting that sent it is at fault.
def failure_kind(error: Exception) -> str:
    from ..llm.http import HttpError, NetworkError
    from ..llm.types import ProviderError
    if isinstance(error, TimedOut):
        return 'timeout'
    if isinstance(error, NetworkError):
        return 'outage'
    if isinstance(error, (HttpError, ProviderError)):
        status = int(getattr(error, 'status', 0) or 0)
        message = f'{getattr(error, "body", "")} {error}'.lower()
        if status == 404 or 'not found' in message:
            return 'missing'
        if status == 429 or status >= 500:
            return 'busy'
    return 'own'


# The page's word for a model problem; None for a meeting's own error, which is no model problem.
PROBLEM_OF_KIND = {'outage': 'no_ollama', 'missing': 'no_model', 'busy': 'loading', 'timeout': 'loading'}


def problem_of(error: Exception) -> Optional[str]:
    return PROBLEM_OF_KIND.get(failure_kind(error))


# What one search says when its own query call failed: the model problem, or for an error of the
# query's own, that search by meaning didn't work this time (nothing is remembered for the next one).
def _search_state(error: Exception) -> str:
    return problem_of(error) or 'no_ollama'


# One query embedding on its own thread: at most one is in flight, so a hung model never collects calls.
class _QueryFlight:
    def __init__(self, key: tuple):
        self.key = key                      # (model, query text)
        self.done = threading.Event()
        self.vector = None
        self.error: Optional[Exception] = None
        self.consumed = False               # a search has reported this error already


class MeetingSearch:
    # `setting(path, default)` reads the live settings; `is_running()` says a meeting is on (it keeps
    # the GPU, so nothing is indexed then); `publish(topic, payload)` sends events; `embed(model, texts)`
    # is the model call (llm.embed); `tasks` is the task manager (default: the app's). For Ask: `complete`
    # and `parse` are the AI completer and its JSON parser, `ai_ready()` says a model is chosen, and
    # `ask_path` is where the conversation is kept (default: the user data folder).
    def __init__(self, store, setting: Callable[[str, object], object], is_running: Callable[[], bool],
                 publish: Callable[[str, dict], None], embed: Optional[Callable] = None, tasks=None,
                 complete: Optional[Callable] = None, parse: Optional[Callable] = None,
                 ai_ready: Optional[Callable[[], bool]] = None, ask_path: Optional[Path] = None):
        self.store = store
        self._setting = setting
        self._is_running = is_running
        self._publish = publish
        self._embed = embed or _default_embed
        self._tasks = tasks
        self._complete = complete
        self._parse = parse
        self._ai_ready = ai_ready
        self._ask_path = Path(ask_path) if ask_path else None
        self._ask_lock = threading.Lock()              # one writer of the Ask conversation at a time
        self._lock = threading.RLock()
        self._index: Optional[MeetingIndex] = None     # built for the model setting, replaced when it changes
        self._pending: dict = {}                       # meeting id -> when it may be indexed (epoch seconds)
        self._problem: Optional[str] = None            # 'no_ollama' | 'no_model' after a failed model call
        self._blocked_until = 0.0                      # the indexer rests until then after a failure
        self._logged: set = set()                      # failure messages already in the log
        self._attempts: dict = {}                      # meeting id -> failed indexing tries since it last changed
        self._scan_needed = False                      # queue every meeting without a current index
        self._was_on = True
        self._last_status: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._flight: Optional[_QueryFlight] = None    # the one query embedding in progress or just finished
        self._batch = threading.local()                # .size: pieces per model call while a retry runs small

    # ── Settings ─────────────────────────────────────────────────────────────

    def _meaning_on(self) -> bool:
        return bool(self._setting('meeting.search.meaning', True))

    def _model(self) -> str:
        return str(self._setting('meeting.search.model', DEFAULT_MODEL) or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    # ── The index for the chosen model ───────────────────────────────────────

    # The index for the model chosen now, and whether the model just changed: then every meeting is
    # queued for the new model and a search never mixes vectors of two models.
    def _current_index(self, now: Optional[float] = None):
        model = self._model()
        with self._lock:
            if self._index is not None and self._index.model == model:
                return self._index, False
            switched = self._index is not None
            # The model is captured here, so an index only ever asks the model it was made for.
            self._index = MeetingIndex(self.store.folder, model,
                                       lambda texts, model=model: self._embed_pieces(model, texts))
            if switched:
                self._clear_problem()
                self._attempts.clear()                    # a new model: every meeting gets new tries
                self._pending.clear()
                self._queue_all(self._clock() if now is None else now)
        if switched:
            self._publish_status()
        return self._index, switched

    @staticmethod
    def _clock() -> float:
        return time.time()

    # The index's model call: all pieces at once, or SMALL_BATCH at a time while this thread retries a
    # meeting whose whole batch timed out.
    def _embed_pieces(self, model: str, texts: list):
        size = getattr(self._batch, 'size', None)
        if not size or len(texts) <= size:
            return self._embed(model, texts)
        import numpy as np
        return np.concatenate([np.asarray(self._embed(model, texts[start:start + size]), dtype=np.float32)
                               for start in range(0, len(texts), size)])

    # Forgets the model problem: the model answers again. Meetings' tries stay: they count only their
    # own errors and timeouts, which a working model doesn't fix.
    def _clear_problem(self) -> None:
        with self._lock:
            self._problem = None
            self._blocked_until = 0.0

    # ── Which meetings exist, and which are out of date ──────────────────────

    # Ids of the saved meetings, from the file names (no meeting is opened).
    def _meeting_ids(self) -> list:
        folder = self.store.folder
        if not folder.exists():
            return []
        return sorted(p.name[:-len('.json')] for p in folder.glob('*.json')
                      if not p.name.endswith((BRIEF_SUFFIX, CHAT_SUFFIX)))

    # When the meeting or its summary was last written: the index must be newer than this.
    def _changed_at(self, meeting_id: str) -> float:
        newest = 0.0
        for path in (self.store.path(meeting_id), self.store.brief_path(meeting_id)):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
        return newest

    def _queue_all(self, now: float) -> None:
        with self._lock:
            for meeting_id in self._meeting_ids():
                self._pending.setdefault(meeting_id, now)

    # Queues every meeting whose index is missing, older than the meeting, or made with another model.
    def _queue_stale(self, index: MeetingIndex, now: float) -> None:
        for meeting_id in self._meeting_ids():
            if not index.is_current(meeting_id, self._changed_at(meeting_id)):
                with self._lock:
                    self._pending.setdefault(meeting_id, now)

    # ── The indexer ──────────────────────────────────────────────────────────

    # The store tells us a meeting changed: it is indexed once it has been quiet for DEBOUNCE_S.
    def changed(self, meeting_id: str) -> None:
        with self._lock:
            self._attempts.pop(meeting_id, None)          # new content: worth another try
            self._pending[meeting_id] = self._clock() + DEBOUNCE_S

    # Ids waiting to be indexed, in a stable order.
    def pending(self) -> list:
        with self._lock:
            return sorted(self._pending)

    # Starts the indexer thread once; its first pass queues every meeting whose index isn't current.
    # Each run has its own stop event, so a thread still finishing after stop() never runs on with a
    # restarted one's event.
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return
            self._scan_needed = True
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, args=(self._stop,), daemon=True,
                                            name='meeting-search-indexer')
            self._thread.start()

    # Asks the indexer to stop; the thread reference stays until the thread has really exited.
    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        if thread is not None and not thread.is_alive():
            self._thread = None

    def _loop(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_S):
            try:
                self.run_pending(self._clock())
            except Exception as e:                        # the thread must outlive any one bad pass
                logger.warning(f'Meeting indexer pass failed: {e}', exc_info=True)

    # One indexer step: the model/setting checks, then at most one due meeting. Does nothing while a
    # meeting runs (Whisper keeps the GPU) or while resting after a failed model call.
    def run_pending(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        if not self._meaning_on():
            self._was_on = False
            return
        index, switched = self._current_index(now)
        if switched:
            return                                        # everything is queued; start with the next tick
        if not self._was_on:                              # turned on again: catch up on what changed meanwhile
            self._was_on = True
            self._scan_needed = True
        if self._scan_needed:
            self._scan_needed = False
            self._queue_stale(index, now)
            self._publish_status()
        if self._is_running() or now < self._blocked_until:
            return
        with self._lock:
            due = [(when, meeting_id) for meeting_id, when in self._pending.items() if when <= now]
        if due:
            self._index_one(min(due)[1], index, now)

    # Indexes everything queued (after checking which meetings are stale), ignoring the debounce and
    # the running-meeting wait. The tests' and tools' way to finish the work now.
    def run_all(self) -> None:
        if not self._meaning_on():
            return
        index, _ = self._current_index()
        self._queue_stale(index, 0.0)
        for meeting_id in self.pending():
            self._index_one(meeting_id, index, self._clock())

    # Reloads the meeting from disk (the live copy changes every few seconds) and updates its index.
    # A failed model call keeps the meeting queued and rests the indexer; a deleted meeting loses its index.
    # A timeout (a long meeting on a slow, CPU-only model) is tried once more in small batches at once.
    def _index_one(self, meeting_id: str, index: MeetingIndex, now: float) -> None:
        try:
            meeting = self.store.load(meeting_id)
        except FileNotFoundError:
            index.remove(meeting_id)
            self._finish(meeting_id)
            return
        except Exception as e:
            logger.warning(f'Meeting search could not read {meeting_id}: {e}')
            self._finish(meeting_id)
            return
        try:
            brief = self.store.load_brief(meeting_id)
            try:
                index.update(meeting, brief)
            except TimedOut:
                logger.info(f'Meeting search timed out on {meeting_id}; trying {SMALL_BATCH} pieces at a time')
                self._batch.size = SMALL_BATCH
                try:
                    index.update(meeting, brief)
                finally:
                    self._batch.size = None
        except Exception as e:
            self._fail(meeting_id, e, now)
            return
        with self._lock:
            if self._index is not index:                  # the model changed meanwhile: this file is for the old one
                self._pending[meeting_id] = now
                return
            if self._pending.get(meeting_id, 0.0) <= now:     # not changed again while it was being embedded
                self._pending.pop(meeting_id, None)
            self._attempts.pop(meeting_id, None)
            recovered = self._problem is not None
        if recovered:
            self._clear_problem()
        self._publish_status(force=recovered)

    def _finish(self, meeting_id: str) -> None:
        with self._lock:
            self._pending.pop(meeting_id, None)
        self._publish_status()

    # An update failed. A model problem (Ollama off, model missing, busy, a timeout) is remembered as
    # the current problem and rests the whole indexer for RETRY_S. Any other error is this meeting's own
    # (a broken file, a locked index, input Ollama refused) and is logged once. The meeting goes to the
    # back of the queue, so one bad meeting never holds up the ones behind it, and is given up after
    # MAX_ATTEMPTS tries (until it changes).
    def _fail(self, meeting_id: str, error: Exception, now: float) -> None:
        # Ollama off, busy or the model missing says nothing about this meeting, so it isn't a try. A
        # timeout even in small batches is: this meeting is too much for the model, others may not be.
        kind = failure_kind(error)
        counts = kind in ('own', 'timeout')
        if kind == 'own':
            self._log_once(f'Meeting search could not index {meeting_id}: {error}', error)
        else:
            self._note_failure(error)
            with self._lock:
                self._blocked_until = now + RETRY_S
        with self._lock:
            tries = self._attempts.get(meeting_id, 0) + (1 if counts else 0)
            self._attempts[meeting_id] = tries
            if tries >= MAX_ATTEMPTS:
                self._pending.pop(meeting_id, None)
                logger.warning(f'Meeting search gave up indexing {meeting_id} after {tries} tries')
                if isinstance(error, TimedOut) and self._problem == 'loading':
                    self._problem = None                  # it was this meeting, not a model still starting
            else:
                self._pending[meeting_id] = now + RETRY_S
        self._publish_status()

    # Logs a warning once per distinct error text.
    def _log_once(self, message: str, error: Exception) -> None:
        with self._lock:
            first = str(error) not in self._logged
            self._logged.add(str(error))
        if first:
            logger.warning(message)

    # Records a model failure as the current problem (shown on the Meetings screen) and logs it once.
    # Ollama off or the model missing also rests searches for RETRY_S: each would wait for the same refusal.
    # An error of the call's own input is only logged: it is no problem of the model.
    def _note_failure(self, error: Exception) -> Optional[str]:
        problem = problem_of(error)
        if problem is None:
            self._log_once(f'Meeting search model call failed: {error}', error)
            return None
        with self._lock:
            self._problem = problem
            if problem in BLOCKING:
                self._blocked_until = max(self._blocked_until, self._clock() + RETRY_S)
        self._log_once(f'Meeting search model call failed ({problem}): {error}', error)
        return problem

    # ── Search ───────────────────────────────────────────────────────────────

    # Words always; meaning when it is on and the model answers in time. `meaning` tells the page why
    # a meaning part is missing: off, loading, no_ollama, no_model; or indexing/on when it is there.
    def search(self, query: str, meeting_id: Optional[str] = None) -> dict:
        word_hits = self.store.search(query, meeting_id=meeting_id)
        meaning_hits, state = self._meaning(query, meeting_id, QUERY_WAIT_S)
        return {'results': merge(word_hits, meaning_hits, places=10 if meeting_id else 3), 'meaning': state}

    # The meaning pieces (best first, at most `per_meeting` from one meeting and `total` overall) that
    # answer a question, for the Ask feature. [] when meaning search isn't available.
    def hits_for(self, question: str, meeting_id: Optional[str], per_meeting: int, total: int) -> list:
        hits, _state = self._meaning(question, meeting_id, ASK_WAIT_S)
        return cap(hits, per_meeting, total)

    # (pieces, state) for a question. The model call runs on a thread so the caller waits at most
    # `wait` seconds; a slow call goes on in the background and warms the model for the next try.
    def _meaning(self, question: str, meeting_id: Optional[str], wait: float):
        if not self._meaning_on():
            return [], 'off'
        index, _switched = self._current_index()
        vector, problem = self._query_vector(index, question, wait)
        if problem:
            self._publish_status()
            return [], problem
        hits = index.search(vector, meeting_id)
        with self._lock:
            recovered = self._problem is not None
        if recovered:
            self._clear_problem()
            self._publish_status()
        return hits, 'indexing' if self._indexing() else 'on'

    # The question's vector, or (None, reason) when it isn't there: 'loading' after `wait` seconds, or the
    # problem of a failed call. Only one model call is ever in flight: a search arriving meanwhile waits on
    # it for what remains of `wait` instead of starting another, and a call for another question is
    # waited out before this one's own starts. A call that fails after its search gave up records the
    # problem (so the status shows it) and the next search reports it. While Ollama is known to be off or
    # the model missing, searches answer that at once until the rest is over (RETRY_S), then try again.
    def _query_vector(self, index: MeetingIndex, question: str, wait: float):
        text = index.query_text(question)
        key = (index.model, text)
        deadline = time.monotonic() + wait
        while True:
            with self._lock:
                if self._problem in BLOCKING and self._clock() < self._blocked_until:
                    if self._flight is not None and self._flight.done.is_set() and self._flight.error is not None:
                        self._flight.consumed = True      # its error is the problem reported here
                    return None, self._problem
                flight = self._flight
                if flight is not None and flight.done.is_set():
                    if flight.error is not None:
                        if not flight.consumed:
                            flight.consumed = True
                            return None, _search_state(flight.error)
                        flight = None
                    elif flight.key != key:
                        flight = None                     # another question's vector is of no use
                if flight is None:
                    flight = self._flight = _QueryFlight(key)
                    threading.Thread(target=self._run_query, args=(flight, index.model, text), daemon=True,
                                     name='meeting-search-query').start()
            if not flight.done.wait(max(0.0, deadline - time.monotonic())):
                return None, 'loading'
            if flight.error is not None:
                with self._lock:
                    flight.consumed = True
                return None, _search_state(flight.error)
            if flight.key == key:
                return flight.vector, None

    # The thread of one query embedding: keeps the vector or the error, and notes a failure at once.
    def _run_query(self, flight: _QueryFlight, model: str, text: str) -> None:
        try:
            flight.vector = self._embed(model, [text])[0]
        except Exception as e:
            flight.error = e
            self._note_failure(e)
            self._publish_status()
        finally:
            flight.done.set()

    # The index is catching up: meetings are queued and the indexer isn't held back by a live meeting.
    # Meetings that already failed are not waited for, so the status settles when one can't be indexed.
    def _indexing(self) -> bool:
        with self._lock:
            waiting = any(not self._attempts.get(meeting_id) for meeting_id in self._pending)
            return waiting and not self._is_running()

    # ── Ask all meetings ─────────────────────────────────────────────────────

    def _ask_file(self) -> Path:
        if self._ask_path is None:
            from ..utils import get_user_app_data_path
            self._ask_path = Path(get_user_app_data_path()) / ASK_FILE
        return self._ask_path

    # The saved conversation, oldest first; [] when there is none or the file is unreadable.
    def ask_history(self) -> list:
        try:
            data = json.loads(self._ask_file().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def ask_clear(self) -> None:
        with self._ask_lock:
            self._ask_file().unlink(missing_ok=True)

    # Written to a temp file first, so a crash mid-write never loses the conversation.
    def _ask_save(self, messages: list) -> None:
        path = self._ask_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, path)

    # Asks the AI about all meetings and keeps the question and answer; returns {'messages': the conversation}.
    # Raises ValueError when no AI model is chosen, SummaryError when the AI fails.
    def ask(self, question: str) -> dict:
        ready = self._ai_ready() if self._ai_ready is not None else self._complete is not None
        if not ready or self._complete is None:
            raise ValueError("Choose an AI model in Settings › AI first")
        states: list = []                                 # what each meaning search said, to tell if it was unavailable

        def hits_for(text: str, meeting_id: Optional[str], per_meeting: int, total: int) -> list:
            hits, state = self._meaning(text, meeting_id, ASK_WAIT_S)
            states.append(state)
            return cap(hits, per_meeting, total)

        def unavailable() -> bool:
            return any(state in ('off', 'loading', 'no_ollama', 'no_model') for state in states)

        answer = meeting_ask.ask(question, self.ask_history(), self.store.list(), self.store.load, hits_for,
                                 self._complete, self._parse or json.loads, unavailable)
        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        with self._ask_lock:                              # read again: another question may have been saved meanwhile
            history = self.ask_history() + [{'role': 'user', 'text': str(question).strip(), 'at': now},
                                            {'role': 'assistant', 'text': answer['reply'], 'at': now,
                                             'refs': answer['refs']}]
            self._ask_save(history)
        return {'messages': history}

    # ── Status ───────────────────────────────────────────────────────────────

    # What the Meetings screen shows: whether meaning search works, how many meetings are indexed.
    def status(self) -> dict:
        model = self._model()
        ids = self._meeting_ids()
        with self._lock:
            index = self._index if self._index is not None and self._index.model == model else None
            problem = self._problem
        indexed = sum(1 for meeting_id in ids
                      if index is not None and index.is_current(meeting_id, self._changed_at(meeting_id)))
        if not self._meaning_on():
            meaning = 'off'
        elif problem:
            meaning = problem
        elif indexed < len(ids) and self._indexing():
            meaning = 'indexing'
        else:
            meaning = 'on'
        return {'meaning': meaning, 'indexed': indexed, 'total': len(ids), 'model': model}

    # Publishes the status as the 'meeting_search' event when it differs from the last one sent.
    def _publish_status(self, force: bool = False) -> None:
        try:
            status = self.status()
        except Exception as e:
            logger.debug(f'Meeting search status failed: {e}')
            return
        with self._lock:
            if status == self._last_status and not force:
                return
            self._last_status = status
        self._publish('meeting_search', status)

    # ── Downloading the model ────────────────────────────────────────────────

    # Pulls the model through Ollama as a background task with progress; every meeting is then indexed.
    # Returns the task's id. Progress never goes backward: Ollama reports 0 between layers.
    def download(self) -> int:
        from ..llm.transports import ollama as ollama_wire
        tasks = self._tasks
        if tasks is None:
            from ..ui.tasks import get_task_manager
            tasks = get_task_manager()
        model = self._model()
        address = str(self._setting('ai.ollama.address', DEFAULT_ADDRESS) or DEFAULT_ADDRESS)

        def pull_model(reporter):
            best = [0.0]

            def on_progress(fraction, status):
                reporter.check_cancelled()
                best[0] = max(best[0], float(fraction))
                reporter.report(best[0], status)
            ollama_wire.pull(address, model, on_progress)
            self._clear_problem()
            with self._lock:
                self._attempts.clear()                    # a fresh model: every meeting gets new tries
            self._queue_all(self._clock())
            self._publish_status(force=True)

        # Its own ref, so the Meetings screen shows this download and not a Whisper model's.
        return tasks.run('Downloading the meaning model', pull_model, kind='model', ref='meeting-search').id
