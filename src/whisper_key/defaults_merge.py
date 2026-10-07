# defaults_merge.py
# Layers the shipped defaults under the user's copy of app_rules / commands /
# transforms / profiles, the same split config already uses (config.defaults.yaml
# ships the base, user_settings.yaml holds only overrides).
#
# Before this, each user file was a one-time copy of the defaults, so every
# improvement to a shipped file after someone's first launch reached new
# installs only. The shipped entries now always load from the package, and the
# user file carries just their own entries plus per-id overrides.

import logging
import shutil
from datetime import date
from pathlib import Path

from ruamel.yaml import YAML

from .utils import resolve_asset_path

logger = logging.getLogger(__name__)

# Marks a user file as overrides-only. A file without it is a pre-0.21 full
# copy of the defaults and gets migrated on first load.
FORMAT_KEY = 'format'
CURRENT_FORMAT = 2

# Set on a user entry to drop the shipped entry it names, e.g.
#   - id: chat-apps
#     disabled: true
DISABLED_KEY = 'disabled'


# ── Reading ──────────────────────────────────────────────────────────────────

# Returns None when the file exists but doesn't parse, which the caller must
# tell apart from an empty file: a typo in the user's YAML would otherwise look
# like "no entries" and the migration below would overwrite what they wrote.
def _read_yaml(path: Path):
    try:
        with open(path, encoding='utf-8') as f:
            return YAML().load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Failed to parse {path}: {e}")
        return None


# Public: read one top-level key straight from the shipped file, for callers
# that need a default the user's overrides-only file no longer carries (e.g.
# which profile is active on a fresh install).
def shipped_value(defaults_filename: str, key: str):
    return _read_shipped(defaults_filename, key)


def _read_shipped(defaults_filename: str, section: str):
    path = Path(resolve_asset_path(defaults_filename))
    if not path.exists():
        logger.error(f"Shipped defaults missing: {path}")
        return None
    data = _read_yaml(path)
    if data is None:                      # shipped file itself is corrupt
        return None
    return data.get(section)


# ── Merging ──────────────────────────────────────────────────────────────────

# Shipped entries keep their shipped order, because for app_rules the first
# match wins and the shipped order is deliberate (specific apps before the
# broader title matches). The user's own entries go in front of all of them, so
# a rule someone wrote themselves always beats a shipped one.
def _merge_lists(shipped: list, user: list, id_key: str) -> list:
    shipped_ids = {str(e[id_key]) for e in shipped
                   if isinstance(e, dict) and e.get(id_key) is not None}

    by_id = {}
    for entry in user:
        if isinstance(entry, dict) and str(entry.get(id_key, '')) in shipped_ids:
            by_id[str(entry[id_key])] = entry

    # Anything that doesn't name a shipped entry is the user's own. Their own
    # entries can be switched off too (the app window's enable toggle).
    merged = [e for e in user
              if isinstance(e, dict) and str(e.get(id_key, '')) not in shipped_ids
              and not e.get(DISABLED_KEY)]

    for entry in shipped:
        if not isinstance(entry, dict):
            continue
        override = by_id.get(str(entry.get(id_key, '')))
        if override is None:
            merged.append(dict(entry))
            continue
        if override.get(DISABLED_KEY):
            continue
        combined = dict(entry)
        combined.update({k: v for k, v in override.items() if k != DISABLED_KEY})
        merged.append(combined)
    return merged


# Profiles are a mapping keyed by name, so the key is the id.
def _merge_dicts(shipped: dict, user: dict) -> dict:
    merged = {}
    for name, entry in shipped.items():
        override = user.get(name)
        if isinstance(override, dict) and override.get(DISABLED_KEY):
            continue
        merged[name] = dict(entry) if isinstance(entry, dict) else entry
        if isinstance(override, dict) and isinstance(merged[name], dict):
            merged[name].update({k: v for k, v in override.items() if k != DISABLED_KEY})
    for name, entry in user.items():
        if name not in shipped and not (isinstance(entry, dict) and entry.get(DISABLED_KEY)):
            merged[name] = entry
    return merged


# ── Migration (one time, pre-0.21 files) ─────────────────────────────────────

# Turns an old full copy into overrides-only. An entry identical to the shipped
# one it came from is dropped, since the package now supplies it. An entry that
# was edited becomes {id, <only the keys that differ>}, so keys the user never
# touched keep following the shipped defaults. Anything with no shipped
# counterpart is the user's own and is kept as-is.
def _to_overrides(shipped: list, user: list, id_key: str, identity, rematch=None) -> list:
    shipped_by_identity = {}
    for entry in shipped:
        if isinstance(entry, dict):
            shipped_by_identity[identity(entry)] = entry

    overrides = []
    for entry in user:
        if not isinstance(entry, dict):
            continue
        source = shipped_by_identity.get(identity(entry))
        if source is None and rematch is not None:
            # The user edited the very thing that identifies the entry. It is
            # still an edit of that shipped entry, so `rematch` finds which one:
            # left as a separate entry, the shipped one would sit behind it and
            # quietly keep matching whatever they took out.
            source = rematch(entry, shipped)
        if source is None:
            overrides.append(entry)
            continue
        # A key the user left out is NOT an override: their file predates the
        # key, so it should keep inheriting whatever the shipped entry says.
        diff = {k: v for k, v in entry.items()
                if k != id_key and (k not in source or source[k] != v)}
        if diff:
            overrides.append({id_key: source.get(id_key), **diff})
    return overrides


def _to_override_dict(shipped: dict, user: dict) -> dict:
    overrides = {}
    for name, entry in user.items():
        source = shipped.get(name)
        if source is None:
            overrides[name] = entry
            continue
        if not isinstance(entry, dict) or not isinstance(source, dict):
            continue
        diff = {k: v for k, v in entry.items()
                if k not in source or source[k] != v}
        if diff:
            overrides[name] = diff
    return overrides


def _backup(path: Path) -> Path:
    target = path.with_suffix(path.suffix + f'.{date.today().isoformat()}.bak')
    counter = 2
    while target.exists():
        target = path.with_suffix(path.suffix + f'.{date.today().isoformat()}-{counter}.bak')
        counter += 1
    shutil.copy2(path, target)
    return target


def _write(path: Path, header: str, payload: dict):
    yaml = YAML()
    yaml.default_flow_style = False
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(header)
        yaml.dump(payload, f)
    tmp.replace(path)


# ── Public entry point ───────────────────────────────────────────────────────

# Loads one section, layering the user's file over the shipped defaults, and
# creates or migrates the user file when needed.
#
# `id_key` is the field that names a shipped entry, and it is whatever the file
# already uses as an identity: `trigger` for commands, `name` for transforms, a
# purpose-made `id` for app rules, whose `match` list is too long to retype and
# grows as apps are added. `identity` maps an entry to what identified it before
# ids existed, and is only used when migrating an old full-copy file.
#
# Returns (merged, user_data) so the caller can still read its own top-level
# keys, e.g. profiles' `active`.
def load_layered(defaults_filename: str, user_path: Path, section: str,
                 header: str, id_key: str = 'id', identity=None, rematch=None,
                 mapping: bool = False):
    shipped = _read_shipped(defaults_filename, section)
    if shipped is None:
        # No package defaults (a broken install): fall back to the user file
        # alone rather than losing their entries. `mapping` says which empty
        # container to hand back, since there is no shipped section to ask.
        data = _read_yaml(user_path) or {}
        section_data = data.get(section)
        if section_data is None:
            section_data = {} if mapping else []
        return section_data, data

    is_mapping = isinstance(shipped, dict)

    def shipped_copy():
        return dict(shipped) if is_mapping else [dict(e) for e in shipped]

    if not user_path.exists():
        _write(user_path, header,
               {FORMAT_KEY: CURRENT_FORMAT, section: {} if is_mapping else []})
        return shipped_copy(), {}

    data = _read_yaml(user_path)
    if data is None:
        # Their YAML is broken. Run on the shipped entries so the app still
        # works, and don't rewrite the file they're in the middle of editing.
        logger.error(f"{user_path.name} could not be parsed, so only the shipped "
                     f"entries are in effect. Your file has been left as it is.")
        return shipped_copy(), {}

    user_section = data.get(section)
    if user_section is None:
        user_section = {} if is_mapping else []

    if data.get(FORMAT_KEY) != CURRENT_FORMAT:
        if is_mapping:
            user_section = _to_override_dict(shipped, user_section)
        else:
            user_section = _to_overrides(
                shipped, user_section, id_key,
                identity or (lambda e: str(e.get(id_key, ''))), rematch)
        backup = _backup(user_path)
        payload = {FORMAT_KEY: CURRENT_FORMAT}
        payload.update({k: v for k, v in data.items()
                        if k not in (FORMAT_KEY, section)})
        payload[section] = user_section
        _write(user_path, header, payload)
        data = payload
        logger.info(f"Migrated {user_path.name} to the shipped-defaults format; "
                    f"your previous file is at {backup.name}")

    if is_mapping:
        return _merge_dicts(shipped, user_section if isinstance(user_section, dict) else {}), data
    return _merge_lists(shipped,
                        user_section if isinstance(user_section, list) else [],
                        id_key), data


# ── Writing (the app window's editors) ───────────────────────────────────────
#
# The editors change one entry at a time and keep the overrides-only format:
# an edit to a shipped entry stores only the keys that differ (a key the edit
# removed is stored as null, which is how a user override clears a shipped
# action), "turn off" stores `disabled: true`, and "reset" drops the override.
# The user's own entries are stored whole. Own entries without an id are
# addressed as "#<n>" (their position among the user's own entries).

OWN_PREFIX = '#'


def _load_for_write(defaults_filename: str, user_path: Path, section: str, header: str,
                    id_key: str, mapping: bool):
    # Make sure the file exists and is migrated before editing it.
    load_layered(defaults_filename, user_path, section, header, id_key=id_key, mapping=mapping)
    data = _read_yaml(user_path)
    if data is None:
        raise RuntimeError(f"{user_path.name} has a typo, so it can't be edited here. "
                           f"Fix it in a text editor first.")
    shipped = _read_shipped(defaults_filename, section) or ({} if mapping else [])
    user_section = data.get(section)
    if user_section is None:
        user_section = {} if mapping else []
    return data, shipped, user_section


def _save(user_path: Path, header: str, data: dict, section: str, user_section):
    payload = {FORMAT_KEY: CURRENT_FORMAT}
    payload.update({k: v for k, v in data.items() if k not in (FORMAT_KEY, section)})
    payload[section] = user_section
    _write(user_path, header, payload)


def _plain(obj):
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def _shipped_ids(shipped: list, id_key: str) -> set:
    return {str(e.get(id_key)) for e in shipped if isinstance(e, dict) and e.get(id_key) is not None}


# Every entry as the editors see it: merged values, where it comes from
# ('shipped', 'changed' or 'yours'), and whether it is switched off.
# Switched-off entries are included here, unlike load_layered().
def layered_view(defaults_filename: str, user_path: Path, section: str, header: str,
                 id_key: str = 'id', mapping: bool = False) -> list:
    load_layered(defaults_filename, user_path, section, header, id_key=id_key, mapping=mapping)
    data = _read_yaml(user_path) or {}
    shipped = _read_shipped(defaults_filename, section) or ({} if mapping else [])
    user_section = data.get(section) or ({} if mapping else [])
    out = []
    if mapping:
        user_map = user_section if isinstance(user_section, dict) else {}
        for name, entry in shipped.items():
            override = user_map.get(name)
            merged = dict(entry) if isinstance(entry, dict) else {}
            disabled = False
            changed = False
            if isinstance(override, dict):
                disabled = bool(override.get(DISABLED_KEY))
                changed = any(k != DISABLED_KEY for k in override)
                merged.update({k: v for k, v in override.items() if k != DISABLED_KEY})
            out.append({'id': name, 'origin': 'changed' if changed else 'shipped',
                        'disabled': disabled, 'entry': _plain(merged)})
        for name, entry in user_map.items():
            if name in shipped:
                continue
            e = dict(entry) if isinstance(entry, dict) else {}
            disabled = bool(e.pop(DISABLED_KEY, False))
            out.append({'id': name, 'origin': 'yours', 'disabled': disabled, 'entry': _plain(e)})
        return out

    user_list = user_section if isinstance(user_section, list) else []
    shipped_ids = _shipped_ids(shipped, id_key)
    overrides = {str(e.get(id_key)): e for e in user_list
                 if isinstance(e, dict) and str(e.get(id_key, '')) in shipped_ids}
    own_index = 0
    for e in user_list:
        if not isinstance(e, dict) or str(e.get(id_key, '')) in shipped_ids:
            continue
        entry = {k: v for k, v in e.items() if k != DISABLED_KEY}
        ident = str(e.get(id_key)) if e.get(id_key) not in (None, '') else f"{OWN_PREFIX}{own_index}"
        out.append({'id': ident, 'origin': 'yours', 'disabled': bool(e.get(DISABLED_KEY)),
                    'entry': _plain(entry)})
        own_index += 1
    for e in shipped:
        if not isinstance(e, dict):
            continue
        ident = str(e.get(id_key))
        override = overrides.get(ident)
        merged = dict(e)
        disabled = False
        changed = False
        if override:
            disabled = bool(override.get(DISABLED_KEY))
            changed = any(k not in (id_key, DISABLED_KEY) for k in override)
            merged.update({k: v for k, v in override.items() if k != DISABLED_KEY})
        out.append({'id': ident, 'origin': 'changed' if changed else 'shipped',
                    'disabled': disabled, 'entry': _plain(merged)})
    return out


def _find_own(user_list: list, ident: str, id_key: str, shipped_ids: set) -> int:
    own = [i for i, e in enumerate(user_list)
           if isinstance(e, dict) and str(e.get(id_key, '')) not in shipped_ids]
    if ident.startswith(OWN_PREFIX) and ident[1:].isdigit():
        n = int(ident[1:])
        return own[n] if n < len(own) else -1
    for i in own:
        if str(user_list[i].get(id_key, '')) == ident:
            return i
    return -1


# Create or update one entry. `original_id` names the entry being edited (it
# differs from the new id when the user renamed it). For mappings (profiles)
# `name` is the entry's key. Returns the entry's id.
def save_entry(defaults_filename: str, user_path: Path, section: str, header: str,
               entry: dict, id_key: str = 'id', original_id: str = None, mapping: bool = False,
               name: str = None) -> str:
    data, shipped, user_section = _load_for_write(defaults_filename, user_path, section, header,
                                                  id_key, mapping)
    entry = {k: v for k, v in dict(entry).items() if k != DISABLED_KEY}

    if mapping:
        if not isinstance(user_section, dict):
            user_section = {}
        new_name = name or original_id
        if not new_name:
            raise RuntimeError("A name is needed")
        if original_id and original_id != new_name:
            user_section.pop(original_id, None)
            if original_id in shipped:
                user_section[original_id] = {DISABLED_KEY: True}
        source = shipped.get(new_name)
        if isinstance(source, dict):
            diff = {k: v for k, v in entry.items() if source.get(k) != v}
            if diff:
                user_section[new_name] = diff
            else:
                user_section.pop(new_name, None)
        else:
            user_section[new_name] = entry
        _save(user_path, header, data, section, user_section)
        return new_name

    if not isinstance(user_section, list):
        user_section = []
    shipped_by_id = {str(e.get(id_key)): e for e in shipped if isinstance(e, dict) and e.get(id_key) is not None}
    shipped_ids = set(shipped_by_id)
    new_id = entry.get(id_key)
    new_id = str(new_id) if new_id not in (None, '') else None

    # Renaming: the old own entry goes away; a renamed shipped entry is turned
    # off and the edit becomes the user's own entry.
    insert_at = 0
    if original_id and original_id != new_id:
        idx = _find_own(user_section, original_id, id_key, shipped_ids)
        if idx >= 0:
            insert_at = idx
            user_section.pop(idx)
        elif original_id in shipped_ids:
            user_section[:] = [e for e in user_section
                               if not (isinstance(e, dict) and str(e.get(id_key, '')) == original_id)]
            user_section.append({id_key: original_id, DISABLED_KEY: True})

    if new_id and new_id in shipped_ids:
        source = shipped_by_id[new_id]
        diff = {k: v for k, v in entry.items() if k != id_key and source.get(k) != v}
        diff.update({k: None for k in source if k not in entry and k != id_key})
        user_section[:] = [e for e in user_section
                           if not (isinstance(e, dict) and str(e.get(id_key, '')) == new_id)]
        if diff:
            user_section.append({id_key: new_id, **diff})
        _save(user_path, header, data, section, user_section)
        return new_id

    if new_id:
        idx = _find_own(user_section, new_id, id_key, shipped_ids)
        if idx >= 0:
            user_section[idx] = entry
            _save(user_path, header, data, section, user_section)
            return new_id
    user_section.insert(min(insert_at, len(user_section)), entry)
    _save(user_path, header, data, section, user_section)
    return new_id or f"{OWN_PREFIX}{insert_at}"


def set_entry_disabled(defaults_filename: str, user_path: Path, section: str, header: str,
                       ident: str, disabled: bool, id_key: str = 'id', mapping: bool = False):
    data, shipped, user_section = _load_for_write(defaults_filename, user_path, section, header,
                                                  id_key, mapping)
    if mapping:
        if not isinstance(user_section, dict):
            user_section = {}
        entry = dict(user_section.get(ident) or {})
        if disabled:
            entry[DISABLED_KEY] = True
        else:
            entry.pop(DISABLED_KEY, None)
        if entry or ident not in shipped:
            user_section[ident] = entry
        else:
            user_section.pop(ident, None)
        _save(user_path, header, data, section, user_section)
        return

    shipped_ids = _shipped_ids(shipped, id_key)
    if ident in shipped_ids:
        found = None
        for e in user_section:
            if isinstance(e, dict) and str(e.get(id_key, '')) == ident:
                found = e
        if disabled:
            if found is None:
                user_section.append({id_key: ident, DISABLED_KEY: True})
            else:
                found[DISABLED_KEY] = True
        elif found is not None:
            found.pop(DISABLED_KEY, None)
            if set(found) <= {id_key}:
                user_section.remove(found)
    else:
        idx = _find_own(user_section, ident, id_key, shipped_ids)
        if idx < 0:
            raise RuntimeError("That entry no longer exists")
        if disabled:
            user_section[idx][DISABLED_KEY] = True
        else:
            user_section[idx].pop(DISABLED_KEY, None)
    _save(user_path, header, data, section, user_section)


# Delete the user's own entry, or reset a shipped one to how it ships.
def delete_entry(defaults_filename: str, user_path: Path, section: str, header: str,
                 ident: str, id_key: str = 'id', mapping: bool = False):
    data, shipped, user_section = _load_for_write(defaults_filename, user_path, section, header,
                                                  id_key, mapping)
    if mapping:
        if isinstance(user_section, dict):
            user_section.pop(ident, None)
    else:
        shipped_ids = _shipped_ids(shipped, id_key)
        if ident in shipped_ids:
            user_section[:] = [e for e in user_section
                               if not (isinstance(e, dict) and str(e.get(id_key, '')) == ident)]
        else:
            idx = _find_own(user_section, ident, id_key, shipped_ids)
            if idx >= 0:
                user_section.pop(idx)
    _save(user_path, header, data, section, user_section)


# Put the user's own entries in a new order (for app rules, where order decides
# which rule wins). Shipped entries always come after the user's own.
def reorder_own(defaults_filename: str, user_path: Path, section: str, header: str,
                ordered_ids: list, id_key: str = 'id'):
    data, shipped, user_section = _load_for_write(defaults_filename, user_path, section, header,
                                                  id_key, False)
    shipped_ids = _shipped_ids(shipped, id_key)
    own_positions = [i for i, e in enumerate(user_section)
                     if isinstance(e, dict) and str(e.get(id_key, '')) not in shipped_ids]
    keyed = {}
    for n, i in enumerate(own_positions):
        e = user_section[i]
        ident = str(e.get(id_key)) if e.get(id_key) not in (None, '') else f"{OWN_PREFIX}{n}"
        keyed[ident] = e
    reordered = [keyed[i] for i in ordered_ids if i in keyed]
    reordered += [e for i, e in keyed.items() if i not in ordered_ids]
    rest = [e for i, e in enumerate(user_section) if i not in own_positions]
    _save(user_path, header, data, section, reordered + rest)
