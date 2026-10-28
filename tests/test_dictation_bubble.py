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


# ── Task 2: the session model ───────────────────────────────────────────────

class DictationSessionTests(unittest.TestCase):
    def setUp(self):
        from koli.dictation_session import DictationSession, join_piece
        self.S, self.join = DictationSession, join_piece

    def test_finals_append_with_spaces_and_capitals(self):
        s = self.S('phrases', 1, None)
        s.add_final('Hello there.')
        s.add_final('how are you?')
        self.assertEqual(s.text(), 'Hello there. How are you?')

    def test_mid_sentence_piece_is_lowercased_but_not_I(self):
        s = self.S('phrases', 1, None)
        s.add_final('I went to the')
        s.add_final('Store')
        self.assertEqual(s.text(), 'I went to the store')
        s.add_final('I think')
        self.assertEqual(s.text(), 'I went to the store I think')

    def test_all_caps_word_is_kept(self):
        self.assertEqual(self.join('send it to', 'NASA today', ''), ' NASA today')

    def test_selection_is_replaced_by_the_next_final(self):
        s = self.S('phrases', 1, None)
        s.add_final('Send it to Jon today.')
        self.assertTrue(s.open())
        s.edit(s.text(), 11, 14)
        s.add_final('John')
        self.assertEqual(s.text(), 'Send it to John today.')

    def test_caret_in_the_middle_inserts_there(self):
        s = self.S('openai', 1, None)
        s.add_final('one three')
        s.open()
        s.edit('one three', 4, 4)
        s.add_final('two')
        self.assertEqual(s.text(), 'one two three')

    def test_caret_moves_after_the_insertion(self):
        s = self.S('openai', 1, None)
        s.add_final('one four')
        s.open()
        s.edit('One four', 4, 4)
        s.add_final('two')
        s.add_final('three')
        self.assertEqual(s.text(), 'One two three four')

    def test_emoji_before_caret_uses_code_points(self):
        s = self.S('phrases', 1, None)
        s.open()
        s.edit('🙂 hi', 4, 4)
        s.add_final('there')
        self.assertEqual(s.text(), '🙂 hi there')

    def test_hebrew_join(self):
        s = self.S('phrases', 1, None)
        s.add_final('שלום')
        s.add_final('מה נשמע')
        self.assertEqual(s.text(), 'שלום מה נשמע')

    def test_interim_is_shown_not_settled(self):
        s = self.S('openai', 1, None)
        s.set_interim('hel')
        self.assertEqual(s.text(), '')
        self.assertEqual(s.snapshot()['interim'], 'hel')
        s.commit_interim()
        self.assertEqual(s.text(), 'Hel')
        self.assertEqual(s.snapshot()['interim'], '')

    def test_a_final_clears_the_interim(self):
        s = self.S('openai', 1, None)
        s.set_interim('hello the')
        s.add_final('Hello there.')
        self.assertEqual(s.snapshot()['interim'], '')

    def test_local_engine_never_opens(self):
        self.assertFalse(self.S('local', 1, None).open())

    def test_edit_marks_edited_and_original_keeps_engine_text(self):
        s = self.S('phrases', 1, None)
        s.add_final('teh cat')
        s.open()
        s.edit('The cat', 7, 7)
        self.assertTrue(s.edited)
        self.assertEqual(s.original(), 'Teh cat')
        self.assertEqual(s.text(), 'The cat')

    def test_moving_the_caret_alone_is_not_an_edit(self):
        s = self.S('phrases', 1, None)
        s.add_final('the cat')
        s.open()
        s.edit('The cat', 2, 2)
        self.assertFalse(s.edited)

    def test_snapshot_shape_and_words(self):
        s = self.S('phrases', 7, {'exe': 'notepad.exe'})
        s.add_final('two words')
        snap = s.snapshot()
        for key in ('id', 'engine', 'settled', 'interim', 'start', 'end', 'opened', 'listening'):
            self.assertIn(key, snap)
        self.assertEqual((snap['id'], snap['settled'], snap['start'], snap['end']), (7, 'Two words', 9, 9))
        self.assertEqual(s.words(), 2)

    def test_replacement_follows_the_case_of_the_replaced_word(self):
        self.assertEqual(self.join('Fix ', 'The', ' cat', replaced='teh'), 'the')
        self.assertEqual(self.join('Send it to ', 'john', ' today.', replaced='Jon'), 'John')

    def test_mid_text_insertion_drops_whispers_closing_period(self):
        s = self.S('phrases', 1, None)
        s.add_final('One three.')
        s.open()
        s.edit('One three.', 4, 4)
        s.add_final('Two.')
        self.assertEqual(s.text(), 'One two three.')

    def test_newline_ends_a_sentence(self):
        self.assertEqual(self.join('Dear Ann,' + chr(10), 'thanks', ''), 'Thanks')


# ── Task 3: usage and cost ──────────────────────────────────────────────────

PRICES = {'openai/gpt-live-transcribe': {'per_minute': 0.017},
          'gemini/gemini-3.6-flash': {'input_per_million': 0.75, 'output_per_million': 3.75}}


class LiveWordsUsageTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from koli import live_words_usage
        self.u = live_words_usage
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / 'live_words_usage.json'

    def test_records_add_up_per_day_and_engine(self):
        self.u.record('phrases', 30, 50, False, path=self.path, day='2026-10-28')
        self.u.record('phrases', 30, 70, True, path=self.path, day='2026-10-28')
        rows = self.u.summary(PRICES, path=self.path)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['key'], r['recordings'], r['words'], r['fixes'], r['days']), ('phrases', 2, 120, 1, 1))
        self.assertAlmostEqual(r['minutes'], 1.0)
        self.assertAlmostEqual(r['words_per_minute'], 120.0)
        self.assertEqual(r['cost'], 0)

    def test_openai_cost_is_per_minute(self):
        self.assertAlmostEqual(self.u.cost('cloud/openai/gpt-live-transcribe', 120, 0, 0, PRICES), 0.034)

    def test_gemini_cost_is_per_token(self):
        self.assertAlmostEqual(self.u.cost('cloud/gemini/gemini-3.6-flash', 60, 1_000_000, 0, PRICES), 0.75)
        self.assertAlmostEqual(self.u.cost('cloud/gemini/gemini-3.6-flash', 60, 0, 1_000_000, PRICES), 3.75)

    def test_unknown_model_and_local_cost_nothing(self):
        self.assertEqual(self.u.cost('cloud/openai/mystery', 600, 0, 0, PRICES), 0)
        self.assertEqual(self.u.cost('local', 600, 0, 0, PRICES), 0)

    def test_summary_spans_days_newest_first(self):
        self.u.record('phrases', 60, 100, False, path=self.path, day='2026-10-20')
        self.u.record('cloud/openai/gpt-live-transcribe', 60, 90, False, path=self.path, day='2026-10-27')
        self.u.record('cloud/openai/gpt-live-transcribe', 60, 90, False, path=self.path, day='2026-10-28')
        rows = self.u.summary(PRICES, path=self.path)
        self.assertEqual([r['key'] for r in rows], ['cloud/openai/gpt-live-transcribe', 'phrases'])
        self.assertEqual((rows[0]['first_day'], rows[0]['last_day'], rows[0]['days']), ('2026-10-27', '2026-10-28', 2))
        self.assertAlmostEqual(rows[0]['cost'], 0.034)
        self.assertEqual(rows[0]['label'], 'OpenAI · gpt-live-transcribe')

    def test_no_minutes_means_zero_words_per_minute(self):
        self.u.record('phrases', 0, 3, False, path=self.path, day='2026-10-28')
        self.assertEqual(self.u.summary(PRICES, path=self.path)[0]['words_per_minute'], 0)

    def test_a_corrupt_file_reads_empty_and_is_replaced(self):
        self.path.write_text('{not json', encoding='utf-8')
        self.assertEqual(self.u.summary(PRICES, path=self.path), [])
        self.u.record('phrases', 6, 1, False, path=self.path, day='2026-10-28')
        self.assertEqual(len(self.u.summary(PRICES, path=self.path)), 1)


if __name__ == '__main__':
    unittest.main()
