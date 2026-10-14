# tests/test_practice.py
# Practice (teach Koli your voice): the word-by-word comparison, the rule
# findings, reading a model's answers, the built-in texts, the Learned store
# and what dictation reads from it, and a whole round driven by a fake
# microphone and a fake Whisper. No model, microphone or network is used, and
# nothing is written to the real settings folder.
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def words(*pairs):
    return [{'word': f" {w}", 'prob': p} for w, p in pairs]


# ── The comparison ───────────────────────────────────────────────────────────

class AlignTests(unittest.TestCase):
    def test_right_wrong_missing_extra_and_unsure(self):
        from koli.practice import align
        c = align.compare("Please refactor the service, today.", "",
                          words(('please', 0.9), ('re', 0.8), ('factor', 0.8), ('the', 0.3), ('today', 0.9), ('now', 0.9)))
        status = [(w['text'], w['status']) for w in c['words']]
        self.assertEqual(status, [('Please', 'right'), ('refactor', 'wrong'), ('the', 'unsure'),
                                  ('service,', 'missing'), ('today.', 'right')])
        self.assertEqual(c['words'][1]['heard'], 're factor')
        self.assertEqual(c['extra'], ['now'])
        self.assertEqual(c['accuracy'], 0.6)

    def test_hebrew_and_punctuation(self):
        from koli.practice import align
        c = align.compare("צריך לבחור תמונת כריכה חדשה.", "צריך לבחור תמונת בריכה חדשה")
        wrong = [w for w in c['words'] if w['status'] == 'wrong']
        self.assertEqual([(w['text'], w['heard']) for w in wrong], [('כריכה', 'בריכה')])
        span = c['spans'][0]
        self.assertEqual((span['before'], span['after']), ('תמונת', 'חדשה.'))

    def test_mixed_hebrew_with_english_term(self):
        from koli.practice import align
        c = align.compare("צריך לעשות refactor ל-API", "צריך לעשות Refactor לAPI")
        self.assertTrue(all(w['status'] == 'right' for w in c['words']))

    def test_lone_hebrew_prefix_joins_the_term(self):
        from koli.practice import align
        c = align.compare("אוסיף את ה clipboard היום", "אוסיף את הקליפבורד היום")
        wrong = [(w['text'], w['heard']) for w in c['words'] if w['status'] == 'wrong']
        self.assertEqual(wrong, [('ה-clipboard', 'הקליפבורד')])
        self.assertTrue(all(w['status'] == 'right' for w in align.compare("את ה clipboard", "את ה-clipboard")['words']))

    def test_trailing_silence_is_trimmed(self):
        from koli.practice.session import trim_silence
        speech = (np.sin(np.linspace(0, 400, 16000)) * 0.3).astype(np.float32)
        audio = np.concatenate([speech, np.zeros(40000, dtype=np.float32)])
        trimmed = trim_silence(audio)
        self.assertLess(len(trimmed), 16000 + 0.5 * 16000)
        self.assertGreaterEqual(len(trimmed), 16000)


# ── Rule findings ────────────────────────────────────────────────────────────

class RuleFindingsTests(unittest.TestCase):
    def _findings(self, text, heard, language='he', domain=''):
        from koli.practice import align, findings
        return findings.rule_findings([{'language': language, 'comparison': align.compare(text, heard)}], domain)

    def test_short_heard_word_takes_its_neighbour(self):
        found = self._findings("צריך לבחור תמונת כריכה חדשה", "צריך לבחור תמונת בריכה חדשה")
        corrections = [f for f in found if f['kind'] == 'correction']
        # "בריכה" is a real word: only the phrase is corrected.
        self.assertEqual((corrections[0]['term'], corrections[0]['heard']), ('תמונת כריכה', ['תמונת בריכה']))
        found = self._findings("I said the word now", "I said the bird now", language='en')
        corr = [f for f in found if f['kind'] == 'correction']
        self.assertEqual((corr[0]['term'], corr[0]['heard']), ('the word', ['the bird']))
        # Several heard words for one: specific enough alone.
        found = self._findings("check the Kubernetes pod", "check the cooper net is pod", language='en')
        corr = [f for f in found if f['kind'] == 'correction']
        self.assertEqual((corr[0]['term'], corr[0]['heard']), ('Kubernetes', ['cooper net is']))

    def test_common_text_word_gets_no_correction(self):
        found = self._findings("אני רוצה את זה עכשיו", "אני רוצה אתה זה עכשיו")
        self.assertFalse([f for f in found if f['kind'] == 'correction'])
        self.assertFalse([f for f in found if f['kind'] == 'favored'])

    def test_favored_words_without_prefix_and_no_rule_prompt_terms(self):
        found = self._findings("Check the Kubernetes pod logs", "Check the cooper net is pod logs",
                               language='en', domain='software')
        kinds = {(f['kind'], f['term']) for f in found}
        self.assertIn(('favored', 'Kubernetes'), kinds)
        self.assertFalse([f for f in found if f['kind'] == 'prompt'])   # the model's job
        found = self._findings("אוסיף את ה clipboard היום", "אוסיף את הקליפבורד היום", domain='software')
        self.assertIn(('favored', 'clipboard'), {(f['kind'], f['term']) for f in found})
        corr = next(f for f in found if f['kind'] == 'correction')
        self.assertEqual((corr['term'], corr['heard']), ('את ה-clipboard', ['את הקליפבורד']))

    def test_one_correction_with_every_way_it_was_heard(self):
        from koli.practice import align, findings
        readings = [{'language': 'he', 'comparison': align.compare("אתחיל session חדש", "התחיל סשן חדש")},
                    {'language': 'he', 'comparison': align.compare("אתחיל session חדש", "נתחיל סשן חדש")}]
        corr = [f for f in findings.rule_findings(readings) if f['kind'] == 'correction']
        self.assertEqual(len(corr), 1)
        self.assertEqual(sorted(corr[0]['heard']), ['התחיל סשן', 'נתחיל סשן'])

    def test_model_language_for_a_term_read_in_hebrew(self):
        from koli.practice import findings
        rules = [{'kind': 'favored', 'language': 'he', 'term': 'commit', 'heard': [], 'profile': '',
                  'reason': 'r', 'source': 'rules', 'count': 1, 'core': 'commit'}]
        model = [{'kind': 'favored', 'language': 'en', 'term': 'commit', 'heard': [], 'profile': '',
                  'reason': 'm', 'source': 'ai', 'count': 1, 'core': 'commit'}]
        merged = findings.merge(rules, model)
        self.assertEqual([(f['language'], f['reason'], f['source']) for f in merged], [('he', 'm', 'ai')])

    def test_counts_repeats(self):
        from koli.practice import align, findings
        readings = [{'language': 'he', 'comparison': align.compare("תמונת כריכה", "תמונת בריכה")}] * 2
        found = findings.rule_findings(readings)
        self.assertEqual(found[0]['count'], 2)
        self.assertIn('2×', found[0]['reason'])


# ── Model answers ────────────────────────────────────────────────────────────

class ModelAnswerTests(unittest.TestCase):
    def test_parse_json_from_fences_and_prose(self):
        from koli.llm import parse_json
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(parse_json('Sure! {"a": {"b": "x}y"}} done'), {'a': {'b': 'x}y'}})
        self.assertEqual(parse_json('{"q": "say \\"hi\\" {"}'), {'q': 'say "hi" {'})
        self.assertIsNone(parse_json('no json here'))

    def test_parse_sentences_keeps_the_usable_ones(self):
        from koli.practice import texts
        answer = json.dumps({'sentences': [
            {'language': 'en', 'text': 'Deploy the service tonight.', 'targets': ['deploy']},
            {'language': 'xx', 'text': 'שלום לכולם מהצוות', 'targets': []},
            {'language': 'en', 'text': ''}, 'junk',
            {'language': 'en', 'text': 'Deploy the service tonight.'}]})
        out = texts.parse_sentences(f"```json\n{answer}\n```", ['en', 'he'], set())
        self.assertEqual([(s['language'], s['text']) for s in out],
                         [('en', 'Deploy the service tonight.'), ('he', 'שלום לכולם מהצוות')])

    def test_model_findings_merge_with_rules(self):
        from koli.practice import findings
        rules = [{'kind': 'favored', 'language': 'he', 'term': 'כריכה', 'heard': [], 'profile': '',
                  'reason': 'Misheard', 'source': 'rules', 'count': 2}]
        answer = json.dumps({'findings': [
            {'kind': 'favored', 'language': 'he', 'term': 'כריכה', 'reason': 'כ heard as ב'},
            {'kind': 'correction', 'language': 'he', 'term': 'תמונת כריכה', 'heard': ['תמונת בריכה']},
            {'kind': 'correction', 'language': 'he', 'term': 'no heard'},
            {'kind': 'other', 'term': 'x'}],
            'notes': {'he': 'כ is heard as ב.'}})
        model, notes, errors = findings.parse_model_answer(answer)
        self.assertEqual(errors, [])
        self.assertEqual(len(model), 2)
        merged = findings.merge(rules, model)
        self.assertEqual(len(merged), 2)
        fav = next(f for f in merged if f['kind'] == 'favored')
        self.assertEqual((fav['reason'], fav['source']), ('כ heard as ב', 'ai'))
        self.assertEqual(notes, {'he': 'כ is heard as ב.'})

    # From the first real round (2026-10-14): the model put a list in one
    # term and offered "כמו איזה" → "voice". A text error ("recieve") teaches
    # nothing.
    def test_model_answer_hygiene(self):
        from koli.practice import findings
        answer = json.dumps({'findings': [
            {'kind': 'prompt', 'language': 'he', 'term': 'clipboard, voice, commit', 'profile': 'code'},
            {'kind': 'correction', 'language': 'he', 'term': 'voice', 'heard': ['כמו איזה']},
            {'kind': 'correction', 'language': 'en', 'term': 'recieve', 'heard': ['receive']}],
            'text_errors': ['recieve']})
        model, _notes, errors = findings.parse_model_answer(answer)
        self.assertEqual([f['term'] for f in model if f['kind'] == 'prompt'], ['clipboard', 'voice', 'commit'])
        self.assertFalse([f for f in model if f['term'] == 'voice' and f['kind'] == 'correction'])
        kept = findings.drop_text_errors(model, errors)
        self.assertFalse([f for f in kept if f['term'] == 'recieve'])

    def test_model_correction_replaces_rule_phrases(self):
        from koli.practice import align, findings
        readings = [{'language': 'he', 'comparison': align.compare("תמונת המסך מעלפת", "תמונת המסך מאלפת")},
                    {'language': 'he', 'comparison': align.compare("הייתה מעלפת אתמול", "הייתה מאלפת אתמול")}]
        rules = findings.rule_findings(readings)
        self.assertEqual(len([f for f in rules if f['kind'] == 'correction']), 2)
        model = [{'kind': 'correction', 'language': 'he', 'term': 'מעלפת', 'heard': ['מאלפת'], 'profile': '',
                  'reason': 'x', 'source': 'ai', 'count': 1, 'core': 'מעלפת'}]
        merged = findings.merge(rules, model)
        self.assertEqual([f['term'] for f in merged if f['kind'] == 'correction'], ['מעלפת'])

    def test_no_correction_from_ordinary_or_long_stretches(self):
        from koli.practice import align, findings
        ordinary = findings.rule_findings([{'language': 'he', 'comparison': align.compare(
            "ואבדוק voice לפני", "ואבדוק כמו איזה לפני")}])
        self.assertFalse([f for f in ordinary if f['kind'] == 'correction'])
        long = findings.rule_findings([{'language': 'he', 'comparison': align.compare(
            "אבדוק clipboard ואקליט voice עכשיו", "אבדוק קלי בורד והקליט וויס עכשיו")}])
        self.assertFalse([f for f in long if f['kind'] == 'correction' and len(f['term'].split()) > 3])

    def test_drop_known(self):
        from koli.practice import findings
        found = [{'kind': 'favored', 'language': 'en', 'term': 'Koli', 'heard': [], 'profile': ''},
                 {'kind': 'correction', 'language': 'en', 'term': 'CAPEX', 'heard': ['cap ex'], 'profile': ''},
                 {'kind': 'favored', 'language': 'en', 'term': 'new', 'heard': [], 'profile': ''}]
        kept = findings.drop_known(found, ['koli'], {'CAPEX': ['cap ex']}, [])
        self.assertEqual([f['term'] for f in kept], ['new'])


# ── Built-in texts ───────────────────────────────────────────────────────────

class TextsTests(unittest.TestCase):
    def test_plan_and_halves(self):
        from koli.practice import texts
        self.assertEqual(texts.plan(['en']), [{'language': 'en', 'count': 8}])
        full = texts.plan(['en', 'he'])
        self.assertEqual(sum(p['count'] for p in full), 10)
        first, second = texts.half(full, False), texts.half(full, True)
        self.assertEqual(sum(p['count'] for p in first) + sum(p['count'] for p in second), 10)

    def test_builtin_sentences_put_your_words_first_and_never_repeat(self):
        import random
        from koli.practice import texts
        intake = {'languages': ['en', 'he'], 'work': 'software', 'words': ['כריכה', 'Kubernetes']}
        already = set()
        batch = [{'language': 'en', 'count': 3}, {'language': 'he', 'count': 3}, {'language': 'mixed', 'count': 1}]
        out = texts.builtin_sentences(batch, intake, [], already, rng=random.Random(1))
        self.assertEqual(len(out), 7)
        self.assertIn('Kubernetes', out[0]['text'])
        he = [s for s in out if s['language'] == 'he']
        self.assertTrue(any('כריכה' in s['text'] for s in he))
        self.assertEqual(len({s['text'] for s in out}), 7)

    def test_no_model_falls_back_to_packs(self):
        from koli import llm
        from koli.practice import texts

        class Config:
            def get_path(self, path, default=None):
                return default
        llm.configure(Config())
        written, error = texts.sentences([{'language': 'en', 'count': 2}], {'languages': ['en'], 'work': 'business'},
                                         [], [], set(), use_model=True)
        self.assertEqual(len(written), 2)
        self.assertIn('Settings › AI', error)
        p, _ = texts.paragraph({'languages': ['he'], 'work': 'general'}, [], 'he', set(), use_model=False)
        self.assertGreater(len(p['text'].split()), 40)


# ── Learned vocabulary ───────────────────────────────────────────────────────

class LearnedTests(unittest.TestCase):
    def setUp(self):
        from koli.learned_vocabulary import LearnedVocabulary
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LearnedVocabulary(Path(self.tmp.name) / 'learned.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_once_delete_and_start_fresh(self):
        f = [{'kind': 'favored', 'language': 'he', 'term': 'כריכה'},
             {'kind': 'correction', 'language': 'he', 'term': 'תמונת כריכה', 'heard': ['תמונת בריכה']},
             {'kind': 'favored', 'language': 'he', 'term': 'כריכה'}, {'kind': 'bogus', 'term': 'x'}]
        self.assertEqual(self.store.add(f, mic='Jabra', session='r1'), 2)
        self.assertEqual(self.store.add(f[:1]), 0)
        items = self.store.items()
        self.assertEqual({i['mic'] for i in items}, {'Jabra'})
        self.assertEqual(self.store.delete([items[0]['id']]), 1)
        self.store.set_intake({'languages': ['he']})
        self.store.set_note('he', 'note')
        self.store.delete_all()
        snap = self.store.snapshot()
        self.assertEqual((snap['items'], snap['notes']), ([], {}))
        self.assertEqual(snap['intake'], {'languages': ['he']})

    def test_extras_by_language_and_profile(self):
        self.store.add([{'kind': 'favored', 'language': 'he', 'term': 'כריכה'},
                        {'kind': 'favored', 'language': 'en', 'term': 'Kubernetes'},
                        {'kind': 'prompt', 'language': 'en', 'term': 'TypeScript', 'profile': 'code'},
                        {'kind': 'prompt', 'language': 'en', 'term': 'Koli'}])
        self.assertEqual(self.store.extras('he', 'dictation'), {'hotwords': ['כריכה'], 'prompt_terms': []})
        self.assertEqual(self.store.extras('en', 'code')['prompt_terms'], ['TypeScript', 'Koli'])
        self.assertEqual(self.store.extras('en', 'chat')['prompt_terms'], ['Koli'])
        self.assertEqual(sorted(self.store.extras(None, 'chat')['hotwords']), ['Kubernetes', 'כריכה'])

    def test_corrections_merge_for_the_language_only(self):
        self.store.add([{'kind': 'correction', 'language': 'he', 'term': 'תמונת כריכה', 'heard': ['תמונת בריכה']}])
        mine = {'CAPEX': ['cap ex']}
        self.assertEqual(self.store.merged_corrections(mine, 'he'),
                         {'CAPEX': ['cap ex'], 'תמונת כריכה': ['תמונת בריכה']})
        self.assertEqual(self.store.merged_corrections(mine, 'en'), {'CAPEX': ['cap ex']})
        self.assertEqual(mine, {'CAPEX': ['cap ex']})   # yours untouched

    def test_engine_adds_learned_words_and_terms(self):
        from koli.whisper_engine import WhisperEngine
        engine = WhisperEngine.__new__(WhisperEngine)
        engine.model = object()
        engine.vocabulary_extras = lambda lang: {'hotwords': ['כריכה'], 'prompt_terms': ['TypeScript']}
        import logging
        engine.logger = logging.getLogger('test')
        prompt, words = engine._with_learned(engine.model, 'he', 'Base.', 'Koli')
        self.assertEqual((prompt, words), ('Base. Terms: TypeScript.', 'Koli, כריכה'))
        # The second model gets no prompt terms.
        prompt, words = engine._with_learned(object(), 'he', None, None)
        self.assertEqual((prompt, words), (None, 'כריכה'))


# ── A whole round ────────────────────────────────────────────────────────────

class FakeEngine:
    # "Hears" the text it is handed, with one word swapped, to stand in for Whisper.
    def __init__(self):
        self.next_text = ''
        self.calls = []

    def transcribe_words(self, audio, language=None, prompt=None, with_vocabulary=True):
        self.calls.append((language, prompt, with_vocabulary))
        heard = self.next_text.replace('כריכה', 'בריכה').replace('Kubernetes', 'cooper net is')
        return {'text': heard, 'words': [{'word': f" {w}", 'prob': 0.9} for w in heard.split()],
                'language': 'he', 'model': 'small'}


class Config:
    def __init__(self, values):
        self.values = values

    def get_path(self, path, default=None):
        return self.values.get(path, default)


class RoundTests(unittest.TestCase):
    def setUp(self):
        from koli import llm
        from koli.learned_vocabulary import LearnedVocabulary
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.store = LearnedVocabulary(self.folder / 'learned.json')
        self.cm = Config({'whisper.language': 'auto', 'whisper.initial_prompt': 'Base prompt', 'whisper.hotwords': []})
        llm.configure(self.cm)
        self.engine = FakeEngine()
        self.sm = mock.Mock(is_paused=False)
        self.sm._mic_key.return_value = 'Jabra'

    def tearDown(self):
        self.tmp.cleanup()

    def _service(self):
        from koli.services.practice import PracticeService
        return PracticeService(self.sm, self.cm, store=self.store, folder=self.folder / 'practice',
                               history_loader=lambda: [], engine=self.engine,
                               capture=lambda limit, pause, stop: np.zeros(16000, dtype=np.float32))

    def _read_all(self, service, view):
        for item in view['items']:
            if item['stage'] == view['stage'] and not item['result']:
                self.engine.next_text = item['text']
                service.record(item['index'])

    def test_round_without_a_model(self):
        service = self._service()
        service.save_intake({'languages': ['he'], 'work': 'general', 'words': ['כריכה']})
        view = service.start()
        self.assertTrue(self.sm.is_paused)
        self.assertEqual(view['stage'], 'first')
        self.assertEqual(len(view['items']), 4)
        self._read_all(service, view)
        view = service.next()
        self.assertEqual((view['stage'], len(view['items'])), ('second', 8))
        self._read_all(service, view)
        view = service.next()
        self.assertEqual(view['stage'], 'paragraph')
        self._read_all(service, view)
        self.assertEqual(service.next()['stage'], 'results')
        results = service.results()
        terms = {(f['kind'], f['term']) for f in results['findings']}
        self.assertIn(('favored', 'כריכה'), terms)
        self.assertTrue(any(k == 'correction' for k, _ in terms))
        # Dictated with your prompt and vocabulary, bare without.
        self.assertIn(('auto', 'Base prompt', True), self.engine.calls)
        self.assertIn(('auto', None, False), self.engine.calls)
        recordings = list((self.folder / 'practice').glob('*/*.wav'))
        self.assertEqual(len(recordings), 9)
        added = service.apply([f['id'] for f in results['findings']])
        self.assertEqual(added, len(results['findings']))
        self.assertFalse(self.sm.is_paused)
        self.assertEqual(self.store.snapshot()['sessions'][0]['mic'], 'Jabra')
        service.delete_all()
        self.assertFalse((self.folder / 'practice').exists())

    def test_round_with_a_model(self):
        from koli import llm
        from koli.llm.types import Result
        self.cm.values['ai.model'] = 'codex/gpt-5.6-luna'

        def fake_complete(feature, messages, **kw):
            system = messages[0]['content']
            if '"paragraph"' in system:
                text = ' '.join(['הבוקר בחרנו תמונת כריכה חדשה לדוח של הצוות.'] * 4)
                return Result(text=f'{{"paragraph": {{"language": "he", "text": "{text}", "targets": ["כריכה"]}}}}')
            if '"sentences"' in system:
                return Result(text='```json\n{"sentences": [{"language": "he", "text": "צריך תמונת כריכה חדשה לאלבום", "targets": ["כריכה"]}]}\n```')
            return Result(text=json.dumps({'findings': [{'kind': 'correction', 'language': 'he', 'term': 'תמונת כריכה',
                                                         'heard': ['תמונת בריכה'], 'reason': 'כ heard as ב'}],
                                           'notes': {'he': 'כ is heard as ב.'}}))
        with mock.patch.object(llm, 'complete', side_effect=fake_complete):
            service = self._service()
            service.save_intake({'languages': ['he'], 'work': 'general'})
            view = service.start()
            self.assertEqual(view['model'], 'codex/gpt-5.6-luna')
            self.assertEqual(view['items'][0]['source'], 'ai')     # the model's, topped up from the packs
            for _ in range(3):
                self._read_all(service, view)
                view = service.next()
            self.assertEqual(view['stage'], 'results')
            results = service.results()
        self.assertEqual(results['notes'], {'he': 'כ is heard as ב.'})
        correction = next(f for f in results['findings'] if f['term'] == 'תמונת כריכה')
        self.assertEqual(correction['source'], 'ai')
        service.apply([correction['id']])
        self.assertEqual(self.store.snapshot()['notes'], {'he': 'כ is heard as ב.'})

    def test_nothing_heard_is_a_plain_error(self):
        from koli.services.practice import PracticeService
        service = PracticeService(self.sm, self.cm, store=self.store, folder=self.folder / 'p',
                                  history_loader=lambda: [], engine=self.engine, capture=lambda *a: None)
        service.save_intake({'languages': ['en']})
        service.start()
        with self.assertRaises(ValueError):
            service.record(0)
        service.close()


class IntakeTests(unittest.TestCase):
    def test_personal_terms_from_what_koli_knows(self):
        from koli.practice import intake
        history = [{'text': 'Deploy to Kubernetes again', 'language': 'en'}] * 3 + \
                  [{'text': 'the the the', 'language': 'en'}]
        terms = intake.personal_terms({'words': ['כריכה']}, history, {'CAPEX': ['cap ex']},
                                      [{'from': 'x', 'to': 'Koli'}], [{'kind': 'favored', 'term': 'refactor'}])
        # "again" is a common word; "the" is too short.
        self.assertEqual([t['term'] for t in terms], ['כריכה', 'CAPEX', 'Koli', 'refactor', 'Deploy', 'Kubernetes'])
        self.assertEqual(terms[0]['from'], 'you')

    def test_normalize_intake(self):
        from koli.practice import intake
        got = intake.normalize_intake({'languages': ['auto', 'he', 'he'], 'work': 'nonsense'}, ['en'])
        self.assertEqual((got['languages'], got['work']), (['he'], 'other'))
        self.assertEqual(intake.normalize_intake({}, ['he', 'en'])['languages'], ['he', 'en'])


if __name__ == '__main__':
    unittest.main()
