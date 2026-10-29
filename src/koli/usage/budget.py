# usage/budget.py
# A monthly limit on what the online models may cost (usage.monthly_budget,
# 0 = no limit). At 80% Koli says so once; at 100% it stops sending anything
# to a paid model until the month ends: dictation and meetings switch to the
# local model of usage.over_budget_model, or stop with "budget used up" when
# none is set, and AI features on a paid key say the budget is reached.
# Subscriptions and local models are never limited. Listeners hear when the
# month goes over or comes back under (a new month), so the local engine can
# be swapped. Design: docs/design/online-models.md.

import datetime
import logging
import threading
from typing import Callable

from . import ledger

logger = logging.getLogger(__name__)

WARN_AT = 0.8

_setting: Callable = lambda path, default=None: default
_lock = threading.Lock()
_state = {'over': None, 'warned': '', 'reported': ''}
_listeners: list = []


# Called at startup with the ConfigManager, so the limit is read fresh.
def configure(config_manager) -> None:
    global _setting

    def read(path, default=None):
        value = config_manager.get_path(path, default)
        return default if value is None else value
    _setting = read


def limit() -> float:
    try:
        return max(0.0, float(_setting('usage.monthly_budget', 0) or 0))
    except (TypeError, ValueError):
        return 0.0


# The local model that takes over when the budget is used up; '' stops.
def fallback_model() -> str:
    return str(_setting('usage.over_budget_model', '') or '').strip()


def month_cost() -> float:
    return ledger.month_cost()


def over() -> bool:
    cap = limit()
    return cap > 0 and month_cost() >= cap


# The words shown when the budget stops a call.
def used_up_message() -> str:
    month = datetime.date.today().strftime('%B')
    return f"This month's online budget (${limit():g}) is used up until {month} ends"


# {limit, spent, over, fallback} for the screens.
def status() -> dict:
    cap = limit()
    spent = month_cost()
    return {'limit': cap, 'spent': round(spent, 4), 'over': cap > 0 and spent >= cap,
            'share': round(spent / cap, 3) if cap else None, 'fallback': fallback_model()}


def on_change(fn: Callable[[bool], None]) -> None:
    _listeners.append(fn)


# Called after every recorded call and before each dictation: warns at 80%,
# reports 100% once a month, and tells listeners when "over" flips (it flips
# back by itself when a new month starts).
def check() -> bool:
    cap = limit()
    spent = month_cost() if cap else 0.0
    now_over = cap > 0 and spent >= cap
    month = datetime.date.today().strftime('%Y-%m')
    with _lock:
        flipped = _state['over'] is not None and _state['over'] != now_over
        first = _state['over'] is None
        _state['over'] = now_over
        warn = cap > 0 and not now_over and spent >= cap * WARN_AT and _state['warned'] != month
        if warn:
            _state['warned'] = month
        report = now_over and _state['reported'] != month
        if report:
            _state['reported'] = month
    if warn:
        _problem(f"Online models have cost ${spent:.2f} of this month's ${cap:g} budget")
    if report:
        fallback = fallback_model()
        _problem(f"{used_up_message()}. " + (f"Dictation uses {fallback} on this computer until then."
                                             if fallback else "Online dictation is stopped until then."))
    if flipped and not first:
        logger.info(f"Online budget {'used up' if now_over else 'available again'}")
        for fn in list(_listeners):
            try:
                fn(now_over)
            except Exception as e:
                logger.warning(f"Budget listener failed: {e}")
    return now_over


def _problem(message: str) -> None:
    try:
        from ..ui.events import report_problem
        report_problem(message)
    except Exception:
        logger.warning(message)
