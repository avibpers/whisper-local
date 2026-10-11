# services/rules.py
# The Apps & profiles screen: per-app rules (ordered, first match wins) and
# profiles (named sets of settings). Rules are edited through defaults_merge's
# write API; activating a profile goes through the settings service so every
# value applies exactly like a change made by hand, with one Undo.

import re
from pathlib import Path

from .. import app_rules as ar
from .. import defaults_merge as dm
from .. import profiles as pf
from ..utils import get_user_app_data_path

RULE_KEYS = ('match', 'window_title', 'auto_send', 'auto_send_delay', 'auto_paste', 'suppress', 'initial_prompt',
             'language', 'task', 'style', 'auto_dictation') + ar.FORMATTING_KEYS


def _rules_path() -> Path:
    return Path(get_user_app_data_path()) / ar.USER_FILE


def _profiles_path() -> Path:
    return Path(get_user_app_data_path()) / pf.PROFILES_FILE


def list_rules() -> list:
    return dm.layered_view(ar.DEFAULTS_FILE, _rules_path(), 'rules', ar.USER_FILE_HEADER, id_key='id')


def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')[:40] or 'rule'


# A list of match strings from the page: a comma-separated string or a list,
# trimmed, with blanks dropped.
def _clean_list(value) -> list:
    if isinstance(value, str):
        value = value.split(',')
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def save_rule(entry: dict, original_id: str = None) -> str:
    match = _clean_list(entry.get('match'))
    if not match:
        raise ValueError("Add at least one program name or window title")
    clean = {}
    if entry.get('id'):
        clean['id'] = str(entry['id'])
    elif not original_id:
        clean['id'] = 'my-' + _slug(match[0].replace('.exe', ''))
    elif not original_id.startswith(dm.OWN_PREFIX):
        clean['id'] = original_id
    clean['match'] = match
    window_title = _clean_list(entry.get('window_title'))
    if window_title:
        clean['window_title'] = window_title
    if entry.get('name'):
        clean['name'] = str(entry['name'])
    for key in RULE_KEYS:
        if key in ('match', 'window_title'):
            continue
        value = entry.get(key)
        if value in (None, ''):
            continue
        if key == 'auto_send_delay':
            # Same seconds and limits as the main "Wait before sending".
            from ..settings import schema
            value = schema.coerce(schema.get('clipboard.auto_send_delay'), value)
        clean[key] = value
    return dm.save_entry(ar.DEFAULTS_FILE, _rules_path(), 'rules', ar.USER_FILE_HEADER,
                         clean, id_key='id', original_id=original_id)


def set_rule_enabled(ident: str, enabled: bool):
    dm.set_entry_disabled(ar.DEFAULTS_FILE, _rules_path(), 'rules', ar.USER_FILE_HEADER,
                          ident, not enabled, id_key='id')


def delete_rule(ident: str):
    dm.delete_entry(ar.DEFAULTS_FILE, _rules_path(), 'rules', ar.USER_FILE_HEADER, ident, id_key='id')


def reorder_rules(ordered_ids: list):
    dm.reorder_own(ar.DEFAULTS_FILE, _rules_path(), 'rules', ar.USER_FILE_HEADER, ordered_ids, id_key='id')


# Which rule (if any) would apply to an app.
def matching_rule(rules: list, exe: str, title: str):
    for item in rules:
        if not item.get('disabled') and ar.rule_matches(item['entry'], exe, title):
            return item['id']
    return None


# ── Profiles ─────────────────────────────────────────────────────────────────

def list_profiles(manager) -> dict:
    view = dm.layered_view(pf.PROFILES_DEFAULTS, _profiles_path(), 'profiles', pf.USER_FILE_HEADER,
                           mapping=True)
    return {'profiles': view, 'active': manager.get_active() if manager else None}


def _flatten(prefix: str, value, out: dict):
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    else:
        out[prefix] = value


# The settings a profile changes, as dotted paths. Map-valued settings (a whole
# models map, say) are taken as one value.
def profile_patch(entry: dict) -> dict:
    from ..settings import schema
    patch = {}
    for section, values in (entry.get('overrides') or {}).items():
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            path = f"{section}.{key}"
            if isinstance(value, dict) and path not in schema.MAP_PATHS and schema.get(path) is None:
                _flatten(path, value, patch)
            else:
                patch[path] = value
    return {p: v for p, v in patch.items() if schema.get(p) is not None}


def activate_profile(settings, manager, name: str) -> dict:
    view = {p['id']: p for p in list_profiles(manager)['profiles']}
    if name not in view:
        raise ValueError(f"No profile named {name}")
    result = settings.set_many(profile_patch(view[name]['entry']), label=f"Profile: {name}")
    manager.profiles = {k: v['entry'] for k, v in view.items() if not v['disabled']}
    manager.active = name
    manager._persist_active()
    return result


def save_profile(entry: dict, name: str, original_id: str = None) -> str:
    name = _slug(name).replace('-', '_') if name else ''
    if not name:
        raise ValueError("A profile needs a name")
    clean = {'description': str(entry.get('description', '')), 'overrides': entry.get('overrides') or {}}
    return dm.save_entry(pf.PROFILES_DEFAULTS, _profiles_path(), 'profiles', pf.USER_FILE_HEADER,
                         clean, mapping=True, name=name, original_id=original_id)


def delete_profile(name: str):
    dm.delete_entry(pf.PROFILES_DEFAULTS, _profiles_path(), 'profiles', pf.USER_FILE_HEADER, name,
                    mapping=True)


def set_profile_enabled(name: str, enabled: bool):
    dm.set_entry_disabled(pf.PROFILES_DEFAULTS, _profiles_path(), 'profiles', pf.USER_FILE_HEADER,
                          name, not enabled, mapping=True)
