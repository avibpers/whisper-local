# services/commands.py
# The Commands & AI screen: voice commands and AI transforms. Both live in
# layered files (shipped entries plus the user's overrides), edited one entry
# at a time through defaults_merge's write API. The running app picks up
# changes on its own (both files hot-reload); transforms also re-register
# their shortcuts.

from pathlib import Path

from .. import defaults_merge as dm
from .. import transforms as tf
from .. import voice_commands as vc
from ..utils import get_user_app_data_path

ACTION_KEYS = ('hotkey', 'type', 'run', 'rephrase')


def _commands_path() -> Path:
    return Path(get_user_app_data_path()) / 'commands.yaml'


def _transforms_path() -> Path:
    return Path(get_user_app_data_path()) / tf.USER_FILE


def _action_of(entry: dict) -> str:
    for key in ACTION_KEYS:
        if entry.get(key) is not None:
            return key
    return ''


# One action as saved: the action itself, plus where its keys go when it
# presses keys and isn't aimed at the app in front ("windows" or an .exe).
def _clean_action(source: dict, action: str) -> dict:
    clean = {action: source[action]}
    send_to = str(source.get('send_to') or '').strip().lower()
    if action == 'hotkey' and send_to:
        clean['send_to'] = send_to
    return clean


# `{n}` (a spoken number) appears at most once in the phrase, and keys or text
# can only use it when the phrase has it.
def _check_number_placeholder(trigger: str, actions: list):
    count = trigger.count(vc.NUMBER_PLACEHOLDER)
    if count > 1:
        raise ValueError("Use {n} only once in the phrase")
    uses = any(vc.NUMBER_PLACEHOLDER in str(a.get(f) or '') for a in actions for f in vc.NUMBER_FIELDS)
    if uses and not count:
        raise ValueError("The keys use the number you say, so add {n} to the phrase, like “d {n}”")


def list_commands() -> list:
    view = dm.layered_view(vc.DEFAULTS_FILE, _commands_path(), 'commands', vc.USER_FILE_HEADER,
                           id_key='trigger')
    for item in view:
        item['action'] = _action_of(item['entry'])
    return view


# Validate and save one command. `entry` holds trigger (or match_regex), one
# action (keys may carry send_to) and optional then-steps / confirm.
def save_command(entry: dict, original_id: str = None) -> str:
    entry = {k: v for k, v in dict(entry).items() if v not in (None, '') or k in ACTION_KEYS}
    trigger = str(entry.get('trigger', '')).strip()
    if not trigger and not entry.get('match_regex'):
        raise ValueError("A command needs a trigger phrase")
    if entry.get('match_regex'):
        import re
        try:
            re.compile(entry['match_regex'])
        except re.error as e:
            raise ValueError(f"The pattern isn't valid: {e}")
    actions = [k for k in ACTION_KEYS if entry.get(k) not in (None, '')]
    if len(actions) != 1:
        raise ValueError("Pick exactly one thing for the command to do")
    clean = {'trigger': trigger} if trigger else {}
    if entry.get('match_regex'):
        clean['match_regex'] = entry['match_regex']
    clean.update(_clean_action(entry, actions[0]))
    steps = []
    for step in entry.get('then') or []:
        if not isinstance(step, dict):
            continue
        if step.get('delay') not in (None, ''):
            steps.append({'delay': max(0.0, min(60.0, float(step['delay'])))})
            continue
        step_actions = [k for k in ACTION_KEYS if step.get(k) not in (None, '')]
        if len(step_actions) == 1:
            steps.append(_clean_action(step, step_actions[0]))
    _check_number_placeholder(trigger, [clean] + steps)
    if steps:
        clean['then'] = steps
    if entry.get('confirm') is True:
        clean['confirm'] = True
    return dm.save_entry(vc.DEFAULTS_FILE, _commands_path(), 'commands', vc.USER_FILE_HEADER,
                         clean, id_key='trigger', original_id=original_id)


def set_command_enabled(ident: str, enabled: bool):
    dm.set_entry_disabled(vc.DEFAULTS_FILE, _commands_path(), 'commands', vc.USER_FILE_HEADER,
                          ident, not enabled, id_key='trigger')


def delete_command(ident: str):
    dm.delete_entry(vc.DEFAULTS_FILE, _commands_path(), 'commands', vc.USER_FILE_HEADER,
                    ident, id_key='trigger')


# Which command a phrase would run, without running it.
def test_phrase(manager, text: str) -> dict:
    if manager is None or not getattr(manager, 'enabled', False) or not hasattr(manager, 'commands'):
        return {'matched': False, 'reason': 'Voice commands are off'}
    command = manager.match_command(text or '')
    if not command:
        return {'matched': False}
    return {'matched': True, 'trigger': command.get('trigger') or command.get('match_regex'),
            'action': _action_of(command), 'value': command.get(_action_of(command))}


# ── Transforms ───────────────────────────────────────────────────────────────

def list_transforms() -> list:
    return dm.layered_view(tf.DEFAULTS_FILE, _transforms_path(), 'transforms', tf.USER_FILE_HEADER,
                           id_key='name')


def save_transform(entry: dict, original_id: str = None) -> str:
    name = str(entry.get('name', '')).strip()
    if not name:
        raise ValueError("A transform needs a name")
    if not str(entry.get('prompt', '')).strip():
        raise ValueError("A transform needs an instruction")
    clean = {'name': name, 'prompt': str(entry['prompt'])}
    for key in ('description', 'hotkey', 'model'):
        if entry.get(key):
            clean[key] = str(entry[key])
    if entry.get('timeout') not in (None, ''):
        clean['timeout'] = float(entry['timeout'])
    return dm.save_entry(tf.DEFAULTS_FILE, _transforms_path(), 'transforms', tf.USER_FILE_HEADER,
                         clean, id_key='name', original_id=original_id)


def set_transform_enabled(ident: str, enabled: bool):
    dm.set_entry_disabled(tf.DEFAULTS_FILE, _transforms_path(), 'transforms', tf.USER_FILE_HEADER,
                          ident, not enabled, id_key='name')


def delete_transform(ident: str):
    dm.delete_entry(tf.DEFAULTS_FILE, _transforms_path(), 'transforms', tf.USER_FILE_HEADER,
                    ident, id_key='name')
