"""make_mock_data.py
Writes src/whisper_key/ui/web/js/mock-data.json: the default settings, the
settings schema, and the shipped rules, commands, transforms and profiles, so
the browser mock (index.html?mock=1) shows the same shapes as the real app.
Run it after changing config.defaults.yaml or the settings schema."""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import yaml  # noqa: E402

from whisper_key import app_rules, profiles, transforms, voice_commands  # noqa: E402
from whisper_key import defaults_merge as dm  # noqa: E402
from whisper_key.config_manager import _resolve_platform_values  # noqa: E402
from whisper_key.services.commands import _action_of  # noqa: E402
from whisper_key.settings import schema, simple_mode  # noqa: E402

OUT = ROOT / 'src' / 'whisper_key' / 'ui' / 'web' / 'js' / 'mock-data.json'


def shipped(defaults_file, section, id_key=None, mapping=False):
    """The shipped entries only, in the layered-view shape. The user file is a
    throwaway one in a temp folder, since a missing one gets created."""
    with tempfile.TemporaryDirectory() as tmp:
        user = Path(tmp) / f'{section}.yaml'
        if mapping:
            return dm.layered_view(defaults_file, user, section, '', mapping=True)
        return dm.layered_view(defaults_file, user, section, '', id_key=id_key)


def main():
    # Resolved for Windows, as ConfigManager does ("ctrl+win | macos: fn+ctrl").
    defaults = _resolve_platform_values(yaml.safe_load(
        (ROOT / 'src' / 'whisper_key' / 'config.defaults.yaml').read_text(encoding='utf-8')))

    def has(path):
        node = defaults
        for key in path.split('.'):
            if not isinstance(node, dict) or key not in node:
                return False
            node = node[key]
        return True

    def get(path):
        node = defaults
        for key in path.split('.'):
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    simple_ctx = {'language': get('whisper.language') or 'auto', 'gpu': False,
                  'available_models': list((get('whisper.models') or {}).keys())}
    simple = simple_mode.from_settings(get, simple_ctx)
    simple['startup'] = False

    commands = shipped(voice_commands.DEFAULTS_FILE, 'commands', id_key='trigger')
    for item in commands:
        item['action'] = _action_of(item['entry'])
    data = {
        'settings': defaults,
        'schema': schema.export(has),
        'rules': shipped(app_rules.DEFAULTS_FILE, 'rules', id_key='id'),
        'commands': commands,
        'simple': simple,
        'transforms': shipped(transforms.DEFAULTS_FILE, 'transforms', id_key='name'),
        'profiles': shipped(profiles.PROFILES_DEFAULTS, 'profiles', mapping=True),
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str), encoding='utf-8')
    print(f"Wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")


if __name__ == '__main__':
    main()
