# tests/test_dictation_bubble.py
# The dictation bubble (docs/design/dictation-bubble.md): which engine fills
# the bubble, the session model behind it, usage counters, the cloud clients
# and how a session drives a dictation from start to the written text.

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _yaml(path):
    from ruamel.yaml import YAML
    return YAML(typ='safe').load(path.read_text(encoding='utf-8'))


# ── Task 1: which engine fills the bubble ────────────────────────────────────

class LiveWordsDecisionTests(unittest.TestCase):
    def setUp(self):
        from koli.live_words import choose_engine, resolve_language, usage_key, EngineChoice
        self.choose, self.resolve, self.usage_key, self.Choice = choose_engine, resolve_language, usage_key, EngineChoice

    def _pick(self, cfg, rule=None, mode='dictate', language='he', key=True, local=True):
        return self.choose(cfg, rule, mode=mode, language=language, has_key=lambda p: key, local_ready=local)

    def test_rule_overrides_global_engine(self):
        self.assertEqual(self._pick({'engine': 'off'}, {'live_words': 'phrases'}).engine, 'phrases')

    def test_rule_without_the_key_inherits(self):
        self.assertEqual(self._pick({'engine': 'phrases'}, {'auto_send': True}).engine, 'phrases')

    def test_command_rephrase_translate_and_auto_get_off(self):
        for mode in ('command', 'rephrase', 'translate', 'auto'):
            self.assertEqual(self._pick({'engine': 'cloud'}, mode=mode).engine, 'off', mode)

    def test_local_non_english_runs_phrases_with_notice(self):
        c = self._pick({'engine': 'local'}, language='he')
        self.assertEqual(c.engine, 'phrases')
        self.assertIn('English', c.notice)

    def test_local_english_stays_local(self):
        self.assertEqual(self._pick({'engine': 'local'}, language='en').engine, 'local')

    def test_local_not_loaded_runs_phrases(self):
        self.assertEqual(self._pick({'engine': 'local'}, language='en', local=False).engine, 'phrases')

    def test_cloud_without_key_runs_phrases_with_notice(self):
        c = self._pick({'engine': 'cloud', 'cloud': {'provider': 'gemini'}}, key=False)
        self.assertEqual(c.engine, 'phrases')
        self.assertIn('Gemini', c.notice)

    def test_cloud_openai_default_model(self):
        c = self._pick({'engine': 'cloud', 'cloud': {}})
        self.assertEqual((c.engine, c.provider, c.model), ('openai', 'openai', 'gpt-live-transcribe'))

    def test_cloud_gemini_model_from_settings(self):
        c = self._pick({'engine': 'cloud', 'cloud': {'provider': 'gemini', 'gemini_model': 'gemini-3.5-flash-lite'}})
        self.assertEqual((c.engine, c.model), ('gemini', 'gemini-3.5-flash-lite'))

    def test_unknown_engine_is_off(self):
        self.assertEqual(self._pick({'engine': 'banana'}).engine, 'off')

    def test_language_order(self):
        self.assertEqual(self.resolve({'language': 'auto', 'languages': ['he', 'en']}, {'language': 'en'}), 'en')
        self.assertEqual(self.resolve({'language': 'he'}, None), 'he')
        self.assertEqual(self.resolve({'language': 'auto', 'languages': ['he', 'en']}, None), 'he')
        self.assertEqual(self.resolve({'language': 'auto'}, None), '')

    def test_usage_keys(self):
        C = self.Choice
        self.assertEqual(self.usage_key(C('openai', 'openai', 'gpt-live-transcribe')), 'cloud/openai/gpt-live-transcribe')
        self.assertEqual(self.usage_key(C('gemini', 'gemini', 'gemini-3.6-flash')), 'cloud/gemini/gemini-3.6-flash')
        self.assertEqual(self.usage_key(C('phrases')), 'phrases')
        self.assertEqual(self.usage_key(C('local')), 'local')


class LiveWordsSettingsTests(unittest.TestCase):
    def setUp(self):
        self.defaults = _yaml(ROOT / 'src' / 'koli' / 'config.defaults.yaml')

    def test_new_section_defaults(self):
        lw = self.defaults['live_words']
        self.assertEqual(lw['engine'], 'off')
        self.assertEqual(lw['phrase_pause_seconds'], 1)
        self.assertEqual(lw['cloud']['provider'], 'openai')
        self.assertEqual(lw['cloud']['openai_model'], 'gpt-live-transcribe')
        self.assertEqual(lw['cloud']['gemini_model'], 'gemini-3.6-flash')
        self.assertEqual(lw['prices']['openai/gpt-live-transcribe'], {'per_minute': 0.017})
        self.assertEqual(self.defaults['overlay']['live_opacity'], 90)

    def test_old_keys_are_gone(self):
        self.assertNotIn('streaming_enabled', self.defaults['streaming'])
        self.assertNotIn('deliver_to_cursor', self.defaults['streaming'])
        self.assertNotIn('phrase_pause_seconds', self.defaults['auto_dictation'])

    def test_schema_covers_the_new_keys(self):
        from koli.settings.schema import SCHEMA as SETTINGS
        paths = {s.path for s in SETTINGS}
        for p in ('live_words.engine', 'live_words.phrase_pause_seconds', 'live_words.cloud.provider',
                  'live_words.cloud.openai_model', 'live_words.cloud.gemini_model', 'live_words.prices',
                  'overlay.live_opacity'):
            self.assertIn(p, paths)
        for p in ('streaming.streaming_enabled', 'streaming.deliver_to_cursor', 'auto_dictation.phrase_pause_seconds'):
            self.assertNotIn(p, paths)

    def test_app_rules_document_the_key(self):
        text = (ROOT / 'src' / 'koli' / 'app_rules.defaults.yaml').read_text(encoding='utf-8')
        self.assertIn('live_words:', text)


if __name__ == '__main__':
    unittest.main()
