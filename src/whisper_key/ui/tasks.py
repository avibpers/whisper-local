# ui/tasks.py
# Background jobs with progress: model downloads and switches, GPU installs,
# microphone restarts, self-test. Each job runs on its own thread and reports
# through the event bus, so the title-bar chip, the floating bar and the row
# that started it can all show the same progress without polling.

import itertools
import logging
import threading
import time
from typing import Callable, Optional

from .events import publish

logger = logging.getLogger(__name__)


class TaskCancelled(Exception):
    pass


class Task:
    def __init__(self, task_id: int, label: str, kind: str, ref: Optional[str] = None):
        self.id = task_id
        self.label = label
        self.kind = kind
        self.ref = ref                          # what it is about, e.g. the model key being downloaded
        self.progress: Optional[float] = None   # 0..1, or None when unknown
        self.status = 'running'                 # running | done | failed | cancelled
        self.detail = ''
        self.error = ''
        self.result = None
        self.started = time.time()
        self.finished: Optional[float] = None
        self._cancel = threading.Event()

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'label': self.label, 'kind': self.kind, 'ref': self.ref,
            'progress': self.progress, 'status': self.status,
            'detail': self.detail, 'error': self.error,
            'started': self.started, 'finished': self.finished,
        }

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


# Passed to every job. Jobs call report() as they go and check cancelled
# between steps; raising TaskCancelled ends the job cleanly.
class TaskReporter:
    def __init__(self, task: Task, manager: 'TaskManager'):
        self._task = task
        self._manager = manager

    def report(self, progress: Optional[float] = None, detail: Optional[str] = None):
        if progress is not None:
            self._task.progress = max(0.0, min(1.0, float(progress)))
        if detail is not None:
            self._task.detail = str(detail)
        self._manager._emit(self._task)

    @property
    def cancelled(self) -> bool:
        return self._task.cancelled

    def check_cancelled(self):
        if self._task.cancelled:
            raise TaskCancelled()


class TaskManager:
    KEEP_FINISHED = 20

    def __init__(self):
        self._tasks = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # Start `fn(reporter)` on a thread. `kind` lets callers find a running job
    # of the same sort (one model switch at a time). Returns the Task.
    def run(self, label: str, fn: Callable[[TaskReporter], object], kind: str = 'general',
            on_done: Optional[Callable[[Task], None]] = None, ref: Optional[str] = None) -> Task:
        task = Task(next(self._ids), label, kind, ref)
        with self._lock:
            self._tasks[task.id] = task
            self._trim()
        self._emit(task)

        def runner():
            reporter = TaskReporter(task, self)
            try:
                task.result = fn(reporter)
                task.status = 'cancelled' if task.cancelled else 'done'
                if task.status == 'done':
                    task.progress = 1.0
            except TaskCancelled:
                task.status = 'cancelled'
            except Exception as e:
                task.status = 'failed'
                task.error = str(e) or e.__class__.__name__
                logger.error(f"Task '{label}' failed: {e}", exc_info=True)
            task.finished = time.time()
            self._emit(task)
            if on_done:
                try:
                    on_done(task)
                except Exception as e:
                    logger.debug(f"Task completion callback failed: {e}")

        threading.Thread(target=runner, daemon=True, name=f'task-{task.id}').start()
        return task

    def cancel(self, task_id: int) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status != 'running':
            return False
        task._cancel.set()
        return True

    def get(self, task_id: int) -> Optional[Task]:
        return self._tasks.get(task_id)

    def running(self, kind: Optional[str] = None) -> list:
        with self._lock:
            return [t for t in self._tasks.values()
                    if t.status == 'running' and (kind is None or t.kind == kind)]

    def list(self) -> list:
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    def _emit(self, task: Task):
        publish('task', task.to_dict())

    def _trim(self):
        finished = [t for t in self._tasks.values() if t.status != 'running']
        for t in sorted(finished, key=lambda t: t.started)[:-self.KEEP_FINISHED]:
            self._tasks.pop(t.id, None)


_manager = TaskManager()


def get_task_manager() -> TaskManager:
    return _manager
