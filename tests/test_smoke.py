import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LF = chr(10)  # newline, for readable assertions about layout
sys.path.insert(0, str(ROOT / "src"))


# ── Faking the platform layer ─────────────────────────────────────────────────
# koli.platform imports the OS backend (pyobjc / pywin32 / PIL), which
# the lean CI env doesn't install, so a test that needs a module importing it
# swaps in a stand-in for the WHOLE package. Faking only a submodule is not
# enough: `from .platform import app` still runs the real package __init__.
# The real platform modules are never evicted either. On macOS, re-running
# platform/macos/app.py redefines an Objective-C class, which pyobjc refuses.
_PLATFORM_SUBMODULES = ('hotkeys', 'keyboard', 'foreground', 'app', 'console',
                        'permissions', 'icons', 'paths', 'instance_lock', 'gpu',
                        'audio_sessions', 'browser_tabs', 'window_focus', 'session')


# A module whose every attribute is a MagicMock unless set explicitly.
def _stand_in_module(name):
    import types
    import unittest.mock as mock

    class StandIn(types.ModuleType):
        __path__ = []

        def __getattr__(self, attr):
            if attr.startswith('__'):
                raise AttributeError(attr)
            value = mock.MagicMock(name=f'{self.__name__}.{attr}')
            setattr(self, attr, value)
            return value
    return StandIn(name)


# sys.modules entries for a stand-in platform package. `app_attrs` are set on
# the fake platform.app (TK_MAIN_THREAD_ONLY defaults to False).
def fake_platform_modules(**app_attrs) -> dict:
    modules = {f'koli.platform.{m}': _stand_in_module(f'koli.platform.{m}')
               for m in _PLATFORM_SUBMODULES}
    package = _stand_in_module('koli.platform')
    package.IS_MACOS = package.IS_WINDOWS = False
    package.PLATFORM = 'unsupported'
    for m in _PLATFORM_SUBMODULES:
        setattr(package, m, modules[f'koli.platform.{m}'])
    app = modules['koli.platform.app']
    app.TK_MAIN_THREAD_ONLY = False
    for key, value in app_attrs.items():
        setattr(app, key, value)
    modules['koli.platform'] = package
    return modules


# For the rest of `test`: patch sys.modules with `fakes` and drop the app's own
# modules so they re-import against them. Everything (including the attributes
# the re-imports bind on the koli package) is restored afterwards, so
# nothing faked leaks into later tests.
def reimport_under(test, fakes: dict):
    import unittest.mock as mock
    import koli
    saved_attrs = dict(vars(koli))
    patcher = mock.patch.dict(sys.modules, fakes)
    patcher.start()

    def restore():
        patcher.stop()
        for key in [k for k in vars(koli) if k not in saved_attrs]:
            delattr(koli, key)
        for key, value in saved_attrs.items():
            setattr(koli, key, value)
    test.addCleanup(restore)
    for name in [n for n in sys.modules
                 if n.startswith('koli.') and n not in fakes
                 and not n.startswith('koli.platform') and n != 'koli.utils']:
        sys.modules.pop(name)


class HotkeyParsingTests(unittest.TestCase):
    def test_parse_hotkey_splits_on_plus(self):
        from koli.utils import parse_hotkey
        self.assertEqual(parse_hotkey("ctrl+a"), ["ctrl", "a"])
        self.assertEqual(parse_hotkey("Ctrl+Shift+F1"), ["ctrl", "shift", "f1"])

    def test_parse_hotkey_handles_empty(self):
        from koli.utils import parse_hotkey
        self.assertEqual(parse_hotkey(""), [])
        self.assertEqual(parse_hotkey(None), [])

    def test_beautify_hotkey_uppercases(self):
        from koli.utils import beautify_hotkey
        self.assertEqual(beautify_hotkey("ctrl+a"), "CTRL+A")
        self.assertEqual(beautify_hotkey(""), "")


class DocumentationStandardTests(unittest.TestCase):
    # CLAUDE.md mandates that every module opens with a 2-4 line header comment
    # describing its purpose. Enforced here so the standard can't silently decay
    # as new modules are added — a new file without a header fails the suite.
    def test_every_module_has_a_header_comment(self):
        pkg = ROOT / "src" / "koli"
        missing = []
        for path in sorted(pkg.rglob("*.py")):
            if "__pycache__" in path.as_posix() or path.name == "__init__.py":
                continue
            header_lines = 0
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    if header_lines:
                        break
                    continue  # tolerate blank lines before the header starts
                if stripped.startswith("#"):
                    header_lines += 1
                else:
                    break  # hit code — header (if any) is over
            if header_lines < 2:
                missing.append(f"{path.relative_to(pkg).as_posix()} ({header_lines} line(s))")
        self.assertEqual(
            missing, [],
            "Modules missing a 2+ line header comment (see CLAUDE.md):\n  "
            + "\n  ".join(missing),
        )


class VersionMetadataTests(unittest.TestCase):
    def test_pyproject_has_local_name(self):
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["project"]["name"], "koli")
        self.assertIn("koli", data["project"]["scripts"])
        self.assertNotIn("wl", data["project"]["scripts"])

    def test_no_orphan_update_checker(self):
        self.assertFalse((ROOT / "src" / "koli" / "update_checker.py").exists())

    def test_main_does_not_import_update_checker(self):
        main_src = (ROOT / "src" / "koli" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("update_checker", main_src)
        self.assertNotIn("check_for_updates", main_src)


class VoiceCommandsDefaultsTests(unittest.TestCase):
    def test_no_registry_writes_in_defaults(self):
        defaults = (ROOT / "src" / "koli" / "commands.defaults.yaml").read_text(encoding="utf-8")
        self.assertNotIn("reg add", defaults.lower())


class WhisperBackendTests(unittest.TestCase):
    def test_default_backend_is_faster_whisper(self):
        from ruamel.yaml import YAML
        path = ROOT / "src" / "koli" / "config.defaults.yaml"
        with open(path, encoding="utf-8") as f:
            cfg = YAML().load(f)
        self.assertEqual(cfg["whisper"].get("backend"), "faster_whisper")

    def test_whisper_cpp_module_imports_lazily(self):
        # Import the module — the heavy pywhispercpp import is lazy inside __init__
        from koli import whisper_engine_cpp
        self.assertTrue(hasattr(whisper_engine_cpp, 'WhisperEngineCpp'))

    def test_optional_dep_declared(self):
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        extras = data.get("project", {}).get("optional-dependencies", {})
        self.assertIn("whispercpp", extras)
        self.assertTrue(any("pywhispercpp" in d for d in extras["whispercpp"]))


class InstanceManagerTests(unittest.TestCase):
    def test_exposes_cleanup_pid_file(self):
        src = (ROOT / "src" / "koli" / "instance_manager.py").read_text(encoding="utf-8")
        self.assertIn("def cleanup_pid_file", src)
        self.assertIn("os.kill", src)
        self.assertIn("_wait_for_lock", src)

    def test_main_calls_cleanup_pid_file(self):
        main_src = (ROOT / "src" / "koli" / "main.py").read_text(encoding="utf-8")
        self.assertIn("cleanup_pid_file(instance_name)", main_src)


class TextPostprocessTests(unittest.TestCase):
    def test_strip_filler_words(self):
        from koli.text_postprocess import postprocess
        cfg = {'strip_filler_words': True}
        self.assertEqual(postprocess("um, hello like world", cfg), "hello world")

    def test_capitalize_first(self):
        from koli.text_postprocess import postprocess
        cfg = {'capitalize_first': True}
        self.assertEqual(postprocess("hello world", cfg), "Hello world")

    def test_ensure_punctuation(self):
        from koli.text_postprocess import postprocess
        cfg = {'ensure_punctuation': True}
        self.assertEqual(postprocess("hello world", cfg), "hello world.")
        self.assertEqual(postprocess("hello world.", cfg), "hello world.")

    def test_postprocess_passthrough_when_empty(self):
        from koli.text_postprocess import postprocess
        self.assertEqual(postprocess("hello", {}), "hello")
        self.assertEqual(postprocess("", {'capitalize_first': True}), "")

    def test_strip_trailing_period(self):
        from koli.text_postprocess import postprocess
        cfg = {'strip_trailing_period': True}
        self.assertEqual(postprocess("hello world.", cfg), "hello world")
        self.assertEqual(postprocess("done.\n", cfg), "done\n")
        self.assertEqual(postprocess("e.g..", cfg), "e.g..")  # don't touch ellipsis-like
        self.assertEqual(postprocess("no period here", cfg), "no period here")

    def test_inline_formatting_basics(self):
        from koli.text_postprocess import postprocess
        cfg = {'inline_formatting': True}
        self.assertEqual(postprocess("hello comma world period", cfg), "hello, world.")
        self.assertEqual(postprocess("what time is it question mark", cfg), "what time is it?")

    def test_inline_formatting_custom_replaces_defaults(self):
        # Custom list replaces the English defaults (non-English use case).
        from koli.text_postprocess import postprocess
        cfg = {
            'inline_formatting': True,
            'inline_formatting_replacements': [
                {'phrase': 'przecinek', 'replacement': ','},
                {'phrase': 'strzałka', 'replacement': '→'},
            ],
        }
        self.assertEqual(postprocess("tekst przecinek dalej", cfg), "tekst, dalej")
        self.assertEqual(postprocess("a strzałka b", cfg), "a → b")
        # English defaults are NOT active in replace mode.
        self.assertEqual(postprocess("hello comma world", cfg), "hello comma world")

    def test_inline_formatting_extend_keeps_defaults(self):
        from koli.text_postprocess import postprocess
        cfg = {
            'inline_formatting': True,
            'inline_formatting_extend': True,
            'inline_formatting_replacements': [{'phrase': 'arrow', 'replacement': '→'}],
        }
        # both the English default AND the custom phrase apply
        self.assertEqual(postprocess("hello comma arrow there", cfg), "hello, → there")

    def test_absorb_cleans_whisper_prosody_punctuation(self):
        # Whisper adds its own commas/periods around spoken cue words; absorb should
        # eat them so the output isn't polluted (Discussion #1 bug report).
        from koli.text_postprocess import postprocess
        cfg = {
            'inline_formatting': True,
            'inline_formatting_absorb_punctuation': True,
            'inline_formatting_replacements': [
                {'phrase': 'comma', 'replacement': ', '},
                {'phrase': 'arrow', 'replacement': ' → '},
            ],
        }
        whisper_out = "Hello, comma, and welcome, arrow. Common greeting."
        self.assertEqual(postprocess(whisper_out, cfg),
                         "Hello, and welcome → Common greeting.")

    def test_absorb_off_leaves_prosody_artifacts(self):
        # Documents that WITHOUT absorb the artifacts remain (opt-in, no regression).
        from koli.text_postprocess import postprocess
        cfg = {
            'inline_formatting': True,
            'inline_formatting_replacements': [{'phrase': 'comma', 'replacement': ','}],
        }
        self.assertIn(",,", postprocess("a, comma, b", cfg))

    def test_absorb_with_builtins_does_not_glue_or_eat_breaks(self):
        # SEC #4: absorb + built-in English cue words must keep spacing and newlines.
        from koli.text_postprocess import postprocess
        cfg = {'inline_formatting': True, 'inline_formatting_absorb_punctuation': True}
        self.assertEqual(postprocess("Hello, comma, world.", cfg), "Hello, world.")
        # "new paragraph" break must survive a following cue's absorb
        out = postprocess("first new paragraph second period", cfg)
        self.assertIn("\n\n", out)

    def test_absorb_respects_word_boundary(self):
        # "comma" must not fire inside "commander"/"Common".
        from koli.text_postprocess import postprocess
        cfg = {
            'inline_formatting': True,
            'inline_formatting_absorb_punctuation': True,
            'inline_formatting_replacements': [{'phrase': 'comma', 'replacement': ', '}],
        }
        out = postprocess("the commander said comma done", cfg)
        self.assertIn("commander", out)
        self.assertNotIn("commaander", out)

    def test_inline_formatting_replacement_is_literal(self):
        # A replacement containing regex-special chars must be inserted literally.
        from koli.text_postprocess import postprocess
        cfg = {
            'inline_formatting': True,
            'inline_formatting_replacements': [{'phrase': 'backref', 'replacement': r'\1\g<0>'}],
        }
        self.assertEqual(postprocess("x backref y", cfg), r"x \1\g<0> y")

    def test_ollama_polish_handles_curly_braces_in_text(self):
        from koli.text_postprocess import _ollama_polish
        cfg = {'enabled': True, 'endpoint': 'http://127.0.0.1:0', 'timeout': 0.1,
               'prompt': 'Polish:\n\n{text}'}
        result = _ollama_polish("config = {a: 1, b: 2}", cfg)
        self.assertEqual(result, '')

    def test_ollama_polish_handles_curly_braces_in_prompt(self):
        from koli.text_postprocess import _ollama_polish
        cfg = {'enabled': True, 'endpoint': 'http://127.0.0.1:0', 'timeout': 0.1,
               'prompt': 'Format: {format_var} not a placeholder. {text}'}
        result = _ollama_polish("hi", cfg)
        self.assertEqual(result, '')

    # --- Post-transcription replacements (correction-learning backing store) ---
    def test_replacement_literal_whole_word(self):
        from koli.text_postprocess import postprocess
        cfg = {'replacements': [{'from': 'see translate two', 'to': 'CTranslate2'}]}
        self.assertEqual(postprocess("we use see translate two here", cfg),
                         "we use CTranslate2 here")

    def test_replacement_whole_word_does_not_match_substring(self):
        from koli.text_postprocess import postprocess
        cfg = {'replacements': [{'from': 'cat', 'to': 'dog'}]}
        self.assertEqual(postprocess("the category cat", cfg), "the category dog")

    def test_replacement_case_insensitive_by_default(self):
        from koli.text_postprocess import postprocess
        cfg = {'replacements': [{'from': 'my sequel', 'to': 'MySQL'}]}
        self.assertEqual(postprocess("I love My Sequel", cfg), "I love MySQL")

    def test_replacement_to_text_is_literal(self):
        # Replacement text with regex-special chars is inserted verbatim.
        from koli.text_postprocess import postprocess
        cfg = {'replacements': [{'from': 'foo', 'to': r'\1$&'}]}
        self.assertEqual(postprocess("a foo b", cfg), r"a \1$& b")

    def test_replacement_invalid_regex_is_skipped(self):
        from koli.text_postprocess import postprocess
        cfg = {'replacements': [{'from': '(unclosed', 'to': 'x', 'regex': True}]}
        # Must not raise; text passes through unchanged.
        self.assertEqual(postprocess("hello (unclosed world", cfg),
                         "hello (unclosed world")

    def test_replacement_matches_punctuation_edged_terms(self):
        # Regression: \b…\b silently dropped C++ / C# / .NET (non-word edges).
        from koli.text_postprocess import postprocess
        cfg = {'replacements': [
            {'from': 'C++', 'to': 'CPP'},
            {'from': '.NET', 'to': 'dotnet'},
        ]}
        self.assertEqual(postprocess("I love C++ and .NET here", cfg),
                         "I love CPP and dotnet here")

    def test_replacement_whole_word_still_holds_for_normal_terms(self):
        from koli.text_postprocess import postprocess
        cfg = {'replacements': [{'from': 'cat', 'to': 'dog'}]}
        self.assertEqual(postprocess("category cat scatter", cfg),
                         "category dog scatter")

    # --- Deterministic smart formatting ---
    def test_smart_formatting_times(self):
        from koli.text_postprocess import postprocess
        cfg = {'smart_formatting': {'times': True}}
        self.assertEqual(postprocess("meet at 3 p.m. sharp", cfg), "meet at 3 PM sharp")
        self.assertEqual(postprocess("call at 3:30pm", cfg), "call at 3:30 PM")

    def test_smart_formatting_times_does_not_touch_words(self):
        from koli.text_postprocess import postprocess
        cfg = {'smart_formatting': {'times': True}}
        self.assertEqual(postprocess("spam and jam", cfg), "spam and jam")

    def test_smart_formatting_times_ignores_digit_suffixed_token(self):
        # Regression: "pm2.5" must not be mangled into a time.
        from koli.text_postprocess import postprocess
        cfg = {'smart_formatting': {'times': True}}
        self.assertEqual(postprocess("pm2.5 was high at 3 pm2 station", cfg),
                         "pm2.5 was high at 3 pm2 station")

    def test_smart_formatting_email(self):
        from koli.text_postprocess import postprocess
        cfg = {'smart_formatting': {'emails': True}}
        self.assertEqual(postprocess("email john at example dot com please", cfg),
                         "email john@example.com please")

    def test_smart_formatting_email_ignores_plain_prose(self):
        from koli.text_postprocess import postprocess
        cfg = {'smart_formatting': {'emails': True}}
        self.assertEqual(postprocess("let us meet at noon", cfg), "let us meet at noon")

    def test_smart_formatting_url(self):
        from koli.text_postprocess import postprocess
        cfg = {'smart_formatting': {'urls': True}}
        self.assertEqual(postprocess("go to example dot com now", cfg),
                         "go to example.com now")

    def test_smart_formatting_off_by_default(self):
        from koli.text_postprocess import postprocess
        # No smart_formatting key → spoken forms left untouched.
        self.assertEqual(postprocess("john at example dot com", {'inline_formatting': False}),
                         "john at example dot com")

    # --- Voice editing ("scratch that") ---
    def test_voice_editing_scratch_that(self):
        from koli.text_postprocess import postprocess
        cfg = {'voice_editing': True}
        self.assertEqual(postprocess("book the flight. scratch that cancel it", cfg),
                         "book the flight. cancel it")

    def test_voice_editing_delete_and_strike_variants(self):
        from koli.text_postprocess import postprocess
        cfg = {'voice_editing': True}
        self.assertEqual(postprocess("hello world delete that", cfg), "")  # nothing survives
        self.assertEqual(postprocess("first. junk here strike that", cfg), "first.")

    def test_voice_editing_off_leaves_phrase(self):
        from koli.text_postprocess import postprocess
        self.assertEqual(postprocess("please scratch that itch", {'voice_editing': False}),
                         "please scratch that itch")

    # Your own phrases, in your language, work next to the English ones.
    def test_voice_editing_your_own_phrases(self):
        from koli.text_postprocess import postprocess
        cfg = {'voice_editing': True, 'voice_editing_phrases': ['מחק את זה']}
        self.assertEqual(postprocess("נפגשים בשתיים. בשלוש מחק את זה בארבע", cfg), "נפגשים בשתיים. בארבע")
        self.assertEqual(postprocess("first. junk scratch that second", cfg), "first. second")
        # Whisper's spacing isn't guaranteed: a double space still matches.
        self.assertEqual(postprocess("ok. טעות מחק  את זה", cfg), "ok.")

    # Only whole words: a phrase inside a longer word is left alone.
    def test_voice_editing_phrase_needs_whole_words(self):
        from koli.text_postprocess import postprocess
        cfg = {'voice_editing': True, 'voice_editing_phrases': ['undo']}
        self.assertEqual(postprocess("undone work stays", cfg), "undone work stays")

    # A hand-edited single phrase (a string, not a list) still works as one phrase.
    def test_voice_editing_phrase_as_plain_string(self):
        from koli.text_postprocess import postprocess
        cfg = {'voice_editing': True, 'voice_editing_phrases': 'oops'}
        self.assertEqual(postprocess("done. wrong part oops right part", cfg), "done. right part")

    def test_malformed_config_sections_do_not_crash(self):
        # A hand-edited user_settings.yaml can put a scalar where a mapping/list
        # belongs. That's a config mistake, but it must never take down the
        # transcription pipeline — degrade to "feature off" instead.
        from koli.text_postprocess import postprocess
        for bad in (
            {'smart_formatting': 'not-a-dict'},
            {'smart_formatting': True},
            {'ollama': 'not-a-dict'},
            {'replacements': 'not-a-list'},
            {'replacements': [None, 42, 'str', {'no_from_key': 1}]},
            {'inline_formatting': True, 'inline_formatting_replacements': 'nope'},
        ):
            self.assertEqual(postprocess("some text here", bad), "some text here",
                             f"malformed config mishandled: {bad}")

    def test_long_transcript_postprocesses_quickly(self):
        # Regression: both the absorb pass and voice editing used to be O(n^2)
        # (a lazy leading scan / a re-scanned leading run), taking 6-13 s on a
        # long dictation and appearing to freeze the app. Both are linear now;
        # this budget is ~100x the measured time, so it only fires on a genuine
        # complexity regression, not on slow CI hardware.
        import time
        from koli.text_postprocess import postprocess
        text = "comma " * 3000 + "scratch that " * 200 + "word " * 3000
        cfg = {
            'inline_formatting': True,
            'inline_formatting_absorb_punctuation': True,
            'voice_editing': True,
            'smart_formatting': {'times': True, 'emails': True, 'urls': True},
        }
        started = time.perf_counter()
        postprocess(text, cfg)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0,
                        f"post-processing a {len(text)}-char transcript took {elapsed:.2f}s "
                        "— suspect an O(n^2) regression in absorb or voice editing")

    def test_absorb_equivalence_on_representative_inputs(self):
        # The absorb pass was rewritten from a regex to find-then-expand; these
        # pin the exact behaviour that rewrite had to preserve.
        from koli.text_postprocess import postprocess
        cfg = {'inline_formatting': True, 'inline_formatting_absorb_punctuation': True,
               'inline_formatting_replacements': [{'phrase': 'comma', 'replacement': ', '},
                                                  {'phrase': 'arrow', 'replacement': ' -> '}]}
        self.assertEqual(postprocess("Hello, comma, world.", cfg), "Hello, world.")
        self.assertEqual(postprocess("a arrow b", cfg), "a -> b")
        # Two spoken cues in a row yield two symbols — matches the pre-rewrite
        # behaviour exactly (verified by differential test against the old impl).
        self.assertEqual(postprocess("x comma comma y", cfg), "x,, y")
        self.assertEqual(postprocess("a,,, comma ,,, b", cfg), "a, b")
        self.assertEqual(postprocess("no cues here", cfg), "no cues here")

    def test_voice_editing_preserves_line_break_after_command(self):
        # Regression: the trailing class must not eat the newline separating the
        # scratched clause from the next line.
        from koli.text_postprocess import postprocess
        cfg = {'voice_editing': True}
        self.assertEqual(postprocess("alpha. beta scratch that\ngamma", cfg),
                         "alpha.\ngamma")


class AppRulesShapeTests(unittest.TestCase):
    def test_defaults_yaml_is_valid(self):
        from ruamel.yaml import YAML
        path = ROOT / "src" / "koli" / "app_rules.defaults.yaml"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8") as f:
            data = YAML().load(f)
        self.assertIn('rules', data)
        self.assertIsInstance(data['rules'], list)
        for rule in data['rules']:
            self.assertIn('match', rule)


class AppRulesFormattingTests(unittest.TestCase):
    def test_formatting_overrides_extracts_only_format_keys(self):
        from koli.app_rules import formatting_overrides
        rule = {
            'match': ['code.exe'],
            'auto_paste': False,          # delivery key, not a formatting key
            'capitalize_first': False,
            'ensure_punctuation': False,
        }
        self.assertEqual(
            formatting_overrides(rule),
            {'capitalize_first': False, 'ensure_punctuation': False},
        )

    def test_formatting_overrides_empty_and_none(self):
        from koli.app_rules import formatting_overrides
        self.assertEqual(formatting_overrides(None), {})
        self.assertEqual(formatting_overrides({'match': ['x'], 'auto_send': True}), {})

    def test_merge_overrides_global_postprocess(self):
        # Simulate the pipeline merge: rule formatting overrides global config.
        from koli.app_rules import formatting_overrides
        global_cfg = {'capitalize_first': True, 'ensure_punctuation': True, 'inline_formatting': True}
        rule = {'capitalize_first': False, 'ensure_punctuation': False}
        merged = {**global_cfg, **formatting_overrides(rule)}
        self.assertFalse(merged['capitalize_first'])
        self.assertFalse(merged['ensure_punctuation'])
        self.assertTrue(merged['inline_formatting'])  # untouched key inherited


class TransformsShapeTests(unittest.TestCase):
    def test_defaults_yaml_is_valid(self):
        from ruamel.yaml import YAML
        path = ROOT / "src" / "koli" / "transforms.defaults.yaml"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8") as f:
            data = YAML().load(f)
        self.assertIn('transforms', data)
        for t in data['transforms']:
            self.assertIn('name', t)
            self.assertIn('prompt', t)

    def test_transforms_manager_loads(self):
        # Against a throwaway config dir, never the developer's own: the
        # manager writes to it now (it seeds and migrates transforms.yaml),
        # and a test has no business touching a real install.
        import tempfile
        import unittest.mock as mock
        import koli.transforms as tr
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tr, 'get_user_app_data_path', return_value=tmp):
                manager = tr.TransformsManager()
            names = [t.get('name') for t in manager.list_transforms()]
        self.assertIn('polish', names)
        self.assertIn('prompt-engineer', names)


class StreakComputationTests(unittest.TestCase):
    def test_streak_basic(self):
        import datetime
        from koli.stats import _compute_streaks
        today = datetime.date(2026, 5, 18)
        active = {
            '2026-05-18', '2026-05-17', '2026-05-16',
            '2026-05-10', '2026-05-09',
        }
        current, longest = _compute_streaks(active, today)
        self.assertEqual(current, 3)
        self.assertEqual(longest, 3)

    def test_streak_breaks_yesterday(self):
        import datetime
        from koli.stats import _compute_streaks
        today = datetime.date(2026, 5, 18)
        active = {'2026-05-10'}
        current, longest = _compute_streaks(active, today)
        self.assertEqual(current, 0)
        self.assertEqual(longest, 1)


class ProfilesShapeTests(unittest.TestCase):
    def test_defaults_yaml_is_valid(self):
        from ruamel.yaml import YAML
        path = ROOT / "src" / "koli" / "profiles.defaults.yaml"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8") as f:
            data = YAML().load(f)
        self.assertIn('profiles', data)
        self.assertIn('dictation', data['profiles'])


class UtilsTests(unittest.TestCase):
    def test_resolve_asset_path_relative(self):
        from koli.utils import resolve_asset_path
        result = resolve_asset_path("config.defaults.yaml")
        self.assertTrue(result.endswith("config.defaults.yaml"))

    def test_resolve_asset_path_absolute_passthrough(self):
        from koli.utils import resolve_asset_path
        absolute = os.path.abspath(__file__)
        self.assertEqual(resolve_asset_path(absolute), absolute)


class NoiseSuppresionTests(unittest.TestCase):
    def test_passthrough_without_noisereduce(self):
        import sys
        import unittest.mock as mock
        import numpy as np
        sys.modules.pop('koli.noise_suppression', None)
        import importlib
        with mock.patch.dict(sys.modules, {'noisereduce': None}):
            import koli.noise_suppression as ns_mod
            importlib.reload(ns_mod)
            audio = np.zeros(16000, dtype=np.float32)
            result = ns_mod.apply_noise_reduction(audio, 16000, 0.75)
            np.testing.assert_array_equal(result, audio)

    # The first run loads the library: 4.8 s on 2026-10-12, long enough for an
    # auto-dictation phrase to outlive its session. It is warmed up once, in
    # the background, as soon as noise suppression is on.
    def test_warm_up_runs_once_in_the_background(self):
        import unittest.mock as mock
        import koli.noise_suppression as ns_mod
        ns_mod._warmed.clear()
        started = []

        class InlineThread:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                started.append(1)
                self.target()
        with mock.patch.object(ns_mod.threading, 'Thread', InlineThread), \
             mock.patch.object(ns_mod, 'apply_noise_reduction') as run:
            ns_mod.warm_up_in_background()
            ns_mod.warm_up_in_background()
        self.assertEqual(len(started), 1)
        run.assert_called_once()
        # Exact silence makes noisereduce divide 0 by 0 (a RuntimeWarning in
        # the console at startup): the warm-up uses faint noise instead.
        self.assertTrue(run.call_args.args[0].any())

    # A muted microphone records exact zeros; noisereduce then returns NaN for
    # the whole clip, which must never reach Whisper.
    def test_exact_silence_comes_back_as_it_was(self):
        import warnings
        import numpy as np
        import koli.noise_suppression as ns_mod
        silent = np.zeros(8000, dtype=np.float32)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            out = ns_mod.apply_noise_reduction(silent, 16000, 0.75)
        self.assertTrue(np.isfinite(out).all())

    def test_the_recorder_warms_it_up_when_turned_on(self):
        import unittest.mock as mock
        try:
            from koli.audio_recorder import AudioRecorder
        except ImportError as e:
            self.skipTest(f'audio_recorder needs deps the lean env lacks: {e}')
        recorder = AudioRecorder.__new__(AudioRecorder)
        with mock.patch('koli.noise_suppression.warm_up_in_background') as warm:
            recorder.apply_live_config(noise_suppression={'enabled': False})
            warm.assert_not_called()
            recorder.apply_live_config(noise_suppression={'enabled': True})
            warm.assert_called_once()

    def test_config_in_defaults(self):
        from ruamel.yaml import YAML
        path = ROOT / "src" / "koli" / "config.defaults.yaml"
        with open(path, encoding="utf-8") as f:
            cfg = YAML().load(f)
        ns = cfg["audio"]["noise_suppression"]
        self.assertFalse(ns["enabled"])
        self.assertAlmostEqual(float(ns["strength"]), 0.75, places=2)


class UpdateCheckTests(unittest.TestCase):
    def test_is_newer_basic(self):
        from koli.update_check import _is_newer
        self.assertTrue(_is_newer("1.0.0", "0.9.0"))
        self.assertFalse(_is_newer("0.9.0", "1.0.0"))
        self.assertFalse(_is_newer("0.9.0", "0.9.0"))

    def test_no_network_when_disabled(self):
        import unittest.mock as mock
        from koli.update_check import maybe_check_for_update
        with mock.patch('koli.update_check._check_in_background') as m:
            maybe_check_for_update(lambda _: None, {'enabled': False})
            m.assert_not_called()

    def test_update_check_in_config_defaults(self):
        from ruamel.yaml import YAML
        path = ROOT / "src" / "koli" / "config.defaults.yaml"
        with open(path, encoding="utf-8") as f:
            cfg = YAML().load(f)
        self.assertIn("update_check", cfg)
        self.assertFalse(cfg["update_check"]["enabled"])


class TranscriptLogTests(unittest.TestCase):
    def test_record_and_load(self):
        import tempfile
        import unittest.mock as mock
        from koli import transcript_log
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch('koli.transcript_log.get_user_app_data_path', return_value=tmpdir):
                transcript_log.record_transcript(
                    "Hello world", duration_s=1.5,
                    window={'exe': 'discord.exe', 'title': '@Efrat - Discord', 'hwnd': 4242, 'pid': 77})
                entries = transcript_log.load_transcripts()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "Hello world")
        self.assertEqual(entries[0]["app"], "discord.exe")
        self.assertEqual(entries[0]["title"], "@Efrat - Discord")
        self.assertEqual(entries[0]["window_id"], 4242)
        self.assertEqual(entries[0]["pid"], 77)

    # No foreground window (or a failed probe) still logs the text, just without identity.
    def test_record_without_window(self):
        import tempfile
        import unittest.mock as mock
        from koli import transcript_log
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch('koli.transcript_log.get_user_app_data_path', return_value=tmpdir):
                transcript_log.record_transcript("Hello", window={})
                entries = transcript_log.load_transcripts()
        self.assertEqual(entries[0]["app"], "")
        self.assertNotIn("title", entries[0])
        self.assertNotIn("window_id", entries[0])

    def test_empty_text_not_logged(self):
        import tempfile
        import unittest.mock as mock
        from koli import transcript_log
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch('koli.transcript_log.get_user_app_data_path', return_value=tmpdir):
                transcript_log.record_transcript("", window={'exe': 'test.exe'})
                entries = transcript_log.load_transcripts()
        self.assertEqual(len(entries), 0)


class SettingsUiModuleTests(unittest.TestCase):
    def test_module_importable(self):
        from koli import settings_ui
        self.assertTrue(hasattr(settings_ui, 'run_settings_window'))


class ModelTransferTests(unittest.TestCase):
    # Offline model transfer for locked-down networks where huggingface.co is
    # blocked. These use a stub model folder so they never need a real download.
    def _stub_model(self, root, name="koli-model-base"):
        from pathlib import Path
        folder = Path(root) / name
        folder.mkdir(parents=True)
        (folder / "model.bin").write_bytes(b"weights")
        (folder / "config.json").write_text('{"model_type":"whisper"}', encoding="utf-8")
        (folder / "tokenizer.json").write_text("{}", encoding="utf-8")
        return folder

    def test_export_destination_with_a_dot_is_not_rewritten(self):
        # Regression: an earlier version treated any dot in DEST as a file
        # extension and stripped it, so "D:\transfer.v2" silently became
        # "D:\transfer" and the model landed in the wrong directory.
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import model_transfer
        # export_model pulls ModelRegistry -> faster_whisper, absent in lean CI.
        try:
            import faster_whisper  # noqa: F401
        except Exception:
            self.skipTest("faster_whisper not installed")
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / "transfer.v2"
            snapshot = self._stub_model(root, name="snap")
            with mock.patch.object(model_transfer, '_find_cached_snapshot', return_value=snapshot):
                with mock.patch('koli.config_manager.ConfigManager') as CM:
                    CM.return_value.get_whisper_config.return_value = {'model': 'base', 'models': {}}
                    rc = model_transfer.export_model(str(dest))
            # Assert INSIDE the context — the temp tree is gone once it exits.
            self.assertEqual(rc, 0)
            self.assertTrue((dest / "koli-model-base" / "model.bin").exists(),
                            "export must land inside the dotted destination, not a truncated one")
            self.assertFalse((Path(root) / "transfer").exists(),
                             "must not create a truncated path")

    def test_export_to_missing_drive_explains_itself(self):
        # Reported in use: `--export-model D:\transfer` on a machine with no D:
        # drive produced a raw "[WinError 3] cannot find the path", which doesn't
        # tell the user the actual problem. The message must name the drive and
        # offer a destination that exists.
        import io
        import contextlib
        from pathlib import Path
        from koli import model_transfer
        if sys.platform != 'win32':
            self.skipTest("drive-letter validation is Windows-only")
        # Find a drive letter that genuinely doesn't exist on this machine.
        import os
        import string
        free = [d for d in string.ascii_uppercase if not os.path.exists(d + ':' + os.sep)]
        if not free:
            self.skipTest("no unused drive letter to test with")
        problem = model_transfer._validate_destination(Path(f"{free[0]}:\\transfer"))
        self.assertTrue(problem, "missing drive must be reported as a problem")
        self.assertIn(f"{free[0]}:", problem)
        self.assertIn("Drives on this machine", problem)
        self.assertIn("--export-model", problem)  # actionable suggestion

    def test_export_default_destination_is_usable(self):
        from pathlib import Path
        from koli import model_transfer
        default = model_transfer._default_export_dir()
        self.assertTrue(Path(default).is_dir(), "default export dir must exist")
        self.assertEqual(model_transfer._validate_destination(Path(default)), "")

    def test_import_rejects_non_model_folder(self):
        import tempfile
        from koli import model_transfer
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(model_transfer.import_model(d), 1)  # empty dir
            self.assertEqual(model_transfer.import_model(d + "/nope"), 1)  # missing

    def test_import_registers_and_activates_model(self):
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from ruamel.yaml import YAML
        from koli import model_transfer
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as appdata:
            bundle = self._stub_model(src_root)
            with mock.patch.object(model_transfer, 'get_user_app_data_path', return_value=appdata):
                rc = model_transfer.import_model(str(bundle))
            self.assertEqual(rc, 0)
            settings = Path(appdata) / "user_settings.yaml"
            data = YAML().load(settings.read_text(encoding="utf-8"))
            key = data["whisper"]["model"]
            self.assertEqual(key, "local-base")
            entry = data["whisper"]["models"][key]
            self.assertTrue(Path(entry["source"]).is_dir())
            # Model files must have been copied, not just referenced.
            self.assertTrue((Path(entry["source"]) / "model.bin").exists())

    def test_import_keep_in_place_does_not_copy(self):
        # The network-share case: IT hosts one copy, every machine points at it.
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from ruamel.yaml import YAML
        from koli import model_transfer
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as appdata:
            bundle = self._stub_model(src_root)
            with mock.patch.object(model_transfer, 'get_user_app_data_path', return_value=appdata):
                rc = model_transfer.import_model(str(bundle), keep_in_place=True)
            self.assertEqual(rc, 0)
            data = YAML().load((Path(appdata) / "user_settings.yaml").read_text(encoding="utf-8"))
            entry = data["whisper"]["models"][data["whisper"]["model"]]
            self.assertEqual(Path(entry["source"]), bundle)
            self.assertFalse((Path(appdata) / "models").exists())

    def test_imported_model_is_seen_as_cached_by_registry(self):
        # The whole point: a local path must count as "already downloaded" so the
        # app never tries to reach huggingface.co for it.
        import tempfile
        try:
            from koli.model_registry import ModelRegistry
        except Exception:
            self.skipTest("model_registry not importable (faster_whisper absent)")
        with tempfile.TemporaryDirectory() as d:
            bundle = self._stub_model(d)
            reg = ModelRegistry(whisper_models_config={
                'local-base': {'source': str(bundle), 'label': 'x', 'group': 'custom'}})
            self.assertTrue(reg.get_model('local-base').is_local_path)
            self.assertTrue(reg.is_model_cached('local-base'))
            self.assertEqual(reg.get_source('local-base'), str(bundle))

    def test_registered_settings_survive_existing_content(self):
        # Registering must not wipe the user's other settings/comments.
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from ruamel.yaml import YAML
        from koli import model_transfer
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as appdata:
            settings = Path(appdata) / "user_settings.yaml"
            settings.write_text(
                "# my header\nwhisper:\n  hotwords:\n    - Kubernetes\naudio:\n  max_duration: 90\n",
                encoding="utf-8")
            bundle = self._stub_model(src_root)
            with mock.patch.object(model_transfer, 'get_user_app_data_path', return_value=appdata):
                model_transfer.import_model(str(bundle))
            data = YAML().load(settings.read_text(encoding="utf-8"))
            self.assertEqual(list(data["whisper"]["hotwords"]), ["Kubernetes"])
            self.assertEqual(data["audio"]["max_duration"], 90)
            self.assertEqual(data["whisper"]["model"], "local-base")


class SystemAudioTests(unittest.TestCase):
    # The real capture path needs audio hardware (verified separately); here we
    # test the plumbing: graceful absence of soundcard, and mono/rate shaping.
    def test_capture_without_soundcard_returns_none(self):
        import io
        import contextlib
        # Simulate soundcard being ABSENT even when it's actually installed:
        # binding the name to None in sys.modules makes `import soundcard` raise
        # ImportError (documented CPython behaviour). Merely popping it would let
        # a real installed copy be re-imported, so this test would pass only on
        # machines without the optional extra.
        saved = sys.modules.get('soundcard')
        sys.modules['soundcard'] = None
        try:
            from koli import system_audio
            self.assertFalse(system_audio.is_available())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out = system_audio.capture_loopback(1)
            self.assertIsNone(out)
            self.assertIn('loopback', buf.getvalue().lower())
        finally:
            # Restore exactly: put back a real module, or REMOVE the None
            # sentinel entirely — leaving it would break soundcard imports for
            # every later test in this process.
            if saved is not None:
                sys.modules['soundcard'] = saved
            else:
                sys.modules.pop('soundcard', None)

    def test_capture_with_mock_soundcard_returns_mono_float32(self):
        import types
        import numpy as np

        class _Rec:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def record(self, numframes):
                return np.full((numframes, 2), 0.5, dtype=np.float32)  # stereo

        class _Mic:
            def recorder(self, samplerate, channels):
                return _Rec()

        class _Spk:
            name = 'Speakers'

        fake = types.ModuleType('soundcard')
        fake.default_speaker = lambda: _Spk()
        fake.get_microphone = lambda id, include_loopback=False: _Mic()
        sys.modules['soundcard'] = fake
        try:
            from koli import system_audio
            out = system_audio.capture_loopback(1, samplerate=16000)
            self.assertIsNotNone(out)
            self.assertEqual(out.ndim, 1)              # collapsed to mono
            self.assertEqual(str(out.dtype), 'float32')
            self.assertEqual(len(out), 16000)          # 1s @ 16 kHz
        finally:
            del sys.modules['soundcard']


class ModelDownloadHintTests(unittest.TestCase):
    # First-run UX: the download message names an approximate size per model.
    def test_size_hint_for_known_models(self):
        try:
            from koli.whisper_engine import _model_size_hint
        except Exception:
            self.skipTest("whisper_engine not importable (faster_whisper absent)")
        self.assertEqual(_model_size_hint('base'), '~141 MB')
        self.assertEqual(_model_size_hint('base.en'), '~141 MB')
        self.assertEqual(_model_size_hint('large-v3'), '~3 GB')
        self.assertEqual(_model_size_hint('unknown-model'), '')


class InstanceManagerNoConsoleTests(unittest.TestCase):
    # The silent-close fix must import cleanly and print on all platforms.
    def test_notify_no_console_prints(self):
        import io
        import contextlib
        import unittest.mock as mock
        # instance_manager pulls `.platform`, which eagerly imports the OS backend
        # (win32api / AppKit) — absent in the lean CI env, so skip there.
        try:
            from koli import instance_manager
        except Exception:
            self.skipTest("instance_manager not importable on this platform")
        buf = io.StringIO()
        # Force "console attached" so the blocking MessageBox path can never run
        # (this process may itself be headless).
        with mock.patch.object(instance_manager, '_console_attached', return_value=True):
            with contextlib.redirect_stdout(buf):
                instance_manager._notify_no_console("T", "already running message")
        self.assertIn("already running message", buf.getvalue())


class CorrectionsTests(unittest.TestCase):
    # Correction persistence (postprocess.replacements) — the learning-loop store.
    def _with_temp_settings(self):
        import tempfile
        import unittest.mock as mock
        d = tempfile.mkdtemp()
        from koli import corrections as cmod
        patcher = mock.patch.object(cmod, 'get_user_app_data_path', return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)
        return cmod, d

    def test_add_and_list_replacement(self):
        cmod, _ = self._with_temp_settings()
        self.assertTrue(cmod.add_replacement('see translate two', 'CTranslate2'))
        items = cmod.list_replacements()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['from'], 'see translate two')
        self.assertEqual(items[0]['to'], 'CTranslate2')

    def test_add_replacement_rejects_empty_and_noop(self):
        cmod, _ = self._with_temp_settings()
        self.assertFalse(cmod.add_replacement('', 'x'))
        self.assertFalse(cmod.add_replacement('same', 'same'))
        self.assertEqual(cmod.list_replacements(), [])

    def test_add_replacement_updates_existing_from(self):
        cmod, _ = self._with_temp_settings()
        cmod.add_replacement('foo', 'bar')
        self.assertTrue(cmod.add_replacement('foo', 'baz'))  # same from, new to → update
        items = cmod.list_replacements()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['to'], 'baz')

    def test_add_replacement_dedupes_identical(self):
        cmod, _ = self._with_temp_settings()
        cmod.add_replacement('foo', 'bar')
        self.assertFalse(cmod.add_replacement('foo', 'bar'))  # exact dup → no-op
        self.assertEqual(len(cmod.list_replacements()), 1)

    def test_add_replacement_updates_case_insensitively(self):
        # "teh" and "Teh" must not create two rules that both fire (matching is
        # case-insensitive), so adding a case variant updates in place.
        cmod, _ = self._with_temp_settings()
        cmod.add_replacement('Teh', 'The')
        self.assertTrue(cmod.add_replacement('teh', 'THE'))
        items = cmod.list_replacements()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['to'], 'THE')

    def test_saved_correction_is_applied_by_postprocess(self):
        # End-to-end: what corrections.py writes must be consumable by postprocess.
        cmod, _ = self._with_temp_settings()
        cmod.add_replacement('my sequel', 'MySQL')
        from koli.text_postprocess import postprocess
        cfg = {'replacements': cmod.list_replacements()}
        self.assertEqual(postprocess('I use my sequel daily', cfg), 'I use MySQL daily')


class HotwordSuggestTests(unittest.TestCase):
    def test_suggest_picks_frequent_proper_nouns(self):
        from koli.dictionary import suggest_hotwords
        texts = [
            "We deployed Kubernetes today.",
            "The Kubernetes cluster is fine. Kubernetes again.",
            "I love Kubernetes and also PostgreSQL.",
        ]
        got = dict(suggest_hotwords(texts, min_count=3))
        self.assertIn('Kubernetes', got)
        self.assertEqual(got['Kubernetes'], 3)  # sentence-initial one not counted

    def test_suggest_excludes_known_hotwords(self):
        from koli.dictionary import suggest_hotwords
        texts = ["Use Redis now.", "Redis is great with Redis.", "More Redis here."]
        got = dict(suggest_hotwords(texts, known={'redis'}, min_count=2))
        self.assertNotIn('Redis', got)

    def test_suggest_ignores_plain_sentence_starts(self):
        from koli.dictionary import suggest_hotwords
        texts = ["Today was good.", "Today is better.", "Today we ship."]
        # "Today" only ever appears sentence-initial → not a suggestion.
        self.assertEqual(suggest_hotwords(texts, min_count=2), [])


class OnboardingBannerTests(unittest.TestCase):
    # UX #4b: the first-launch banner must reflect the user's ACTUAL configured
    # hotkeys (and notify message), not hardcoded Windows defaults.
    def test_banner_uses_supplied_hotkeys(self):
        import io
        import contextlib
        import unittest.mock as mock
        from koli import onboarding_tutorial
        seen = {}

        def fake_notify(msg):
            seen['notify'] = msg

        buf = io.StringIO()
        with mock.patch.object(onboarding_tutorial, 'mark_complete', lambda: None):
            with contextlib.redirect_stdout(buf):
                onboarding_tutorial.show_console_welcome(
                    hotkeys={'record': 'F9', 'rephrase': 'F10', 'command': 'F11',
                             'cancel': 'F12', 'pause': 'F8'},
                    notify=fake_notify,
                )
        out = buf.getvalue()
        self.assertIn('F9', out)
        self.assertIn('F10', out)
        self.assertNotIn('Ctrl+Win', out)  # no leaked default
        self.assertEqual(seen.get('notify'), 'Welcome! Hold F9 to start dictating.')


class PostprocessHotReloadTests(unittest.TestCase):
    # UX #1/#3: editing postprocess in user_settings.yaml applies on next dictation
    # (get_postprocess_config) without an app restart.
    def test_postprocess_reloads_on_file_change(self):
        import os
        import tempfile
        import unittest.mock as mock
        from ruamel.yaml import YAML
        # Import the submodule object so patch.object resolves it without relying
        # on it already being an attribute of the package (string-target patches
        # fail under a fresh `unittest` run where nothing imported it first).
        # config_manager pulls `.platform`, which eagerly imports the OS-specific
        # backend (win32api / AppKit) — absent in the lean CI env, so skip there.
        try:
            from koli import config_manager as cm_mod
        except Exception:
            self.skipTest("config_manager not importable on this platform")
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cm_mod, 'get_user_app_data_path', return_value=d):
                cm = cm_mod.ConfigManager(quiet=True)
                self.assertFalse(cm.get_postprocess_config().get('strip_filler_words'))
                sp = os.path.join(d, 'user_settings.yaml')
                base = cm._postprocess_mtime or os.path.getmtime(sp)
                with open(sp, 'w', encoding='utf-8') as f:
                    YAML().dump({'postprocess': {'strip_filler_words': True}}, f)
                os.utime(sp, (base + 10, base + 10))  # guarantee a newer mtime
                self.assertTrue(cm.get_postprocess_config().get('strip_filler_words'))


class SettingsResetTests(unittest.TestCase):
    # SEC #1: "Reset to defaults" promises hotwords survive — verify they do.
    def test_reset_preserves_hotwords(self):
        import tempfile, os
        from ruamel.yaml import YAML
        from koli.settings_ui import reset_settings_preserving_hotwords
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'user_settings.yaml')
            with open(path, 'w', encoding='utf-8') as f:
                YAML().dump({'whisper': {'model': 'small', 'hotwords': ['Kubernetes', 'drajb']},
                             'clipboard': {'auto_paste': False}}, f)
            preserved = reset_settings_preserving_hotwords(path)
            self.assertEqual(preserved, ['Kubernetes', 'drajb'])
            with open(path, encoding='utf-8') as f:
                after = YAML().load(f)
            # hotwords kept, everything else gone (back to defaults)
            self.assertEqual(list(after['whisper']['hotwords']), ['Kubernetes', 'drajb'])
            self.assertNotIn('clipboard', after)
            self.assertNotIn('model', after['whisper'])

    def test_reset_with_no_hotwords_removes_file(self):
        import tempfile, os
        from ruamel.yaml import YAML
        from koli.settings_ui import reset_settings_preserving_hotwords
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'user_settings.yaml')
            with open(path, 'w', encoding='utf-8') as f:
                YAML().dump({'clipboard': {'auto_paste': False}}, f)
            self.assertEqual(reset_settings_preserving_hotwords(path), [])
            self.assertFalse(os.path.exists(path))  # clean wipe when nothing to keep


class HistoryWindowModuleTests(unittest.TestCase):
    def test_module_importable(self):
        from koli import history_window
        self.assertTrue(hasattr(history_window, 'show_history'))


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_exists(self):
        self.assertTrue((ROOT / ".github" / "workflows" / "release.yml").exists())

    def test_release_workflow_triggers_on_tag(self):
        with open(ROOT / ".github" / "workflows" / "release.yml", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("tags:", content)
        self.assertIn("v*", content)
        self.assertIn("pypa/gh-action-pypi-publish", content)


class PyprojectOptionalDepsTests(unittest.TestCase):
    def test_noise_optional_dep(self):
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        extras = data.get("project", {}).get("optional-dependencies", {})
        self.assertIn("noise", extras)
        self.assertTrue(any("noisereduce" in d for d in extras["noise"]))


class SelftestModuleTests(unittest.TestCase):
    def test_module_importable(self):
        from koli import selftest
        self.assertTrue(hasattr(selftest, 'run_selftest'))

    def test_report_helper(self):
        import io
        import sys
        from koli.selftest import _report
        failures = []
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            _report(True, "looks good", failures, "fake")
            _report(False, ("uh oh", "fix-me"), failures, "fake-2")
        finally:
            sys.stdout = old_stdout
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "fake-2")


class FirstRunTests(unittest.TestCase):
    def test_flag_file_lifecycle(self):
        import tempfile
        import unittest.mock as mock
        from koli import first_run

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch('koli.first_run.get_user_app_data_path', return_value=tmpdir):
                self.assertTrue(first_run.is_first_run())
                first_run.mark_first_run_complete()
                self.assertFalse(first_run.is_first_run())

    def test_show_welcome_module_importable(self):
        from koli import first_run
        self.assertTrue(hasattr(first_run, 'show_welcome_window'))


class CheatSheetTests(unittest.TestCase):
    def test_module_importable(self):
        from koli import cheat_sheet
        self.assertTrue(hasattr(cheat_sheet, 'show_cheat_sheet'))


class BundleLogsTests(unittest.TestCase):
    def test_redaction_replaces_usernames(self):
        from koli.bundle_logs import _redact
        sample = "Path: C:\\Users\\rohit\\AppData\\Roaming and email me at test@example.com"
        red = _redact(sample)
        self.assertNotIn("rohit", red.lower())
        self.assertNotIn("test@example.com", red)
        self.assertIn("<USER>", red)
        self.assertIn("<EMAIL>", red)

    def test_redaction_macos_linux_paths(self):
        from koli.bundle_logs import _redact
        self.assertIn("<USER>", _redact("/Users/alice/.koli/"))
        self.assertIn("<USER>", _redact("/home/bob/log.txt"))

    def test_bundle_creates_zip(self):
        import io
        import sys
        import tempfile
        import unittest.mock as mock
        import zipfile
        from koli import bundle_logs

        with tempfile.TemporaryDirectory() as appdata:
            with tempfile.TemporaryDirectory() as out:
                output = f"{out}/bundle.zip"
                old_stdout = sys.stdout
                sys.stdout = io.StringIO()
                try:
                    with mock.patch('koli.bundle_logs.get_user_app_data_path', return_value=appdata), \
                         mock.patch('koli.bundle_logs._capture_doctor', return_value='[doctor mocked]'):
                        rc = bundle_logs.bundle_logs(output)
                finally:
                    sys.stdout = old_stdout
                self.assertEqual(rc, 0)
                with zipfile.ZipFile(output) as zf:
                    names = zf.namelist()
                self.assertIn('about.txt', names)
                self.assertIn('doctor.txt', names)


class LocalServerTests(unittest.TestCase):
    def test_module_importable(self):
        from koli import local_server
        self.assertTrue(hasattr(local_server, 'run_server'))
        self.assertEqual(local_server.DEFAULT_PORT, 7777)

    def test_decode_wav_fallback(self):
        import io
        import wave
        import numpy as np
        from koli.local_server import _decode_wav_fallback

        with io.BytesIO() as buf:
            with wave.open(buf, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                samples = (np.sin(2 * np.pi * 440 * np.arange(16000) / 16000) * 32767).astype(np.int16)
                wf.writeframes(samples.tobytes())
            wav_bytes = buf.getvalue()

        decoded = _decode_wav_fallback(wav_bytes)
        self.assertEqual(decoded.dtype, np.float32)
        self.assertEqual(len(decoded), 16000)
        self.assertGreater(float(np.max(np.abs(decoded))), 0.5)


class MainCliFlagsTests(unittest.TestCase):
    def test_main_registers_new_flags(self):
        main_src = (ROOT / "src" / "koli" / "main.py").read_text(encoding="utf-8")
        for flag in ('--selftest', '--cheat-sheet', '--bundle-logs', '--serve'):
            self.assertIn(flag, main_src, f"main.py should register {flag}")


class TroubleshootingDocTests(unittest.TestCase):
    def test_docs_exist(self):
        self.assertTrue((ROOT / "docs" / "troubleshooting.md").exists())
        self.assertTrue((ROOT / "docs" / "faq.md").exists())


class VersionBumpTests(unittest.TestCase):
    def test_pyproject_version(self):
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        version = data["project"]["version"]
        major, minor = version.split('.')[:2]
        self.assertGreaterEqual((int(major), int(minor)), (0, 10))

    def test_citation_matches_pyproject(self):
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as f:
            pyproject_version = tomllib.load(f)["project"]["version"]
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        self.assertIn(f"version: {pyproject_version}", citation,
                      "CITATION.cff version must match pyproject.toml")


class _FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler for parser tests."""
    def __init__(self, body: bytes, boundary: str, content_length=None):
        import io
        self.headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Content-Length': str(content_length if content_length is not None else len(body)),
        }
        self.rfile = io.BytesIO(body)


def _build_multipart(boundary: str, file_bytes: bytes) -> bytes:
    b = boundary.encode()
    return (
        b'--' + b + b'\r\n'
        b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n'
        + file_bytes + b'\r\n'
        b'--' + b + b'--\r\n'
    )


class LocalServerSecurityTests(unittest.TestCase):
    # SRV-1: binary audio ending in 0x2D ('-') must NOT be truncated.
    def test_parse_multipart_preserves_trailing_dashes(self):
        from koli.local_server import _parse_multipart
        audio = b'\x00\x01\x02\x2d\x2d\x2d'  # ends in three dashes
        body = _build_multipart('BoUnDaRy123', audio)
        fields = _parse_multipart(_FakeHandler(body, 'BoUnDaRy123'))
        self.assertEqual(fields['file']['data'], audio)

    # SRV-2: oversized Content-Length is rejected before the body is read.
    def test_oversized_content_length_rejected(self):
        from koli import local_server
        huge = local_server.MAX_UPLOAD_BYTES + 1
        handler = _FakeHandler(b'x', 'B', content_length=huge)
        with self.assertRaises(ValueError):
            local_server._parse_multipart(handler)

    # SEC #3: negative Content-Length must be rejected (read(-1) would drain socket).
    def test_negative_content_length_rejected(self):
        from koli import local_server
        handler = _FakeHandler(b'x', 'B', content_length=-1)
        with self.assertRaises(ValueError):
            local_server._parse_multipart(handler)


class BundleRedactionTests(unittest.TestCase):
    # SEC #2: URL credentials + secret query params masked in ALL bundled files
    # (this is what protects doctor.txt, not just user_settings.yaml).
    def test_redact_masks_url_credentials_and_tokens(self):
        from koli.bundle_logs import _redact
        out = _redact("Ollama post-edit: http://user:s3cret@ollama.host:11434 reachable")
        self.assertNotIn("s3cret", out)
        self.assertIn("<REDACTED>@", out)
        out2 = _redact("GET https://api.example.com/x?token=abc123&z=1")
        self.assertNotIn("abc123", out2)
        self.assertIn("<REDACTED>", out2)

    # PRIV-1: sensitive config fields are masked in user_settings.yaml.
    def test_redact_yaml_masks_sensitive_fields(self):
        from koli.bundle_logs import _redact_yaml
        sample = (
            "whisper:\n"
            "  hotwords: [SecretName, CodeWord]\n"
            "  initial_prompt: my private context\n"
            "postprocess:\n"
            "  ollama:\n"
            "    endpoint: http://user:pass@host:11434\n"
        )
        out = _redact_yaml(sample)
        self.assertNotIn('SecretName', out)
        self.assertNotIn('CodeWord', out)
        self.assertNotIn('private context', out)
        self.assertNotIn('user:pass', out)
        self.assertIn('<REDACTED>', out)

    def test_redact_yaml_masks_block_hotwords(self):
        from koli.bundle_logs import _redact_yaml
        sample = "whisper:\n  hotwords:\n    - Alpha\n    - Bravo\n  model: tiny\n"
        out = _redact_yaml(sample)
        self.assertNotIn('Alpha', out)
        self.assertNotIn('Bravo', out)
        self.assertIn('model: tiny', out)  # non-sensitive keys untouched


class VoiceCommandQuotingTests(unittest.TestCase):
    # VC-1: clipboard content is shell-quoted when expanded into a run: command.
    def test_shell_safe_quotes_clipboard(self):
        try:
            from koli.voice_commands import VoiceCommandManager
        except Exception:
            self.skipTest("voice_commands not importable on this platform")
        import shlex
        import unittest.mock as mock
        vc = VoiceCommandManager.__new__(VoiceCommandManager)
        with mock.patch('koli.voice_commands.pyperclip.paste', return_value='; rm -rf ~'):
            safe = vc._expand_template('echo ${clipboard}', shell_safe=True)
            raw = vc._expand_template('echo ${clipboard}', shell_safe=False)
        self.assertEqual(safe, 'echo ' + shlex.quote('; rm -rf ~'))
        self.assertEqual(raw, 'echo ; rm -rf ~')


# "d {n}" matches a spoken number and puts it in the keys; `send_to` decides
# which window gets them.
class VoiceCommandNumberAndTargetTests(unittest.TestCase):
    def setUp(self):
        reimport_under(self, fake_platform_modules())
        try:
            import koli.voice_commands as vc
        except ImportError as e:
            self.skipTest(f'voice_commands needs deps the lean env lacks: {e}')
        self.vc = vc
        self.manager = vc.VoiceCommandManager.__new__(vc.VoiceCommandManager)
        self.manager.logger = __import__('logging').getLogger('test.voice_commands')
        self.manager.commands = [
            {'trigger': 'd {n}', 'hotkey': 'ctrl+shift+{n}', 'send_to': 'windows',
             'then': [{'type': 'desk {n}'}]},
            {'trigger': 'open notepad', 'run': 'notepad.exe'},
        ]

    def test_a_number_matches_as_a_digit_or_a_word(self):
        for said, number in (('D 1', '1'), ('D1.', '1'), ('D-2', '2'), ('d one', '1'),
                             ('Please D nine', '9'), ('D 0', '0')):
            matched = self.manager.match_command(said)
            self.assertIsNotNone(matched, said)
            self.assertEqual(matched['hotkey'], f'ctrl+shift+{number}', said)

    def test_other_phrases_do_not_match_a_number_trigger(self):
        for said in ('D 12', 'dog 1', 'and 1', 'D', 'D something'):
            self.assertIsNone(self.manager.match_command(said), said)

    def test_the_number_fills_every_step_and_leaves_the_command_alone(self):
        matched = self.manager.match_command('D 3')
        self.assertEqual(matched['then'][0]['type'], 'desk 3')
        self.assertEqual(self.manager.commands[0]['hotkey'], 'ctrl+shift+{n}')
        self.assertEqual(self.manager.commands[0]['then'][0]['type'], 'desk {n}')

    def test_keys_without_send_to_go_to_the_app_in_front(self):
        self.manager._execute_action({'hotkey': 'ctrl+c'}, 'copy')
        self.vc.keyboard.send_hotkey.assert_called_once_with('ctrl', 'c')
        self.vc.window_focus.run_focused.assert_not_called()

    def test_send_to_presses_the_keys_inside_the_target_window(self):
        def run_focused(target, press):
            self.assertEqual(target, 'windows')
            self.vc.keyboard.send_hotkey.assert_not_called()  # not before the switch
            press()
        self.vc.window_focus.run_focused.side_effect = run_focused
        self.manager._execute_action({'hotkey': 'ctrl+shift+1', 'send_to': 'windows'}, 'd 1')
        self.vc.keyboard.send_hotkey.assert_called_once_with('ctrl', 'shift', '1')

    def test_a_closed_target_app_presses_nothing_and_says_so(self):
        import unittest.mock as mock
        self.vc.window_focus.run_focused.side_effect = LookupError("chrome.exe isn't open")
        with mock.patch.object(self.vc, 'publish') as publish:
            self.manager._execute_action({'hotkey': 'ctrl+t', 'send_to': 'chrome.exe'}, 'new tab')
        self.vc.keyboard.send_hotkey.assert_not_called()
        publish.assert_called_once()
        self.assertIn("chrome.exe isn't open", publish.call_args[0][1]['message'])


# Sending keys to another window and coming back (Windows). The user's
# "Desk{n}" command pressed Ctrl+Alt+N for a desktop switcher: coming back to
# the window you started in dragged you back to its desktop.
class WindowFocusRoundTripTests(unittest.TestCase):
    def setUp(self):
        import unittest.mock as mock
        if sys.platform != 'win32':
            self.skipTest('Windows only')
        import koli.platform.windows.window_focus as wf
        self.wf = wf
        self.front = [100]                      # the window in front, as a stack
        self.desktop_of = {100: 'A', 200: 'A', 300: 'B'}
        self.current = ['A']
        user32 = mock.Mock()
        user32.GetForegroundWindow.side_effect = lambda: self.front[-1]
        for name, value in (('_user32', user32), ('_sink_window', lambda: 200),
                            ('_bring_to_front', self._bring), ('FOCUS_SETTLE_SECONDS', 0),
                            ('KEYS_SETTLE_SECONDS', 0), ('DESKTOP_SWITCH_WAIT_SECONDS', 0.05),
                            ('_on_current_desktop', lambda h: self.desktop_of.get(h, self.current[0]) == self.current[0]),
                            ('_top_app_window_here', lambda: 300 if self.current[0] == 'B' else 100)):
            patcher = mock.patch.object(wf, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _bring(self, hwnd):
        self.front.append(hwnd)
        return True

    def test_keys_that_switch_desktops_leave_you_on_the_new_one(self):
        def switch_desktop():
            self.current[0] = 'B'
            self.desktop_of[200] = 'B'          # Koli's key window is on every desktop
        self.wf.run_focused('windows', switch_desktop)
        self.assertEqual(self.front[-1], 300, 'the top app of the new desktop, not the old window')
        self.assertNotIn(100, self.front[1:], 'never dragged back to the old desktop')

    def test_other_keys_bring_you_back_to_your_window(self):
        self.wf.run_focused('windows', lambda: None)
        self.assertEqual(self.front[-1], 100)


# Saving a command keeps where its keys go and checks the {n} placeholder.
class CommandSaveTargetTests(unittest.TestCase):
    def setUp(self):
        import unittest.mock as mock
        reimport_under(self, fake_platform_modules())
        try:
            from koli.services import commands
        except ImportError as e:
            self.skipTest(f'services.commands needs deps the lean env lacks: {e}')
        self.commands = commands
        patcher = mock.patch.object(commands.dm, 'save_entry', side_effect=lambda *a, **k: a[4])
        self.save_entry = patcher.start()
        self.addCleanup(patcher.stop)

    def test_send_to_is_kept_on_the_command_and_its_steps(self):
        saved = self.commands.save_command({
            'trigger': 'd {n}', 'hotkey': 'ctrl+shift+{n}', 'send_to': 'windows',
            'then': [{'hotkey': 'enter', 'send_to': 'Chrome.EXE'}, {'hotkey': 'esc', 'send_to': ''}]})
        self.assertEqual(saved['send_to'], 'windows')
        self.assertEqual(saved['then'][0], {'hotkey': 'enter', 'send_to': 'chrome.exe'})
        self.assertEqual(saved['then'][1], {'hotkey': 'esc'})

    def test_send_to_only_belongs_on_keys(self):
        saved = self.commands.save_command({'trigger': 'hi', 'type': 'hello', 'send_to': 'windows'})
        self.assertNotIn('send_to', saved)

    def test_n_in_the_keys_needs_n_in_the_phrase(self):
        with self.assertRaisesRegex(ValueError, r'\{n\}'):
            self.commands.save_command({'trigger': 'desk', 'hotkey': 'ctrl+{n}'})

    def test_n_can_appear_once_in_the_phrase(self):
        with self.assertRaisesRegex(ValueError, r'\{n\}'):
            self.commands.save_command({'trigger': 'd {n} {n}', 'hotkey': 'ctrl+{n}'})

    def test_other_phrases_are_trimmed_and_repeats_dropped(self):
        saved = self.commands.save_command({'trigger': 'play spotify', 'run': 'start spotify:',
                                            'other_triggers': [' תפעיל ספוטיפיי ', '', 'Play Spotify!', 'תפעיל ספוטיפיי']})
        self.assertEqual(saved['other_triggers'], ['תפעיל ספוטיפיי'])

    def test_no_other_phrases_leaves_the_key_out(self):
        saved = self.commands.save_command({'trigger': 'hi', 'type': 'hello', 'other_triggers': ['', ' ']})
        self.assertNotIn('other_triggers', saved)

    def test_every_phrase_needs_n_when_the_keys_use_it(self):
        with self.assertRaisesRegex(ValueError, r'\{n\}'):
            self.commands.save_command({'trigger': 'd {n}', 'other_triggers': ['desk'], 'hotkey': 'ctrl+{n}'})


# Clearing every "Also say" phrase of a shipped command must stick: the
# override in the user's file says so, so the shipped phrases don't return.
class CommandOtherTriggersLayeringTests(unittest.TestCase):
    def test_clearing_a_shipped_commands_phrases_sticks(self):
        import tempfile
        import unittest.mock as mock
        reimport_under(self, fake_platform_modules())
        try:
            from koli.services import commands
        except ImportError as e:
            self.skipTest(f'services.commands needs deps the lean env lacks: {e}')
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(commands, '_commands_path', return_value=Path(tmp) / 'commands.yaml'):
            shipped = next(c for c in commands.list_commands() if c['id'] == 'next song')
            self.assertTrue(shipped['entry']['other_triggers'])
            commands.save_command({'trigger': 'next song', 'hotkey': 'media_next'}, 'next song')
            entry = next(c for c in commands.list_commands() if c['id'] == 'next song')['entry']
            self.assertFalse(entry.get('other_triggers'))
            self.assertEqual(commands.vc.command_phrases(entry), ['next song'])


# More phrases per command (any language), and the media-only filter used by
# wake-word follow-ups.
class VoiceCommandPhrasesTests(unittest.TestCase):
    def setUp(self):
        reimport_under(self, fake_platform_modules())
        try:
            import koli.voice_commands as vc
        except ImportError as e:
            self.skipTest(f'voice_commands needs deps the lean env lacks: {e}')
        self.vc = vc
        self.manager = vc.VoiceCommandManager.__new__(vc.VoiceCommandManager)
        self.manager.logger = __import__('logging').getLogger('test.voice_commands')
        self.manager.commands = [
            {'trigger': 'undo', 'hotkey': 'ctrl+z'},
            {'trigger': 'next song', 'other_triggers': ['השיר הבא'], 'hotkey': 'media_next'},
            {'trigger': 'open notepad', 'run': 'notepad.exe'},
            {'trigger': 'notes', 'other_triggers': ['open notepad plus'], 'run': 'notepad++.exe'},
            {'trigger': 'desk {n}', 'other_triggers': ['שולחן {n}'], 'hotkey': 'ctrl+{n}'},
        ]

    def test_a_hebrew_phrase_runs_its_command(self):
        self.assertEqual(self.manager.match_command('השיר הבא.')['hotkey'], 'media_next')

    def test_the_longest_phrase_wins_across_commands(self):
        self.assertEqual(self.manager.match_command('Open Notepad plus, please')['run'], 'notepad++.exe')
        self.assertEqual(self.manager.match_command('open notepad')['run'], 'notepad.exe')

    def test_n_works_in_another_phrase(self):
        self.assertEqual(self.manager.match_command('שולחן 3')['hotkey'], 'ctrl+3')

    def test_only_media_blocks_everything_else(self):
        only = self.vc.is_media_command
        self.assertIsNone(self.manager.match_command('undo', only=only))
        self.assertIsNone(self.manager.match_command('open notepad', only=only))
        self.assertEqual(self.manager.match_command('next song', only=only)['hotkey'], 'media_next')

    def test_what_counts_as_a_media_command(self):
        media = self.vc.is_media_command
        self.assertTrue(media({'hotkey': 'volume_up'}))
        self.assertTrue(media({'hotkey': 'Media_Play_Pause'}))
        self.assertFalse(media({'hotkey': 'ctrl+volume_up'}))
        self.assertFalse(media({'hotkey': 'volume_up', 'then': [{'hotkey': 'ctrl+z'}]}))
        self.assertFalse(media({'run': 'volume_up'}))

    def test_the_phrase_heard_is_reported(self):
        command = self.manager.commands[1]
        self.assertEqual(self.vc.phrase_heard(command, 'השיר הבא'), 'השיר הבא')
        self.assertEqual(self.vc.phrase_heard(command, 'next song please'), 'next song')

    def test_shipped_media_commands_load_and_are_media(self):
        import yaml
        from pathlib import Path
        shipped = yaml.safe_load((Path(self.vc.__file__).parent / self.vc.DEFAULTS_FILE).read_text(encoding='utf-8'))
        triggers = {c['trigger']: c for c in shipped['commands']}
        for trigger in ('pause music', 'next song', 'previous song', 'volume up', 'volume down', 'mute sound'):
            self.assertTrue(self.vc.is_media_command(triggers[trigger]), trigger)
            self.assertTrue(triggers[trigger].get('other_triggers'), trigger)


class AutostartTests(unittest.TestCase):
    def test_module_and_api(self):
        from koli import autostart
        for fn in ('is_supported', 'is_enabled', 'enable', 'disable', 'toggle'):
            self.assertTrue(callable(getattr(autostart, fn, None)), f"missing {fn}")

    def test_is_supported_matches_platform(self):
        import sys
        from koli import autostart
        self.assertEqual(autostart.is_supported(), sys.platform in ('win32', 'darwin'))

    def test_launch_command_nonempty(self):
        from koli import autostart
        cmd = autostart._launch_command()
        self.assertIsInstance(cmd, list)
        self.assertTrue(cmd and cmd[0])

    def test_pyapp_autostart_uses_exe_not_bare_interpreter(self):
        # Regression for issue #2. Under pyapp, sys.executable is the private
        # unpacked CPython, NOT koli.exe. Using it put a bare interpreter
        # in the Run key, so boot opened an interactive Python console instead of
        # the app. $PYAPP carries the real .exe path (PYAPP_PASS_LOCATION=1).
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import autostart
        with tempfile.TemporaryDirectory() as d:
            fake_exe = Path(d) / "koli.exe"
            fake_exe.write_text("stub", encoding="utf-8")
            unpacked_python = str(Path(d) / "python.exe")
            with mock.patch.dict(os.environ, {"PYAPP": str(fake_exe)}):
                with mock.patch.object(autostart.sys, 'executable', unpacked_python):
                    cmd = autostart._launch_command()
        self.assertEqual(cmd, [str(fake_exe)],
                         "pyapp autostart must launch the .exe, not sys.executable")
        # The whole failure mode was "a bare interpreter with no script".
        self.assertFalse(cmd[0].endswith("python.exe"))

    def test_broken_bare_interpreter_detection(self):
        # The repair must fire on the broken issue-#2 value and on nothing else,
        # so it can never clobber a healthy or user-customised Run entry.
        from koli.autostart import _is_broken_bare_interpreter as broken
        # Broken: a lone interpreter, no script to run.
        self.assertTrue(broken(r"C:\pyapp\python.exe"))
        self.assertTrue(broken(r'"C:\Program Files\pyapp\python.exe"'))
        self.assertTrue(broken(r"C:\x\pythonw.exe"))
        self.assertTrue(broken(r"C:\x\python3.exe"))
        # Healthy: has arguments, or is the app executable itself.
        self.assertFalse(broken(r"C:\x\pythonw.exe -m koli.main"))
        self.assertFalse(broken(r'"C:\Program Files\py\pythonw.exe" -m koli.main'))
        self.assertFalse(broken(r"C:\apps\koli.exe"))
        self.assertFalse(broken(""))
        self.assertFalse(broken("   "))

    def test_repair_is_noop_when_not_enabled(self):
        import unittest.mock as mock
        from koli import autostart
        with mock.patch.object(autostart, '_win_is_enabled', return_value=False):
            self.assertFalse(autostart.repair_if_broken())

    def test_repair_rewrites_only_broken_entry(self):
        import unittest.mock as mock
        from koli import autostart
        if sys.platform != 'win32':
            self.skipTest("registry repair is Windows-only")
        good = r"C:\x\pythonw.exe -m koli.main"
        with mock.patch.object(autostart, '_win_is_enabled', return_value=True):
            # Healthy entry -> untouched.
            with mock.patch.object(autostart, '_win_stored_command', return_value=good):
                with mock.patch.object(autostart, '_win_enable') as enable:
                    self.assertFalse(autostart.repair_if_broken())
                    enable.assert_not_called()
            # Broken entry -> rewritten.
            with mock.patch.object(autostart, '_win_stored_command', return_value=r"C:\p\python.exe"):
                with mock.patch.object(autostart, '_win_command_string', return_value=good):
                    with mock.patch.object(autostart, '_win_enable') as enable:
                        self.assertTrue(autostart.repair_if_broken())
                        enable.assert_called_once()

    def test_pip_install_autostart_prefers_windowless_interpreter(self):
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import autostart
        if sys.platform != 'win32':
            self.skipTest("pythonw.exe selection is Windows-only")
        with tempfile.TemporaryDirectory() as d:
            py = Path(d) / "python.exe"
            pyw = Path(d) / "pythonw.exe"
            py.write_text("stub", encoding="utf-8")
            pyw.write_text("stub", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != 'PYAPP'}
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(autostart.sys, 'executable', str(py)):
                    cmd = autostart._launch_command()
        self.assertEqual(cmd, [str(pyw), "-m", "koli.main"])

    def test_missing_pythonw_falls_back_without_crashing(self):
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import autostart
        if sys.platform != 'win32':
            self.skipTest("pythonw.exe selection is Windows-only")
        with tempfile.TemporaryDirectory() as d:
            py = Path(d) / "python.exe"
            py.write_text("stub", encoding="utf-8")  # no pythonw beside it
            env = {k: v for k, v in os.environ.items() if k != 'PYAPP'}
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(autostart.sys, 'executable', str(py)):
                    with mock.patch.object(autostart.sys, '_base_executable', str(py)):
                        cmd = autostart._launch_command()
        # Falls back to console python, but still passes -m so the APP runs.
        self.assertEqual(cmd, [str(py), "-m", "koli.main"])

    def test_macos_plist_roundtrip(self):
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import autostart
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'agent.plist'
            with mock.patch('koli.autostart._mac_plist_path', return_value=p):
                self.assertFalse(autostart._mac_is_enabled())
                autostart._mac_enable()
                self.assertTrue(autostart._mac_is_enabled())
                content = p.read_text(encoding='utf-8')
                self.assertIn('RunAtLoad', content)
                self.assertIn('com.drajb.koli', content)
                autostart._mac_disable()
                self.assertFalse(autostart._mac_is_enabled())

    def test_main_registers_autostart_flags(self):
        main_src = (ROOT / "src" / "koli" / "main.py").read_text(encoding="utf-8")
        self.assertIn('--enable-autostart', main_src)
        self.assertIn('--disable-autostart', main_src)


class DefaultsTests(unittest.TestCase):
    def test_default_model_and_recording_mode(self):
        from ruamel.yaml import YAML
        path = ROOT / "src" / "koli" / "config.defaults.yaml"
        with open(path, encoding="utf-8") as f:
            cfg = YAML().load(f)
        self.assertEqual(cfg["whisper"]["model"], "base")
        self.assertEqual(cfg["hotkey"]["recording_mode"], "push_to_talk")

    def test_profiles_no_tiny_downgrade(self):
        from ruamel.yaml import YAML
        path = ROOT / "src" / "koli" / "profiles.defaults.yaml"
        with open(path, encoding="utf-8") as f:
            data = YAML().load(f)
        for name, prof in data["profiles"].items():
            model = (prof.get("overrides", {}).get("whisper") or {}).get("model")
            self.assertNotEqual(model, "tiny", f"profile '{name}' still pins tiny")

    def test_export_covers_all_user_files(self):
        # backup/restore must include every user-editable config file.
        from koli.settings_io import EXPORTABLE_FILES
        for f in ("user_settings.yaml", "commands.yaml", "profiles.yaml",
                  "app_rules.yaml", "transforms.yaml"):
            self.assertIn(f, EXPORTABLE_FILES)


class ReviewFixTests(unittest.TestCase):
    # update_check must not crash on a "-dev" suffixed local version.
    def test_is_newer_handles_dev_suffix(self):
        from koli.update_check import _is_newer
        self.assertTrue(_is_newer("0.12.0", "0.11.0-dev"))
        self.assertFalse(_is_newer("0.11.0", "0.11.0-dev"))   # same core, not newer
        self.assertFalse(_is_newer("1.2", "1.2.0"))           # padded equal
        self.assertTrue(_is_newer("v1.3.0", "1.2.9"))         # leading v tolerated

    # settings_ui._coerce must keep free-text settings as strings even when numeric-looking.
    def test_coerce_is_type_aware(self):
        from koli.settings_ui import _coerce
        sentinel = object()  # not a BooleanVar
        self.assertEqual(_coerce(sentinel, "2024", "whisper.initial_prompt"), "2024")
        self.assertEqual(_coerce(sentinel, "3", "postprocess.ollama.model"), "3")
        self.assertEqual(_coerce(sentinel, "5", "whisper.beam_size"), 5)
        self.assertAlmostEqual(_coerce(sentinel, "0.75", "audio.noise_suppression.strength"), 0.75)

    # autostart.toggle returns the achieved state (macOS path, temp plist).
    def test_autostart_toggle_returns_state(self):
        import sys
        if sys.platform != "darwin":
            self.skipTest("toggle round-trip exercised via macOS plist path only")
        import tempfile, unittest.mock as mock
        from pathlib import Path
        from koli import autostart
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("koli.autostart._mac_plist_path", return_value=Path(d) / "a.plist"):
                self.assertTrue(autostart.toggle())
                self.assertFalse(autostart.toggle())


class StreamingDeliveryDecisionTests(unittest.TestCase):
    def _cfg(self, on=True):
        return {'deliver_to_cursor': on}

    def test_off_by_default(self):
        from koli.streaming_delivery import decide_stream_delivery
        self.assertFalse(decide_stream_delivery({}, True, True, True, None))

    def test_all_conditions_met(self):
        from koli.streaming_delivery import decide_stream_delivery
        self.assertTrue(decide_stream_delivery(self._cfg(), True, True, True, None))

    def test_requires_streaming_available(self):
        from koli.streaming_delivery import decide_stream_delivery
        self.assertFalse(decide_stream_delivery(self._cfg(), False, True, True, None))

    def test_requires_auto_paste(self):
        from koli.streaming_delivery import decide_stream_delivery
        self.assertFalse(decide_stream_delivery(self._cfg(), True, False, True, None))

    def test_requires_textable_foreground(self):
        from koli.streaming_delivery import decide_stream_delivery
        self.assertFalse(decide_stream_delivery(self._cfg(), True, True, False, None))

    def test_respects_app_rule_suppress_and_copyonly(self):
        from koli.streaming_delivery import decide_stream_delivery
        self.assertFalse(decide_stream_delivery(self._cfg(), True, True, True, {'suppress': True}))
        self.assertFalse(decide_stream_delivery(self._cfg(), True, True, True, {'auto_paste': False}))
        # a rule that doesn't touch delivery is fine
        self.assertTrue(decide_stream_delivery(self._cfg(), True, True, True, {'initial_prompt': 'x'}))


class StreamingDeliveryWorkerTests(unittest.TestCase):
    def test_segments_delivered_in_order_and_recorded(self):
        from koli.streaming_delivery import StreamingDelivery
        delivered = []
        sd = StreamingDelivery(deliver_fn=delivered.append)
        sd.start()
        sd.submit_final("hello")
        sd.submit_final("world")
        full = sd.stop()
        self.assertEqual(delivered, ["hello ", "world "])
        self.assertEqual(full, "hello world")
        self.assertTrue(sd.submitted_any)

    def test_blank_segments_ignored(self):
        from koli.streaming_delivery import StreamingDelivery
        delivered = []
        sd = StreamingDelivery(deliver_fn=delivered.append)
        sd.start()
        sd.submit_final("   ")
        sd.submit_final("")
        self.assertEqual(sd.stop(), "")
        self.assertEqual(delivered, [])
        self.assertFalse(sd.submitted_any)

    def test_stop_is_idempotent(self):
        from koli.streaming_delivery import StreamingDelivery
        sd = StreamingDelivery(deliver_fn=lambda s: None)
        sd.start()
        sd.submit_final("x")
        self.assertEqual(sd.stop(), "x")
        self.assertEqual(sd.stop(), "x")  # second stop returns same, no error

    def test_deliver_fn_exception_does_not_crash_and_flags_failure(self):
        from koli.streaming_delivery import StreamingDelivery
        def boom(_):
            raise RuntimeError("boom")
        sd = StreamingDelivery(deliver_fn=boom)
        sd.start()
        sd.submit_final("x")
        # stop() must still return cleanly even though delivery raised
        self.assertEqual(sd.stop(), "")
        self.assertTrue(sd.had_failure)

    def test_no_failure_flag_on_clean_delivery(self):
        from koli.streaming_delivery import StreamingDelivery
        sd = StreamingDelivery(deliver_fn=lambda s: None)
        sd.start()
        sd.submit_final("ok")
        sd.stop()
        self.assertFalse(sd.had_failure)

    def test_submit_after_stop_is_ignored(self):
        # Closes the submit/stop race: a late segment after stop is dropped, not enqueued.
        from koli.streaming_delivery import StreamingDelivery
        delivered = []
        sd = StreamingDelivery(deliver_fn=delivered.append)
        sd.start()
        sd.submit_final("a")
        sd.stop()
        sd.submit_final("late")
        self.assertEqual(delivered, ["a "])


if __name__ == "__main__":
    unittest.main()


class MergedFeatureTests(unittest.TestCase):
    """Features merged in 0.18.0, pinned here so a
    future refactor can't quietly drop them."""

    # --- AMD GPU classification ---
    def test_amd_gpu_classification(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'wk_gpu', str(ROOT / 'src' / 'koli' / 'platform' / 'windows' / 'gpu.py'))
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        cases = [
            # RX 580 is Polaris, NOT RDNA. The old one-digit match classified it
            # as RDNA1 and offered a runtime that cannot drive it.
            ('AMD Radeon RX 580', None),
            ('AMD Radeon RX 5700 XT', 'amd_rdna1'),
            ('RX5700', 'amd_rdna1'),          # vendor strings omit the space
            ('AMD Radeon RX 7900 XTX', 'amd_rdna2+'),
            ('AMD Radeon RX 9070 XT', 'amd_rdna2+'),
            # Strix Halo / Ryzen AI MAX report no "RX" at all.
            ('AMD Radeon 8060S Graphics', 'amd_rdna2+'),
            ('AMD Radeon 8040S', 'amd_rdna2+'),
            ('AMD Radeon 780M', None),
        ]
        for name, expected in cases:
            self.assertEqual(gpu._classify_gpu('amd', name), expected, f"for {name!r}")
        self.assertEqual(gpu._classify_gpu('nvidia', 'GeForce RTX 4090'), 'nvidia')

    # --- Vocabulary corrections ---
    def test_corrections_map_many_variants_to_one_term(self):
        from koli.text_postprocess import postprocess
        cfg = {'corrections': {'CAPEX': ['cap x', 'copics'], 'MySQL': ['my sequel']}}
        self.assertEqual(postprocess('the cap x budget', cfg), 'the CAPEX budget')
        self.assertEqual(postprocess('we use copics here', cfg), 'we use CAPEX here')
        self.assertEqual(postprocess('I love my sequel', cfg), 'I love MySQL')

    def test_corrections_prefer_longest_variant(self):
        # "cap" must not shadow "cap x" and leave a dangling "x".
        from koli.text_postprocess import postprocess
        cfg = {'corrections': {'CAP': ['cap'], 'CAPEX': ['cap x']}}
        self.assertEqual(postprocess('the cap x budget', cfg), 'the CAPEX budget')

    def test_corrections_are_whole_word_and_case_insensitive(self):
        from koli.text_postprocess import postprocess
        cfg = {'corrections': {'MySQL': ['my sequel']}}
        self.assertEqual(postprocess('MY SEQUEL rocks', cfg), 'MySQL rocks')
        cfg2 = {'corrections': {'DOG': ['cat']}}
        self.assertEqual(postprocess('category cat', cfg2), 'category DOG')

    def test_corrections_tolerate_malformed_config(self):
        from koli.text_postprocess import postprocess
        for bad in ({'corrections': 'nope'}, {'corrections': {'A': None}},
                    {'corrections': {'A': 123}}, {'corrections': []},
                    {'corrections': {'A': {'not': 'a list'}}}):
            self.assertEqual(postprocess('some text', bad), 'some text', f"for {bad}")

    def test_corrections_and_replacements_coexist(self):
        # Our per-entry `replacements` must still work alongside the new map.
        from koli.text_postprocess import postprocess
        cfg = {'corrections': {'CAPEX': ['cap x']},
               'replacements': [{'from': 'budget', 'to': 'spend'}]}
        self.assertEqual(postprocess('the cap x budget', cfg), 'the CAPEX spend')

    # --- Terminal tab title ---
    def test_terminal_title_parses_static_and_animated_states(self):
        from koli.terminal_title import TerminalTitle
        t = TerminalTitle({'idle': '', 'recording': [['R', 0.1], ['  ', 0.2]],
                           'processing': '...'})
        self.assertEqual(t._frames['idle'], [('Koli', 60.0)])
        self.assertEqual(t._frames['recording'],
                         [('R Koli', 0.1), ('   Koli', 0.2)])
        self.assertEqual(t._frames['processing'], [('... Koli', 60.0)])

    def test_terminal_title_falls_back_on_bad_frames(self):
        from koli.terminal_title import TerminalTitle
        t = TerminalTitle({'recording': [['R', 'not-a-number']], 'idle': {'bad': 1}})
        # Bad frames fall back to the shipped defaults rather than raising.
        self.assertTrue(t._frames['recording'])
        self.assertEqual(t._frames['idle'], [('Koli', 60.0)])

    def test_terminal_title_lifecycle_is_safe_without_a_tty(self):
        from koli.terminal_title import TerminalTitle
        t = TerminalTitle({})
        t.update_state('recording')
        t.start()
        t.stop()  # must not raise even when disabled

    # --- Startup ready sound ---
    def test_ready_sound_wired_and_asset_present(self):
        import inspect
        # The asset must ship regardless of whether the audio backend is
        # installed, so assert that before the import that may be skipped.
        asset = ROOT / 'src' / 'koli' / 'assets' / 'sounds' / 'app_ready.wav'
        self.assertTrue(asset.is_file(), 'app_ready.wav must ship with the package')
        # audio_feedback pulls playsound3, which the lean CI env lacks.
        try:
            from koli.audio_feedback import AudioFeedback
        except Exception:
            self.skipTest('audio_feedback not importable (playsound3 absent)')
        params = inspect.signature(AudioFeedback.__init__).parameters
        self.assertIn('ready_enabled', params)
        self.assertIn('ready_sound', params)
        self.assertTrue(hasattr(AudioFeedback, 'play_ready_sound'))

    # --- config defaults for all of the above ---
    def test_new_config_sections_present_and_inert(self):
        from ruamel.yaml import YAML
        with open(ROOT / 'src' / 'koli' / 'config.defaults.yaml', encoding='utf-8') as f:
            cfg = YAML().load(f)
        self.assertEqual(cfg['postprocess']['corrections'], {})
        self.assertTrue(cfg['audio_feedback']['ready_enabled'])
        self.assertEqual(cfg['audio_feedback']['ready_sound'],
                         'assets/sounds/app_ready.wav')
        self.assertIn('terminal_title', cfg)
        for state in ('idle', 'recording', 'processing'):
            self.assertIn(state, cfg['terminal_title'])
        # Shipping defaults must not alter ordinary text.
        from koli.text_postprocess import postprocess
        self.assertEqual(postprocess('hello world', dict(cfg['postprocess'])), 'hello world')


class ReportedIssueTests(unittest.TestCase):
    """Bugs reported by users, pinned so they cannot come back."""

    # --- #6: pause hotkey permanently disabled every hotkey (Windows) ---
    def test_windows_register_replaces_bindings(self):
        # global-hotkeys keeps registrations across stop_checking_hotkeys(), so a
        # second register() raised "already registered". That exception aborted
        # the pause path before start() ran, leaving every hotkey dead — pause
        # included, so there was no way back short of restarting the app.
        if sys.platform != 'win32':
            self.skipTest('global-hotkeys is Windows-only')
        try:
            from koli.platform.windows import hotkeys
        except Exception:
            self.skipTest('global_hotkeys not installed')
        full = [['ctrl+shift+f13', lambda: None, None],
                ['ctrl+alt+f14', lambda: None, None]]
        pause_only = [['ctrl+alt+f14', lambda: None, None]]
        try:
            # The exact sequence the pause hotkey performs, twice over.
            for _ in range(2):
                hotkeys.register(full)
                hotkeys.start()
                hotkeys.stop()
                hotkeys.register(pause_only)   # raised before the fix
                hotkeys.start()
                hotkeys.stop()
        finally:
            try:
                hotkeys.stop()
            except Exception:
                pass

    def test_windows_register_clears_first(self):
        # The mechanism behind the fix: register() must have replace semantics,
        # matching the macOS mirror, per docs/platform-abstraction.md.
        source = (ROOT / 'src' / 'koli' / 'platform' / 'windows' / 'hotkeys.py').read_text(encoding='utf-8')
        self.assertIn('clear_hotkeys()', source,
                      'register() must clear before registering (issue #6)')

    # --- #3: tray Restart broke on pip installs (Windows) ---
    def test_relaunch_command_never_uses_argv(self):
        # argv[0] for a pip console script is the launcher path, and pip ships
        # only a compiled .exe stub there — handing it to python.exe fails with
        # "can't open file". The command must invoke the module instead.
        from koli.utils import build_relaunch_command
        import os
        saved = os.environ.pop('PYAPP', None)
        try:
            cmd = build_relaunch_command()
        finally:
            if saved is not None:
                os.environ['PYAPP'] = saved
        self.assertIn('-m', cmd)
        self.assertIn('koli.main', cmd)
        for part in cmd:
            self.assertFalse(
                part.endswith('Scripts\\koli') or part.endswith('Scripts/koli'),
                f'relaunch command must not reference the console script: {cmd}')

    def test_relaunch_command_prefers_pyapp_executable(self):
        # The standalone build must relaunch the .exe, not pyapp's private
        # interpreter (the issue #2 failure mode, shared by this code path).
        import os
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import utils
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / 'koli.exe'
            fake.write_text('stub', encoding='utf-8')
            with mock.patch.dict(os.environ, {'PYAPP': str(fake)}):
                with mock.patch.object(utils.sys, 'executable', str(Path(d) / 'python.exe')):
                    self.assertEqual(utils.build_relaunch_command(), [str(fake)])

    def test_windowless_variant_used_only_for_autostart(self):
        # Autostart wants pythonw (no console at login); the tray restart wants
        # the console-visible interpreter so a terminal user keeps their console.
        import os
        if sys.platform != 'win32':
            self.skipTest('pythonw selection is Windows-only')
        from koli.utils import build_relaunch_command
        saved = os.environ.pop('PYAPP', None)
        try:
            windowed = build_relaunch_command(windowless=False)
            hidden = build_relaunch_command(windowless=True)
        finally:
            if saved is not None:
                os.environ['PYAPP'] = saved
        self.assertTrue(windowed[0].lower().endswith('python.exe'))
        self.assertTrue(hidden[0].lower().endswith('pythonw.exe'))

    def test_autostart_delegates_to_shared_builder(self):
        # The two call sites disagreed once (autostart fixed for #2, tray left
        # broken for #3). They must share one implementation now.
        try:
            from koli import autostart
        except Exception:
            self.skipTest('autostart not importable on this platform')
        from koli.utils import build_relaunch_command
        self.assertEqual(autostart._launch_command(), build_relaunch_command(windowless=True))

    # --- #4: macOS hotkeys must be able to suppress the underlying key ---
    def test_macos_hotkeys_use_event_tap_with_fallback(self):
        source = (ROOT / 'src' / 'koli' / 'platform' / 'macos' / 'hotkeys.py').read_text(encoding='utf-8')
        self.assertIn('CGEventTapCreate', source, 'event tap needed to consume keystrokes')
        # The NSEvent monitor must survive as the no-permission fallback.
        self.assertIn('addGlobalMonitorForEventsMatchingMask_handler_', source)
        # A disabled tap must be re-enabled or hotkeys silently die after a timeout.
        self.assertIn('kCGEventTapDisabledByTimeout', source)


class ReportedIssueHandlerTests(unittest.TestCase):
    """Drive the real handlers, not just the primitives underneath them."""

    # --- #6: the actual pause handler must survive a full round trip ---
    def _listener(self):
        import unittest.mock as mock
        try:
            from koli.hotkey_listener import HotkeyListener
        except Exception:
            self.skipTest('hotkey_listener not importable on this platform')
        # Build without __init__ so no real hotkeys are registered by construction.
        listener = HotkeyListener.__new__(HotkeyListener)
        listener.logger = __import__('logging').getLogger('test')
        listener.is_paused = False
        listener.pause_hotkey = 'ctrl+alt+f14'
        listener.hotkey_bindings = [
            ['ctrl+shift+f13', lambda: None, None],
            ['ctrl+alt+f14', lambda: None, None],
        ]
        listener.state_manager = mock.Mock()
        return listener

    def test_pause_never_touches_registrations(self):
        # Issue #7. Re-registering from inside a hotkey callback is unsafe with
        # global-hotkeys 0.1.7: the checker iterates a LIVE view of its bindings
        # dict and calls back from inside that loop, so clearing mid-iteration
        # raises RuntimeError and kills the thread; and its stop() never joins,
        # so restarting while the chord is still held re-fires pause instantly.
        # Pause must therefore only flip a flag.
        import io
        import contextlib
        import unittest.mock as mock
        listener = self._listener()
        try:
            from koli.platform import hotkeys as hk
        except Exception:
            self.skipTest('platform hotkeys not importable')

        with mock.patch.object(hk, 'register') as reg, \
             mock.patch.object(hk, 'start') as start, \
             mock.patch.object(hk, 'stop') as stop:
            with contextlib.redirect_stdout(io.StringIO()):
                listener._pause_hotkey_pressed()   # pause
                listener._pause_hotkey_pressed()   # resume
        reg.assert_not_called()
        start.assert_not_called()
        stop.assert_not_called()
        self.assertFalse(listener.is_paused, 'second press must resume')

    def test_pause_gates_other_callbacks_but_not_itself(self):
        # The flag is only useful if it actually suppresses the other hotkeys —
        # and if pause itself stays live, otherwise you could never un-pause.
        import io
        import contextlib
        listener = self._listener()
        fired = []
        gated = listener._gated(lambda: fired.append('record'), is_pause=False)
        pause_cb = listener._gated(lambda: fired.append('pause'), is_pause=True)

        gated(); pause_cb()
        self.assertEqual(fired, ['record', 'pause'], 'both fire while running')

        fired.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            listener._pause_hotkey_pressed()          # now paused
        gated(); pause_cb()
        self.assertEqual(fired, ['pause'],
                         'while paused only the pause key may fire (issue #7)')

        fired.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            listener._pause_hotkey_pressed()          # resumed
        gated()
        self.assertEqual(fired, ['record'], 'resume must re-enable the others')

    def test_gate_passes_through_none_release_callbacks(self):
        # Bindings without a release callback must stay None, not become a
        # wrapper that the backend would try to call.
        listener = self._listener()
        self.assertIsNone(listener._gated(None, is_pause=False))
        self.assertIsNone(listener._gated(None, is_pause=True))

    def test_pause_state_is_reported_to_state_manager(self):
        import io
        import contextlib
        listener = self._listener()
        with contextlib.redirect_stdout(io.StringIO()):
            listener._pause_hotkey_pressed()
            listener._pause_hotkey_pressed()
        calls = [c.args[0] for c in listener.state_manager.set_paused.call_args_list]
        self.assertEqual(calls, [True, False])

    # --- #3: the actual tray handler must spawn a runnable command ---
    def test_tray_restart_spawns_a_runnable_command(self):
        import unittest.mock as mock
        try:
            from koli.system_tray import SystemTray
        except Exception:
            self.skipTest('system_tray not importable on this platform')
        tray = SystemTray.__new__(SystemTray)
        tray.logger = __import__('logging').getLogger('test')
        tray.notify = lambda *a, **k: None

        spawned = {}
        with mock.patch('subprocess.Popen', side_effect=lambda cmd, *a, **k: spawned.update(cmd=cmd)), \
             mock.patch('os.kill') as killed:
            tray._restart_application_from_tray()
        cmd = spawned.get('cmd')
        self.assertTrue(cmd, 'restart must spawn something')
        self.assertTrue(killed.called, 'the old instance must exit after a successful spawn')
        # It must be runnable: either the app executable, or python -m.
        if len(cmd) > 1:
            self.assertIn('-m', cmd)
            self.assertIn('koli.main', cmd)

    def test_tray_restart_does_not_exit_when_spawn_fails(self):
        # A failed relaunch must never leave the user with no app at all.
        import unittest.mock as mock
        try:
            from koli.system_tray import SystemTray
        except Exception:
            self.skipTest('system_tray not importable on this platform')
        tray = SystemTray.__new__(SystemTray)
        tray.logger = __import__('logging').getLogger('test')
        notes = []
        tray.notify = lambda msg, *a, **k: notes.append(msg)
        with mock.patch('subprocess.Popen', side_effect=OSError('boom')), \
             mock.patch('os.kill') as killed:
            tray._restart_application_from_tray()
        self.assertFalse(killed.called, 'must NOT kill the running app if relaunch failed')
        self.assertTrue(notes, 'user must be told the restart failed')


class MacOSHotkeyTests(unittest.TestCase):
    """Real coverage for the #4 event-tap patch. Skips off macOS / without
    pyobjc, but CI installs pyobjc on the macOS runner so this actually runs."""

    def _module(self):
        if sys.platform != 'darwin':
            self.skipTest('macOS-only')
        try:
            from koli.platform.macos import hotkeys
        except Exception as e:
            self.skipTest(f'pyobjc not available: {e}')
        return hotkeys

    def test_module_imports_and_quartz_symbols_resolve(self):
        # The main risk in code I could not run: a mistyped Quartz symbol would
        # only surface at import time on a real Mac.
        hotkeys = self._module()
        for name in ('CGEventTapCreate', 'CGEventTapEnable', 'CGEventMaskBit',
                     'CGEventGetIntegerValueField', 'CFMachPortCreateRunLoopSource',
                     'CFRunLoopAddSource', 'CFRunLoopGetCurrent', 'CFRunLoopStop',
                     'kCGEventKeyDown', 'kCGEventKeyUp', 'kCGEventFlagsChanged',
                     'kCGEventTapDisabledByTimeout', 'kCGSessionEventTap',
                     'kCGKeyboardEventKeycode'):
            self.assertTrue(hasattr(hotkeys, name), f'Quartz symbol missing: {name}')

    def test_register_and_stop_are_safe_without_permission(self):
        # stop() must be callable even when start() never created a tap, and
        # must not raise when there is nothing to tear down.
        hotkeys = self._module()
        hotkeys.register([['ctrl+shift+f13', lambda: None, None]])
        hotkeys.stop()
        hotkeys.stop()  # idempotent

    def test_key_down_dispatch_reports_whether_it_matched(self):
        # The patch changed _handle_key_down to return True/False so the tap can
        # decide whether to consume the event. A silent revert to None would make
        # every keystroke pass through again — the exact bug #4 reported.
        import inspect
        hotkeys = self._module()
        source = inspect.getsource(hotkeys._handle_key_down)
        self.assertIn('return True', source)
        self.assertIn('return False', source)


class UninstallSafetyTests(unittest.TestCase):
    """--uninstall is the only code here that deletes user data. These pin the
    safety properties, especially that a SHARED HuggingFace cache is respected."""

    def test_only_our_models_are_ever_selected(self):
        # The cache holds other tools' models (CLIP, diarization, ...). Selecting
        # one of those for deletion would destroy unrelated work.
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import uninstall
        with tempfile.TemporaryDirectory() as d:
            hub = Path(d) / 'hub'
            hub.mkdir()
            ours = ['models--Systran--faster-whisper-base',
                    'models--Systran--faster-distil-whisper-small.en',
                    'models--openai--whisper-tiny']
            theirs = ['models--openai--clip-vit-base-patch32',
                      'models--pyannote--speaker-diarization-community-1',
                      '.locks', 'models--meta-llama--Llama-3']
            for name in ours + theirs:
                (hub / name).mkdir()
                (hub / name / 'blob').write_bytes(b'x' * 10)
            with mock.patch.object(uninstall, '_hf_hub_root', return_value=hub):
                selected = {p.name for p, _ in uninstall._our_cached_models()}
        self.assertEqual(selected, set(ours))
        for name in theirs:
            self.assertNotIn(name, selected, f'must never select {name}')

    def test_no_models_selected_when_cache_absent(self):
        import unittest.mock as mock
        from pathlib import Path
        from koli import uninstall
        with mock.patch.object(uninstall, '_hf_hub_root', return_value=Path('/nonexistent-xyz')):
            self.assertEqual(uninstall._our_cached_models(), [])

    def test_declining_removes_nothing(self):
        # Answering "no" at the first prompt must leave everything untouched.
        import io
        import contextlib
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import uninstall
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / 'koli'
            cfg.mkdir()
            (cfg / 'user_settings.yaml').write_text('x', encoding='utf-8')
            with mock.patch.object(uninstall, 'get_user_app_data_path', return_value=str(cfg)),                  mock.patch.object(uninstall, '_our_cached_models', return_value=[]),                  mock.patch.object(uninstall, '_confirm', return_value=False):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = uninstall.run_uninstall()
            self.assertEqual(rc, 1)
            self.assertTrue(cfg.exists(), 'declining must not delete anything')
            self.assertTrue((cfg / 'user_settings.yaml').exists())

    def test_models_need_their_own_confirmation(self):
        # Confirming the settings removal must NOT imply consent to delete
        # gigabytes of models -- that is a second, separate decision.
        import io
        import contextlib
        import tempfile
        import unittest.mock as mock
        from pathlib import Path
        from koli import uninstall
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / 'koli'; cfg.mkdir()
            model = Path(d) / 'models--Systran--faster-whisper-base'; model.mkdir()
            (model / 'model.bin').write_bytes(b'x' * 100)
            answers = iter([True, False])   # yes to settings, NO to models
            with mock.patch.object(uninstall, 'get_user_app_data_path', return_value=str(cfg)),                  mock.patch.object(uninstall, '_our_cached_models', return_value=[(model, 100)]),                  mock.patch.object(uninstall, '_confirm', side_effect=lambda *a: next(answers)):
                with contextlib.redirect_stdout(io.StringIO()):
                    uninstall.run_uninstall()
            self.assertFalse(cfg.exists(), 'settings should have been removed')
            self.assertTrue(model.exists(), 'models must survive a "no" on the second prompt')

    def test_clean_machine_reports_nothing_to_do(self):
        import io
        import contextlib
        import unittest.mock as mock
        from koli import autostart, uninstall
        # The real Start-on-login entry must not decide this test.
        with mock.patch.object(uninstall, 'get_user_app_data_path', return_value='/nonexistent-xyz'),              mock.patch.object(uninstall, '_our_cached_models', return_value=[]),              mock.patch.object(autostart, 'is_enabled', return_value=False),              mock.patch.object(uninstall, '_confirm', side_effect=AssertionError('must not prompt')):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = uninstall.run_uninstall()
        self.assertEqual(rc, 0)
        self.assertIn('already clean', buf.getvalue())


class UserReportedSeptemberTests(unittest.TestCase):
    """Issues #9-#13, reported by users running 0.18.3."""

    # --- #9: filler stripping destroyed inline-formatting newlines ---
    def _fmt_cfg(self, **extra):
        cfg = {'inline_formatting': True, 'inline_formatting_absorb_punctuation': True,
               'inline_formatting_replacements': [
                   {'phrase': 'period', 'replacement': '. '},
                   {'phrase': 'new line', 'replacement': LF},
                   {'phrase': 'new paragraph', 'replacement': LF + LF}]}
        cfg.update(extra)
        return cfg

    def test_filler_stripping_preserves_line_structure(self):
        from koli.text_postprocess import postprocess
        text = 'alpha like period new paragraph bravo new line charlie'
        without = postprocess(text, self._fmt_cfg(strip_filler_words=False))
        with_strip = postprocess(text, self._fmt_cfg(strip_filler_words=True))
        # The ONLY difference may be the filler word itself.
        self.assertEqual(without.replace(' like', ''), with_strip)
        self.assertIn(LF + LF, with_strip, 'paragraph break must survive (issue #9)')

    def test_filler_stripping_still_removes_fillers(self):
        from koli.text_postprocess import postprocess
        got = postprocess('um hello uh there you know friend', {'strip_filler_words': True})
        for filler in ('um ', 'uh ', 'you know'):
            self.assertNotIn(filler, got)
        self.assertIn('hello', got)

    def test_filler_stripping_does_not_collapse_paragraphs(self):
        # The second half of the bug: even newlines that survived the filler
        # pattern were flattened by a blanket whitespace collapse.
        from koli.text_postprocess import postprocess
        self.assertEqual(postprocess('one' + LF + LF + 'two', {'strip_filler_words': True}),
                         'one' + LF + LF + 'two')
        # Runs of plain spaces SHOULD still collapse.
        self.assertEqual(postprocess('one    two', {'strip_filler_words': True}), 'one two')

    # --- #10: --history killed its own window ---
    def test_window_launchers_return_their_thread(self):
        # main.py waits on this thread. Returning None again would restore the
        # bug: the window lives on a daemon thread and sys.exit kills it.
        import inspect
        for module, func in (('history_window', 'show_history'),
                             ('cheat_sheet', 'show_cheat_sheet')):
            try:
                mod = __import__('koli.' + module, fromlist=[func])
            except Exception:
                self.skipTest(module + ' not importable on this platform')
            source = inspect.getsource(getattr(mod, func))
            self.assertIn('return thread', source,
                          func + ' must return its thread so the CLI can join it (issue #10)')

    def test_history_cli_joins_instead_of_sleeping(self):
        source = (ROOT / 'src' / 'koli' / 'main.py').read_text(encoding='utf-8')
        block = source[source.index('if args.history:'):source.index('if args.enable_autostart:')]
        self.assertIn('.join()', block)
        self.assertNotIn('time.sleep(0.5)', block,
                         'sleeping then exiting kills the daemon window (issue #10)')

    # --- #12: clipboard-free dictation leaked to the clipboard ---
    def _clipboard(self, method, also_copy):
        # clipboard_manager pulls the platform keyboard backend and PIL,
        # neither of which the lean CI env installs.
        try:
            from koli.clipboard_manager import ClipboardManager
        except Exception:
            self.skipTest('clipboard_manager not importable in this env')
        c = ClipboardManager.__new__(ClipboardManager)
        c.delivery_method = method
        c.type_also_copy_to_clipboard = also_copy
        return c

    def test_silent_copy_policy(self):
        self.assertFalse(self._clipboard('type', False).silent_copy_allowed,
                         'clipboard-free dictation must stay clipboard-free (issue #12)')
        self.assertTrue(self._clipboard('type', True).silent_copy_allowed)
        # Paste delivery needs the clipboard by definition.
        self.assertTrue(self._clipboard('paste', False).silent_copy_allowed)

    def test_fallback_window_respects_clipboard_policy(self):
        import inspect
        from koli import fallback_window
        params = inspect.signature(fallback_window.FallbackWindow.show).parameters
        self.assertIn('allow_clipboard', params)
        body = inspect.getsource(fallback_window.FallbackWindow._run_window)
        self.assertIn('if allow_clipboard:', body,
                      'the popup must not auto-copy when copying is off')
        # The explicit Copy button stays unconditional - that is the user asking.
        self.assertIn('def _copy_again', body)

    def test_recovery_paths_check_the_policy(self):
        source = (ROOT / 'src' / 'koli' / 'state_manager.py').read_text(encoding='utf-8')
        self.assertEqual(source.count('silent_copy_allowed'), 2,
                         'both recovery paths (suppress + no-text-field) must check it')

    # --- #11: an app rule silencing paste looked like a broken app ---
    def test_copy_only_is_announced_once_per_rule(self):
        import unittest.mock as mock
        try:
            from koli.state_manager import StateManager
        except Exception:
            self.skipTest('state_manager not importable on this platform')
        sm = StateManager.__new__(StateManager)
        sm.logger = __import__('logging').getLogger('test')
        sm.system_tray = mock.Mock()
        rule = {'match': ['code.exe', 'cursor.exe'], 'auto_paste': False}
        sm._announce_copy_only(rule)
        sm._announce_copy_only(rule)                    # same rule again
        sm._announce_copy_only({'match': ['slack.exe']})
        self.assertEqual(sm.system_tray.notify.call_count, 2,
                         'once per rule, not once per dictation')
        said = sm.system_tray.notify.call_args_list[0].args[0]
        self.assertIn('code.exe', said)
        self.assertIn('app_rules.yaml', said, 'tell them where to change it')

    # --- #13: macOS 27 SIGTRAP from off-main-thread tray writes ---
    def test_platform_exposes_ui_thread_marshal(self):
        # Importing the platform package loads the OS backend (win32api /
        # AppKit), which the lean CI env doesn't install.
        try:
            from koli.platform import app as platform_app
        except Exception:
            self.skipTest('platform backend not importable in this env')
        self.assertTrue(hasattr(platform_app, 'run_on_ui_thread'))
        ran = []
        platform_app.run_on_ui_thread(lambda: ran.append(1))
        self.assertEqual(ran, [1], 'must actually run the callable')

    def test_no_unguarded_tray_writes_remain(self):
        # Every AppKit-touching write must go through _on_ui_thread. A direct
        # assignment from a worker thread is an instant SIGTRAP on macOS 27.
        import re
        source = (ROOT / 'src' / 'koli' / 'system_tray.py').read_text(encoding='utf-8')
        offenders = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if re.match(r'^self[.]icon[.](icon|menu|title)\s*=', stripped):
                offenders.append(str(i) + ': ' + stripped)
            if stripped.startswith('self.icon.notify(') and '_on_ui_thread' not in line:
                offenders.append(str(i) + ': ' + stripped)
        self.assertEqual(offenders, [],
                         'tray writes must be marshalled to the UI thread (issue #13)')

    def test_macos_marshal_uses_main_queue(self):
        source = (ROOT / 'src' / 'koli' / 'platform' / 'macos' / 'app.py').read_text(encoding='utf-8')
        self.assertIn('NSThread.isMainThread()', source, 'fast path when already on main')
        self.assertIn('mainQueue', source, 'must dispatch to the main queue')


class UserReportedLateSeptemberTests(unittest.TestCase):
    """Issues #14 and #15, reported by users running 0.19.0."""

    # Load a platform backend file directly, so it can be exercised on any OS
    # with its OS-only dependencies faked out.
    def _load(self, relative_path, name):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            name, str(ROOT / 'src' / 'koli' / relative_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # --- #15: pip-installed CUDA libraries were on no DLL search path ---
    def test_nvidia_dll_dirs_are_found_in_site_packages(self):
        import tempfile
        from koli.utils import find_nvidia_dll_dirs
        with tempfile.TemporaryDirectory() as root:
            for lib in ('cublas', 'cudnn', 'cuda_runtime'):
                (Path(root) / 'nvidia' / lib / 'bin').mkdir(parents=True)
            (Path(root) / 'nvidia' / 'cudnn' / 'include').mkdir()
            found = find_nvidia_dll_dirs([root, root])  # duplicate root on purpose
        names = sorted(Path(d).parent.name for d in found)
        self.assertEqual(names, ['cublas', 'cuda_runtime', 'cudnn'], 'each bin dir exactly once')

    def test_nvidia_dll_path_registers_both_loaders_on_windows(self):
        import unittest.mock as mock
        from koli import utils
        dirs = [os.path.join('sp', 'nvidia', 'cublas', 'bin'),
                os.path.join('sp', 'nvidia', 'cudnn', 'bin')]
        added = []
        with mock.patch.object(utils.sys, 'platform', 'win32'), \
             mock.patch.object(utils, 'find_nvidia_dll_dirs', return_value=dirs), \
             mock.patch.object(utils.os, 'add_dll_directory', added.append, create=True), \
             mock.patch.dict(os.environ, {'PATH': 'existing'}):
            utils.setup_nvidia_dll_path()
            utils.setup_nvidia_dll_path()  # idempotent: no PATH growth
            path_parts = os.environ['PATH'].split(os.pathsep)
        # add_dll_directory serves ctypes/extension loads; PATH serves the plain
        # LoadLibrary calls ctranslate2 and cuDNN make. Both are needed.
        self.assertEqual(added[:2], dirs)
        self.assertEqual(path_parts, dirs + ['existing'])

    def test_nvidia_dll_path_is_a_no_op_off_windows(self):
        import unittest.mock as mock
        from koli import utils
        with mock.patch.object(utils.sys, 'platform', 'darwin'), \
             mock.patch.object(utils, 'find_nvidia_dll_dirs') as finder:
            utils.setup_nvidia_dll_path()
        finder.assert_not_called()

    def test_dll_path_is_set_up_before_the_ml_stack_imports(self):
        source = (ROOT / 'src' / 'koli' / 'main.py').read_text(encoding='utf-8')
        self.assertLess(source.index('setup_nvidia_dll_path()'),
                        source.index('from .whisper_engine import'))

    def _gpu_probe(self, loadable, compute_types=('float16',)):
        import types
        import unittest.mock as mock
        gpu = self._load('platform/windows/gpu.py', 'wk_gpu_probe')

        def win_dll(name, winmode=None):
            # winmode=0 means the plain LoadLibrary search (PATH included),
            # which is what ctranslate2 itself uses.
            self.assertEqual(winmode, 0)
            if name not in loadable:
                raise OSError(f'{name} not found')
        gpu.ctypes = types.SimpleNamespace(WinDLL=win_dll)
        ct2 = types.ModuleType('ctranslate2')
        ct2.get_supported_compute_types = lambda device: list(compute_types)
        patcher = mock.patch.dict(sys.modules, {'ctranslate2': ct2})
        patcher.start()
        self.addCleanup(patcher.stop)
        return gpu

    def test_gpu_probe_fails_without_cublas_and_cudnn(self):
        import io
        import contextlib
        # The reported machine: driver present, no cuBLAS/cuDNN anywhere. The
        # probe used to pass here and onboarding switched the app to CUDA.
        gpu = self._gpu_probe(loadable=())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(gpu._test_ct2_gpu('cuda'))
        self.assertIn('cublas64_12.dll', out.getvalue(), 'name what is missing')

    def test_gpu_probe_passes_when_every_library_loads(self):
        gpu = self._gpu_probe(loadable=('cublas64_12.dll', 'cublasLt64_12.dll', 'cudnn64_9.dll',
                                        'cudnn_ops64_9.dll', 'cudnn_cnn64_9.dll'))
        self.assertTrue(gpu._test_ct2_gpu('cuda'))
        self.assertEqual(gpu._missing_cuda_libraries(), [])

    def test_gpu_probe_reports_a_single_missing_cudnn_part(self):
        gpu = self._gpu_probe(loadable=('cublas64_12.dll', 'cublasLt64_12.dll',
                                        'cudnn64_9.dll', 'cudnn_ops64_9.dll'))
        self.assertEqual(gpu._missing_cuda_libraries(), ['cudnn_cnn64_9.dll'])

    def test_rocm_builds_are_not_held_to_nvidia_libraries(self):
        import unittest.mock as mock
        gpu = self._gpu_probe(loadable=())
        self.assertTrue(gpu._test_ct2_gpu('rocm'))
        with mock.patch.object(gpu, '_detect_ct2_variant', return_value='rocm'):
            self.assertEqual(gpu.missing_gpu_libraries(), [])
        with mock.patch.object(gpu, '_detect_ct2_variant', return_value='cuda'):
            self.assertIn('cudnn64_9.dll', gpu.missing_gpu_libraries())

    def test_macos_gpu_mirror_has_the_same_api(self):
        mac = self._load('platform/macos/gpu.py', 'wk_gpu_mac')
        self.assertEqual(mac.missing_gpu_libraries(), [])

    # whisper_engine imports numpy, which refuses a second import per process.
    # Import it BEFORE patch.dict snapshots sys.modules so the restore keeps it.
    def _preload_numpy(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest('numpy not installed')

    def _fake_hardware_detection(self, missing):
        import types
        fake = types.ModuleType('koli.hardware_detection')
        fake.missing_gpu_libraries = lambda: list(missing)
        return fake

    def test_cuda_engine_fails_fast_instead_of_hanging(self):
        # Existing configs already say device: cuda. With the libraries absent the
        # model loads, then the first dictation hangs; the engine must refuse up
        # front so main's GPU-failure path can offer setup or CPU.
        self._preload_numpy()
        import contextlib
        import io
        import types
        import unittest.mock as mock
        fake_fw = types.ModuleType('faster_whisper')
        fake_fw.WhisperModel = mock.Mock(name='WhisperModel')
        with mock.patch.dict(sys.modules, {
                'faster_whisper': fake_fw,
                'koli.hardware_detection': self._fake_hardware_detection(['cudnn_ops64_9.dll'])}):
            sys.modules.pop('koli.whisper_engine', None)
            from koli.whisper_engine import WhisperEngine
            # The engine prints emoji progress lines; main() switches stdout to
            # UTF-8 first, but a cp1252 test console (Windows CI) would choke.
            with self.assertRaises(RuntimeError) as ctx, contextlib.redirect_stdout(io.StringIO()):
                WhisperEngine(model_key='base', device='cuda', compute_type='float16')
            fake_fw.WhisperModel.assert_not_called()
        self.assertIn('cudnn_ops64_9.dll', str(ctx.exception))

    def test_cpu_engine_skips_the_gpu_check(self):
        self._preload_numpy()
        import contextlib
        import io
        import types
        import unittest.mock as mock
        fake_fw = types.ModuleType('faster_whisper')
        fake_fw.WhisperModel = mock.Mock(name='WhisperModel')
        with mock.patch.dict(sys.modules, {
                'faster_whisper': fake_fw,
                'koli.hardware_detection': self._fake_hardware_detection(['cublas64_12.dll'])}):
            sys.modules.pop('koli.whisper_engine', None)
            from koli.whisper_engine import WhisperEngine
            with contextlib.redirect_stdout(io.StringIO()):  # emoji vs a cp1252 console
                WhisperEngine(model_key='base', device='cpu', compute_type='int8')
            fake_fw.WhisperModel.assert_called_once()

    def test_gpu_recovery_prompt_is_skipped_without_a_console(self):
        # The fail-fast check above lands in this prompt. Under pythonw/autostart
        # nobody can answer it, so it must fall back to CPU rather than wait.
        import types
        import io
        import contextlib
        import unittest.mock as mock
        reimport_under(self, fake_platform_modules(getch=lambda: self.fail('must not wait for a keypress')))
        from koli import onboarding
        with mock.patch.object(onboarding.sys, 'stdin', None), \
             mock.patch.object(onboarding, 'prompt_choice') as prompt, \
             contextlib.redirect_stdout(io.StringIO()):
            onboarding.handle_gpu_failure(RuntimeError('CUDA libraries not found'), mock.Mock())
        prompt.assert_not_called()

    def test_doctor_flags_missing_cuda_libraries(self):
        import io
        import contextlib
        import unittest.mock as mock
        from koli import doctor
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with mock.patch.dict(sys.modules, {
                    'koli.hardware_detection': self._fake_hardware_detection(['cublas64_12.dll'])}):
                self.assertEqual(doctor._check_gpu_libraries(), 1)
            with mock.patch.dict(sys.modules, {
                    'koli.hardware_detection': self._fake_hardware_detection([])}):
                self.assertEqual(doctor._check_gpu_libraries(), 0)
        self.assertIn('cublas64_12.dll', out.getvalue())

    # --- #14: macOS abort (exit 134) from Tk vs pyobjc's NSApplication ---
    def test_platform_app_mirrors_declare_the_tk_constraint(self):
        import re
        for plat, expected in (('macos', 'True'), ('windows', 'False')):
            source = (ROOT / 'src' / 'koli' / 'platform' / plat / 'app.py').read_text(encoding='utf-8')
            self.assertRegex(source, r'(?m)^TK_MAIN_THREAD_ONLY = ' + re.escape(expected) + '$',
                             plat + ' app.py must declare TK_MAIN_THREAD_ONLY (mirrored API)')

    def test_tk_constraint_helper(self):
        import unittest.mock as mock
        from koli.utils import tk_requires_main_thread
        for constraint in (True, False):
            with mock.patch.dict(sys.modules, fake_platform_modules(TK_MAIN_THREAD_ONLY=constraint)):
                self.assertIs(tk_requires_main_thread(), constraint)

    def test_macos_setup_lets_tk_create_the_shared_application(self):
        # Whoever calls sharedApplication() first decides NSApp's class. Tk must
        # win, or its drawing code sends TKApplication-only selectors to a plain
        # NSApplication and the process aborts.
        import types
        import unittest.mock as mock
        order = []

        class FakeNSObject:
            @classmethod
            def alloc(cls):
                return cls()

            def init(self):
                return self

        class FakeNSApp:
            def setActivationPolicy_(self, policy):
                pass

            def setDelegate_(self, delegate):
                pass

        class FakeNSApplication:
            @staticmethod
            def sharedApplication():
                order.append('sharedApplication')
                return FakeNSApp()

        class FakeTk:
            def __init__(self):
                order.append('tk.Tk')

            def withdraw(self):
                pass

        appkit = types.ModuleType('AppKit')
        appkit.NSApplication = FakeNSApplication
        appkit.NSApplicationActivationPolicyAccessory = 1
        appkit.NSEventMaskAny = 0
        appkit.NSDefaultRunLoopMode = 'default'
        foundation = types.ModuleType('Foundation')
        foundation.NSDate = foundation.NSOperationQueue = foundation.NSThread = mock.Mock()
        foundation.NSObject = FakeNSObject
        tkinter = types.ModuleType('tkinter')
        tkinter.Tk = FakeTk
        with mock.patch.dict(sys.modules, {'AppKit': appkit, 'Foundation': foundation, 'tkinter': tkinter}):
            mac_app = self._load('platform/macos/app.py', 'wk_mac_app')
            mac_app.setup()
        self.assertEqual(order, ['tk.Tk', 'sharedApplication'])
        self.assertIsNotNone(mac_app._tk_preload_root, 'the hidden root must stay alive')

    def test_overlay_stands_down_where_tk_is_main_thread_only(self):
        import unittest.mock as mock
        from koli.level_overlay import LevelOverlay
        overlay = LevelOverlay(level_provider=lambda: 0.0)
        overlay._available = True  # as if tkinter were importable
        with mock.patch('koli.utils.tk_requires_main_thread', return_value=True), \
             mock.patch('koli.level_overlay.threading.Thread') as thread:
            overlay.start()
        thread.assert_not_called()
        self.assertFalse(overlay._available, 'every later overlay call must be a no-op')
        overlay.show_recording()
        overlay.flash_failure('still safe')

    def test_welcome_runs_inline_on_macos_and_threaded_elsewhere(self):
        import threading
        import unittest.mock as mock
        from koli import first_run
        seen = []
        done = threading.Event()

        def fake_run(on_close, hotkey_label, shutdown_event):
            seen.append(threading.current_thread() is threading.main_thread())
            done.set()
        event = threading.Event()
        with mock.patch.object(first_run, '_run_welcome', fake_run):
            with mock.patch('koli.utils.tk_requires_main_thread', return_value=True):
                first_run.show_welcome_window(shutdown_event=event)
            with mock.patch('koli.utils.tk_requires_main_thread', return_value=False):
                done.clear()
                first_run.show_welcome_window(shutdown_event=event)
                self.assertTrue(done.wait(2))
        self.assertEqual(seen, [True, False])

    # A just-enough tkinter for the welcome window. mainloop() models the real
    # one: it only returns once quit() has been called, because with the macOS
    # preload root alive, destroy() alone never ends it.
    def _run_welcome_with_fake_tk(self, drive, shutdown_event=None):
        import types
        import unittest.mock as mock
        calls = []

        class Widget:
            def __init__(self, *args, **kwargs):
                pass

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        class BooleanVar(Widget):
            def get(self):
                return False

        class Tk(Widget):
            def __init__(self):
                self.after_callbacks = []
                self.close_handler = None

            def protocol(self, name, handler):
                self.close_handler = handler

            def after(self, ms, callback):
                self.after_callbacks.append(callback)

            def quit(self):
                calls.append('quit')

            def destroy(self):
                calls.append('destroy')

            def mainloop(self):
                calls.append('mainloop')
                drive(self)
                if 'quit' not in calls:
                    raise AssertionError('mainloop() would never return: quit() was not called')
                calls.append('mainloop returned')

        tkinter = types.ModuleType('tkinter')
        tkinter.Tk = Tk
        tkinter.Frame = tkinter.Label = tkinter.Button = tkinter.Checkbutton = Widget
        tkinter.BooleanVar = BooleanVar
        on_close = mock.Mock()
        from koli import first_run
        with mock.patch.dict(sys.modules, {'tkinter': tkinter}), \
             mock.patch.object(first_run, 'mark_first_run_complete') as mark:
            first_run._run_welcome(on_close, 'CTRL+WIN', shutdown_event)
        return calls, on_close, mark

    def test_welcome_dismissal_quits_before_destroying(self):
        calls, on_close, mark = self._run_welcome_with_fake_tk(lambda root: root.close_handler())
        self.assertEqual(calls, ['mainloop', 'quit', 'mainloop returned', 'destroy'])
        mark.assert_called_once()
        on_close.assert_called_once()

    def test_welcome_closes_on_shutdown_without_counting_as_dismissed(self):
        import threading
        event = threading.Event()

        def drive(root):
            event.set()  # SIGTERM arrives while the window is open
            for callback in list(root.after_callbacks):
                callback()
        calls, on_close, mark = self._run_welcome_with_fake_tk(drive, shutdown_event=event)
        self.assertIn('quit', calls)
        self.assertEqual(calls[-1], 'destroy')
        mark.assert_not_called()
        on_close.assert_not_called()

    def test_main_hands_the_welcome_window_the_shutdown_event(self):
        source = (ROOT / 'src' / 'koli' / 'main.py').read_text(encoding='utf-8')
        call = source[source.index('show_welcome_window('):]
        call = call[:call.index(')\n')]
        self.assertIn('shutdown_event=shutdown_event', call)

    # --- #14 follow-through: every other Tk window on macOS ---
    def test_windows_stay_in_process_where_tk_threads_are_fine(self):
        import unittest.mock as mock
        from koli import window_launcher
        with mock.patch('koli.utils.tk_requires_main_thread', return_value=False), \
             mock.patch.object(window_launcher.subprocess, 'Popen') as popen:
            self.assertFalse(window_launcher.open_in_child_process('cheat-sheet'))
        popen.assert_not_called()

    def test_macos_windows_open_in_a_child_with_the_payload_on_stdin(self):
        import json
        import unittest.mock as mock
        from koli import window_launcher
        child = mock.Mock()
        with mock.patch('koli.utils.tk_requires_main_thread', return_value=True), \
             mock.patch.object(window_launcher.subprocess, 'Popen', return_value=child) as popen, \
             mock.patch.object(window_launcher.threading, 'Thread'):
            handled = window_launcher.open_in_child_process('fallback', {'transcript': 'secret words'})
        self.assertTrue(handled)
        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], ['--window', 'fallback'])
        self.assertNotIn('secret words', ' '.join(command), 'transcripts must not go in argv')
        sent = json.loads(child.stdin.write.call_args.args[0].decode('utf-8'))
        self.assertEqual(sent['transcript'], 'secret words')
        child.stdin.close.assert_called_once()

    def test_failed_spawn_never_falls_back_to_in_process_tk(self):
        import unittest.mock as mock
        from koli import window_launcher
        with mock.patch('koli.utils.tk_requires_main_thread', return_value=True), \
             mock.patch.object(window_launcher.subprocess, 'Popen', side_effect=OSError('nope')):
            self.assertTrue(window_launcher.open_in_child_process('add-word'))

    def test_child_runs_each_window_blocking_on_its_main_thread(self):
        import io
        import unittest.mock as mock
        from koli import window_launcher
        for name, target in (('cheat-sheet', 'koli.cheat_sheet.show_cheat_sheet'),
                             ('add-word', 'koli.dictionary.show_add_word_dialog'),
                             ('history', 'koli.history_window.show_history')):
            with mock.patch(target) as show:
                self.assertEqual(window_launcher.run_window(name, io.StringIO('')), 0)
            self.assertTrue(show.call_args.kwargs.get('blocking'), name + ' must run blocking')
        with mock.patch('koli.fallback_window.FallbackWindow._run_window') as run:
            code = window_launcher.run_window(
                'fallback', io.StringIO('{"transcript": "hi", "reason": "r", "allow_clipboard": false}'))
        self.assertEqual(code, 0)
        run.assert_called_once_with('hi', 'r', False)
        self.assertEqual(window_launcher.run_window('fallback', io.StringIO('not json')), 1)

    def test_fallback_window_hands_off_instead_of_threading(self):
        import unittest.mock as mock
        from koli.fallback_window import FallbackWindow
        window = FallbackWindow()
        window._available = True
        with mock.patch('koli.window_launcher.open_in_child_process', return_value=True) as launch, \
             mock.patch('koli.fallback_window.threading.Thread') as thread:
            window.show('dictated text', allow_clipboard=False)
        thread.assert_not_called()
        payload = launch.call_args.args[1]
        self.assertEqual(payload['transcript'], 'dictated text')
        self.assertFalse(payload['allow_clipboard'])
        self.assertTrue(payload['reason'], 'the default reason must travel with it')

    def test_window_flag_choices_match_the_launcher(self):
        from koli.window_launcher import WINDOW_NAMES
        source = (ROOT / 'src' / 'koli' / 'main.py').read_text(encoding='utf-8')
        line = source[source.index("'--window'"):]
        line = line[:line.index('\n')]
        for name in WINDOW_NAMES:
            self.assertIn(repr(name), line)


class WisprStyleFeatureTests(unittest.TestCase):
    """Wispr Flow–style editing added in 0.20.0: backtrack, stutters, spoken
    lists, styles, snippets, paste-last, double-tap lock, whisper mode."""

    # --- Backtrack ---
    def test_backtrack_replaces_the_value_it_corrects(self):
        from koli.dictation_cleanup import apply_backtrack
        cases = {
            "Let's meet at 2, actually 3 tomorrow.": "Let's meet at 3 tomorrow.",
            'Tuesday, no wait, Wednesday works.': 'Wednesday works.',
            'Send 5 dollars, I mean 6.': 'Send 6 dollars.',
            'I have 2 kids, sorry, 3 kids.': 'I have 3 kids.',
            'At 2pm, actually 3pm works.': 'At 3pm works.',
            'We ship in March, no wait, April. It costs 5, actually 7.': 'We ship in April. It costs 7.',
        }
        for spoken, typed in cases.items():
            self.assertEqual(apply_backtrack(spoken), typed, spoken)

    def test_backtrack_leaves_ordinary_prose_alone(self):
        from koli.dictation_cleanup import apply_backtrack
        for text in ('I actually like it.', 'Sorry, 3 people are here.',
                     'It was 2. Actually 3 would be better.',   # never reaches across a sentence
                     'Meet Tuesday at 2, actually Friday.'):    # class mismatch picks the weekday
            expected = 'Meet Friday at 2.' if 'Friday' in text else text
            self.assertEqual(apply_backtrack(text), expected, text)

    def test_backtrack_is_linear_on_huge_dictations(self):
        import time
        from koli.dictation_cleanup import apply_backtrack
        text = 'I actually think ' * 20000 + 'at 2 actually 3'
        start = time.monotonic()
        self.assertTrue(apply_backtrack(text).endswith('at 3'))
        self.assertLess(time.monotonic() - start, 2.0, 'must stay O(n)')

    def test_backtrack_runs_only_when_enabled(self):
        from koli.text_postprocess import postprocess
        self.assertEqual(postprocess('at 2 actually 3', {'voice_editing': False}), 'at 2 actually 3')
        self.assertEqual(postprocess('at 2 actually 3', {'backtrack': {'enabled': True}}), 'at 3')
        self.assertEqual(postprocess('at 2 swap 3', {'backtrack': {'enabled': True, 'cues': ['swap']}}), 'at 3')

    # --- Stutters ---
    def test_repeated_words_collapse_but_real_doubles_stay(self):
        from koli.dictation_cleanup import remove_repeated_words
        self.assertEqual(remove_repeated_words('I I think the the cat'), 'I think the cat')
        self.assertEqual(remove_repeated_words('We, we should go'), 'We should go')
        for keep in ('I had had enough', 'very very good', 'Bye bye', '11 11'):
            self.assertEqual(remove_repeated_words(keep), keep)

    # --- Spoken lists ---
    def test_spoken_list_becomes_a_list(self):
        from koli.dictation_cleanup import apply_list_formatting
        self.assertEqual(apply_list_formatting('My list: first, milk. Second, eggs. Third, bread.'),
                         'My list:\n1. Milk\n2. Eggs\n3. Bread')
        self.assertEqual(apply_list_formatting('One, buy milk. Two, call mom, and three, pay rent.'),
                         '1. Buy milk\n2. Call mom\n3. Pay rent')
        self.assertEqual(apply_list_formatting('first, milk. second, eggs', style='bullets'),
                         '- Milk\n- Eggs')

    def test_prose_that_counts_is_not_a_list(self):
        from koli.dictation_cleanup import apply_list_formatting
        for text in ('One of the two options is fine.', 'First, we go.',
                     'First of all, hello. Second, world.'):
            self.assertEqual(apply_list_formatting(text), text)

    def test_list_takes_no_closing_period(self):
        from koli.text_postprocess import postprocess
        self.assertEqual(postprocess('first, milk. second, eggs.',
                                     {'list_formatting': True, 'ensure_punctuation': True}),
                         '1. Milk\n2. Eggs')

    # --- Lowercase / styles ---
    def test_lowercase_keeps_acronyms_mixed_case_and_taught_terms(self):
        from koli.dictation_cleanup import apply_lowercase
        self.assertEqual(apply_lowercase('Hello There, NASA and iPhone and Rohit.', protected=['Rohit']),
                         'hello there, NASA and iPhone and Rohit.')

    def test_style_resolution(self):
        from koli.styles import resolve_style
        self.assertTrue(resolve_style('very casual')['lowercase'])
        self.assertEqual(resolve_style('nope'), {}, 'unknown styles are ignored')
        self.assertEqual(resolve_style(''), {})
        custom = resolve_style('shouty', {'styles': {'shouty': {'capitalize_first': True, 'ollama': True}}})
        self.assertEqual(custom, {'capitalize_first': True}, 'a style can only set formatting toggles')

    def test_style_precedence(self):
        from koli.app_rules import effective_postprocess_config
        global_cfg = {'style': 'formal', 'capitalize_first': False}
        cfg = effective_postprocess_config(global_cfg)
        self.assertTrue(cfg['capitalize_first'], 'the global style wins over global toggles')
        self.assertNotIn('style', cfg, 'resolved once, so postprocess() cannot re-apply it')
        cfg = effective_postprocess_config(global_cfg, {'style': 'very_casual'})
        self.assertTrue(cfg['lowercase'])
        self.assertFalse(cfg['ensure_punctuation'], "the app's style beats the global one")
        cfg = effective_postprocess_config(global_cfg, {'style': 'very_casual', 'lowercase': False})
        self.assertFalse(cfg['lowercase'], "a rule's own toggle beats its style")

    def test_postprocess_applies_a_style_key(self):
        from koli.text_postprocess import postprocess
        self.assertEqual(postprocess('Hello There', {'style': 'formal'}), 'Hello There.')
        self.assertEqual(postprocess('Hello There.', {'style': 'very_casual'}), 'hello there')

    def test_shipped_app_rules_use_known_styles(self):
        from ruamel.yaml import YAML
        from koli.styles import STYLE_PRESETS
        rules = YAML().load((ROOT / 'src' / 'koli' / 'app_rules.defaults.yaml').read_text(encoding='utf-8'))['rules']
        styles = [r['style'] for r in rules if 'style' in r]
        self.assertTrue(styles)
        for style in styles:
            self.assertIn(style, STYLE_PRESETS)

    # --- Snippets ---
    _SNIPPETS = [{'trigger': 'my signature', 'expansion': 'Best,\nRohit'},
                 {'trigger': 'my work signature', 'expansion': 'Rohit B.\nEngineer'},
                 {'trigger': 'my address', 'expansion': '1 Main St', 'mode': 'alone'},
                 {'trigger': 'weird', 'expansion': r'\1 $0 (.*)'},
                 {'nonsense': True}, 'not even a dict']

    def test_snippet_expands_inline(self):
        from koli.snippets import expand_snippets
        self.assertEqual(expand_snippets('Thanks. My signature.', self._SNIPPETS), ('Thanks. Best,\nRohit', False))
        self.assertEqual(expand_snippets('Thanks. my work signature', self._SNIPPETS)[0],
                         'Thanks. Rohit B.\nEngineer', 'the longer trigger wins')

    def test_whole_utterance_snippet_is_delivered_verbatim(self):
        from koli.text_postprocess import postprocess
        cfg = {'capitalize_first': True, 'ensure_punctuation': True, 'snippets': self._SNIPPETS}
        self.assertEqual(postprocess('My signature.', cfg), 'Best,\nRohit')
        self.assertEqual(postprocess('My address', cfg), '1 Main St')
        self.assertEqual(postprocess('send my address now', cfg), 'Send my address now.',
                         '"alone" snippets never fire mid-sentence')

    def test_snippet_expansion_is_literal_and_variables_fill_in(self):
        import datetime
        from koli.snippets import expand_snippets
        self.assertEqual(expand_snippets('weird', self._SNIPPETS)[0], r'\1 $0 (.*)')
        today = datetime.date.today().strftime('%Y-%m-%d')
        text, _ = expand_snippets('stamp', [{'trigger': 'stamp', 'expansion': 'On ${date}'}])
        self.assertEqual(text, 'On ' + today)

    def test_lowercase_style_never_touches_a_snippet(self):
        from koli.text_postprocess import postprocess
        cfg = {'style': 'very_casual', 'snippets': self._SNIPPETS}
        self.assertEqual(postprocess('Thanks. My signature.', cfg), 'thanks. Best,\nRohit')

    # --- Double-tap lock ---
    def _latch(self, recording=True):
        from koli.hotkey_gestures import TapLatch
        now = [0.0]
        timers = []

        class FakeTimer:
            def __init__(self, seconds, callback):
                self.callback, self.cancelled = callback, False
                timers.append(self)

            def cancel(self):
                self.cancelled = True
        events = []
        latch = TapLatch(start=lambda: events.append('start'), stop=lambda: events.append('stop'),
                         window_ms=400, is_recording=lambda: recording,
                         clock=lambda: now[0], timer_factory=FakeTimer)
        return latch, events, now, timers

    def test_hold_is_plain_push_to_talk(self):
        latch, events, now, _ = self._latch()
        latch.press()
        now[0] = 2.0
        latch.release()
        self.assertEqual(events, ['start', 'stop'])
        self.assertEqual(latch.state, latch.IDLE)

    def test_single_quick_tap_stops_after_the_window(self):
        latch, events, now, timers = self._latch()
        latch.press()
        now[0] = 0.1
        latch.release()
        self.assertEqual(events, ['start'], 'must not stop inside the double-tap window')
        timers[-1].callback()
        self.assertEqual(events, ['start', 'stop'])

    def test_double_tap_locks_and_a_third_tap_stops(self):
        latch, events, now, timers = self._latch()
        latch.press(); now[0] = 0.1; latch.release()
        now[0] = 0.2; latch.press()
        self.assertTrue(timers[-1].cancelled)
        self.assertEqual(latch.state, latch.LATCHED)
        now[0] = 0.3; latch.release()
        self.assertEqual(events, ['start'], 'releasing while locked does nothing')
        now[0] = 9.0; latch.press()
        self.assertEqual(events, ['start', 'stop'])

    def test_lock_resyncs_when_recording_ended_elsewhere(self):
        latch, events, now, _ = self._latch(recording=False)
        latch.state = latch.LATCHED          # e.g. the silence timeout stopped it
        latch.press()
        self.assertEqual(events, ['start'], 'the next tap starts a new recording')

    # --- Paste-last and hotkey wiring (real classes, heavy deps faked) ---
    def _import_app_modules(self):
        import types
        import unittest.mock as mock
        try:
            import numpy  # noqa: F401  (must predate the sys.modules snapshot)
        except ImportError:
            self.skipTest('numpy not installed')
        fakes = fake_platform_modules()
        for name in ('PIL', 'PIL.Image', 'PIL.ImageDraw', 'pystray', 'sounddevice', 'soxr',
                     'faster_whisper', 'playsound3'):
            fakes[name] = _stand_in_module(name)
        reimport_under(self, fakes)
        from koli.hotkey_listener import HotkeyListener
        from koli.state_manager import StateManager
        return HotkeyListener, StateManager

    def _state_manager(self, last=None, state='idle'):
        import unittest.mock as mock
        _, StateManager = self._import_app_modules()
        sm = StateManager.__new__(StateManager)
        sm.logger = __import__('logging').getLogger('test')
        sm.last_transcription = last
        sm.clipboard_manager = mock.Mock()
        sm.system_tray = mock.Mock()
        sm.get_current_state = lambda: state
        sm._wait_for_modifiers_released = lambda: None
        return sm

    def test_paste_last_retypes_without_auto_send(self):
        sm = self._state_manager(last='hello world')
        self.assertTrue(sm.paste_last_transcription())
        sm.clipboard_manager.deliver_transcription.assert_called_once_with('hello world', use_auto_enter=False)

    def test_paste_last_with_nothing_to_paste_says_so(self):
        sm = self._state_manager(last=None)
        self.assertFalse(sm.paste_last_transcription())
        sm.clipboard_manager.deliver_transcription.assert_not_called()
        sm.system_tray.notify.assert_called_once()

    def test_paste_last_waits_while_recording(self):
        sm = self._state_manager(last='x', state='recording')
        self.assertFalse(sm.paste_last_transcription())
        sm.clipboard_manager.deliver_transcription.assert_not_called()

    def test_listener_registers_the_new_hotkeys(self):
        import unittest.mock as mock
        HotkeyListener, _ = self._import_app_modules()
        with mock.patch.object(HotkeyListener, 'start_listening'):
            listener = HotkeyListener(mock.Mock(), 'ctrl+win', 'ctrl', paste_last_hotkey='alt+shift+z',
                                      double_tap_to_lock=True, double_tap_window_ms=300)
        combos = [binding[0] for binding in listener.hotkey_bindings]
        self.assertIn('alt+shift+z', combos)
        self.assertIsNotNone(listener.tap_latch)
        self.assertAlmostEqual(listener.tap_latch.window, 0.3)
        paste = next(b for b in listener.hotkey_bindings if b[0] == 'alt+shift+z')
        # On press: the macOS backend only reports releases for modifier-only
        # chords, so a release-only binding never fired there.
        self.assertIsNotNone(paste[1])
        self.assertIsNone(paste[2])
        with mock.patch.object(HotkeyListener, 'start_listening'):
            plain = HotkeyListener(mock.Mock(), 'ctrl+win', 'ctrl', recording_mode='toggle',
                                   double_tap_to_lock=True)
        self.assertIsNone(plain.tap_latch, 'toggle mode is already hands-free')

    # A shortcut the backend refuses must not leave every hotkey dead: the
    # old one is registered again and the error still reaches the caller.
    def test_listener_keeps_the_old_shortcut_when_the_new_one_fails(self):
        import unittest.mock as mock
        HotkeyListener, _ = self._import_app_modules()
        with mock.patch.object(HotkeyListener, 'start_listening'):
            listener = HotkeyListener(mock.Mock(), 'ctrl+win', 'ctrl', recording_mode='toggle')
        registered = []

        def start():
            combos = [b[0] for b in listener.hotkey_bindings]
            if 'ctrl+~' in combos:
                raise Exception('The key [~] not a valid virtual keystroke.')
            registered.append(combos)
        with mock.patch.object(listener, 'start_listening', side_effect=start), \
                mock.patch.object(listener, 'stop_listening'):
            with self.assertRaises(Exception):
                listener.change_hotkey_config('recording_hotkey', 'ctrl+~')
        self.assertEqual(listener.recording_hotkey, 'ctrl+win')
        self.assertIn('ctrl+win', registered[-1])

    # A shortcut saved with an unusable key (older versions accepted "~") must
    # not stop the app starting: it goes back to its default and is reported.
    def test_startup_resets_a_saved_shortcut_with_an_unusable_key(self):
        import unittest.mock as mock
        self._import_app_modules()
        import koli.main as main
        from koli.platform import hotkeys
        hotkeys.unsupported_keys = lambda combo: [k for k in combo.split('+') if k == '~']
        cm = mock.Mock()
        cm._defaults_baseline = {'hotkey': {'recording_hotkey': 'ctrl+win'}}
        config = {'recording_hotkey': 'ctrl+shift+win+~', 'stop_key': 'ctrl'}
        self.addCleanup(main.STARTUP_NOTICES.clear)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            main.repair_unusable_hotkeys(config, cm)
        self.assertEqual(config['recording_hotkey'], 'ctrl+win')
        self.assertEqual(config['stop_key'], 'ctrl')
        cm.set_path.assert_called_once_with('hotkey.recording_hotkey', 'ctrl+win')
        self.assertEqual([n['kind'] for n in main.STARTUP_NOTICES], ['hotkey'])

    # --- Whisper mode ---
    def test_whisper_mode_boosts_only_quiet_speech(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy not installed')
        from koli.audio_gain import boost_quiet_audio, TARGET_PEAK
        t = np.linspace(0, 1, 16000, dtype=np.float32)
        quiet = (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)  # needs 6x, under the 8x cap
        boosted = boost_quiet_audio(quiet, max_gain=8.0)
        self.assertAlmostEqual(float(np.max(np.abs(boosted))), TARGET_PEAK, delta=0.02)
        self.assertEqual(boosted.dtype, np.float32)
        capped = boost_quiet_audio((0.01 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), max_gain=4.0)
        self.assertAlmostEqual(float(np.max(np.abs(capped))), 0.04, delta=0.005)
        loud = (0.9 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        self.assertIs(boost_quiet_audio(loud), loud, 'never turns the volume down')
        silence = np.full(16000, 1e-5, dtype=np.float32)
        self.assertIs(boost_quiet_audio(silence), silence, 'never amplifies a muted mic')

    # --- Config & settings ---
    def test_new_settings_ship_with_safe_defaults(self):
        from ruamel.yaml import YAML
        cfg = YAML().load((ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8'))
        pp = cfg['postprocess']
        self.assertFalse(pp['backtrack']['enabled'])
        self.assertFalse(pp['remove_repeated_words'])
        self.assertFalse(pp['list_formatting'])
        self.assertFalse(pp['lowercase'])
        self.assertEqual(pp['style'], '')
        self.assertEqual(list(pp['snippets']), [])
        self.assertFalse(cfg['hotkey']['double_tap_to_lock'])
        self.assertFalse(cfg['audio']['whisper_mode']['enabled'])
        self.assertIn('macos:', cfg['hotkey']['paste_last_hotkey'])


class ReleaseReviewFixTests(unittest.TestCase):
    """Findings from the pre-release review of 0.20.0, each pinned so it can't
    come back."""

    def test_backtrack_handles_decimals_and_money(self):
        from koli.dictation_cleanup import apply_backtrack
        self.assertEqual(apply_backtrack('The rate is 2.5, actually 3'), 'The rate is 3')
        self.assertEqual(apply_backtrack('It costs $1.50, sorry, $2'), 'It costs $2')
        self.assertEqual(apply_backtrack('Version 3.1, I mean 4'), 'Version 4')

    def test_backtrack_am_pm_needs_a_word_end(self):
        from koli.dictation_cleanup import apply_backtrack
        self.assertEqual(apply_backtrack('I have 2 amazing kids, actually 3'), 'I have 3 amazing kids')

    def test_backtrack_leaves_prose_after_a_cue_alone(self):
        from koli.dictation_cleanup import apply_backtrack
        for text in ('We saw 3 movies, actually one of them was great',
                     'I have 2 questions, actually one more thing',
                     'Invite 2 guests, actually 3 amazing people'):
            self.assertEqual(apply_backtrack(text), text)

    def test_backtrack_string_cue_is_one_cue(self):
        from koli.dictation_cleanup import apply_backtrack
        from koli.text_postprocess import postprocess
        self.assertEqual(apply_backtrack('I bought 2 apples and a 3 pack', 'actually'),
                         'I bought 2 apples and a 3 pack')
        self.assertEqual(postprocess('at 2 actually 3', {'backtrack': {'enabled': True, 'cues': 'actually'}}),
                         'at 3')

    def test_backtrack_stays_linear_with_many_corrections(self):
        import time
        from koli.dictation_cleanup import apply_backtrack
        text = 'at 2 tomorrow actually 3. ' * 20000
        start = time.monotonic()
        result = apply_backtrack(text)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertTrue(result.startswith('at 3 tomorrow. at 3 tomorrow.'))

    def test_stutter_removal_respects_contractions_and_clauses(self):
        from koli.dictation_cleanup import remove_repeated_words
        for text in ("Thank you, you're welcome.", "You can, can't you?",
                     "If you want it, it's yours.", 'Whatever you choose, choose wisely.'):
            self.assertEqual(remove_repeated_words(text), text)
        self.assertEqual(remove_repeated_words('Ok. I, I think so'), 'Ok. I think so')

    def test_list_ends_at_its_last_sentence(self):
        from koli.dictation_cleanup import apply_list_formatting
        self.assertEqual(apply_list_formatting('Groceries: first, milk. second, eggs. See you tonight.'),
                         'Groceries:\n1. Milk\n2. Eggs\n\nSee you tonight.')

    def test_odd_case_folding_never_raises(self):
        from koli.dictation_cleanup import apply_list_formatting
        from koli.snippets import expand_snippets
        self.assertEqual(apply_list_formatting('FİRST, milk. second, eggs.'), 'FİRST, milk. second, eggs.')
        expand_snippets('say fİve now', [{'trigger': 'five', 'expansion': '5'}])

    def test_snippet_keeps_sentence_punctuation_mid_text(self):
        from koli.snippets import expand_snippets
        text, _ = expand_snippets('Send it to my email, then call me. My email.',
                                  [{'trigger': 'my email', 'expansion': 'me@x.com'}])
        self.assertEqual(text, 'Send it to me@x.com, then call me. me@x.com')

    def test_lowercase_keeps_contracted_i_and_multiword_terms(self):
        from koli.dictation_cleanup import apply_lowercase
        self.assertEqual(apply_lowercase("I'm sure Rohit Burani and I'll Go", protected=['Rohit Burani']),
                         "I'm sure Rohit Burani and I'll go")

    def test_settings_save_keeps_custom_styles_and_corrections(self):
        reimport_under(self, fake_platform_modules())  # config_manager imports the platform layer
        from koli.config_manager import _compute_overrides
        defaults = {'postprocess': {'styles': {}, 'corrections': {}, 'style': ''}}
        user = {'postprocess': {'styles': {'shouty': {'capitalize_first': True}},
                                'corrections': {'CAPEX': ['cap x']}, 'style': 'shouty'}}
        kept = _compute_overrides(user, defaults)['postprocess']
        self.assertIn('shouty', kept['styles'])
        self.assertIn('CAPEX', kept['corrections'])

    def test_verbatim_keeps_spoken_formatting(self):
        from koli.styles import resolve_style
        self.assertNotIn('inline_formatting', resolve_style('verbatim'),
                         '"new line" / "comma" must keep working in code editors')

    def test_specific_app_rules_come_before_title_matches(self):
        from ruamel.yaml import YAML
        rules = YAML().load((ROOT / 'src' / 'koli' / 'app_rules.defaults.yaml').read_text(encoding='utf-8'))['rules']
        order = [r['match'][0] for r in rules]
        self.assertLess(order.index('code.exe'), order.index('slack.exe'),
                        'a VS Code window titled "slack_bot.py" must not auto-send')
        self.assertLess(order.index('code.exe'), order.index('outlook.exe'))

    # --- Hands-free lock vs a stop key inside the record chord ---
    def _latch(self, recording=True):
        from koli.hotkey_gestures import TapLatch
        now = [0.0]
        events = []

        class FakeTimer:
            def __init__(self, seconds, callback):
                self.callback = callback

            def cancel(self):
                pass
        latch = TapLatch(start=lambda: events.append('start'), stop=lambda: events.append('stop'),
                         window_ms=400, is_recording=lambda: recording,
                         clock=lambda: now[0], timer_factory=FakeTimer)
        return latch, events, now

    def test_stop_key_is_ignored_while_the_second_tap_begins(self):
        latch, events, now = self._latch()
        latch.press(); now[0] = 0.1; latch.release()
        now[0] = 0.2
        self.assertTrue(latch.intercept_stop_key(), 'Ctrl of the 2nd Ctrl+Win tap must not stop')
        latch.press()
        self.assertEqual(latch.state, latch.LATCHED)

    def test_stop_key_ends_a_lock_without_restarting(self):
        latch, events, now = self._latch()
        latch.state = latch.LATCHED
        self.assertFalse(latch.intercept_stop_key(), 'a real stop while locked')
        now[0] = 0.05
        latch.press()                    # the chord completing right after Ctrl
        self.assertEqual(events, [], 'must not start a junk recording')
        now[0] = 5.0
        latch.press()
        self.assertEqual(events, ['start'], 'later presses work normally')

    # --- Paste-last honours app rules ---
    def _paste_sm(self, rule):
        import unittest.mock as mock
        helper = WisprStyleFeatureTests('test_paste_last_retypes_without_auto_send')
        helper.addCleanup = self.addCleanup
        helper.skipTest = self.skipTest
        sm = helper._state_manager(last='rm -rf build\n')
        sm.app_rules = mock.Mock()
        sm.app_rules.match_for_foreground.return_value = rule
        return sm

    def test_paste_last_skips_suppressed_apps(self):
        sm = self._paste_sm({'match': ['1password.exe'], 'suppress': True})
        self.assertFalse(sm.paste_last_transcription())
        sm.clipboard_manager.deliver_transcription.assert_not_called()
        sm.clipboard_manager.copy_text.assert_not_called()

    def test_paste_last_only_copies_in_copy_only_apps(self):
        sm = self._paste_sm({'match': ['wt.exe'], 'auto_paste': False})
        self.assertTrue(sm.paste_last_transcription())
        sm.clipboard_manager.deliver_transcription.assert_not_called()
        sm.clipboard_manager.copy_text.assert_called_once()

    # --- Whisper mode config is validated, and boosts after the trims ---
    def test_whisper_mode_config_is_validated(self):
        helper = WisprStyleFeatureTests('test_paste_last_retypes_without_auto_send')
        helper.addCleanup = self.addCleanup
        helper.skipTest = self.skipTest
        helper._import_app_modules()
        from koli.audio_recorder import AudioRecorder
        parse = AudioRecorder._parse_whisper_mode
        self.assertIsNone(parse(True))
        self.assertIsNone(parse({'enabled': False}))
        self.assertIsNone(parse({'enabled': 'no'}), 'YAML 1.2 "no" is a string, not an opt-in')
        self.assertEqual(parse({'enabled': True, 'max_gain': '8x'}), 8.0)
        self.assertEqual(parse({'enabled': True, 'max_gain': 4}), 4.0)

    def test_whisper_mode_boosts_after_silence_trimming(self):
        source = (ROOT / 'src' / 'koli' / 'audio_recorder.py').read_text(encoding='utf-8')
        body = source[source.index('def _build_audio_array'):source.index('def _parse_whisper_mode')]
        self.assertLess(body.index('_trim_trailing_silence(kept'), body.index('boost_gain('),
                        'a boosted noise floor would defeat the anti-hallucination trims')

    def test_settings_window_tolerates_non_mapping_sections(self):
        source = (ROOT / 'src' / 'koli' / 'settings_ui.py').read_text(encoding='utf-8')
        self.assertIn("wm = wm if isinstance(wm, dict) else {}", source)
        self.assertIn("bt = bt if isinstance(bt, dict) else {}", source)

    def test_bad_numeric_hotkey_setting_falls_back(self):
        source = (ROOT / 'src' / 'koli' / 'main.py').read_text(encoding='utf-8')
        self.assertIn("_int_setting(hotkey_config.get('double_tap_window_ms'), 400)", source)

    def test_yaml_no_means_off(self):
        from koli.text_postprocess import postprocess
        self.assertEqual(postprocess('first, milk. second, eggs', {'list_formatting': 'no'}),
                         'first, milk. second, eggs')
        self.assertEqual(postprocess('first, milk. second, eggs', {'list_formatting': 'yes'}),
                         '1. Milk\n2. Eggs')
        self.assertEqual(postprocess('hello', {'capitalize_first': True}), 'Hello')


# ── Layered defaults (0.21.0) ────────────────────────────────────────────────
# Until 0.21 each of app_rules / commands / transforms / profiles was copied out
# of the package once, on first launch, and never touched again. Every later
# improvement to a shipped file reached new installs only: a machine set up in
# May still had May's rules eleven releases on, including the ordering bug that
# let a code editor showing "slack_bot.py" match the chat rule and auto-send.
# The shipped entries now load from the package every time, and the user's file
# holds only their own entries plus per-id overrides.
class LayeredDefaultsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix='wl-layer-'))

    def _read(self, name):
        from ruamel.yaml import YAML
        with open(self.dir / name, encoding='utf-8') as f:
            return YAML().load(f) or {}

    def _write(self, name, text):
        (self.dir / name).write_text(text, encoding='utf-8')

    # A user file in the pre-0.21 shape: a full copy of the defaults as they
    # were, chat rules ahead of code editors, and no styles.
    def _write_old_app_rules(self):
        self._write('app_rules.yaml', (
            'rules:\n'
            '  - match: ["slack.exe", "discord.exe", "telegram.exe", "whatsapp.exe", "Slack", "Discord"]\n'
            '    auto_send: true\n'
            '    initial_prompt: "Casual chat message. Conversational tone."\n'
            '  - match: ["code.exe", "cursor.exe", "windsurf.exe", "rider64.exe", "pycharm64.exe", "idea64.exe"]\n'
            '    auto_paste: false\n'
            '    initial_prompt: "Programming context. Identifiers, snake_case, camelCase, file paths, regex."\n'
            '  - match: ["mytool.exe"]\n'
            '    suppress: true\n'))

    def _load_rules(self):
        import unittest.mock as mock
        import koli.app_rules as ar
        with mock.patch.object(ar, 'get_user_app_data_path', return_value=str(self.dir)):
            return ar.AppRules().rules

    def test_shipped_rules_load_with_no_user_file(self):
        rules = self._load_rules()
        self.assertEqual([r.get('id') for r in rules],
                         ['password-managers', 'terminals', 'code-editors',
                          'chat-apps', 'email-clients', 'meeting-discord', 'meeting-zoom',
                          'meeting-teams', 'meeting-slack', 'meeting-google-meet',
                          'meeting-teams-web', 'meeting-zoom-web', 'meeting-discord-web'])
        self.assertEqual(self._read('app_rules.yaml').get('rules'), [],
                         'a fresh user file must start empty, not as a copy of the defaults')

    def test_every_shipped_rule_has_an_id(self):
        # An id-less shipped rule could never be overridden or disabled, and a
        # migration would keep re-adding the user's copy of it as a duplicate.
        from ruamel.yaml import YAML
        path = ROOT / 'src' / 'koli' / 'app_rules.defaults.yaml'
        with open(path, encoding='utf-8') as f:
            shipped = YAML().load(f)['rules']
        ids = [r.get('id') for r in shipped]
        self.assertTrue(all(ids), f'shipped rule without an id: {ids}')
        self.assertEqual(len(ids), len(set(ids)), f'duplicate shipped ids: {ids}')

    def test_editors_are_matched_before_chat(self):
        # The slack_bot.py bug: window titles match by substring, so the code
        # editor rule has to come first.
        ids = [r.get('id') for r in self._load_rules()]
        self.assertLess(ids.index('code-editors'), ids.index('chat-apps'))

    def test_old_file_gains_later_shipped_rules_and_order(self):
        self._write_old_app_rules()
        rules = self._load_rules()
        ids = [r.get('id') for r in rules]
        self.assertIn('email-clients', ids, 'a rule added after the user first launched')
        self.assertLess(ids.index('code-editors'), ids.index('chat-apps'))
        editors = next(r for r in rules if r.get('id') == 'code-editors')
        self.assertEqual(editors.get('style'), 'verbatim',
                         'a key added to a shipped rule after the copy was made')

    def test_migration_keeps_the_users_own_rule_and_drops_the_copies(self):
        self._write_old_app_rules()
        rules = self._load_rules()
        own = [r for r in rules if not r.get('id')]
        self.assertEqual([r['match'] for r in own], [['mytool.exe']])
        self.assertEqual(self._read('app_rules.yaml').get('rules'),
                         [{'match': ['mytool.exe'], 'suppress': True}],
                         'unedited copies of shipped rules should not survive migration')

    def test_migration_backs_up_the_original(self):
        self._write_old_app_rules()
        before = (self.dir / 'app_rules.yaml').read_text(encoding='utf-8')
        self._load_rules()
        backups = list(self.dir.glob('app_rules.yaml.*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), before)

    def test_migration_runs_once(self):
        self._write_old_app_rules()
        first = self._load_rules()
        second = self._load_rules()
        self.assertEqual([r.get('id') for r in first], [r.get('id') for r in second])
        self.assertEqual(len(list(self.dir.glob('*.bak'))), 1)

    def test_an_edited_shipped_rule_becomes_an_override(self):
        # Only the edited key is kept, so every other key keeps following the
        # shipped rule as it changes.
        self._write('app_rules.yaml', (
            'rules:\n'
            '  - match: ["outlook.exe", "thunderbird.exe", "Mail"]\n'
            '    style: casual\n'
            '    initial_prompt: "Professional email body. Polite, clear sentences."\n'))
        rules = self._load_rules()
        self.assertEqual(self._read('app_rules.yaml')['rules'],
                         [{'id': 'email-clients', 'style': 'casual'}])
        email = next(r for r in rules if r.get('id') == 'email-clients')
        self.assertEqual(email['style'], 'casual')
        self.assertEqual(email['match'], ['outlook.exe', 'thunderbird.exe', 'Mail'])

    def test_user_rules_are_matched_before_shipped_rules(self):
        self._write('app_rules.yaml',
                    'format: 2\nrules:\n  - match: ["code.exe"]\n    style: casual\n')
        rules = self._load_rules()
        self.assertEqual(rules[0]['match'], ['code.exe'])
        self.assertEqual(rules[0]['style'], 'casual')

    def test_disabled_drops_a_shipped_rule(self):
        self._write('app_rules.yaml',
                    'format: 2\nrules:\n  - id: terminals\n    disabled: true\n')
        rules = self._load_rules()
        self.assertNotIn('terminals', [r.get('id') for r in rules])
        self.assertTrue(all('disabled' not in r for r in rules))

    def test_override_inherits_the_keys_it_does_not_set(self):
        self._write('app_rules.yaml',
                    'format: 2\nrules:\n  - id: chat-apps\n    auto_send: false\n')
        chat = next(r for r in self._load_rules() if r.get('id') == 'chat-apps')
        self.assertIs(chat['auto_send'], False)
        self.assertEqual(chat['style'], 'casual')
        self.assertIn('slack.exe', chat['match'])

    def test_a_broken_package_falls_back_to_the_user_file(self):
        # Losing the shipped file must not throw the user's own rules away.
        import unittest.mock as mock
        import koli.defaults_merge as dm
        self._write('app_rules.yaml', 'format: 2\nrules:\n  - match: ["mytool.exe"]\n')
        with mock.patch.object(dm, 'resolve_asset_path', return_value=str(self.dir / 'nope.yaml')):
            merged, _ = dm.load_layered('nope.yaml', self.dir / 'app_rules.yaml',
                                        'rules', '', id_key='id')
        self.assertEqual(merged, [{'match': ['mytool.exe']}])

    def test_profiles_active_falls_back_to_the_shipped_default(self):
        import unittest.mock as mock
        import koli.profiles as pf
        with mock.patch.object(pf, 'get_user_app_data_path', return_value=str(self.dir)):
            manager = pf.ProfileManager(config_manager=mock.MagicMock())
        self.assertEqual(manager.active, 'dictation')
        self.assertEqual(sorted(manager.profiles),
                         ['chat', 'code', 'dictation', 'notes', 'translate'])

    def test_profiles_migration_preserves_the_active_choice(self):
        import unittest.mock as mock
        import koli.profiles as pf
        self._write('profiles.yaml',
                    'active: code\nprofiles:\n  code:\n    description: Mine\n')
        with mock.patch.object(pf, 'get_user_app_data_path', return_value=str(self.dir)):
            manager = pf.ProfileManager(config_manager=mock.MagicMock())
        self.assertEqual(manager.active, 'code')
        self.assertEqual(manager.profiles['code']['description'], 'Mine')
        self.assertIn('overrides', manager.profiles['code'], 'the shipped body should survive')

    def test_transform_override_inherits_the_prompt(self):
        import unittest.mock as mock
        try:
            import koli.transforms as tr
        except ImportError as e:
            self.skipTest(f'transforms needs deps the lean env lacks: {e}')
        self._write('transforms.yaml',
                    'format: 2\ntransforms:\n  - name: polish\n    hotkey: "win+alt+9"\n'
                    '  - name: expand\n    disabled: true\n')
        with mock.patch.object(tr, 'get_user_app_data_path', return_value=str(self.dir)):
            manager = tr.TransformsManager()
        names = [t.get('name') for t in manager.transforms]
        self.assertNotIn('expand', names)
        polish = next(t for t in manager.transforms if t['name'] == 'polish')
        self.assertEqual(polish['hotkey'], 'win+alt+9')
        self.assertTrue(polish.get('prompt'), 'the shipped prompt should still be there')

    def test_a_null_action_lets_an_override_swap_a_commands_action(self):
        # The merge keeps the shipped `hotkey` key, so clearing it has to mean
        # "no action here" to both the validator and the dispatcher.
        import unittest.mock as mock
        reimport_under(self, fake_platform_modules())
        try:
            import koli.voice_commands as vc
        except ImportError as e:
            self.skipTest(f'voice_commands needs deps the lean env lacks: {e}')
        self._write('commands.yaml',
                    'format: 2\ncommands:\n  - trigger: "maximize"\n'
                    '    hotkey: null\n    type: "maximised"\n')
        with mock.patch.object(vc, 'get_user_app_data_path', return_value=str(self.dir)):
            manager = vc.VoiceCommandManager(enabled=True)
        swapped = next(c for c in manager.commands if c.get('trigger') == 'maximize')
        self.assertIsNone(swapped['hotkey'])
        self.assertEqual(swapped['type'], 'maximised')

    def test_dispatcher_tests_action_values_not_key_presence(self):
        source = (ROOT / 'src' / 'koli' / 'voice_commands.py').read_text(encoding='utf-8')
        body = source[source.index('def _execute_action'):]
        body = body[:body.index('def ', 10)]
        for key in ('run', 'hotkey', 'type', 'rephrase'):
            self.assertIn("command.get('" + key + "') is not None", body,
                          f"a cleared '{key}' would still be dispatched")

    def test_doctor_reports_the_effective_counts(self):
        # The user file holds overrides only, so counting its entries would tell
        # someone they have 0 rules while five are in force.
        source = (ROOT / 'src' / 'koli' / 'doctor.py').read_text(encoding='utf-8')
        self.assertIn('rules in effect', source)
        self.assertIn('Voice commands in effect', source)

    def test_an_edited_match_list_overrides_instead_of_duplicating(self):
        # Dropping an app from a shipped rule has to actually drop it. If the
        # edited rule were kept as a separate one, the shipped rule would sit
        # behind it and go on auto-sending in the app that was removed.
        self._write('app_rules.yaml',
                    'rules:' + LF +
                    '  - match: ["slack.exe", "telegram.exe"]' + LF +
                    '    auto_send: true' + LF)
        rules = self._load_rules()
        chat = [r for r in rules if r.get('id') == 'chat-apps']
        self.assertEqual(len(chat), 1, 'the edited rule and the shipped one both loaded')
        self.assertEqual(chat[0]['match'], ['slack.exe', 'telegram.exe'])
        self.assertEqual([r for r in rules if not r.get('id')], [],
                         'it should have become an override, not a rule of its own')
        import koli.app_rules as ar
        self.assertFalse(any(ar.rule_matches(r, 'discord.exe', '') for r in rules if not ar.is_meeting_only(r)),
                         'discord was removed from the list but still matches')
        self.assertTrue(any(ar.rule_matches(r, 'slack.exe', '') for r in rules))

    def test_broken_user_yaml_is_never_overwritten(self):
        # A typo in their file must not read as "no entries" and trigger the
        # migration, which would rewrite what they were in the middle of
        # editing. The app runs on the shipped rules until they fix it.
        broken = 'rules:' + LF + '  - match: ["a.exe"' + LF + '    style: casual' + LF
        self._write('app_rules.yaml', broken)
        rules = self._load_rules()
        self.assertEqual([r.get('id') for r in rules],
                         ['password-managers', 'terminals', 'code-editors',
                          'chat-apps', 'email-clients', 'meeting-discord', 'meeting-zoom',
                          'meeting-teams', 'meeting-slack', 'meeting-google-meet',
                          'meeting-teams-web', 'meeting-zoom-web', 'meeting-discord-web'])
        self.assertEqual((self.dir / 'app_rules.yaml').read_text(encoding='utf-8'), broken)
        self.assertEqual(list(self.dir.glob('*.bak')), [])

    def test_broken_package_hands_back_the_right_container(self):
        # A list-shaped section must not come back as a dict just because the
        # shipped file is unreadable: the callers iterate it.
        import unittest.mock as mock
        import koli.defaults_merge as dm
        missing = str(self.dir / 'nope.yaml')
        with mock.patch.object(dm, 'resolve_asset_path', return_value=missing):
            commands, _ = dm.load_layered('nope.yaml', self.dir / 'commands.yaml',
                                          'commands', '', id_key='trigger')
            profiles, _ = dm.load_layered('nope.yaml', self.dir / 'profiles.yaml',
                                          'profiles', '', mapping=True)
        self.assertEqual(commands, [])
        self.assertEqual(profiles, {})

    def test_a_corrupt_shipped_file_does_not_crash_the_loader(self):
        import unittest.mock as mock
        import koli.defaults_merge as dm
        bad = self.dir / 'bad.defaults.yaml'
        bad.write_text('rules:' + LF + '  - match: ["a.exe"' + LF, encoding='utf-8')
        self._write('app_rules.yaml', 'format: 2' + LF + 'rules: []' + LF)
        with mock.patch.object(dm, 'resolve_asset_path', return_value=str(bad)):
            merged, _ = dm.load_layered('bad.defaults.yaml', self.dir / 'app_rules.yaml',
                                        'rules', '', id_key='id')
        self.assertEqual(merged, [])


# ── New desktop UI ────────────────────────────────────────────────────────────
# The settings schema, Simple mode, the settings service, the event bus, the
# log stream and the web pages behind the app window and the floating bar.

def _defaults_getter():
    import yaml
    from koli.config_manager import _resolve_platform_values
    defaults = _resolve_platform_values(yaml.safe_load(
        (ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8')))

    def get(path):
        node = defaults
        for key in path.split('.'):
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node
    return defaults, get


class SimpleModeTests(unittest.TestCase):
    def test_factory_defaults_match_a_named_text_style(self):
        # A new user must see one of the choices picked, not "custom".
        from koli.settings import simple_mode
        _, get = _defaults_getter()
        self.assertEqual(simple_mode.from_settings(get)['look'], 'standard')


class GpuFailureWithoutPromptTests(unittest.TestCase):
    def test_app_window_mode_falls_back_to_cpu_without_the_console_menu(self):
        # With the app window there may be no console to answer a prompt, so a
        # GPU load failure must never block on one.
        import unittest.mock as mock
        from koli import onboarding
        with mock.patch.object(onboarding, 'prompt_choice') as ask, \
                mock.patch.object(onboarding, 'restart_or_exit') as restart:
            onboarding.handle_gpu_failure(RuntimeError('CUDA libraries not found'), mock.MagicMock(), ask=False)
        ask.assert_not_called()
        restart.assert_not_called()


class FirstLaunchModeTests(unittest.TestCase):
    def test_settings_the_app_writes_by_itself_do_not_make_an_upgrade(self):
        # On first start the app saves the audio driver it picked and the GPU
        # onboarding state; a brand-new user must still get the welcome steps.
        from koli.settings import simple_mode
        fresh = {'audio': {'host': 'Windows WASAPI'}, 'onboarding': {'gpu': 'no_gpu'}, 'server': {'enabled': False}}
        self.assertFalse(simple_mode.is_existing_user(fresh))

    def test_a_changed_setting_means_an_existing_user(self):
        from koli.settings import simple_mode
        self.assertTrue(simple_mode.is_existing_user({'audio': {'host': 'MME', 'channels': 2}}))
        self.assertTrue(simple_mode.is_existing_user({'whisper': {'model': 'small'}}))


class InstanceIpcTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import unittest.mock as mock
        from koli.ui import instance_ipc
        self.ipc = instance_ipc
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(instance_ipc, 'get_user_app_data_path', return_value=tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.name = f'Koli_unittest_{os.getpid()}_{self._testMethodName[-12:]}'

    def _listen(self, **handlers):
        import threading
        got = {}
        done = threading.Event()

        def record(kind):
            def handler(*args):
                got[kind] = args
                done.set()
            return handler
        listener = self.ipc.InstanceListener(self.name, record('open'), on_quit=record('quit'))
        listener.start()
        self.addCleanup(listener.stop)
        return got, done

    def test_a_second_launch_opens_the_running_window(self):
        got, done = self._listen()
        self.assertTrue(self.ipc.send_open(self.name, 'history'))
        done.wait(2)
        self.assertEqual(got.get('open'), ('history',))

    def test_quit_asks_the_running_app_to_shut_down_cleanly(self):
        got, done = self._listen()
        self.assertTrue(self.ipc.send_quit(self.name))
        done.wait(2)
        self.assertIn('quit', got)

    def test_nothing_running_answers_false(self):
        self.assertFalse(self.ipc.send_quit(self.name))


class SettingsSchemaTests(unittest.TestCase):
    # Fails if a new key is added to config.defaults.yaml without a schema
    # entry (it would be missing from the app's Settings screen).
    def test_every_default_setting_has_a_schema_entry(self):
        from koli.settings import schema
        defaults, _ = _defaults_getter()
        missing = [p for p in schema.leaf_paths(defaults) if schema.get(p) is None]
        self.assertEqual(missing, [])

    # Fails if an entry points at a key that doesn't exist (a typo, or a key
    # that was renamed), unless it is marked optional.
    def test_every_schema_entry_points_at_a_real_setting(self):
        from koli.settings import schema
        defaults, _ = _defaults_getter()

        def exists(path):
            node = defaults
            for key in path.split('.'):
                if not isinstance(node, dict) or key not in node:
                    return False
                node = node[key]
            return True
        broken = [s.path for s in schema.SCHEMA if not s.optional and not exists(s.path)]
        self.assertEqual(broken, [])

    def test_choices_coerce_from_strings_and_reject_unknown_values(self):
        from koli.settings import schema
        channels = schema.get('audio.channels')
        self.assertEqual(schema.coerce(channels, '2'), 2)
        with self.assertRaises(ValueError):
            schema.coerce(channels, 7)
        with self.assertRaises(ValueError):
            schema.coerce(schema.get('vad.vad_silence_timeout_seconds'), 'abc')


class SimpleModeRoundTripTests(unittest.TestCase):
    # Every answer written by to_patch must read back as the same answer, or
    # the Simple screen would show a different choice than the one picked.
    def test_answers_read_back_after_they_are_applied(self):
        import copy
        from koli.settings import simple_mode
        defaults, _ = _defaults_getter()
        cases = [('look', 'clean'), ('look', 'relaxed'), ('look', 'exact'), ('look', 'standard'),
                 ('where', 'copy'), ('where', 'type'), ('start', 'press'), ('start', 'hold'),
                 ('sounds', False), ('punctuation', False), ('bar', False), ('stop', 'never'), ('stop', '60'),
                 ('volume', 'off'), ('volume', '10'), ('volume', '30'), ('calls', 'ask'), ('calls', 'off')]
        for question, answer in cases:
            with self.subTest(question=question, answer=answer):
                cfg = copy.deepcopy(defaults)

                def get(path, cfg=cfg):
                    node = cfg
                    for key in path.split('.'):
                        node = node.get(key) if isinstance(node, dict) else None
                    return node
                for path, value in simple_mode.to_patch(question, answer, {}).items():
                    node = cfg
                    keys = path.split('.')
                    for key in keys[:-1]:
                        node = node.setdefault(key, {})
                    node[keys[-1]] = value
                self.assertEqual(simple_mode.from_settings(get)[question], answer)


class _FakeConfig:
    def __init__(self, data):
        import copy
        self.data = copy.deepcopy(data)
        self._defaults_baseline = copy.deepcopy(data)

    def get_path(self, path, default=None):
        node = self.data
        for key in path.split('.'):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def set_path(self, path, value):
        node = self.data
        keys = path.split('.')
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value


class SettingsServiceTests(unittest.TestCase):
    def setUp(self):
        from koli.settings.service import SettingsService
        defaults, _ = _defaults_getter()
        self.cm = _FakeConfig(defaults)
        self.svc = SettingsService(self.cm)

    def test_a_change_is_saved_and_one_undo_puts_it_back(self):
        res = self.svc.set('clipboard.auto_paste', False)
        self.assertEqual((res['old'], res['value']), (True, False))
        self.assertFalse(self.cm.get_path('clipboard.auto_paste'))
        self.assertEqual(self.svc.undo()['paths'], ['clipboard.auto_paste'])
        self.assertTrue(self.cm.get_path('clipboard.auto_paste'))
        self.assertIsNone(self.svc.undo())

    def test_several_settings_undo_together(self):
        self.svc.set_many({'clipboard.auto_paste': False, 'audio_feedback.enabled': False}, label='Profile')
        self.svc.undo()
        self.assertTrue(self.cm.get_path('clipboard.auto_paste'))
        self.assertTrue(self.cm.get_path('audio_feedback.enabled'))

    def test_a_shortcut_already_in_use_is_refused(self):
        from koli.settings.service import SettingsError
        taken = self.cm.get_path('hotkey.paste_last_hotkey')
        with self.assertRaises(SettingsError):
            self.svc.set('hotkey.recording_hotkey', taken)
        self.assertNotEqual(self.cm.get_path('hotkey.recording_hotkey'), taken)

    # A key the OS backend can't register is refused before it's saved, so
    # the page shows the error and the previous shortcut stays.
    def test_a_key_the_backend_cannot_register_is_refused(self):
        import unittest.mock as mock
        from koli.settings.service import SettingsError
        modules = fake_platform_modules()
        modules['koli.platform.hotkeys'].unsupported_keys = (
            lambda combo: [k for k in combo.split('+') if k == '~'])
        before = self.cm.get_path('hotkey.recording_hotkey')
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaises(SettingsError) as caught:
                self.svc.set('hotkey.recording_hotkey', 'ctrl+~')
        self.assertIn('“~”', str(caught.exception))
        self.assertEqual(self.cm.get_path('hotkey.recording_hotkey'), before)
        self.assertFalse(self.svc.can_undo())

    # If registering still fails while applying, the saved value goes back too.
    def test_a_shortcut_that_fails_to_register_is_not_kept(self):
        import unittest.mock as mock
        from koli.settings.service import SettingsError
        self.svc.appliers = mock.Mock()
        self.svc.appliers.apply.side_effect = Exception('not a valid virtual keystroke')
        before = self.cm.get_path('hotkey.recording_hotkey')
        with self.assertRaises(SettingsError):
            self.svc.set('hotkey.recording_hotkey', 'f8')
        self.assertEqual(self.cm.get_path('hotkey.recording_hotkey'), before)
        self.assertFalse(self.svc.can_undo())

    def test_windows_backend_names_the_keys_it_cannot_register(self):
        try:
            from koli.platform.windows.hotkeys import unsupported_keys
        except Exception:
            self.skipTest('global_hotkeys not installed')
        self.assertEqual(unsupported_keys('ctrl+~'), ['~'])
        self.assertEqual(unsupported_keys('ctrl+,'), [','])
        for ok in ('ctrl+win', 'shift+`', 'ctrl+pageup', 'esc', 'alt+shift+z', 'f9'):
            self.assertEqual(unsupported_keys(ok), [], ok)

    def test_settings_the_app_manages_cannot_be_changed_from_the_page(self):
        from koli.settings.service import SettingsError
        with self.assertRaises(SettingsError):
            self.svc.set('ui.welcome_done', True)

    def test_a_change_is_announced_to_the_window(self):
        from koli.ui import events
        seen = []
        unsubscribe = events.get_bus().subscribe('settings', lambda t, p: seen.append(p))
        self.addCleanup(unsubscribe)
        self.svc.set('audio_feedback.enabled', False)
        self.assertEqual([(p['path'], p['new']) for p in seen], [('audio_feedback.enabled', False)])


class EventBusTests(unittest.TestCase):
    def test_a_broken_subscriber_does_not_stop_the_others(self):
        from koli.ui.events import EventBus
        bus = EventBus()
        got = []

        def broken(topic, payload):
            raise RuntimeError('boom')
        bus.subscribe('state', broken)
        bus.subscribe('*', lambda t, p: got.append((t, p)))
        bus.publish('state', {'state': 'recording'})
        self.assertEqual(got, [('state', {'state': 'recording'})])

    def test_unsubscribe_stops_delivery(self):
        from koli.ui.events import EventBus
        bus = EventBus()
        got = []
        off = bus.subscribe('level', lambda t, p: got.append(p))
        off()
        bus.publish('level', {'level': 0.5})
        self.assertEqual(got, [])


class LogStreamTests(unittest.TestCase):
    def test_records_are_kept_in_order_for_the_console(self):
        import logging
        from koli.ui.log_stream import LogStreamHandler
        handler = LogStreamHandler()
        log = logging.getLogger('koli.test_log_stream')
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        self.addCleanup(log.removeHandler, handler)
        log.info('first')
        log.warning('second')
        tail = handler.tail(5)
        self.assertEqual([(r['lv'], r['mod'], r['msg']) for r in tail],
                         [('INFO', 'test_log_stream', 'first'), ('WARNING', 'test_log_stream', 'second')])
        self.assertLess(tail[0]['seq'], tail[1]['seq'])
        handler.clear()
        self.assertEqual(handler.tail(5), [])


class PostprocessTraceTests(unittest.TestCase):
    # The Live screen's "Clean-up" step lists what changed; it must name the
    # steps that changed the text and only those.
    def test_trace_names_the_steps_that_changed_the_text(self):
        from koli.text_postprocess import postprocess
        defaults, _ = _defaults_getter()
        cfg = dict(defaults['postprocess'], strip_filler_words=True)
        trace = []
        out = postprocess('um I will send it', cfg, trace=trace)
        self.assertNotIn('um', out.lower().split())
        self.assertIn('filler_words', trace)

    def test_unchanged_text_leaves_an_empty_trace(self):
        from koli.text_postprocess import postprocess
        defaults, _ = _defaults_getter()
        trace = []
        postprocess('Hello there.', defaults['postprocess'], trace=trace)
        self.assertEqual(trace, [])


class LayeredWriteApiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from koli import app_rules
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.user = Path(tmp.name) / 'app_rules.yaml'
        self.defaults = app_rules.DEFAULTS_FILE

    def _view(self):
        from koli import defaults_merge as dm
        return dm.layered_view(self.defaults, self.user, 'rules', '', id_key='id')

    def test_own_rule_is_added_before_the_shipped_ones_and_can_be_deleted(self):
        from koli import defaults_merge as dm
        new_id = dm.save_entry(self.defaults, self.user, 'rules', '', {'match': ['notepad.exe'], 'auto_paste': False})
        view = self._view()
        self.assertEqual(view[0]['origin'], 'yours')
        self.assertEqual(view[0]['entry']['match'], ['notepad.exe'])
        dm.delete_entry(self.defaults, self.user, 'rules', '', new_id)
        self.assertNotIn('yours', [v['origin'] for v in self._view()])

    def test_turning_off_a_shipped_rule_keeps_it_listed_as_off(self):
        from koli import defaults_merge as dm
        dm.set_entry_disabled(self.defaults, self.user, 'rules', '', 'terminals', True)
        terminals = [v for v in self._view() if v['id'] == 'terminals'][0]
        self.assertTrue(terminals['disabled'])
        dm.set_entry_disabled(self.defaults, self.user, 'rules', '', 'terminals', False)
        terminals = [v for v in self._view() if v['id'] == 'terminals'][0]
        self.assertFalse(terminals['disabled'])


class AppRuleWindowTitleTests(unittest.TestCase):
    # A rule can name a program and also require words in its window title,
    # so "Chrome, but only on WhatsApp Web" is one rule. Without a title
    # condition a rule matches as before.
    CHROME_WHATSAPP = {'match': ['chrome.exe'], 'window_title': ['WhatsApp']}

    def test_the_title_narrows_a_program_rule(self):
        from koli.app_rules import rule_matches
        self.assertTrue(rule_matches(self.CHROME_WHATSAPP, 'chrome.exe', '(2) WhatsApp - Google Chrome'))
        self.assertFalse(rule_matches(self.CHROME_WHATSAPP, 'chrome.exe', 'Inbox - Gmail - Google Chrome'),
                         'any Chrome window matched, the title was ignored')
        self.assertFalse(rule_matches(self.CHROME_WHATSAPP, 'msedge.exe', 'WhatsApp - Microsoft Edge'),
                         'the right title in another program matched')

    def test_any_of_several_titles_will_do(self):
        from koli.app_rules import rule_matches
        rule = {'match': ['chrome.exe'], 'window_title': ['WhatsApp', 'Telegram']}
        self.assertTrue(rule_matches(rule, 'chrome.exe', 'Telegram Web - Google Chrome'))

    def test_without_a_title_any_window_of_the_program_matches(self):
        from koli.app_rules import rule_matches
        self.assertTrue(rule_matches({'match': ['chrome.exe']}, 'chrome.exe', 'Anything at all'))
        self.assertTrue(rule_matches({'match': 'chrome.exe', 'window_title': []}, 'chrome.exe', ''))

    def test_saving_keeps_a_clean_title_list(self):
        import tempfile
        import unittest.mock as mock
        from koli.services import rules as svc
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(svc, '_rules_path', return_value=Path(tmp.name) / 'app_rules.yaml'):
            rule_id = svc.save_rule({'match': ['chrome.exe'], 'window_title': [' WhatsApp ', '', 'Telegram']})
            saved = [r for r in svc.list_rules() if r['id'] == rule_id][0]['entry']
            self.assertEqual(list(saved['window_title']), ['WhatsApp', 'Telegram'])
            svc.save_rule({'match': ['chrome.exe'], 'window_title': []}, rule_id)
            saved = [r for r in svc.list_rules() if r['id'] == rule_id][0]['entry']
            self.assertNotIn('window_title', saved, 'an empty title list should leave the rule unrestricted')

    # The "matching now" badge on the Apps screen must agree with delivery.
    def test_matching_now_respects_the_title(self):
        from koli.services.rules import matching_rule
        items = [{'id': 'my-chrome', 'disabled': False, 'entry': self.CHROME_WHATSAPP}]
        self.assertIsNone(matching_rule(items, 'chrome.exe', 'Gmail - Google Chrome'))
        self.assertEqual(matching_rule(items, 'chrome.exe', 'WhatsApp - Google Chrome'), 'my-chrome')


class BridgeEnvelopeTests(unittest.TestCase):
    # Every page call gets {ok, data} or {ok: False, error}; an exception must
    # never reach the page as a pywebview failure.
    def test_results_and_errors_are_wrapped(self):
        from koli.ui.bridge import _api

        class Fake:
            @_api
            def good(self):
                return 42

            @_api
            def bad(self):
                raise ValueError('Not allowed')
        self.assertEqual(Fake().good(), {'ok': True, 'data': 42})
        self.assertEqual(Fake().bad(), {'ok': False, 'error': 'Not allowed'})


class WebPagesTests(unittest.TestCase):
    WEB = ROOT / 'src' / 'koli' / 'ui' / 'web'

    # The app shows every question in an in-page dialog; browser popups block
    # the window and look broken.
    def test_pages_never_use_browser_popups(self):
        import re
        hits = []
        for path in self.WEB.rglob('*.js'):
            if 'vendor' in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if re.search(r'(?<![\w.])(alert|confirm|prompt)\s*\(|window\.(alert|confirm|prompt)', line):
                    hits.append(f'{path.name}:{n}')
        self.assertEqual(hits, [])

    # The resting tab is 6 px tall. On the text baseline it would sit below the
    # 6 px window: painted by the page background but never hovered.
    def test_the_bubble_page_is_top_aligned(self):
        import re
        css = (self.WEB / 'css' / 'bar.css').read_text(encoding='utf-8')
        rule = re.search(r'#root\s*\{([^}]*)\}', css)
        self.assertIsNotNone(rule)
        self.assertIn('vertical-align: top', rule.group(1))

    def test_pages_only_load_local_files(self):
        import re
        for name in ('index.html', 'bar.html'):
            page = (self.WEB / name).read_text(encoding='utf-8')
            self.assertIn("script-src 'self'", page)
            self.assertIsNone(re.search(r'(src|href)="https?://', page), name)

    def test_every_imported_module_exists(self):
        import re
        missing = []
        for path in self.WEB.rglob('*.js'):
            for target in re.findall(r"from\s+'(\.{1,2}/[^']+)'", path.read_text(encoding='utf-8')):
                if not (path.parent / target).resolve().exists():
                    missing.append(f'{path.name} -> {target}')
        self.assertEqual(missing, [])


class UiModulesImportTests(unittest.TestCase):
    # The window library is optional; without it these must still import so
    # the app can fall back to the tray.
    def test_ui_modules_import_without_the_window_library(self):
        import importlib
        import unittest.mock as mock
        with mock.patch.dict(sys.modules, {'webview': None}):
            for name in ('koli.ui.events', 'koli.ui.log_stream', 'koli.ui.tasks',
                         'koli.ui.instance_ipc', 'koli.settings.schema',
                         'koli.settings.simple_mode', 'koli.settings.service'):
                importlib.import_module(name)
            from koli.ui import host
            ok, reason = host.ui_available()
            self.assertFalse(ok)
            self.assertTrue(reason)

    def test_missing_webview2_falls_back_with_a_reason(self):
        import unittest.mock as mock
        from koli.ui import host, win32
        if host.ui_available()[1].startswith('pywebview'):
            self.skipTest('pywebview not installed here')
        with mock.patch.object(win32, 'IS_WINDOWS', True), mock.patch.object(win32, 'webview2_version', return_value=None):
            ok, reason = host.ui_available()
        self.assertFalse(ok)
        self.assertIn('WebView2', reason)


class SlimTrayMenuTests(unittest.TestCase):
    def _tray(self):
        import unittest.mock as mock
        reimport_under(self, fake_platform_modules())
        from koli import system_tray
        sm = mock.MagicMock()
        sm.list_profiles.return_value = ['dictation', 'chat']
        sm.get_active_profile.return_value = 'chat'
        sm.get_recent_transcriptions.return_value = ['Hello there.']
        sm.is_paused = False
        tray = system_tray.SystemTray(sm, tray_config={'enabled': False}, config_manager=mock.MagicMock())
        return tray, sm

    @staticmethod
    def _labels(menu):
        import pystray
        return [item.text for item in menu.items if item is not pystray.Menu.SEPARATOR]

    # With the app window, everything else lives in the app; the tray keeps
    # only what is handy without opening it.
    def test_app_window_mode_shows_the_short_menu(self):
        tray, _ = self._tray()
        tray.on_open = lambda page: None
        tray.on_pause = lambda paused: None
        self.assertEqual(self._labels(tray._create_menu()),
                         ['Open Koli', 'Pause hotkeys', 'Profile: Chat', 'Recent', 'Quit Koli'])

    def test_pause_item_asks_the_app_to_pause(self):
        tray, sm = self._tray()
        tray.on_open = lambda page: None
        asked = []
        tray.on_pause = asked.append
        pause = [i for i in tray._create_menu().items if getattr(i, 'text', '') == 'Pause hotkeys'][0]
        pause(None)
        self.assertEqual(asked, [True])

    def test_without_the_app_window_the_full_menu_stays(self):
        tray, _ = self._tray()
        self.assertIn('Settings...', self._labels(tray._create_menu()))


class ColourContrastTests(unittest.TestCase):
    # Text colours must reach 4.5:1 (WCAG AA) on every surface they are used
    # on, in both themes, and on the floating bar.
    TEXT_ON_SURFACES = [(t, s) for t in ('ink', 'ink2', 'mute', 'acc', 'rec', 'ok', 'warn', 'purple')
                        for s in ('bg', 'panel', 'sunk')]
    TONE_ON_SOFT = [('acc', 'acc-soft'), ('rec', 'rec-soft'), ('ok', 'ok-soft'), ('warn', 'warn-soft'),
                    ('purple', 'purple-soft'), ('acc-ink', 'acc'), ('on-brand', 'brand')]
    # The edge bubble uses the app's own surfaces and text, so it is covered above.
    BAR = []

    @staticmethod
    def _themes():
        import re
        css = (ROOT / 'src' / 'koli' / 'ui' / 'web' / 'css' / 'tokens.css').read_text(encoding='utf-8')
        blocks = re.findall(r'(:root(?:\[data-theme="dark"\])?)\s*\{(.*?)\}', css, re.S)
        light = dict(re.findall(r'--([\w-]+):\s*(#[0-9A-Fa-f]{6})', blocks[0][1]))
        dark = dict(light, **dict(re.findall(r'--([\w-]+):\s*(#[0-9A-Fa-f]{6})', blocks[1][1])))
        return {'light': light, 'dark': dark}

    @staticmethod
    def _ratio(a, b):
        def lum(hex_colour):
            channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def test_text_is_readable_in_both_themes(self):
        low = []
        for theme, tokens in self._themes().items():
            for text, surface in self.TEXT_ON_SURFACES + self.TONE_ON_SOFT + self.BAR:
                ratio = self._ratio(tokens[text], tokens[surface])
                if ratio < 4.5:
                    low.append(f'{theme}: --{text} on --{surface} is {ratio:.2f}:1')
        self.assertEqual(low, [])


class BrokenSettingsFileTests(unittest.TestCase):
    BROKEN = 'clipboard:' + LF + '  auto_paste: [broken' + LF

    def _cm(self, d):
        import unittest.mock as mock
        try:
            from koli import config_manager as cm_mod
        except Exception:
            self.skipTest("config_manager not importable on this platform")
        import contextlib
        import io
        patcher = mock.patch.object(cm_mod, 'get_user_app_data_path', return_value=d)
        patcher.start()
        self.addCleanup(patcher.stop)
        with contextlib.redirect_stdout(io.StringIO()):  # it prints a symbol cp1252 can't encode
            return cm_mod.ConfigManager(quiet=True)

    @staticmethod
    def _touch_newer(path, base):
        os.utime(path, (base + 10, base + 10))

    # A half-typed hand edit must not reset the running app to defaults (which
    # would also change the hotkeys) or be saved over.
    def test_a_broken_hand_edit_keeps_the_running_settings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cm = self._cm(d)
            cm.set_path('clipboard.auto_paste', False)
            cm.flush()
            path = os.path.join(d, 'user_settings.yaml')
            base = os.path.getmtime(path)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(self.BROKEN)
            self._touch_newer(path, base)
            self.assertEqual(cm.reload_if_changed_externally(), [])
            self.assertFalse(cm.get_path('clipboard.auto_paste'))
            self.assertTrue(cm.reload_error)
            with open(path, encoding='utf-8') as f:
                self.assertIn('[broken', f.read())

    # Started with a file it can't read, the app runs on defaults; its first
    # save must keep a copy of what the user had.
    def test_saving_over_an_unreadable_file_keeps_a_copy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'user_settings.yaml')
            original = 'whisper:' + LF + '  model: small' + LF + self.BROKEN
            with open(path, 'w', encoding='utf-8') as f:
                f.write(original)
            cm = self._cm(d)
            cm.set_path('clipboard.auto_paste', False)
            cm.flush()
            copies = [n for n in os.listdir(d) if n.startswith('user_settings.yaml.') and n.endswith('.bak')]
            self.assertEqual(len(copies), 1)
            with open(os.path.join(d, copies[0]), encoding='utf-8') as f:
                self.assertEqual(f.read(), original)


class OwnWindowLookupTests(unittest.TestCase):
    # Two copies of the app (the normal one and --test) have windows with the
    # same title; each must find its own, never the other's.
    def test_only_this_process_windows_match(self):
        from koli.ui import win32
        windows = [(101, 'Koli', 5000), (102, 'Koli', 6000), (103, 'Koli · bubble', 6000)]
        self.assertEqual(win32.pick_window(windows, 'Koli', 6000), 102)
        self.assertEqual(win32.pick_window(windows, 'Koli · bubble', 6000), 103)
        self.assertEqual(win32.pick_window(windows, 'Koli', 7000), 0)


class CustomTitleBarTests(unittest.TestCase):
    # The app window draws its own title bar. Windows still has to know where
    # the top edge is (resize) and keep a maximized window's content on screen.
    def test_top_edge_resizes_only_while_not_maximized(self):
        from koli.ui import win32
        rect = (100, 100, 1100, 800)
        self.assertEqual(win32.top_edge_hit(500, 103, rect, 8, False), win32.HTTOP)
        self.assertEqual(win32.top_edge_hit(105, 103, rect, 8, False), win32.HTTOPLEFT)
        self.assertEqual(win32.top_edge_hit(1095, 103, rect, 8, False), win32.HTTOPRIGHT)
        self.assertIsNone(win32.top_edge_hit(500, 130, rect, 8, False))
        self.assertIsNone(win32.top_edge_hit(500, 103, rect, 8, True))

    def test_maximized_content_starts_on_screen(self):
        from koli.ui import win32
        # Maximized windows hang over the screen edge by their frame width.
        self.assertEqual(win32.caption_free_client_top(-8, True, 8), 0)
        self.assertEqual(win32.caption_free_client_top(100, False, 8), 100)

    def _host(self, maximized):
        import unittest.mock as mock
        from koli.ui import host as host_mod
        h = object.__new__(host_mod.UiHost)
        h.main = mock.MagicMock()
        h._maximized = maximized
        h.custom_frame = True
        return h

    def test_toggle_maximize_follows_the_tracked_state(self):
        h = self._host(maximized=True)
        h.toggle_maximize()
        h.main.window.restore.assert_called_once()
        h = self._host(maximized=False)
        h.toggle_maximize()
        h.main.window.maximize.assert_called_once()

    def test_page_learns_whether_to_draw_window_buttons(self):
        h = self._host(maximized=True)
        self.assertEqual(h.frame_state(), {'custom': True, 'maximized': True})

    def test_top_bar_has_window_buttons_and_a_drag_region(self):
        web = ROOT / 'src' / 'koli' / 'ui' / 'web'
        app_js = (web / 'js' / 'app.js').read_text(encoding='utf-8')
        for call in ('window_minimize', 'window_toggle_maximize', 'window_close'):
            self.assertIn(call, app_js)
        css = (web / 'css' / 'components.css').read_text(encoding='utf-8')
        self.assertIn('app-region: drag', css)
        self.assertIn('app-region: no-drag', css)


class HostMethodCallsTests(unittest.TestCase):
    # UiHost.run only runs with a real window, so a call to a method that was
    # renamed away (self._bar_origin once) shows up as the app window failing
    # to start. Every self.method(...) in these classes must exist, as a method
    # or as a callback stored on self (self._unsubscribe = ...).
    def test_every_self_call_names_a_real_method(self):
        import ast
        import inspect
        from koli.ui import bridge, host

        def on_self(node):
            return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'self'
        missing = []
        for module, cls in ((host, host.UiHost), (bridge, bridge.Bridge)):
            tree = ast.parse(inspect.getsource(module))
            stored = {t.attr for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets if on_self(t)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and on_self(node.func):
                    if not hasattr(cls, node.func.attr) and node.func.attr not in stored:
                        missing.append(f'{cls.__name__}.{node.func.attr} (line {node.lineno})')
        self.assertEqual(missing, [])


class BarPlacementTests(unittest.TestCase):
    # Sizes are physical pixels; the margin from the screen edge is 6 page
    # pixels, scaled.
    def _origin(self, area, size, scale, position, custom=None, field=None):
        from koli.ui.floating_bar import bar_origin
        return bar_origin(area, size, scale, position, custom=custom, field=field)

    # Taskbar on the left: the work area starts after it, and so does the bar.
    def test_left_taskbar_and_150_percent_scaling(self):
        self.assertEqual(self._origin((72, 0, 1920, 1080), (300, 60), 1.5, 'bottom-left'), (72 + 9, 1080 - 60 - 9))

    def test_top_taskbar_with_the_bar_at_the_top(self):
        self.assertEqual(self._origin((0, 48, 2560, 1440), (200, 40), 1.0, 'top-center'), ((2560 - 200) // 2, 48 + 6))

    # A bar taller than the screen (the quick-actions menu on a small or
    # zoomed display) stays on screen instead of starting above it.
    def test_a_bar_taller_than_the_work_area_stays_on_screen(self):
        x, y = self._origin((0, 0, 1366, 728), (425, 875), 1.25, 'bottom-right')
        self.assertGreaterEqual(y, 0)
        self.assertGreaterEqual(x, 0)

    # Koli's default: a bubble flush with the right edge, halfway down.
    def test_the_bubble_sits_on_the_right_edge(self):
        self.assertEqual(self._origin((0, 0, 2560, 1400), (200, 56), 1.0, 'right-edge'), (2560 - 200, (1400 - 56) // 2))

    def test_the_bubble_can_sit_on_the_left_edge(self):
        self.assertEqual(self._origin((48, 0, 1920, 1080), (250, 70), 1.25, 'left-edge'), (48, (1080 - 70) // 2))

    def test_edge_positions_and_a_dragged_spot_are_settings_choices(self):
        from koli.settings import schema
        choices = [c[0] for c in schema.get('overlay.position').choices]
        for name in ('right-edge', 'left-edge', 'custom'):
            self.assertIn(name, choices)

    def test_the_bar_height_is_limited_to_what_fits(self):
        import unittest.mock as mock
        from koli.ui import host as host_mod
        from koli.ui import win32
        h = object.__new__(host_mod.UiHost)
        h.bar_hwnd = 1
        with mock.patch.object(win32, 'work_area', lambda point=None: (0, 0, 1366, 728)), \
                mock.patch.object(win32, 'dpi_scale', lambda hwnd: 1.25):
            self.assertEqual(h._max_bar_height(), int((728 - 2 * int(6 * 1.25)) / 1.25))

    # ── A spot the user dragged it to ──

    def test_a_dragged_spot_centres_the_bubble_on_it(self):
        self.assertEqual(self._origin((0, 0, 1920, 1040), (44, 6), 1.0, 'custom', custom=(800, 500)), (778, 497))

    def test_a_spot_on_a_side_keeps_the_bubble_flush_with_it(self):
        area = (0, 0, 1920, 1040)
        self.assertEqual(self._origin(area, (6, 44), 1.0, 'custom', custom=(0, 300)), (0, 278))
        self.assertEqual(self._origin(area, (200, 56), 1.0, 'custom', custom=(1920, 300)), (1720, 272))

    def test_a_growing_bubble_at_a_dragged_spot_stays_on_screen(self):
        x, y = self._origin((0, 0, 1920, 1040), (600, 56), 1.0, 'custom', custom=(1900, 1035))
        self.assertEqual((x, y), (1320, 984))

    def test_custom_without_a_spot_falls_back_to_the_bottom_middle(self):
        self.assertEqual(self._origin((0, 0, 1920, 1040), (44, 6), 1.0, 'custom'), ((1920 - 44) // 2, 1040 - 6 - 6))

    # Dropping near a side sticks to it; anywhere else keeps the centre.
    def test_where_a_drop_lands(self):
        from koli.ui.floating_bar import dropped_spot
        area = (0, 0, 1920, 1040)
        self.assertEqual(dropped_spot(area, (500, 400, 544, 406), 1.0), (522, 403))
        self.assertEqual(dropped_spot(area, (10, 400, 54, 406), 1.0), (0, 403))
        # The right side is its last pixel, so a monitor to the right isn't picked.
        self.assertEqual(dropped_spot(area, (1880, 400, 1924, 406), 1.0), (1919, 403))

    # ── Following the text field ──

    def test_following_puts_the_tab_at_the_fields_right_end(self):
        field = (400, 600, 1000, 640)
        self.assertEqual(self._origin((0, 0, 1920, 1040), (6, 44), 1.0, 'right-edge', field=field), (1006, 598))

    def test_a_field_at_the_screen_edge_keeps_the_tab_inside_its_end(self):
        field = (1200, 600, 1918, 640)
        self.assertEqual(self._origin((0, 0, 1920, 1040), (6, 44), 1.0, 'right-edge', field=field), (1906, 598))

    # ── Upright or lying flat ──

    def test_the_tab_stands_upright_on_a_side_and_lies_flat_elsewhere(self):
        from koli.ui.floating_bar import is_upright
        area = (0, 0, 1920, 1040)
        self.assertTrue(is_upright('right-edge', None, area, following=False))
        self.assertFalse(is_upright('bottom-center', None, area, following=False))
        self.assertTrue(is_upright('bottom-center', None, area, following=True))
        self.assertTrue(is_upright('custom', (1920, 300), area, following=False))
        self.assertFalse(is_upright('custom', (800, 300), area, following=False))


class BackgroundChangeFailureTests(unittest.TestCase):
    # A model download that breaks off (or a microphone that won't open) must
    # put the previous setting back, so the app keeps working on it.
    def test_a_failed_background_change_puts_the_old_value_back(self):
        import threading
        import unittest.mock as mock
        from koli.settings.appliers import Appliers
        ctx = mock.MagicMock()
        reverted = threading.Event()
        ctx.settings.revert_silently.side_effect = lambda path, old: reverted.set()
        appliers = Appliers(ctx)

        def broken(reporter):
            raise OSError('Connection reset while downloading')
        result = appliers._background('Switching to small', 'engine', 'whisper.model', 'base', broken)
        self.assertEqual(result['applied'], 'background')
        self.assertTrue(reverted.wait(5))
        ctx.settings.revert_silently.assert_called_once_with('whisper.model', 'base')

    def test_a_cancelled_background_change_puts_the_old_value_back(self):
        import threading
        import time
        import unittest.mock as mock
        from koli.settings.appliers import Appliers
        ctx = mock.MagicMock()
        reverted = threading.Event()
        ctx.settings.revert_silently.side_effect = lambda path, old: reverted.set()
        appliers = Appliers(ctx)

        def slow(reporter):
            while True:
                reporter.check_cancelled()
                time.sleep(0.01)
        result = appliers._background('Switching to small', 'engine', 'whisper.model', 'base', slow)
        appliers.tasks.cancel(result['task_id'])
        self.assertTrue(reverted.wait(5))


class HandsFreeLockSignalTests(unittest.TestCase):
    # The floating bar shows "Hands-free" once a double-tap locks recording.
    def test_double_tap_reports_the_lock(self):
        from koli.hotkey_gestures import TapLatch
        now = [0.0]
        timers = []

        class FakeTimer:
            def __init__(self, seconds, callback):
                self.callback = callback
                timers.append(self)

            def cancel(self):
                pass
        locked = []
        latch = TapLatch(start=lambda: None, stop=lambda: None, window_ms=400, is_recording=lambda: True,
                         clock=lambda: now[0], timer_factory=FakeTimer, on_latch=lambda: locked.append(True))
        latch.press()
        now[0] = 0.1
        latch.release()
        self.assertEqual(locked, [])
        now[0] = 0.2
        latch.press()
        self.assertEqual(locked, [True])


class OptionalPackageTests(unittest.TestCase):
    # Noise suppression silently did nothing without its package; turning it
    # on must say so (the app window then offers the install).
    def test_turning_on_noise_suppression_without_its_package_warns(self):
        import unittest.mock as mock
        from koli.services import system
        from koli.settings.service import SettingsService
        defaults, _ = _defaults_getter()
        svc = SettingsService(_FakeConfig(defaults))
        with mock.patch.object(system, 'extra_installed', return_value=False):
            res = svc.set('audio.noise_suppression.enabled', True)
        self.assertTrue(any('noise' in w.lower() for w in res['warnings']))
        with mock.patch.object(system, 'extra_installed', return_value=True):
            svc.set('audio.noise_suppression.enabled', False)
            res = svc.set('audio.noise_suppression.enabled', True)
        self.assertEqual(res['warnings'], [])

    def test_unknown_extras_are_refused(self):
        from koli.services import system
        with self.assertRaises(ValueError):
            system.extra_install('anything; rm -rf', reporter=None)


class UnknownModelFallbackTests(unittest.TestCase):
    # A model named in user_settings.yaml that this version doesn't define
    # (removed in an update, a typo, a key from another branch) used to stop
    # the app from starting. It falls back to the default model instead.
    def _registry(self):
        from koli.model_registry import ModelRegistry
        defaults, _ = _defaults_getter()
        return ModelRegistry(whisper_models_config=defaults['whisper']['models'])

    def test_an_unknown_model_falls_back_to_the_default(self):
        self.assertEqual(self._registry().usable_key('no-such-model', 'base'), ('base', True))

    def test_known_models_repos_and_folders_are_kept(self):
        import tempfile
        registry = self._registry()
        self.assertEqual(registry.usable_key('small', 'base'), ('small', False))
        self.assertEqual(registry.usable_key('ivrit-ai/whisper-large-v3-turbo-ct2', 'base'),
                         ('ivrit-ai/whisper-large-v3-turbo-ct2', False))
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(registry.usable_key(folder, 'base'), (folder, False))


class GraphicsCardChoiceTests(unittest.TestCase):
    SMI = '0, NVIDIA GeForce RTX 5090, 32607' + LF + '1, NVIDIA GeForce RTX 4070 SUPER, 12282' + LF

    def test_cards_are_listed_in_the_order_cuda_uses(self):
        import unittest.mock as mock
        from koli.services import system
        done = mock.MagicMock(returncode=0, stdout=self.SMI)
        with mock.patch.object(system.subprocess, 'run', return_value=done):
            cards = system.gpu_list()
        self.assertEqual(cards, [{'index': 0, 'name': 'NVIDIA GeForce RTX 5090', 'memory_gb': 31.8},
                                 {'index': 1, 'name': 'NVIDIA GeForce RTX 4070 SUPER', 'memory_gb': 12.0}])
        self.assertEqual(os.environ.get('CUDA_DEVICE_ORDER'), 'PCI_BUS_ID')

    def test_no_nvidia_driver_means_no_cards(self):
        import unittest.mock as mock
        from koli.services import system
        with mock.patch.object(system.subprocess, 'run', side_effect=FileNotFoundError('nvidia-smi')):
            self.assertEqual(system.gpu_list(), [])

    # The chosen card reaches faster-whisper.
    def test_the_engine_loads_on_the_chosen_card(self):
        import unittest.mock as mock
        import contextlib
        import io
        from koli import whisper_engine
        with mock.patch.object(whisper_engine, 'WhisperModel') as model, \
                mock.patch.object(whisper_engine.WhisperEngine, '_verify_gpu_runtime'), \
                contextlib.redirect_stdout(io.StringIO()):  # it prints emoji cp1252 can't encode
            whisper_engine.WhisperEngine(model_key='base', device='cuda', compute_type='float16', device_index=1)
        self.assertEqual(model.call_args.kwargs.get('device_index'), 1)

    def test_changing_the_card_rebuilds_the_engine_in_the_background(self):
        from koli.settings import schema
        from koli.settings.appliers import _HANDLERS, Appliers
        self.assertEqual(schema.get('whisper.device_index').apply, schema.BACKGROUND)
        self.assertIs(_HANDLERS['whisper.device_index'], Appliers.engine_rebuild)


class _FakeMediaBackend:
    def __init__(self, volume=0.6):
        self.volume = volume
        self.paused = 0

    def get_master_volume(self):
        return self.volume

    def set_master_volume(self, value):
        self.volume = value

    def pause_playing(self):
        self.paused += 1
        return ['Spotify.exe']

    def resume_playing(self, apps):
        self.resumed = list(apps)
        return list(apps)


class MediaControlTests(unittest.TestCase):
    def _control(self, volume=0.6):
        from koli.media_control import MediaControl
        backend = _FakeMediaBackend(volume)
        return MediaControl(backend, run_async=lambda fn: fn()), backend

    CFG = {'pause_media_on_record': False, 'lower_volume': {'enabled': True, 'level': 20}}

    # Lowered to the level when talking starts, and put back afterwards.
    def test_a_loud_computer_is_lowered_and_restored(self):
        control, backend = self._control(0.6)
        control.on_recording_start(self.CFG)
        self.assertAlmostEqual(backend.volume, 0.20)
        control.on_recording_end()
        self.assertAlmostEqual(backend.volume, 0.6)

    # Only lowered when it is above the level; a quiet computer stays as it is.
    def test_a_quiet_computer_is_left_alone(self):
        control, backend = self._control(0.15)
        control.on_recording_start(self.CFG)
        self.assertAlmostEqual(backend.volume, 0.15)
        control.on_recording_end()
        self.assertAlmostEqual(backend.volume, 0.15)

    # If the user changed the volume while talking, their choice wins.
    def test_a_volume_changed_during_recording_is_kept(self):
        control, backend = self._control(0.6)
        control.on_recording_start(self.CFG)
        backend.volume = 0.45
        control.on_recording_end()
        self.assertAlmostEqual(backend.volume, 0.45)

    def test_off_means_untouched(self):
        control, backend = self._control(0.6)
        control.on_recording_start({'pause_media_on_record': False, 'lower_volume': {'enabled': False, 'level': 20}})
        self.assertAlmostEqual(backend.volume, 0.6)
        self.assertEqual(backend.paused, 0)

    MUSIC = {'pause_media_on_record': True, 'lower_volume': {'enabled': False}}

    # Without voice detection there is no "you started talking": pause at once.
    def test_pause_music_asks_the_media_players_to_pause(self):
        control, backend = self._control()
        control.on_recording_start(self.MUSIC)
        self.assertEqual(backend.paused, 1)

    # With voice detection the music plays on until you actually speak.
    def test_music_waits_for_your_voice(self):
        control, backend = self._control()
        control.on_recording_start(self.MUSIC, hears_voice=True)
        self.assertEqual(backend.paused, 0, 'nothing said yet')
        control.on_speech()
        self.assertEqual(backend.paused, 1)
        control.on_speech()
        self.assertEqual(backend.paused, 1, 'once per recording')

    # A recording in which you never spoke leaves the music alone.
    def test_no_speech_means_no_pause_and_no_resume(self):
        control, backend = self._control()
        control.on_recording_start(self.MUSIC, hears_voice=True)
        control.on_recording_end()
        control.on_speech()                       # a late event from that recording
        self.assertEqual(backend.paused, 0)
        self.assertFalse(hasattr(backend, 'resumed'))

    # What was paused plays again when the recording ends, once.
    def test_the_paused_music_plays_again_after_the_recording(self):
        control, backend = self._control()
        control.on_recording_start(self.MUSIC, hears_voice=True)
        control.on_speech()
        control.on_recording_end()
        self.assertEqual(backend.resumed, ['Spotify.exe'])
        backend.resumed = None
        control.on_recording_end()                # processing -> idle: no second play
        self.assertIsNone(backend.resumed)

    # The volume also waits for your voice, and comes back at the end.
    def test_volume_waits_for_your_voice(self):
        control, backend = self._control(0.6)
        control.on_recording_start(self.CFG, hears_voice=True)
        self.assertAlmostEqual(backend.volume, 0.6, msg='nothing said yet')
        control.on_speech()
        self.assertAlmostEqual(backend.volume, 0.20)
        control.on_recording_end()
        self.assertAlmostEqual(backend.volume, 0.6)

    # Never spoke: the volume was never touched.
    def test_volume_untouched_without_speech(self):
        control, backend = self._control(0.6)
        control.on_recording_start(self.CFG, hears_voice=True)
        control.on_recording_end()
        control.on_speech()
        self.assertAlmostEqual(backend.volume, 0.6)

    # The background worker keeps order: a play never overtakes its pause.
    def test_the_worker_runs_pause_before_resume(self):
        import threading
        from koli.media_control import _SerialWorker
        worker, done, order = _SerialWorker(), threading.Event(), []
        worker(lambda: order.append('pause'))
        worker(lambda: order.append('resume'))
        worker(done.set)
        self.assertTrue(done.wait(2))
        self.assertEqual(order, ['pause', 'resume'])

    def test_lower_volume_is_in_the_settings(self):
        from koli.settings import schema
        self.assertIsNotNone(schema.get('audio.lower_volume.enabled'))
        self.assertIsNotNone(schema.get('audio.lower_volume.level'))


class HuggingFaceCacheLocationTests(unittest.TestCase):
    # Models download wherever huggingface_hub puts them. A machine that moves
    # that cache (HF_HOME=E:\AI\hf_cache) used to have every model look "not
    # downloaded", and live preview failed right after its download finished.
    def _cache(self, env):
        import unittest.mock as mock
        from koli import utils
        keep = {k: v for k, v in os.environ.items()
                if k not in ('HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE', 'HF_HOME', 'XDG_CACHE_HOME')}
        with mock.patch.dict(os.environ, dict(keep, **env), clear=True):
            return utils.hf_hub_cache()

    def test_hf_home_moves_the_cache(self):
        self.assertEqual(self._cache({'HF_HOME': r'E:\AI\hf_cache'}), os.path.join(r'E:\AI\hf_cache', 'hub'))

    def test_hf_hub_cache_wins(self):
        self.assertEqual(self._cache({'HF_HOME': r'E:\a', 'HF_HUB_CACHE': r'F:\hub'}), r'F:\hub')

    def test_default_is_the_home_cache(self):
        self.assertTrue(self._cache({}).endswith(os.path.join('.cache', 'huggingface', 'hub')))

    def test_every_model_lookup_uses_it(self):
        import unittest.mock as mock
        from koli import utils
        from koli.model_registry import ModelRegistry
        with mock.patch.object(utils, 'hf_hub_cache', return_value=r'E:\AI\hf_cache\hub'):
            self.assertEqual(ModelRegistry({}, {}).get_hf_cache_path(), r'E:\AI\hf_cache\hub')


# ── Auto-dictation: a focused text field starts the recording ────────────────
# Design: docs/design/auto-dictation.md. The coordinator is pure Python, so
# these drive it with a fake watcher and a fake state manager.

class _AutoTenVad:
    def __init__(self):
        self.probability = 0.0

    def process(self, audio):
        return self.probability, 0


class _AutoWatcher:
    def __init__(self):
        self.on_focus = None
        self.on_typing = None
        self.started = 0
        self.stopped = 0

    def start(self, on_focus, on_typing):
        self.on_focus, self.on_typing = on_focus, on_typing
        self.started += 1
        return True

    def stop(self, token=None):
        self.stopped += 1

    def is_running(self):
        return self.on_focus is not None


class _AutoStateManager:
    NON_TEXT_EXES = {'progman.exe'}

    def __init__(self):
        self.is_paused = False
        self.busy = False
        self.auto = False
        self.starts = 0
        self.cancels = []

    def can_start_recording(self):
        return not self.busy and not self.auto

    def begin_auto_recording(self):
        self.starts += 1
        self.auto = True
        return True

    def is_auto_recording(self):
        return self.auto

    def cancel_auto_recording(self, reason=''):
        self.cancels.append(reason)
        self.auto = False
        return True


class _AutoRules:
    # `rule` matches the app in front; `elsewhere_on` stands for a rule that
    # turns auto-dictation on for some other app.
    def __init__(self, rule=None, elsewhere_on=False):
        self.rule = rule
        self.elsewhere_on = elsewhere_on

    def match_for_foreground(self):
        return self.rule

    def any_rule_sets(self, key):
        return self.elsewhere_on or bool(self.rule and self.rule.get(key) is True)


class _AutoCfg:
    def __init__(self, enabled=True, seconds=5):
        self.config = {'auto_dictation': {'enabled': enabled, 'silence_timeout_seconds': seconds}}


def _auto_field(**over):
    info = {'id': (1, 2, 3), 'editable': True, 'password': False, 'hwnd': 42, 'root_pid': 999, 'exe': 'chrome.exe',
            'rect': (100, 100, 400, 140)}
    info.update(over)
    return info


class AutoDictationCoordinatorTests(unittest.TestCase):
    def _make(self, enabled=True, rule=None, elsewhere_on=False):
        from koli.auto_dictation import AutoDictation
        self.sm = _AutoStateManager()
        self.watcher = _AutoWatcher()
        self.rules = _AutoRules(rule, elsewhere_on)
        self.koli_moving = False
        ad = AutoDictation(self.sm, _AutoCfg(enabled), self.rules, watcher=self.watcher, own_pid=4242,
                           koli_moving_focus=lambda: self.koli_moving)
        ad.start()
        return ad

    # A voice command with send_to moves focus to another window and back.
    # Neither hop is the user's: no recording is cancelled, and coming back to
    # the field doesn't start a new one.
    def test_focus_koli_moves_itself_is_ignored(self):
        ad = self._make()
        ad.on_focus(_auto_field(id=(7,)))
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1)
        self.sm.auto = False                       # the dictation finished
        self.koli_moving = True
        ad.on_focus(_auto_field(id=(9,), hwnd=77, root_pid=4242, editable=False, exe='python.exe'))
        ad.on_focus(_auto_field(id=(10,), hwnd=88, exe='chrome.exe'))   # a text field in the target app
        self.koli_moving = False
        ad.on_focus(_auto_field(id=(7,)))          # back where the user was
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1, 'the round trip must not start a recording')
        self.assertEqual(self.sm.cancels, [])

    # An app rule set to On works with the Settings switch off: the watcher
    # runs, and only that app starts recordings.
    def test_app_rule_can_turn_it_on_with_the_setting_off(self):
        ad = self._make(enabled=False, rule={'match': ['chrome.exe'], 'auto_dictation': True})
        self.assertTrue(ad.is_running())
        ad.on_focus(_auto_field())
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1)

    def test_with_the_setting_off_other_apps_stay_quiet(self):
        ad = self._make(enabled=False, rule=None, elsewhere_on=True)
        self.assertTrue(ad.is_running(), 'a rule elsewhere needs the watcher')
        ad.on_focus(_auto_field())
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 0)

    def test_removing_the_last_on_rule_unhooks(self):
        ad = self._make(enabled=False, rule=None, elsewhere_on=True)
        self.rules.elsewhere_on = False
        ad.apply_settings()
        self.assertFalse(ad.is_running())

    def test_off_by_default_hooks_nothing(self):
        ad = self._make(enabled=False)
        self.assertFalse(ad.is_running())
        self.assertEqual(self.watcher.started, 0)

    def test_focused_text_field_starts_after_the_debounce(self):
        ad = self._make()
        ad.on_focus(_auto_field())
        self.assertEqual(self.sm.starts, 0, 'not before the debounce')
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1)

    def test_tabbing_through_fields_starts_only_the_last(self):
        ad = self._make()
        ad.on_focus(_auto_field(id=(1,)))
        ad.on_focus(_auto_field(id=(2,)))
        ad.flush_timer()
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1)

    def test_same_field_does_not_restart_until_focus_moved_elsewhere(self):
        ad = self._make()
        ad.on_focus(_auto_field(id=(7,)))
        ad.flush_timer()
        self.sm.auto = False                      # the recording ended (silence or typing)
        ad.on_focus(_auto_field(id=(7,)))              # same field reported again
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1, 'the same field must not re-arm by itself')
        ad.on_focus(_auto_field(id=(8,), editable=False, rect=(500, 500, 540, 520)))   # a button
        ad.on_focus(_auto_field(id=(7,)))              # back to the field
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 2)

    def test_declines(self):
        cases = {
            'not a field': _auto_field(editable=False),
            'password': _auto_field(password=True),
            'own window': _auto_field(root_pid=4242),
            'desktop': _auto_field(exe='progman.exe'),
        }
        for label, info in cases.items():
            ad = self._make()
            ad.on_focus(info)
            ad.flush_timer()
            self.assertEqual(self.sm.starts, 0, label)

    def test_app_rule_can_turn_it_off(self):
        ad = self._make(rule={'match': ['chrome.exe'], 'auto_dictation': False})
        ad.on_focus(_auto_field())
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 0)
        ad = self._make(rule={'match': ['chrome.exe'], 'auto_paste': False})  # unrelated rule
        ad.on_focus(_auto_field())
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1)

    def test_paused_or_busy_never_starts(self):
        ad = self._make()
        self.sm.is_paused = True
        ad.on_focus(_auto_field(id=(1,)))
        ad.flush_timer()
        self.sm.is_paused = False
        self.sm.busy = True
        ad.on_focus(_auto_field(id=(2,)))
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 0)

    # During a meeting the microphone hears the call, so a click into a text
    # field must not start a recording, live or paused. After it ends, it does.
    def test_a_running_meeting_keeps_auto_dictation_off(self):
        import types
        ad = self._make()
        meeting = {'running': True}
        self.sm.meetings = types.SimpleNamespace(is_running=lambda: meeting['running'])
        ad.on_focus(_auto_field(id=(1,)))
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 0)
        meeting['running'] = False
        ad.on_focus(_auto_field(id=(2,), hwnd=43))  # another field, as a real click would be
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1)

    def test_typing_cancels_an_auto_recording_only(self):
        ad = self._make()
        ad.on_focus(_auto_field())
        ad.flush_timer()
        ad.on_typing('key 0x41 flags 0x00')
        self.assertEqual(self.sm.cancels, ['you started typing (key 0x41 flags 0x00)'])
        self.sm.auto = False                       # a hotkey recording is not ours
        ad.on_typing()
        self.assertEqual(len(self.sm.cancels), 1)

    # Cursor and other Chromium apps re-create the box's DOM node on re-render,
    # so the same field comes back with a new runtime id. Seen in the user's
    # log as cancel-then-restart every few seconds while nothing moved.
    def test_same_field_with_a_new_runtime_id_is_still_the_same_field(self):
        ad = self._make()
        ad.on_focus(_auto_field(id=(1,)))
        ad.flush_timer()
        ad.on_focus(_auto_field(id=(2,)))                       # same hwnd and rect
        ad.on_focus(_auto_field(id=(3,), rect=(102, 101, 398, 139)))   # inner node of the box
        ad.on_focus(_auto_field(id=(4,), rect=(90, 90, 410, 150)))     # outer wrapper of the box
        ad.flush_timer()
        self.assertEqual(self.sm.cancels, [])
        self.assertEqual(self.sm.starts, 1)

    def test_a_field_elsewhere_in_the_same_window_is_another_field(self):
        ad = self._make()
        ad.on_focus(_auto_field(id=(1,)))
        ad.flush_timer()
        ad.on_focus(_auto_field(id=(2,), rect=(100, 200, 400, 240)))   # the next form field
        self.assertEqual(self.sm.cancels, ['the text field lost focus'])
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 2)

    def test_leaving_the_field_cancels_the_recording(self):
        ad = self._make()
        ad.on_focus(_auto_field(id=(1,)))
        ad.flush_timer()
        self.assertTrue(self.sm.auto)
        ad.on_focus(_auto_field(id=(2,), editable=False, rect=(500, 500, 540, 520)))   # a button or another window
        self.assertEqual(self.sm.cancels, ['the text field lost focus'])
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1, 'a button does not start a new one')

    def test_moving_to_another_field_cancels_then_starts_again(self):
        ad = self._make()
        ad.on_focus(_auto_field(id=(1,)))
        ad.flush_timer()
        ad.on_focus(_auto_field(id=(2,), hwnd=77))              # a field in another window
        self.assertEqual(self.sm.cancels, ['the text field lost focus'])
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 2)

    def test_typing_during_the_debounce_drops_the_pending_start(self):
        ad = self._make()
        ad.on_focus(_auto_field())
        ad.on_typing()
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 0)

    def test_apply_settings_follows_the_toggle(self):
        ad = self._make()
        self.assertTrue(ad.is_running())
        ad.cm.config['auto_dictation']['enabled'] = False
        ad.apply_settings()
        self.assertFalse(ad.is_running())
        self.assertEqual(self.watcher.stopped, 1)
        ad.cm.config['auto_dictation']['enabled'] = True
        ad.apply_settings()
        self.assertTrue(ad.is_running())
        ad.on_focus(_auto_field())                     # callbacks work after a restart
        ad.flush_timer()
        self.assertEqual(self.sm.starts, 1)


# One TEN VAD shared by the live detector and the final check crashed
# ("access violation reading 0xFFFFFFFFFFFFFFFF") when auto-dictation's phrase
# delivery ran the check mid-recording; every later check said "silence".
class VadSharingTests(unittest.TestCase):
    def manager(self):
        from koli.voice_activity_detection import VadManager, HAS_TEN_VAD
        if not HAS_TEN_VAD:
            self.skipTest('ten-vad not installed')
        return VadManager(vad_precheck_enabled=True, vad_realtime_enabled=True)

    def test_the_live_detector_and_each_check_have_their_own_instance(self):
        import unittest.mock as mock
        import numpy as np
        from koli.voice_activity_detection import VadManager
        vad = self.manager()
        detector = vad.create_continuous_detector()
        self.assertIsNot(detector.ten_vad, vad.ten_vad)
        made = []
        real = VadManager.new_ten_vad
        with mock.patch.object(VadManager, 'new_ten_vad', side_effect=lambda: made.append(1) or real()):
            vad.check_audio_for_speech(np.zeros(16000, dtype=np.float32))
            vad.check_audio_for_speech(np.zeros(16000, dtype=np.float32))
        self.assertEqual(len(made), 2, 'a fresh one per check')

    def test_a_failed_live_detector_is_replaced(self):
        import numpy as np
        from koli.voice_activity_detection import ContinuousVoiceDetector, VadEvent

        class Broken:
            def process(self, audio):
                raise OSError('exception: access violation reading 0xFFFFFFFFFFFFFFFF')
        fresh = _AutoTenVad()
        det = ContinuousVoiceDetector(Broken(), 0.7, 0.55, 1.0, 0.016, vad_factory=lambda: fresh)
        self.assertEqual(det.process_chunk(np.zeros(256, dtype=np.float32)), VadEvent.NO_EVENT)
        self.assertIs(det.ten_vad, fresh)
        fresh.probability = 0.9
        det.process_chunk(np.zeros(256, dtype=np.float32))
        self.assertEqual(det.last_probability, 0.9, 'working again')

    # The crash's situation, with the real library: a check while the live
    # detector is busy on another thread. Nothing fails, and the check gives
    # the same answer as on its own.
    def test_a_check_during_live_detection_is_safe(self):
        import threading
        import numpy as np
        vad = self.manager()
        detector = vad.create_continuous_detector()
        rng = np.random.default_rng(3)
        audio = (rng.standard_normal(16000 * 3) * 0.1).astype(np.float32)
        alone = vad.check_audio_for_speech(audio)
        errors, stop = [], threading.Event()

        def live():
            chunk = (rng.standard_normal(256) * 0.1).astype(np.float32)
            while not stop.is_set():
                try:
                    detector.ten_vad.process(np.clip(chunk * 32767, -32768, 32767).astype(np.int16))
                except Exception as e:
                    errors.append(e)
        t = threading.Thread(target=live)
        t.start()
        try:
            results = [vad.check_audio_for_speech(audio) for _ in range(5)]
        finally:
            stop.set()
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, [alone] * 5)


class AutoDictationVadTests(unittest.TestCase):
    def _detector(self, timeout=0.032):
        from koli.voice_activity_detection import ContinuousVoiceDetector
        self.vad = _AutoTenVad()
        return ContinuousVoiceDetector(self.vad, 0.7, 0.55, timeout, 0.016)

    def _chunk(self):
        import numpy as np
        return np.zeros(256, dtype=np.float32)

    def test_had_speech_tracks_the_first_speech_frame_and_resets(self):
        det = self._detector()
        self.assertFalse(det.had_speech)
        det.process_chunk(self._chunk())
        self.assertFalse(det.had_speech, 'silence is not speech')
        self.vad.probability = 0.9
        det.process_chunk(self._chunk())
        self.assertTrue(det.had_speech)
        self.assertTrue(det.snapshot()['had_speech'])
        det.reset()
        self.assertFalse(det.had_speech)

    def test_silence_timeout_fires_without_any_speech(self):
        from koli.voice_activity_detection import VadEvent
        det = self._detector(timeout=0.032)       # two frames
        events = [det.process_chunk(self._chunk()) for _ in range(3)]
        self.assertIn(VadEvent.SILENCE_TIMEOUT, events)
        self.assertFalse(det.had_speech)

    def test_phrase_pause_fires_once_per_pause_after_speech_only(self):
        from koli.voice_activity_detection import VadEvent
        det = self._detector(timeout=0.16)        # ten frames
        det.set_phrase_pause(0.032)               # two frames
        silence = [det.process_chunk(self._chunk()) for _ in range(4)]
        self.assertNotIn(VadEvent.PHRASE_PAUSE, silence, 'no speech yet, nothing to deliver')
        self.vad.probability = 0.9
        det.process_chunk(self._chunk())
        self.vad.probability = 0.0
        events = [det.process_chunk(self._chunk()) for _ in range(5)]
        self.assertEqual(events.count(VadEvent.PHRASE_PAUSE), 1, 'once per pause')
        self.assertFalse(det.speech_since_phrase, 'that phrase is delivered')
        self.assertTrue(det.had_speech)
        more = [det.process_chunk(self._chunk()) for _ in range(6)]
        self.assertIn(VadEvent.SILENCE_TIMEOUT, more, 'the long silence still ends the recording')
        self.assertNotIn(VadEvent.PHRASE_PAUSE, more)
        self.assertEqual(det.snapshot()['phrase_pause_s'], 0.032)

    # A TV behind you is speech to TEN VAD: with your level known, speech
    # quieter than you counts as silence, so the phrase pause and the end of
    # the recording come when you stop, not when the TV does.
    def test_speech_quieter_than_you_counts_as_silence(self):
        import numpy as np
        from koli.voice_activity_detection import VadEvent
        det = self._detector(timeout=0.16)        # ten frames
        det.set_phrase_pause(0.032)               # two frames
        det.set_voice_start_level(-20.0)
        you = np.full(256, 0.3, dtype=np.float32)      # about -10 dBFS
        tv = np.full(256, 0.01, dtype=np.float32)      # -40 dBFS
        self.vad.probability = 0.9                      # both are speech to TEN VAD
        [det.process_chunk(you) for _ in range(10)]     # a short word
        events = [det.process_chunk(tv) for _ in range(40)]
        self.assertEqual(events.count(VadEvent.PHRASE_PAUSE), 1, 'you stopped: your phrase is delivered')
        self.assertIn(VadEvent.SILENCE_TIMEOUT, events, 'the TV alone ends the recording')
        det.reset()
        det.set_voice_start_level(-20.0)
        [det.process_chunk(tv) for _ in range(12)]
        self.assertFalse(det.had_speech, 'the TV alone never counts as speech')

    # Measured 2026-10-12: a TV's short loud bursts (up to -11 dB in 16 ms)
    # crossed the line every few seconds, so 5 s of "silence" never came. The
    # line is judged on about 0.3 s of sound, where they average out.
    def test_short_tv_bursts_dont_count_as_you(self):
        import numpy as np
        from koli.voice_activity_detection import VadEvent
        det = self._detector(timeout=0.16)        # ten frames
        det.set_voice_start_level(-20.0)
        tv, burst = np.full(256, 0.01, dtype=np.float32), np.full(256, 0.12, dtype=np.float32)   # -40 / -18 dBFS
        self.vad.probability = 0.9
        events = [det.process_chunk(burst if i % 6 == 0 else tv) for i in range(40)]
        self.assertIn(VadEvent.SILENCE_TIMEOUT, events)
        self.assertFalse(det.had_speech)

    def test_without_your_level_any_speech_keeps_the_recording_going(self):
        import numpy as np
        from koli.voice_activity_detection import VadEvent
        det = self._detector(timeout=0.16)
        self.vad.probability = 0.9
        tv = np.full(256, 0.01, dtype=np.float32)
        self.assertNotIn(VadEvent.SILENCE_TIMEOUT, [det.process_chunk(tv) for _ in range(12)])

    # SPEECH_START (it quiets the music) needs enough speech in total, fires
    # once per recording, and again after a reset.
    def test_speech_start_fires_once_after_real_speech(self):
        from koli.voice_activity_detection import ContinuousVoiceDetector, VadEvent
        self.vad = _AutoTenVad()
        det = ContinuousVoiceDetector(self.vad, 0.7, 0.55, 1.0, 0.016, speech_start_seconds=0.048)  # three frames
        self.vad.probability = 0.9
        blip = [det.process_chunk(self._chunk()) for _ in range(2)]
        self.vad.probability = 0.0
        det.process_chunk(self._chunk())
        self.assertNotIn(VadEvent.SPEECH_START, blip, 'too short to be talking')
        self.vad.probability = 0.9
        talk = [det.process_chunk(self._chunk()) for _ in range(6)]
        self.assertEqual(talk.count(VadEvent.SPEECH_START), 1)
        det.reset()
        again = [det.process_chunk(self._chunk()) for _ in range(3)]
        self.assertEqual(again.count(VadEvent.SPEECH_START), 1, 'the next recording announces again')

    # With your level known, speech quieter than it (a TV, another room) is
    # not you starting to talk; the level holds across recordings.
    def test_speech_start_needs_your_loudness(self):
        import numpy as np
        from koli.voice_activity_detection import ContinuousVoiceDetector, VadEvent, chunk_level_db
        self.vad = _AutoTenVad()
        det = ContinuousVoiceDetector(self.vad, 0.7, 0.55, 1.0, 0.016, speech_start_seconds=0.048)
        det.set_voice_start_level(-20.0)
        quiet = np.full(256, 0.01, dtype=np.float32)   # -40 dBFS
        loud = np.full(256, 0.3, dtype=np.float32)     # about -10 dBFS
        self.assertAlmostEqual(chunk_level_db(quiet), -40.0, places=1)
        self.vad.probability = 0.9
        far_off = [det.process_chunk(quiet) for _ in range(10)]
        self.assertNotIn(VadEvent.SPEECH_START, far_off, 'speech, but much quieter than you')
        # Loudness is judged on the last 0.3 s, so your first words count a
        # few frames (tens of milliseconds) after they start.
        you = [det.process_chunk(loud) for _ in range(8)]
        self.assertEqual(you.count(VadEvent.SPEECH_START), 1)
        det.reset()
        self.assertEqual(det.voice_start_db, -20.0)
        self.assertNotIn(VadEvent.SPEECH_START, [det.process_chunk(quiet) for _ in range(5)])

    def test_set_silence_timeout_changes_the_frame_budget(self):
        det = self._detector(timeout=0.32)        # twenty frames
        det.set_silence_timeout(0.048)
        self.assertEqual(det.frames_for_timeout, 3)
        self.assertEqual(det.snapshot()['timeout_s'], 0.048)


class AutoDictationStateManagerTests(unittest.TestCase):
    def _sm(self, had_speech):
        import unittest.mock as mock
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.logger = __import__('logging').getLogger('test')
        sm._state_lock = __import__('threading').Lock()
        sm._pipeline_lock = __import__('threading').Lock()
        sm._auto_started = True
        sm._auto_pasted = False
        sm._command_mode = False
        sm._rephrase_mode = False
        sm._rephrase_selection = ''
        sm._continuous_aborted = False
        sm._streaming_display_active = False
        sm._streaming_delivery_active = False
        sm.streaming_delivery = None
        sm.is_paused = False
        sm.is_processing = False
        sm.is_model_loading = False
        sm.level_overlay = None
        sm.audio_feedback = mock.Mock()
        sm.system_tray = mock.Mock()
        sm.terminal_title = mock.Mock()
        sm.media_control = mock.Mock()
        sm.vad_manager = mock.Mock(vad_silence_timeout_seconds=30.0)
        sm.audio_recorder = mock.Mock()
        sm.audio_recorder.get_recording_status.return_value = True
        sm.audio_recorder.continuous_vad = mock.Mock(had_speech=had_speech, silence_timeout_sec=5.0,
                                                     phrase_pause_sec=0.0, speech_since_phrase=had_speech)
        sm._transcription_pipeline = mock.Mock()
        return sm

    # Quiet means no chime, not no trace: the terminal says what happened,
    # like it does for a hotkey recording.
    def test_silence_without_speech_cancels_quietly_but_says_so(self):
        import unittest.mock as mock
        from koli.voice_activity_detection import VadEvent
        sm = self._sm(had_speech=False)
        with mock.patch('builtins.print') as out:
            sm.handle_vad_event(VadEvent.SILENCE_TIMEOUT)
        self.assertIn('nothing heard', ' '.join(str(c.args[0]) for c in out.call_args_list))
        sm.audio_recorder.cancel_recording.assert_called_once()
        sm.audio_feedback.play_cancel_sound.assert_not_called()
        sm._transcription_pipeline.assert_not_called()
        self.assertFalse(sm._auto_started)

    def test_silence_after_speech_delivers_as_usual(self):
        import unittest.mock as mock
        from koli.voice_activity_detection import VadEvent
        sm = self._sm(had_speech=True)
        with mock.patch('builtins.print'):   # the console line has an emoji
            sm.handle_vad_event(VadEvent.SILENCE_TIMEOUT)
        sm.audio_recorder.stop_recording.assert_called_once()
        sm._transcription_pipeline.assert_called_once()

    def test_auto_start_tells_the_console_like_a_hotkey_start(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=False)
        sm._auto_started = False
        sm.config_manager = mock.Mock(config={'auto_dictation': {'silence_timeout_seconds': 5, 'phrase_pause_seconds': 1}})
        sm._apply_recording_context = mock.Mock()
        sm._maybe_pause_media = mock.Mock()
        sm._setup_streaming_delivery = mock.Mock()
        sm.audio_recorder.start_recording.return_value = True
        sm.audio_recorder.get_recording_status.return_value = False   # idle before the start
        with mock.patch('builtins.print') as out, mock.patch('koli.state_manager.publish'):
            self.assertTrue(sm.begin_auto_recording())
        said = ' '.join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn('Listening', said)
        self.assertIn('typing', said.lower())
        sm.audio_feedback.play_start_sound.assert_not_called()
        sm.audio_recorder.start_recording.assert_called_once_with(silence_timeout=5.0, phrase_pause=1.0)

    # Speech starting is what pauses the music, and it never ends a recording.
    def test_speech_start_pauses_the_music_only(self):
        from koli.voice_activity_detection import VadEvent
        sm = self._sm(had_speech=True)
        sm.handle_vad_event(VadEvent.SPEECH_START)
        sm.media_control.on_speech.assert_called_once()
        sm.audio_recorder.stop_recording.assert_not_called()
        sm._transcription_pipeline.assert_not_called()

    # The music waits for your voice only when voice detection is running.
    def test_media_control_is_told_whether_voice_is_detected(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=False)
        sm.config_manager = mock.Mock(config={'audio': {'pause_media_on_record': True}})
        sm._maybe_pause_media()
        self.assertTrue(sm.media_control.on_recording_start.call_args.kwargs['hears_voice'])
        sm.audio_recorder.continuous_vad = None
        sm._maybe_pause_media()
        self.assertFalse(sm.media_control.on_recording_start.call_args.kwargs['hears_voice'])

    # The detector is told how loud "you" are: your level less the margin, or
    # None (any speech) until it is learned.
    def test_detector_gets_your_voice_level(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=False)
        sm.config_manager = mock.Mock(config={'audio': {'voice_filter': {'margin_db': 10}}})
        detector = sm.audio_recorder.continuous_vad
        sm.voice_levels = mock.Mock(my_level=mock.Mock(return_value=None))
        sm._maybe_pause_media()
        detector.set_voice_start_level.assert_called_with(None)
        sm.voice_levels.my_level.return_value = -18.0
        sm._maybe_pause_media()
        detector.set_voice_start_level.assert_called_with(-28.0)
        # "Ignore voices quieter than mine" off: any speech counts again.
        sm.config_manager.config['audio']['voice_filter']['quieter_than_me'] = False
        sm._maybe_pause_media()
        detector.set_voice_start_level.assert_called_with(None)

    # Your level is learned and checked on the raw microphone, like the meter
    # under How much quieter shows it: noise reduction doesn't move the line.
    def test_your_level_is_learned_and_checked_on_the_raw_microphone(self):
        import unittest.mock as mock
        import numpy as np
        sm, processed, raw = self._voiceprint_sm(None)
        sm.voice_levels = mock.Mock()
        with mock.patch('koli.state_manager.speech_level_db', return_value=-21.0) as level:
            sm._learn_my_level(processed)
        self.assertIs(level.call_args.args[0], raw)
        self.assertEqual(level.call_args.kwargs.get('gain', 1.0), 1.0)
        sm.voice_levels = mock.Mock(my_level=mock.Mock(return_value=-21.0))
        pieces = [{'text': ' hi', 'start': 0.0, 'end': 1.0}]
        with mock.patch('koli.state_manager.my_voice_pieces', return_value=(pieces, '')) as check:
            sm._keep_my_voice('hi', processed, {'segments': pieces}, True)
        self.assertIs(check.call_args.args[0], raw)
        self.assertEqual(check.call_args.args[4], 1.0, 'no Whisper-mode boost to undo on raw audio')

    def test_typing_cancel_only_touches_auto_recordings(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=False)
        with mock.patch('builtins.print'):
            self.assertTrue(sm.cancel_auto_recording('typing'))
        sm.audio_feedback.play_cancel_sound.assert_not_called()
        sm._auto_started = False                      # a hotkey recording
        self.assertFalse(sm.cancel_auto_recording('typing'))
        sm.audio_recorder.cancel_recording.assert_called_once()

    # Phrase delivery: a short pause pastes the words so far and keeps listening.
    def test_phrase_pause_delivers_without_ending_the_recording(self):
        import unittest.mock as mock
        import numpy as np
        from koli.voice_activity_detection import VadEvent
        sm = self._sm(had_speech=True)
        sm.audio_recorder.take_recorded_audio.return_value = np.zeros(16000, dtype=np.float32)
        sm.handle_vad_event(VadEvent.PHRASE_PAUSE)
        sm.audio_recorder.take_recorded_audio.assert_called_once()
        sm.audio_recorder.stop_recording.assert_not_called()
        sm._transcription_pipeline.assert_called_once()
        self.assertTrue(sm._transcription_pipeline.call_args.kwargs.get('phrase'))
        sm._auto_started = False                      # a hotkey recording never phrases
        sm.handle_vad_event(VadEvent.PHRASE_PAUSE)
        sm.audio_recorder.take_recorded_audio.assert_called_once()

    def test_phrase_run_pastes_with_a_space_silently_and_stays_recording(self):
        import unittest.mock as mock
        import numpy as np
        sm = self._sm(had_speech=True)
        del sm._transcription_pipeline                # use the real one
        sm.audio_recorder.get_audio_duration.return_value = 1.5
        sm.whisper_engine = mock.Mock(last_info={})
        sm.whisper_engine.transcribe_audio.return_value = 'hello there'
        sm.app_rules = mock.Mock()
        sm.app_rules.match_for_foreground.return_value = None
        sm.config_manager = mock.Mock(config={})
        sm.config_manager.get_postprocess_config.return_value = {}
        sm.clipboard_manager = mock.Mock(auto_paste=True, delivery_method='paste')
        sm.clipboard_manager.deliver_transcription.return_value = True
        sm._foreground_is_textable = lambda: True
        sm._pending_model_change = sm._pending_device_change = None
        sm._idle_actions = []
        sm._update_ui_state = mock.Mock()
        with mock.patch('builtins.print'), mock.patch('koli.state_manager.publish'), \
             mock.patch('koli.state_manager.postprocess', side_effect=lambda t, cfg, trace=None: t), \
             mock.patch('koli.state_manager.record_transcription'), mock.patch('koli.state_manager.record_transcript'), \
             mock.patch('koli.state_manager.foreground') as fg:
            fg.get_foreground_app.return_value = {'exe': 'discord.exe'}
            sm._auto_session = sm._live_session = 1
            sm._transcription_pipeline(np.zeros(16000, dtype=np.float32), phrase=True, phrase_session=1)
        sm.clipboard_manager.deliver_transcription.assert_called_once_with('hello there ', False, allow_delay=False,
                                                                           send_delay=None)
        sm.audio_feedback.play_stop_sound.assert_not_called()
        sm.audio_feedback.play_transcription_complete_sound.assert_not_called()
        sm._update_ui_state.assert_not_called()       # still "recording" on the bar
        self.assertTrue(sm._auto_started, 'the session goes on')
        self.assertFalse(sm.is_processing)

    # Runs the real pipeline on an auto-started session with this transcript
    # and app rule; returns the clipboard mock.
    def _run_auto_pipeline(self, text, rule, phrase, prompt=None, main_wait=0, info=None, auto=True,
                           audio=None, levels=None, config=None, prepare=None, phrase_session=None, run=None):
        import unittest.mock as mock
        import numpy as np
        sm = self._sm(had_speech=True)
        sm._auto_started = auto
        if phrase and phrase_session is None:
            sm._auto_session = sm._live_session = phrase_session = 1   # the session still running
        self.last_sm = sm
        del sm._transcription_pipeline
        sm.audio_recorder.get_audio_duration.return_value = 1.5
        sm.audio_recorder.raw_for.side_effect = lambda a: a   # no processing in these tests
        if levels is not None:
            sm.voice_levels = levels
        sm.whisper_engine = mock.Mock(last_info=info or {}, initial_prompt=prompt, language=None)
        sm.whisper_engine.transcribe_audio.return_value = text
        sm.app_rules = mock.Mock()
        sm.app_rules.match_for_foreground.return_value = rule
        sm.config_manager = mock.Mock(config=config or {})
        sm.config_manager.get_postprocess_config.return_value = {}
        sm.clipboard_manager = mock.Mock(auto_paste=True, delivery_method='paste', auto_send_delay=main_wait)
        sm.clipboard_manager.deliver_transcription.return_value = True
        sm.clipboard_manager.send_is_pending.return_value = False
        sm._foreground_is_textable = lambda: True
        sm._pending_model_change = sm._pending_device_change = None
        sm._idle_actions = []
        sm._update_ui_state = mock.Mock()
        if prepare:
            prepare(sm)
        clip = np.zeros(16000, dtype=np.float32) if audio is None else audio
        with mock.patch('builtins.print'), mock.patch('koli.state_manager.publish'), \
             mock.patch('koli.state_manager.postprocess', side_effect=lambda t, cfg, trace=None: t), \
             mock.patch('koli.state_manager.record_transcription'), mock.patch('koli.state_manager.record_transcript'), \
             mock.patch('koli.state_manager.foreground') as fg:
            fg.get_foreground_app.return_value = {'exe': 'whatsapp.root.exe'}
            if run:
                run(sm, clip)
            else:
                sm._transcription_pipeline(clip, phrase=phrase, phrase_session=phrase_session)
        return sm.clipboard_manager

    # ── Recordings nobody started on purpose (app.log, 2026-10-12) ──
    # 16:43 "I'm sorry, I'm so sorry": a phrase took 4.8 s to prepare, the
    # silence timeout ended its session meanwhile, and the phrase then ran as a
    # hotkey dictation. 17:31 "I'm sorry" x4: a continuous-mode restart heard
    # typing. Both skipped the auto-dictation guards and taught Koli "your" voice.

    def _unsure_info(self):
        # The 16:43 clip's own numbers.
        return {'language_probability': 0.47, 'audio_seconds': 2.74, 'segments': [
            {'text': " I'm sorry, I'm so sorry.", 'start': 0.0, 'end': 2.7, 'no_speech_prob': 0.35,
             'avg_logprob': -1.31, 'compression_ratio': 1.0}]}

    def _sure_info(self):
        return {'language_probability': 0.95, 'audio_seconds': 3.0, 'segments': [
            {'text': ' Merge it into master.', 'start': 0.0, 'end': 3.0, 'no_speech_prob': 0.02,
             'avg_logprob': -0.2, 'compression_ratio': 1.1}]}

    def test_a_phrase_that_outlives_its_session_keeps_the_auto_guards(self):
        import unittest.mock as mock
        levels = mock.Mock(my_level=mock.Mock(return_value=None))
        # The session ended quietly (timeout after the last phrase): its flag is
        # gone, but the phrase is still wanted.
        cb = self._run_auto_pipeline("I'm sorry, I'm so sorry.", None, True, auto=False, phrase_session=4,
                                     info=self._unsure_info(), levels=levels,
                                     prepare=lambda sm: setattr(sm, '_live_session', 4))
        cb.deliver_transcription.assert_not_called()
        levels.learn.assert_not_called()

    def test_a_wanted_late_phrase_is_still_pasted(self):
        cb = self._run_auto_pipeline('Merge it into master.', None, True, auto=False, phrase_session=4,
                                     info=self._sure_info(), prepare=lambda sm: setattr(sm, '_live_session', 4))
        self.assertEqual(cb.deliver_transcription.call_args.args[0], 'Merge it into master. ')

    def test_a_phrase_from_a_cancelled_session_is_dropped(self):
        # Typing, leaving the field or Esc cancelled the session while the
        # phrase was being prepared: pasting it now would land somewhere else.
        cb = self._run_auto_pipeline('Merge it into master.', None, True, auto=False, phrase_session=4,
                                     info=self._sure_info(), prepare=lambda sm: setattr(sm, '_live_session', None))
        cb.deliver_transcription.assert_not_called()

    # 16:43 again: the session ended quietly, then you clicked the terminal
    # before the phrase was ready. No cancel reaches a session that already
    # ended, so the phrase checks the window itself.
    def test_a_late_phrase_is_dropped_when_another_window_is_in_front(self):
        def ended_elsewhere(sm):
            sm._live_session = 4
            sm._live_window = ('hwnd', 1234)          # where the session listened
        cb = self._run_auto_pipeline('Merge it into master.', None, True, auto=False, phrase_session=4,
                                     info=self._sure_info(), prepare=ended_elsewhere)
        cb.deliver_transcription.assert_not_called()

    def test_a_late_phrase_in_the_same_window_is_pasted(self):
        def ended_here(sm):
            sm._live_session = 4
            sm._live_window = ('app', 'whatsapp.root.exe', None)   # the window still in front
        cb = self._run_auto_pipeline('Merge it into master.', None, True, auto=False, phrase_session=4,
                                     info=self._sure_info(), prepare=ended_here)
        self.assertEqual(cb.deliver_transcription.call_args.args[0], 'Merge it into master. ')

    # The whole 16:43 sequence on the real handlers: the timeout fires while the
    # phrase's audio is being prepared. Your words are kept, the session ends as
    # "done", and it is sent once, after the paste.
    def test_the_timeout_during_a_slow_phrase_ends_the_session_after_it(self):
        import unittest.mock as mock
        from koli.voice_activity_detection import VadEvent
        order = []

        def race(sm):
            sm._auto_session = sm._live_session = 4
            sm.audio_recorder.continuous_vad.phrase_pause_sec = 1.0
            sm.audio_recorder.continuous_vad.speech_since_phrase = False   # the pause just took it
            sm._finish_auto_session = mock.Mock(side_effect=lambda: order.append('finish'))
            sm.clipboard_manager.deliver_transcription.side_effect = lambda *a, **k: order.append('paste') or True

            def slow_take():
                sm.handle_vad_event(VadEvent.SILENCE_TIMEOUT)
                return __import__('numpy').zeros(16000, dtype='float32')
            sm.audio_recorder.take_recorded_audio.side_effect = slow_take

        with self.assertLogs('test', level='INFO') as logs:
            cb = self._run_auto_pipeline('Merge it into master.', None, True, info=self._sure_info(), prepare=race,
                                         run=lambda sm, clip: sm.handle_vad_event(VadEvent.PHRASE_PAUSE))
        said = '\n'.join(logs.output)
        self.assertIn('session done', said)
        self.assertNotIn('no speech heard', said)
        self.assertEqual(cb.deliver_transcription.call_args.args[0], 'Merge it into master. ')
        self.assertEqual(order, ['paste', 'finish'])

    def test_a_continuous_restart_gets_the_guards_and_teaches_nothing(self):
        import unittest.mock as mock
        levels = mock.Mock(my_level=mock.Mock(return_value=None))
        cb = self._run_auto_pipeline("I'm sorry. I'm sorry.", None, False, auto=False, info=self._unsure_info(),
                                     levels=levels, prepare=lambda sm: setattr(sm, '_hands_free', True))
        cb.deliver_transcription.assert_not_called()
        levels.learn.assert_not_called()
        self.assertFalse(self.last_sm._hands_free, 'the flag belongs to that one recording')
        levels = mock.Mock(my_level=mock.Mock(return_value=None))
        cb = self._run_auto_pipeline('Merge it into master.', None, False, auto=False, info=self._sure_info(),
                                     levels=levels, prepare=lambda sm: setattr(sm, '_hands_free', True))
        self.assertEqual(cb.deliver_transcription.call_args.args[0], 'Merge it into master.')
        levels.learn.assert_not_called()

    def test_a_phrase_carries_the_session_it_was_spoken_in(self):
        import numpy as np
        from koli.voice_activity_detection import VadEvent
        sm = self._sm(had_speech=True)
        sm._auto_session = sm._live_session = 7

        def slow_take():   # the silence timeout ends the session meanwhile
            sm._auto_started = False
            sm._live_session = None
            return np.zeros(16000, dtype=np.float32)
        sm.audio_recorder.take_recorded_audio.side_effect = slow_take
        sm.handle_vad_event(VadEvent.PHRASE_PAUSE)
        self.assertEqual(sm._transcription_pipeline.call_args.kwargs.get('phrase_session'), 7)
        self.assertEqual(sm._phrases_in_flight, 0)

    def test_the_timeout_waits_for_a_phrase_still_being_prepared(self):
        import unittest.mock as mock
        from koli.voice_activity_detection import VadEvent
        sm = self._sm(had_speech=False)          # nothing new since the phrase pause
        sm._auto_session = sm._live_session = 7
        sm._phrases_in_flight = 1
        sm._finish_auto_session = mock.Mock()
        with mock.patch('builtins.print') as out:
            sm.handle_vad_event(VadEvent.SILENCE_TIMEOUT)
        said = ' '.join(str(c.args[0]) for c in out.call_args_list)
        self.assertNotIn('nothing heard', said)
        sm.audio_recorder.cancel_recording.assert_called_once()
        self.assertEqual(sm._live_session, 7, 'the phrase being prepared is still wanted')
        sm._finish_auto_session.assert_not_called()   # not before that phrase is pasted
        sm._phrase_finished()
        sm._finish_auto_session.assert_called_once()

    def test_cancelling_drops_the_session_phrases(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=True)
        sm._auto_session = sm._live_session = 7
        with mock.patch('builtins.print'):
            sm.cancel_auto_recording('you started typing')
        self.assertIsNone(sm._live_session)

    def test_a_continuous_restart_is_marked_hands_free(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=False)
        sm.config_manager = mock.Mock(config={'audio': {'continuous_mode': True}})
        sm.audio_recorder.get_recording_status.return_value = False
        sm._begin_recording = mock.Mock()

        class InlineThread:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                self.target()
        with mock.patch('threading.Thread', InlineThread), mock.patch('time.sleep'):
            sm._maybe_restart_continuous()
        sm._begin_recording.assert_called_once_with(hands_free=True)

    # ── Only my voice (loudness) ──

    def _me_then_tv(self):
        import unittest.mock as mock
        import numpy as np
        t = np.arange(16000) / 16000
        me, tv = 0.1 * np.sin(2 * np.pi * 220 * t), 0.01 * np.sin(2 * np.pi * 220 * t)   # TV 20 dB lower
        audio = np.concatenate([me, tv]).astype(np.float32)
        info = {'language_probability': 0.95, 'segments': [
            {'text': ' Why you stop doing that?', 'start': 0.0, 'end': 1.0, 'no_speech_prob': 0.05,
             'avg_logprob': -0.3, 'compression_ratio': 1.2},
            {'text': " Do you know what I'm saying?", 'start': 1.0, 'end': 2.0, 'no_speech_prob': 0.05,
             'avg_logprob': -0.3, 'compression_ratio': 1.2}]}
        levels = mock.Mock()
        levels.my_level.return_value = -23.0
        return audio, info, levels

    def test_auto_dictation_leaves_out_voices_much_quieter_than_me(self):
        audio, info, levels = self._me_then_tv()
        cb = self._run_auto_pipeline('both', None, True, info=info, audio=audio, levels=levels)
        self.assertEqual(cb.deliver_transcription.call_args.args[0], 'Why you stop doing that? ')

    # A hotkey dictation is you: it teaches Koli your level, and by default
    # isn't filtered.
    def test_a_hotkey_dictation_teaches_my_level(self):
        audio, info, levels = self._me_then_tv()
        cb = self._run_auto_pipeline('both', None, False, info=info, audio=audio, levels=levels, auto=False)
        self.assertEqual(cb.deliver_transcription.call_args.args[0], 'both')
        levels.learn.assert_called_once()
        device, level = levels.learn.call_args.args
        self.assertEqual(device, 'default')
        self.assertAlmostEqual(level, -23.0, delta=0.5)
        levels = self._me_then_tv()[2]
        self._run_auto_pipeline('both', None, True, info=info, audio=audio, levels=levels)
        levels.learn.assert_not_called()      # an auto session could be anyone

    def test_all_recordings_filters_hotkey_dictation_too(self):
        audio, info, levels = self._me_then_tv()
        config = {'audio': {'voice_filter': {'apply_to': 'all', 'quieter_than_me': True, 'margin_db': 12}}}
        cb = self._run_auto_pipeline('both', None, False, info=info, audio=audio, levels=levels, auto=False,
                                     config=config)
        self.assertEqual(cb.deliver_transcription.call_args.args[0], 'Why you stop doing that?')

    def test_the_loudness_check_can_be_turned_off(self):
        audio, info, levels = self._me_then_tv()
        config = {'audio': {'voice_filter': {'quieter_than_me': False}}}
        cb = self._run_auto_pipeline('both', None, True, info=info, audio=audio, levels=levels, config=config)
        self.assertEqual(cb.deliver_transcription.call_args.args[0], 'both ')

    # ── Only my voice (voiceprint) ──
    # Measured 2026-10-12: a TV scored 0.11 against the user's voiceprint from
    # the raw microphone, but 0.45 (over the 0.43 "you" line) after noise
    # reduction and the Whisper-mode boost, the audio Koli scored. Your
    # voiceprint is learned and checked on the raw microphone.

    def _voiceprint_sm(self, similarity):
        import unittest.mock as mock
        import numpy as np
        sm = self._sm(had_speech=True)
        sm.config_manager = mock.Mock(config={})
        sm.meetings = mock.Mock()
        sm.meetings.my_voice_similarity.return_value = similarity
        processed, raw = np.zeros(32000, dtype=np.float32), np.ones(32000, dtype=np.float32)
        sm.audio_recorder.raw_for.side_effect = lambda a: raw if a is processed else a
        return sm, processed, raw

    def test_the_voiceprint_is_checked_on_the_raw_microphone(self):
        sm, processed, raw = self._voiceprint_sm(0.11)
        with self.assertLogs('test', level='INFO') as logs:
            self.assertFalse(sm._sounds_like_me(processed))
        self.assertIs(sm.meetings.my_voice_similarity.call_args.args[0], raw)
        self.assertIn('0.11', '\n'.join(logs.output))

    def test_the_voiceprint_is_learned_from_the_raw_microphone(self):
        sm, processed, raw = self._voiceprint_sm(None)
        sm._learn_my_voiceprint(processed)
        self.assertIs(sm.meetings.learn_my_voice.call_args.args[0], raw)

    # Settings → "Does Koli recognize you?": 5 seconds from the microphone,
    # judged the way auto-dictation judges a phrase.
    def _voice_check(self, score=0.72, level=-22.0, speech=True, learned=45, ready=True, config=None):
        import unittest.mock as mock
        import numpy as np
        sm = self._sm(had_speech=True)
        sm.audio_recorder.get_recording_status.return_value = False
        sm.audio_recorder.capture_raw.return_value = np.full(16000 * 3, 0.05, dtype=np.float32)
        sm.vad_manager.check_audio_for_speech.return_value = speech
        sm.config_manager = mock.Mock(config=config or {})
        sm.voice_levels = mock.Mock(my_level=mock.Mock(return_value=-21.0))
        sm.meetings = mock.Mock()
        sm.meetings.my_voice_status.return_value = {'score': score, 'learned': learned, 'needed': 3, 'ready': ready}
        with mock.patch('koli.state_manager.speech_level_db', return_value=level):
            return sm.check_my_voice(5)

    def test_the_voice_check_recognizes_you(self):
        r = self._voice_check()
        self.assertEqual(r['verdict'], 'type')
        self.assertEqual((r['voiceprint']['status'], r['loudness']['status']), ('match', 'mine'))
        self.assertEqual((r['voiceprint']['score'], r['voiceprint']['needs']), (0.72, 0.43))

    def test_the_voice_check_leaves_out_the_tv(self):
        r = self._voice_check(score=0.11, level=-30.0)
        self.assertEqual(r['verdict'], 'leave_out')
        self.assertEqual(r['voiceprint']['status'], 'no_match')
        self.assertIn("doesn't sound like you", r['reason'])

    def test_the_voice_check_leaves_out_a_much_quieter_voice(self):
        r = self._voice_check(level=-40.0)
        self.assertEqual((r['verdict'], r['loudness']['status']), ('leave_out', 'quieter'))

    def test_the_voice_check_says_when_your_voice_isnt_learned_yet(self):
        r = self._voice_check(score=None, learned=1)
        self.assertEqual(r['voiceprint']['status'], 'not_learned')
        self.assertEqual((r['voiceprint']['learned'], r['voiceprint']['needed']), (1, 3))

    def test_the_voice_check_with_nobody_talking(self):
        self.assertEqual(self._voice_check(speech=False)['verdict'], 'no_speech')

    # "Set it by ear" on How much quieter: your learned level on the microphone
    # in use, to draw the cutoff (your level less the margin) on the meter.
    def test_my_voice_level_is_your_learned_level_on_this_mic(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=True)
        sm.audio_recorder.describe.return_value = {'name': 'Microphone (EMEET Piko+)'}
        sm.voice_levels = mock.Mock(my_level=mock.Mock(return_value=-20.64))
        self.assertEqual(sm.my_voice_level(), {'mic': 'Microphone (EMEET Piko+)', 'mine_db': -20.6})
        sm.voice_levels.my_level.assert_called_with('Microphone (EMEET Piko+)')
        sm.voice_levels.my_level.return_value = None      # fewer than 3 hotkey dictations
        self.assertIsNone(sm.my_voice_level()['mine_db'])

    def test_the_voice_check_waits_for_a_recording_to_finish(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=True)
        sm.audio_recorder.get_recording_status.return_value = True
        with self.assertRaises(ValueError):
            sm.check_my_voice(5)

    # Every phrase says how it scored, kept or not, and a check that couldn't
    # run says so: a silent pass is how the TV got through unnoticed.
    def test_every_voiceprint_result_is_logged(self):
        sm, processed, _ = self._voiceprint_sm(0.71)
        with self.assertLogs('test', level='INFO') as logs:
            self.assertTrue(sm._sounds_like_me(processed))
        self.assertIn('0.71', '\n'.join(logs.output))
        sm, processed, _ = self._voiceprint_sm(None)
        with self.assertLogs('test', level='INFO') as logs:
            self.assertTrue(sm._sounds_like_me(processed))
        self.assertIn('not checked', '\n'.join(logs.output))

    # Auto-dictation hears whatever is around, so a paste never carries its own
    # Enter: not after each pause, and not with the final words.
    def test_auto_dictation_pastes_never_press_enter(self):
        chat = {'id': 'chat-apps', 'match': ['whatsapp'], 'auto_send': True}
        for phrase in (True, False):
            cb = self._run_auto_pipeline('see you tomorrow', chat, phrase)
            args = cb.deliver_transcription.call_args
            self.assertFalse(args.args[1], f'Enter pressed (phrase={phrase})')
            self.assertFalse(args.kwargs['allow_delay'])

    # The session as a whole is sent once, after the rule's wait (or the main
    # one), through the countdown that Esc, typing or Cancel stop.
    def test_a_finished_session_is_sent_after_the_wait(self):
        chat = {'id': 'chat-apps', 'match': ['whatsapp'], 'auto_send': True, 'auto_send_delay': 3}
        cb = self._run_auto_pipeline('see you tomorrow', chat, phrase=False)
        cb.delayed_send.schedule.assert_called_once_with(3.0)
        cb = self._run_auto_pipeline('see you tomorrow', {**chat, 'auto_send_delay': None}, phrase=False, main_wait=5)
        cb.delayed_send.schedule.assert_called_once_with(5.0)

    # No wait anywhere: nothing would stop a stray message, so it isn't sent.
    def test_without_a_wait_the_session_is_not_sent(self):
        chat = {'id': 'chat-apps', 'match': ['whatsapp'], 'auto_send': True}
        cb = self._run_auto_pipeline('see you tomorrow', chat, phrase=False, main_wait=0)
        cb.delayed_send.schedule.assert_not_called()
        cb = self._run_auto_pipeline('see you tomorrow', {'match': ['whatsapp']}, phrase=False, main_wait=5)
        cb.delayed_send.schedule.assert_not_called()

    def test_a_pause_does_not_send(self):
        chat = {'id': 'chat-apps', 'match': ['whatsapp'], 'auto_send': True, 'auto_send_delay': 3}
        cb = self._run_auto_pipeline('see you tomorrow', chat, phrase=True)
        cb.delayed_send.schedule.assert_not_called()
        self.assertTrue(self.last_sm._auto_pasted)

    # The usual end: every word already pasted, then quiet. That is a finished
    # session too, so it is sent.
    def test_a_quiet_end_after_pasted_phrases_sends(self):
        import unittest.mock as mock
        from koli.voice_activity_detection import VadEvent
        sm = self._sm(had_speech=True)
        sm.audio_recorder.continuous_vad.phrase_pause_sec = 1.0
        sm.audio_recorder.continuous_vad.speech_since_phrase = False
        sm._auto_pasted = True
        sm.app_rules = mock.Mock()
        sm.app_rules.match_for_foreground.return_value = {'auto_send': True, 'auto_send_delay': 2}
        sm.clipboard_manager = mock.Mock(auto_send_delay=0)
        sm.cancel_active_recording = mock.Mock()
        with mock.patch('builtins.print'):
            sm.handle_vad_event(VadEvent.SILENCE_TIMEOUT)
        sm.clipboard_manager.delayed_send.schedule.assert_called_once_with(2.0)

    # An Auto wait counts every word the session pasted, not just the last
    # phrase, and the count starts over for the next session.
    def test_an_auto_wait_counts_the_whole_session(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=True)
        sm._auto_pasted = True
        sm._auto_pasted_words = 15
        sm.app_rules = mock.Mock()
        sm.app_rules.match_for_foreground.return_value = {'auto_send': True, 'auto_send_delay': 'auto_medium'}
        sm.clipboard_manager = mock.Mock(auto_send_delay=0)
        with mock.patch('builtins.print'):
            sm._finish_auto_session()
        sm.clipboard_manager.delayed_send.schedule.assert_called_once_with(4.0)
        self.assertEqual(sm._auto_pasted_words, 0)

    # Leaving the field or typing means "not this": nothing is sent.
    def test_a_cancelled_session_is_not_sent(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=True)
        sm._auto_pasted = True
        sm.app_rules = mock.Mock()
        sm.app_rules.match_for_foreground.return_value = {'auto_send': True, 'auto_send_delay': 2}
        sm.clipboard_manager = mock.Mock(auto_send_delay=0)
        sm.cancel_active_recording = mock.Mock()
        with mock.patch('builtins.print'):
            sm.cancel_auto_recording('you started typing')
        sm.clipboard_manager.delayed_send.schedule.assert_not_called()
        self.assertFalse(sm._auto_pasted)

    # Nobody pressed a key to say "I'm talking now", so an auto session drops
    # what Whisper is unsure of; a hotkey recording is not second-guessed.
    def test_auto_dictation_drops_what_whisper_is_unsure_of(self):
        unsure = {'language_probability': 0.48, 'segments': [
            {'text': " Do you know what I'm saying?", 'no_speech_prob': 0.1, 'avg_logprob': -0.5,
             'compression_ratio': 1.2}]}
        cb = self._run_auto_pipeline("Do you know what I'm saying?", None, True, info=unsure)
        cb.deliver_transcription.assert_not_called()
        cb = self._run_auto_pipeline("Do you know what I'm saying?", None, False, info=unsure, auto=False)
        cb.deliver_transcription.assert_called_once()

    # Whisper fills noise with its own prompt: "Conversational chat message."
    # from the chat rule's "Casual chat message. Conversational tone."
    def test_an_echo_of_the_prompt_is_dropped(self):
        prompt = 'Casual chat message. Conversational tone.'
        cb = self._run_auto_pipeline('Conversational chat message.', None, True, prompt=prompt)
        cb.deliver_transcription.assert_not_called()
        cb = self._run_auto_pipeline('Conversational chat message.', None, False, prompt=prompt)
        cb.deliver_transcription.assert_not_called()
        cb = self._run_auto_pipeline('Our chat about the message went well', None, True, prompt=prompt)
        cb.deliver_transcription.assert_called_once()

    def test_session_ends_quietly_when_every_phrase_was_already_pasted(self):
        import unittest.mock as mock
        from koli.voice_activity_detection import VadEvent
        sm = self._sm(had_speech=True)
        sm.audio_recorder.continuous_vad.phrase_pause_sec = 1.0
        sm.audio_recorder.continuous_vad.speech_since_phrase = False   # nothing new since the last paste
        with mock.patch('builtins.print'):
            sm.handle_vad_event(VadEvent.SILENCE_TIMEOUT)
        sm.audio_recorder.cancel_recording.assert_called_once()
        sm._transcription_pipeline.assert_not_called()

    def test_state_event_says_auto_while_listening(self):
        import unittest.mock as mock
        sm = self._sm(had_speech=False)
        with mock.patch('koli.state_manager.publish') as publish:
            sm._publish_state('recording')
            self.assertEqual(publish.call_args.args[1]['mode'], 'auto')
            sm._auto_started = False
            sm._publish_state('recording')
            self.assertEqual(publish.call_args.args[1]['mode'], 'dictate')


class AutoDictationTypingFilterTests(unittest.TestCase):
    def setUp(self):
        try:
            from koli.platform.windows import input_watch
        except Exception:
            self.skipTest('Windows input watcher not importable here')
        self.classify = input_watch.classify_key
        self.INJECTED = input_watch.LLKHF_INJECTED
        self.ALT = input_watch.LLKHF_ALTDOWN

    def test_letters_space_backspace_and_enter_are_typing(self):
        for vk in (0x41, 0x20, 0x08, 0x0D, 0x2E, 0x09, 0xBA):
            self.assertTrue(self.classify(vk, 0), hex(vk))

    def test_shortcuts_modifiers_and_navigation_are_not(self):
        self.assertFalse(self.classify(0x41, self.INJECTED), "Koli's own paste")
        self.assertFalse(self.classify(0x41, self.ALT), 'Alt+letter')
        self.assertFalse(self.classify(0x43, 0, ctrl_down=True), 'Ctrl+C')
        self.assertFalse(self.classify(0x44, 0, win_down=True), 'Win+D')
        for vk in (0x10, 0x11, 0x12, 0xA0, 0x5B, 0x25, 0x24, 0x70, 0x7B, 0x1B, 0x14, 0xAE):
            self.assertFalse(self.classify(vk, 0), hex(vk))


# ctranslate2 tears down per-thread CUDA state in a thread-exit callback that
# threw on the user's two-GPU machine and aborted the process (crash dumps of
# 2026-10-08). Every ctranslate2 call therefore runs on one persistent thread.
class EngineThreadTests(unittest.TestCase):
    def setUp(self):
        try:
            from koli.whisper_engine import _EngineThread
        except Exception:
            self.skipTest('whisper_engine not importable (faster_whisper absent)')
        self.engine_thread = _EngineThread(name='engine-under-test')

    def test_every_call_runs_on_the_same_long_lived_daemon_thread(self):
        import threading
        names = [self.engine_thread.run(lambda: threading.current_thread().name) for _ in range(3)]
        self.assertEqual(names, ['engine-under-test'] * 3)
        self.assertTrue(self.engine_thread._thread.daemon)
        self.assertTrue(self.engine_thread._thread.is_alive(), 'it must outlive the calls')

    def test_results_and_errors_come_back_to_the_caller(self):
        self.assertEqual(self.engine_thread.run(lambda a, b=1: a + b, 2, b=3), 5)
        with self.assertRaises(ValueError):
            self.engine_thread.run(lambda: (_ for _ in ()).throw(ValueError('boom')))
        self.assertEqual(self.engine_thread.run(lambda: 'still fine'), 'still fine')

    def test_nested_calls_from_the_engine_thread_run_inline(self):
        inner = self.engine_thread.run(lambda: self.engine_thread.run(lambda: self.engine_thread.is_engine_thread()))
        self.assertTrue(inner)

    def test_transcription_decodes_on_the_engine_thread(self):
        import threading
        import unittest.mock as mock
        import numpy as np
        from koli import whisper_engine
        seen = []

        class Segment:   # what faster-whisper yields
            text = 'hello'
            start, end = 0.0, 1.0
            no_speech_prob, avg_logprob, compression_ratio = 0.02, -0.2, 1.1

        class Model:
            def transcribe(self, audio, **kwargs):
                seen.append(threading.current_thread().name)
                def segments():
                    seen.append(threading.current_thread().name)
                    yield Segment()
                return segments(), mock.Mock(language='en', language_probability=0.9)

        engine = whisper_engine.WhisperEngine.__new__(whisper_engine.WhisperEngine)
        engine.model = Model()
        engine.vad_manager = None
        engine.logger = __import__('logging').getLogger('test')
        engine.beam_size, engine.language, engine.task = 1, 'en', 'transcribe'
        engine.initial_prompt = engine.hotwords = None
        engine.model_key, engine.device, engine.compute_type = 'tiny', 'cpu', 'int8'
        engine.log_transcriptions = False
        with mock.patch('builtins.print'):
            text = engine.transcribe_audio(np.zeros(16000, dtype=np.float32))
        self.assertEqual(text, 'hello')
        self.assertEqual(seen, ['whisper-engine', 'whisper-engine'], 'both the call and the decode')


class AutoDictationShapeTests(unittest.TestCase):
    def test_rule_key_is_accepted(self):
        from koli.services.rules import RULE_KEYS
        self.assertIn('auto_dictation', RULE_KEYS)

    def test_platform_stub_mirrors_the_api(self):
        import importlib
        for name in ('koli.platform.macos.input_watch', 'koli.platform.windows.input_watch'):
            try:
                mod = importlib.import_module(name)
            except Exception:
                continue
            for fn in ('start', 'stop', 'is_running', 'classify_key'):
                self.assertTrue(callable(getattr(mod, fn, None)), f'{name}.{fn}')

    # The user's log: the configured mic failed to open, the recorder fell back
    # to a 48 kHz default device but kept the 16 kHz block size, and the
    # silence detector never saw a proper frame again. Hotkey recordings hid
    # it; auto-dictation depends on that detector to end a recording.
    def test_falling_back_to_the_default_device_recomputes_the_vad_block_size(self):
        import unittest.mock as mock
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec.logger = __import__('logging').getLogger('test')
        rec.device = 24
        rec.device_hostapi = 'Windows WDM-KS'
        rec.device_native_rate = 48000
        rec._configure_for_device_rate()
        self.assertEqual((rec._recording_rate, rec._vad_blocksize), (16000, 256))

        def default_is_wasapi(info):
            rec.device_hostapi = 'Windows WASAPI'
            rec.device_native_rate = 48000
        with mock.patch.object(rec, '_resolve_hostapi', side_effect=default_is_wasapi):
            rec._fall_back_to_default_device()
        self.assertIsNone(rec.device)
        self.assertEqual((rec._recording_rate, rec._vad_blocksize), (48000, 768))
        self.assertTrue(rec._needs_resampling_cached)
        self.assertEqual(rec._preroll_max_chunks, int(AudioRecorder.PREROLL_SECONDS / (768 / 48000)))

    # Falling back from the microphone you chose is said out loud, naming both:
    # the default can be another microphone altogether (a headset, not the EMEET).
    # Falling back again from the default says nothing new.
    def test_falling_back_from_your_microphone_tells_you_which_one_records(self):
        import unittest.mock as mock
        from koli import audio_recorder
        rec = audio_recorder.AudioRecorder.__new__(audio_recorder.AudioRecorder)
        rec.logger = __import__('logging').getLogger('test')
        rec.device = 24
        names = {24: 'Microphone (EMEET Piko+)', None: 'Microphone (Logitech USB Headset)'}
        with mock.patch.object(rec, '_resolve_hostapi'), mock.patch.object(rec, '_configure_for_device_rate'), \
                mock.patch.object(rec, '_recording_rate', 16000, create=True), \
                mock.patch.object(rec, '_vad_blocksize', 256, create=True), \
                mock.patch.object(audio_recorder.AudioRecorder, '_device_name', side_effect=lambda d: names[d]), \
                mock.patch.object(audio_recorder, '_report_problem') as report:
            rec._fall_back_to_default_device()
            rec._fall_back_to_default_device()
        report.assert_called_once()
        self.assertIn('EMEET Piko+', report.call_args[0][0])
        self.assertIn('Logitech USB Headset', report.call_args[0][0])

    # Every thread that opens a stream needs the same per-thread setup on both
    # systems (COM on Windows, nothing on macOS).
    def test_audio_thread_setup_is_mirrored(self):
        import importlib
        for name in ('koli.platform.macos.app', 'koli.platform.windows.app'):
            try:
                mod = importlib.import_module(name)
            except Exception:
                continue
            with mod.audio_thread():
                pass

    # The voiceprint needs the microphone as it was: noise reduction and the
    # Whisper-mode boost make any voice (a TV) score like yours.
    def test_raw_for_gives_the_clip_before_noise_reduction_and_boost(self):
        import unittest.mock as mock
        import numpy as np
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec.logger = __import__('logging').getLogger('test')
        rec.sample_rate = 16000
        rec._needs_resampling_cached = False
        rec._noise_suppression_config = {'enabled': True, 'strength': 1.0}
        rec._whisper_mode_gain = 8.0
        rec._recent_clips = __import__('collections').deque(maxlen=4)
        chunk = np.full((16000, 1), 0.01, dtype=np.float32)
        # The pauses go from the raw clip too: in a room with a TV they are
        # the TV, and the voiceprint must not learn it.
        with mock.patch.object(rec, '_trim_long_pauses', side_effect=lambda a, decide_on=None: a[:len(a) // 2]), \
             mock.patch.object(rec, '_trim_trailing_silence', side_effect=lambda a, decide_on=None: a), \
             mock.patch('koli.noise_suppression.apply_noise_reduction', side_effect=lambda a, sr, s: a * 0.5):
            out = rec._build_audio_array([chunk])
        np.testing.assert_allclose(rec.raw_for(out), chunk[:8000])
        self.assertGreater(float(abs(out).max()), float(abs(rec.raw_for(out)).max()), 'Whisper still gets the boost')
        other = np.zeros(10, dtype=np.float32)
        self.assertIs(rec.raw_for(other), other, 'unknown audio is its own raw')

    # Whisper's piece times are on the trimmed clip, so the raw clip is cut at
    # exactly the same places, decided on the noise-reduced one (where the room
    # doesn't read as sound).
    def test_the_raw_clip_is_cut_where_the_processed_one_is(self):
        import unittest.mock as mock
        import numpy as np
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec.logger = __import__('logging').getLogger('test')
        rec.sample_rate = 16000
        rec._needs_resampling_cached = False
        rec._noise_suppression_config = {'enabled': True, 'strength': 0.75}
        rec._whisper_mode_gain = None
        rec._recent_clips = __import__('collections').deque(maxlen=4)
        sr = 16000
        talk, room = np.full(sr, 0.2, dtype=np.float32), np.full(sr * 4, 0.02, dtype=np.float32)
        raw = np.concatenate([talk, room, talk]).reshape(-1, 1)          # a TV hums through the pause

        def reduce(a, _sr, _s):                                           # noise reduction removes it
            out = a.copy()
            out[sr:sr * 5] = 0.0
            return out
        with mock.patch('koli.noise_suppression.apply_noise_reduction', side_effect=reduce):
            out = rec._build_audio_array([raw])
        kept_raw = rec.raw_for(out)
        self.assertLess(len(out), len(raw), 'the long pause was shortened')
        self.assertEqual(len(kept_raw), len(out), 'cut at the same places')
        np.testing.assert_allclose(kept_raw[-sr:], talk.reshape(-1, 1)[-sr:])

    # The Settings voice check listens through a tap on the open microphone:
    # no recording starts, and the clip is trimmed like a dictation's raw clip.
    def test_capture_raw_listens_through_a_tap(self):
        import unittest.mock as mock
        import numpy as np
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec.logger = __import__('logging').getLogger('test')
        rec._taps = []
        rec.is_recording = False

        def while_listening(seconds):
            self.assertEqual(seconds, 5)
            for tap in list(rec._taps):
                tap(np.full(800, 0.1, dtype=np.float32), 0.0)
                tap(np.full(800, 0.2, dtype=np.float32), 0.1)
        with mock.patch('koli.audio_recorder.time.sleep', side_effect=while_listening), \
             mock.patch.object(rec, '_trim_long_pauses', side_effect=lambda a: a), \
             mock.patch.object(rec, '_trim_trailing_silence', side_effect=lambda a: a[:1000]):
            clip = rec.capture_raw(5)
        self.assertEqual(len(clip), 1000)
        self.assertEqual(rec._taps, [], 'the tap is removed afterwards')
        self.assertFalse(rec.is_recording)

    def test_taking_recorded_audio_keeps_the_recording_going(self):
        import unittest.mock as mock
        import numpy as np
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec.logger = __import__('logging').getLogger('test')
        rec.is_recording = True
        rec.recording_start_time = 1.0
        rec._buffer_lock = __import__('threading').Lock()
        rec._buffer = __import__('collections').deque([np.ones((256, 1), dtype=np.float32)] * 4)
        rec._build_audio_array = lambda chunks: np.concatenate(chunks) if chunks else None
        audio = rec.take_recorded_audio()
        self.assertEqual(len(audio), 1024)
        self.assertTrue(rec.is_recording, 'the microphone stays open')
        self.assertEqual(len(rec._buffer), 0, 'the next phrase starts clean')
        self.assertGreater(rec.recording_start_time, 1.0, 'the length limit restarts per phrase')
        rec.is_recording = False
        self.assertIsNone(rec.take_recorded_audio())

    def test_recorder_restores_the_configured_timeout_for_hotkey_recordings(self):
        import unittest.mock as mock
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec.logger = __import__('logging').getLogger('test')
        rec.is_recording = False
        rec.vad_manager = mock.Mock(vad_silence_timeout_seconds=30.0)
        rec.continuous_vad = mock.Mock(silence_timeout_sec=30.0)
        rec.continuous_streaming = None
        rec._buffer = []
        rec._vad_blocksize = 256
        rec._recording_rate = 16000
        rec.start_recording(silence_timeout=5.0, phrase_pause=1.0)
        rec.continuous_vad.set_silence_timeout.assert_called_once_with(5.0)
        rec.continuous_vad.set_phrase_pause.assert_called_with(1.0)
        rec.is_recording = False
        rec.continuous_vad.silence_timeout_sec = 5.0
        rec.start_recording()
        rec.continuous_vad.set_silence_timeout.assert_called_with(30.0)


# ── Delayed auto-send ───────────────────────────────────────────────────────
# Auto-send can wait a few seconds after the paste so the user can read the
# text and stop the Enter. These drive DelayedSend with a fake clock, a fake
# foreground window and a fake keyboard watcher; nothing real is pressed.

class _SendClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _SendWatch:
    def __init__(self):
        self.on_typing = None
        self.stopped = []

    def start(self, on_focus, on_typing):
        self.on_typing = on_typing
        return 7

    def stop(self, token=None):
        self.stopped.append(token)


class DelayedSendTests(unittest.TestCase):
    def _sender(self, windows=None):
        from koli.delayed_send import DelayedSend
        self.clock = _SendClock()
        self.sent = []
        self.events = []
        self.watch = _SendWatch()
        self.windows = windows if windows is not None else [{'hwnd': 1, 'exe': 'slack.exe'}]
        return DelayedSend(send_enter=lambda: self.sent.append(self.clock.now) or True,
                           foreground=lambda: self.windows[0],
                           publish=lambda topic, payload: self.events.append((topic, dict(payload))),
                           typing_watch=self.watch, clock=self.clock, run_thread=False)

    def _states(self):
        return [p['state'] for topic, p in self.events if topic == 'auto_send']

    def test_presses_enter_when_the_wait_is_over(self):
        ds = self._sender()
        ds.schedule(5)
        self.clock.now = 4.9
        self.assertIsNone(ds.poll())
        self.assertEqual(self.sent, [])
        self.clock.now = 5.0
        self.assertEqual(ds.poll(), 'sent')
        self.assertEqual(self.sent, [5.0])
        self.assertFalse(ds.is_pending())
        self.assertEqual(self._states()[-1], 'sent')

    def test_counts_down_in_whole_seconds(self):
        ds = self._sender()
        ds.schedule(5)
        self.clock.now = 1.2
        ds.poll()
        counts = [p['remaining'] for topic, p in self.events if p['state'] == 'counting']
        self.assertEqual(counts[:2], [5, 4])
        self.assertEqual(self.events[0][1]['total'], 5)

    def test_cancel_stops_the_enter_and_says_why(self):
        ds = self._sender()
        ds.schedule(5)
        self.assertTrue(ds.cancel('Esc'))
        self.clock.now = 9
        self.assertIsNone(ds.poll())
        self.assertEqual(self.sent, [])
        last = self.events[-1][1]
        self.assertEqual((last['state'], last['reason']), ('cancelled', 'Esc'))
        self.assertFalse(ds.cancel('Esc'))   # nothing left to cancel

    # Enter must never land in a different window than the one with the text.
    def test_switching_windows_drops_the_send(self):
        ds = self._sender(windows=[{'hwnd': 1, 'exe': 'slack.exe'}])
        ds.schedule(5)
        self.windows[0] = {'hwnd': 2, 'exe': 'slack.exe'}
        self.clock.now = 1.0
        self.assertEqual(ds.poll(), 'not_sent')
        self.clock.now = 6.0
        ds.poll()
        self.assertEqual(self.sent, [])
        self.assertEqual(self._states()[-1], 'not_sent')

    def test_send_now_presses_enter_at_once(self):
        ds = self._sender()
        ds.schedule(5)
        self.clock.now = 1.0
        self.assertTrue(ds.send_now())
        self.assertEqual(self.sent, [1.0])
        self.clock.now = 6.0
        ds.poll()
        self.assertEqual(self.sent, [1.0])

    # Typing (fixing a word, or pressing Enter yourself) means "I'll send it".
    def test_typing_cancels_and_the_watch_is_released(self):
        ds = self._sender()
        ds.schedule(5)
        self.watch.on_typing('key 0x41')
        self.assertFalse(ds.is_pending())
        self.assertEqual(self.events[-1][1]['reason'], 'you started typing')
        self.assertEqual(self.watch.stopped, [7])
        self.clock.now = 6.0
        ds.poll()
        self.assertEqual(self.sent, [])

    def test_a_new_send_replaces_the_pending_one(self):
        ds = self._sender()
        ds.schedule(5)
        ds.schedule(3)
        self.clock.now = 3.0
        ds.poll()
        self.clock.now = 6.0
        ds.poll()
        self.assertEqual(self.sent, [3.0])


class InputWatchListenersTests(unittest.TestCase):
    # Auto-dictation and the delayed send both listen for typing; they share
    # one keyboard hook, and each stop removes only its own listener.
    def setUp(self):
        try:
            from koli.platform.windows import input_watch
        except Exception:
            self.skipTest('Windows input watcher not importable here')
        self.iw = input_watch

    def test_two_listeners_share_one_hook(self):
        import unittest.mock as mock
        created = []

        class FakeWatcher:
            def __init__(self, on_focus, on_typing, wants_focus):
                self.on_focus, self.on_typing, self.wants_focus = on_focus, on_typing, wants_focus
                self.stopped = False
                created.append(self)

            def start(self):
                return True

            def stop(self):
                self.stopped = True

        typed_a, typed_b, focused = [], [], []
        with mock.patch.object(self.iw, '_Watcher', FakeWatcher), \
                mock.patch.object(self.iw, '_watcher', None), \
                mock.patch.object(self.iw, '_listeners', {}):
            first = self.iw.start(None, lambda key='': typed_a.append(key))
            self.assertFalse(created[0].wants_focus())
            second = self.iw.start(focused.append, lambda key='': typed_b.append(key))
            self.assertTrue(first and second and first != second)
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].wants_focus())
            created[0].on_typing('key 0x41')
            created[0].on_focus({'editable': True})
            self.assertEqual((typed_a, typed_b, focused), (['key 0x41'], ['key 0x41'], [{'editable': True}]))
            self.iw.stop(first)
            self.assertFalse(created[0].stopped)
            self.assertTrue(self.iw.is_running())
            self.iw.stop(second)
            self.assertTrue(created[0].stopped)
            self.assertFalse(self.iw.is_running())


class DelayedSendDeliveryTests(unittest.TestCase):
    def _clipboard(self, delay):
        import unittest.mock as mock
        from koli.clipboard_manager import ClipboardManager
        cb = ClipboardManager.__new__(ClipboardManager)
        cb.logger = __import__('logging').getLogger('test')
        cb.auto_paste = True
        cb.delivery_method = 'paste'
        cb.auto_send_delay = delay
        cb.delayed_send = mock.Mock()
        cb.execute_delivery = mock.Mock(return_value=True)
        cb.send_enter_key = mock.Mock(return_value=True)
        return cb

    def test_no_wait_presses_enter_right_after_the_paste(self):
        cb = self._clipboard(0)
        self.assertTrue(cb.deliver_transcription('hello', use_auto_enter=True, allow_delay=True))
        cb.send_enter_key.assert_called_once()
        cb.delayed_send.schedule.assert_not_called()

    def test_automatic_auto_send_waits_before_enter(self):
        cb = self._clipboard(5)
        self.assertTrue(cb.deliver_transcription('hello', use_auto_enter=True, allow_delay=True))
        cb.send_enter_key.assert_not_called()
        cb.delayed_send.schedule.assert_called_once_with(5)

    # The send key is a deliberate "send it": no wait, whatever the setting.
    def test_the_send_key_sends_right_away_even_with_a_wait_set(self):
        cb = self._clipboard(5)
        self.assertTrue(cb.deliver_transcription('hello', use_auto_enter=True))
        cb.send_enter_key.assert_called_once()
        cb.delayed_send.schedule.assert_not_called()

    def test_without_auto_send_nothing_is_scheduled(self):
        cb = self._clipboard(5)
        cb.deliver_transcription('hello', use_auto_enter=False)
        cb.send_enter_key.assert_not_called()
        cb.delayed_send.schedule.assert_not_called()

    # An app rule's own wait replaces the main one; 0 sends at once.
    def test_an_app_rules_wait_overrides_the_main_setting(self):
        cb = self._clipboard(5)
        cb.deliver_transcription('hello', use_auto_enter=True, allow_delay=True, send_delay=2)
        cb.delayed_send.schedule.assert_called_once_with(2)
        cb = self._clipboard(5)
        cb.deliver_transcription('hello', use_auto_enter=True, allow_delay=True, send_delay=0)
        cb.send_enter_key.assert_called_once()
        cb.delayed_send.schedule.assert_not_called()
        cb = self._clipboard(0)
        cb.deliver_transcription('hello', use_auto_enter=True, allow_delay=True, send_delay=3)
        cb.delayed_send.schedule.assert_called_once_with(3)

    # Auto waits follow the length of what was pasted.
    def test_an_auto_wait_follows_the_message_length(self):
        cb = self._clipboard('auto_medium')
        cb.deliver_transcription('ok', use_auto_enter=True, allow_delay=True)
        cb.delayed_send.schedule.assert_called_once_with(1.0)
        cb = self._clipboard(0)
        cb.deliver_transcription(' '.join(['word'] * 15), use_auto_enter=True, allow_delay=True,
                                 send_delay='auto_slow')
        cb.delayed_send.schedule.assert_called_once_with(5.0)


class AutoSendWaitTests(unittest.TestCase):
    # The agreed table: 1 s for a word, then by each level's pace, up to its cap.
    def test_each_level_scales_with_words_up_to_its_cap(self):
        from koli.delayed_send import wait_seconds
        table = {
            'auto_fast': {1: 1, 6: 1, 15: 3, 40: 6},
            'auto_medium': {1: 1, 6: 2, 15: 4, 40: 10},
            'auto_slow': {1: 1, 6: 2, 15: 5, 40: 14, 100: 15},
        }
        for level, rows in table.items():
            for words, seconds in rows.items():
                self.assertEqual(wait_seconds(level, words), seconds, f'{level}, {words} words')
        self.assertEqual(wait_seconds('auto_medium', 0), 1, 'never less than a second')

    def test_fixed_seconds_pass_through(self):
        from koli.delayed_send import wait_seconds
        self.assertEqual(wait_seconds(5, 40), 5)
        self.assertEqual(wait_seconds(0, 40), 0)
        self.assertEqual(wait_seconds(None), 0)
        self.assertEqual(wait_seconds('nonsense'), 0, 'an unknown value sends at once')

    def test_words_are_counted_by_spaces_in_any_language(self):
        from koli.delayed_send import count_words
        self.assertEqual(count_words('  on my way,  see you soon '), 6)
        self.assertEqual(count_words('אני בדרך הביתה'), 3)
        self.assertEqual(count_words(''), 0)

    def test_the_setting_offers_the_auto_levels(self):
        from koli.settings import schema
        setting = schema.get('clipboard.auto_send_delay')
        self.assertEqual(schema.coerce(setting, 'auto_fast'), 'auto_fast')
        self.assertEqual(schema.coerce(setting, '10'), 10)
        with self.assertRaises(ValueError):
            schema.coerce(setting, 'auto_turbo')


class ProblemReportTests(unittest.TestCase):
    # Something that went wrong and that you must see is kept until you
    # dismiss it (a window opened later still shows it) and published for the
    # bubble and the app.
    def setUp(self):
        from koli.ui import events
        for p in events.open_problems():
            events.dismiss_problem(p['id'])

    def test_a_problem_is_published_kept_and_dismissed(self):
        import unittest.mock as mock
        from koli.ui import events
        with mock.patch.object(events, 'publish') as publish:
            problem = events.report_problem('The Hebrew model could not load')
        publish.assert_called_once_with('problem', problem)
        self.assertEqual([p['message'] for p in events.open_problems()], ['The Hebrew model could not load'])
        events.dismiss_problem(problem['id'])
        self.assertEqual(events.open_problems(), [])

    def test_only_the_latest_are_kept(self):
        from koli.ui import events
        for i in range(15):
            events.report_problem(f'problem {i}')
        kept = events.open_problems()
        self.assertEqual(len(kept), events.MAX_PROBLEMS)
        self.assertEqual(kept[-1]['message'], 'problem 14')

    def test_a_failed_background_job_is_a_problem(self):
        import time
        from koli.ui import events
        from koli.ui.tasks import TaskManager

        def job(reporter):
            raise RuntimeError('CUDA failed with error out of memory')
        TaskManager().run('Setting up the second model', job)
        for _ in range(50):
            if events.open_problems():
                break
            time.sleep(0.02)
        message = events.open_problems()[-1]['message']
        self.assertIn('Setting up the second model', message)
        self.assertIn('out of memory', message)

    # The main model loaded but the second didn't: dictation works, so it is
    # not a failed job, but you must still hear about it.
    def test_a_second_model_that_fails_to_load_is_a_problem(self):
        import unittest.mock as mock
        from koli import whisper_engine
        from koli.ui import events
        engine = whisper_engine.WhisperEngine.__new__(whisper_engine.WhisperEngine)
        engine.logger = __import__('logging').getLogger('test')
        engine.language_model_lang, engine.language_model_key = 'he', 'hebrew-large-v3-turbo'
        engine.device, engine.device_index, engine.compute_type, engine.registry = 'cuda', 1, 'float16', None
        with mock.patch.object(whisper_engine, 'WhisperModel', side_effect=RuntimeError('out of memory')), \
                mock.patch('builtins.print'):
            engine._load_language_model()
        self.assertIsNone(engine.language_model)
        self.assertIn('out of memory', events.open_problems()[-1]['message'])

    def test_a_failed_transcription_is_a_problem(self):
        import unittest.mock as mock
        import numpy as np
        from koli import whisper_engine
        from koli.ui import events
        engine = whisper_engine.WhisperEngine.__new__(whisper_engine.WhisperEngine)
        engine.logger = __import__('logging').getLogger('test')
        engine.model = mock.Mock()
        engine.model.detect_language.side_effect = RuntimeError('CUDA error')
        engine.model.transcribe.side_effect = RuntimeError('CUDA error')
        engine.language_model = None
        engine.vad_manager = None
        engine.beam_size, engine.language, engine.task = 5, None, 'transcribe'
        engine.initial_prompt = engine.hotwords = None
        engine.model_key = 'large-v3-turbo'
        with mock.patch('builtins.print'):
            self.assertIsNone(engine.transcribe_audio(np.zeros(16000, dtype=np.float32)))
        self.assertIn('CUDA error', events.open_problems()[-1]['message'])


class LanguageModelRoutingTests(unittest.TestCase):
    # A second model for one language (Hebrew → ivrit-ai): the main model
    # detects the language, that language goes to the second model, anything
    # else is transcribed by the main model in the language it detected.
    # `detected` is (language, probability[, every language's score]).
    def _engine(self, detected=('en', 0.98), forced=None, second=True, allowed=None):
        import unittest.mock as mock
        from koli import whisper_engine

        class Segment:
            def __init__(self, text):
                self.text, self.start, self.end = text, 0.0, 1.0
                self.no_speech_prob, self.avg_logprob, self.compression_ratio = 0.02, -0.2, 1.1

        class Model:
            def __init__(self, name):
                self.name, self.calls = name, []

            def detect_language(self, audio, **kwargs):
                self.calls.append(('detect', {}))
                return detected[0], detected[1], (detected[2] if len(detected) > 2 else [])

            def transcribe(self, audio, **kwargs):
                self.calls.append(('transcribe', kwargs))
                return iter([Segment(f'{self.name} text')]), mock.Mock(
                    language=kwargs.get('language') or 'en', language_probability=0.99)

        engine = whisper_engine.WhisperEngine.__new__(whisper_engine.WhisperEngine)
        engine.model, engine.model_key = Model('main'), 'large-v3-turbo'
        engine.language_model = Model('hebrew') if second else None
        engine.language_model_lang = 'he' if second else None
        engine.language_model_key = 'hebrew-large-v3-turbo' if second else None
        engine.allowed_languages = allowed or []
        engine.vad_manager = None
        engine.logger = __import__('logging').getLogger('test')
        engine.beam_size, engine.language, engine.task = 5, forced, 'transcribe'
        engine.initial_prompt, engine.hotwords = 'Casual chat message.', 'Koli'
        engine.device, engine.compute_type = 'cuda', 'float16'
        engine.log_transcriptions = False
        return engine

    def _run(self, engine):
        import unittest.mock as mock
        import numpy as np
        with mock.patch('builtins.print'):
            return engine.transcribe_audio(np.zeros(16000, dtype=np.float32))

    def test_hebrew_goes_to_the_second_model_without_the_english_prompt(self):
        engine = self._engine(detected=('he', 0.97))
        self.assertEqual(self._run(engine), 'hebrew text')
        kind, kwargs = engine.language_model.calls[-1]
        self.assertEqual((kind, kwargs['language']), ('transcribe', 'he'))
        self.assertNotIn('initial_prompt', kwargs)
        self.assertEqual(kwargs.get('hotwords'), 'Koli', 'your words still help')
        self.assertEqual([c[0] for c in engine.model.calls], ['detect'])
        self.assertEqual(engine.last_info['model'], 'hebrew-large-v3-turbo')

    def test_other_languages_stay_on_the_main_model_detected_once(self):
        engine = self._engine(detected=('en', 0.98))
        self.assertEqual(self._run(engine), 'main text')
        kinds = [c[0] for c in engine.model.calls]
        self.assertEqual(kinds, ['detect', 'transcribe'])
        kwargs = engine.model.calls[-1][1]
        self.assertEqual((kwargs['language'], kwargs['initial_prompt']), ('en', 'Casual chat message.'))
        self.assertEqual(engine.language_model.calls, [])
        self.assertEqual(engine.last_info['model'], 'large-v3-turbo')

    # An unsure guess is not passed on: the main model decides for itself.
    def test_an_unsure_detection_lets_the_main_model_decide(self):
        engine = self._engine(detected=('ar', 0.3))
        self._run(engine)
        self.assertIsNone(engine.model.calls[-1][1]['language'])

    # A rule or profile that sets the language skips detection.
    def test_a_forced_language_picks_the_model_directly(self):
        engine = self._engine(forced='he')
        self.assertEqual(self._run(engine), 'hebrew text')
        self.assertEqual(engine.model.calls, [])
        engine = self._engine(forced='en')
        self.assertEqual(self._run(engine), 'main text')
        self.assertEqual([c[0] for c in engine.model.calls], ['transcribe'])

    def test_without_a_second_model_nothing_changes(self):
        engine = self._engine(second=False)
        self.assertEqual(self._run(engine), 'main text')
        self.assertEqual([c[0] for c in engine.model.calls], ['transcribe'])
        self.assertIsNone(engine.model.calls[-1][1]['language'])

    # ── Languages I speak ──
    # A 3-second Hebrew clip that scores Icelandic highest still comes out as
    # Hebrew when only English and Hebrew are allowed.
    _ICELANDIC_FIRST = ('is', 0.55, [('is', 0.55), ('he', 0.30), ('en', 0.05), ('ar', 0.04)])

    def test_the_best_listed_language_wins_over_an_unlisted_one(self):
        engine = self._engine(detected=self._ICELANDIC_FIRST, second=False, allowed=['en', 'he'])
        self._run(engine)
        self.assertEqual([c[0] for c in engine.model.calls], ['detect', 'transcribe'])
        self.assertEqual(engine.model.calls[-1][1]['language'], 'he')
        self.assertAlmostEqual(engine.last_info['language_probability'], 0.857, places=3)
        self.assertAlmostEqual(engine.last_info['language_raw_probability'], 0.30, places=3,
                               msg="Whisper's own score for Hebrew, for the guards")
        self.assertEqual(engine.last_info['audio_seconds'], 1.0)

    # The second model only gets speech that is clearly its language: told
    # "Hebrew", it translates whatever else it hears. On 2026-10-12 a 47 s
    # English dictation scored Hebrew 0.45 and half of it came out translated.
    # Real Hebrew dictations scored 0.8-1.0.
    def test_a_clearly_hebrew_clip_reaches_the_second_model(self):
        engine = self._engine(detected=('he', 0.86, [('he', 0.86), ('en', 0.1)]), allowed=['en', 'he'])
        self.assertEqual(self._run(engine), 'hebrew text')
        self.assertNotIn('initial_prompt', engine.language_model.calls[-1][1])

    def test_an_unsure_hebrew_guess_stays_on_the_main_model_undecided(self):
        for detected in (('he', 0.45, [('he', 0.45), ('en', 0.25)]), self._ICELANDIC_FIRST):
            engine = self._engine(detected=detected, allowed=['en', 'he'])
            self.assertEqual(self._run(engine), 'main text', detected[:2])
            self.assertIsNone(engine.model.calls[-1][1]['language'], 'not told Hebrew: it would translate')
            self.assertEqual(engine.language_model.calls, [])

    # Meeting mode asks for no prompt: the app's style hint stays out.
    def test_use_prompt_false_leaves_the_prompt_out(self):
        import unittest.mock as mock
        import numpy as np
        engine = self._engine(second=False)
        with mock.patch('builtins.print'):
            engine.transcribe_audio(np.zeros(16000, dtype=np.float32), language='auto', use_prompt=False)
        self.assertNotIn('initial_prompt', engine.model.calls[-1][1])
        self.assertEqual(engine.model.calls[-1][1].get('hotwords'), 'Koli', 'your words still help')

    # A wake-word check: beam 1, always transcribe (even under Translate),
    # nothing printed, and the last dictation's info left alone.
    def test_a_quiet_check_is_fast_silent_and_keeps_last_info(self):
        import unittest.mock as mock
        import numpy as np
        engine = self._engine(second=False)
        engine.task = 'translate'
        engine.log_transcriptions = True
        engine.last_info = {'model': 'from the dictation'}
        with mock.patch('builtins.print') as printed, self.assertNoLogs('test', level='INFO'):
            text = engine.transcribe_audio(np.zeros(16000, dtype=np.float32), use_prompt=False,
                                           beam_size=1, task='transcribe', quiet=True)
        self.assertEqual(text, 'main text')
        kwargs = engine.model.calls[-1][1]
        self.assertEqual((kwargs['beam_size'], kwargs['task']), (1, 'transcribe'))
        printed.assert_not_called()
        self.assertEqual(engine.last_info, {'model': 'from the dictation'})

    # The wake word's spelling replaces your favored words for one call;
    # '' sends none; leaving it out keeps yours.
    def test_hotwords_can_be_replaced_for_one_call(self):
        import unittest.mock as mock
        import numpy as np
        engine = self._engine(second=False)
        with mock.patch('builtins.print'):
            engine.transcribe_audio(np.zeros(16000, dtype=np.float32), hotwords='Hey Avia', quiet=True)
            self.assertEqual(engine.model.calls[-1][1].get('hotwords'), 'Hey Avia')
            engine.transcribe_audio(np.zeros(16000, dtype=np.float32), hotwords='', quiet=True)
            self.assertNotIn('hotwords', engine.model.calls[-1][1])
            engine.transcribe_audio(np.zeros(16000, dtype=np.float32), quiet=True)
            self.assertEqual(engine.model.calls[-1][1].get('hotwords'), 'Koli')

    # A list of one is a fixed language: no detection at all.
    def test_a_one_language_list_skips_detection(self):
        engine = self._engine(second=False, allowed=['he'])
        self._run(engine)
        self.assertEqual([c[0] for c in engine.model.calls], ['transcribe'])
        self.assertEqual(engine.model.calls[-1][1]['language'], 'he')

    # A language set in settings, a rule or a profile beats the list.
    def test_a_forced_language_ignores_the_list(self):
        engine = self._engine(forced='fr', second=False, allowed=['en', 'he'])
        self._run(engine)
        self.assertEqual([c[0] for c in engine.model.calls], ['transcribe'])
        self.assertEqual(engine.model.calls[-1][1]['language'], 'fr')

    # English-only models can't detect a language; they get English.
    def test_an_english_only_model_skips_detection(self):
        import unittest.mock as mock
        engine = self._engine(second=False, allowed=['en', 'he'])
        engine.model.model = mock.Mock(is_multilingual=False)
        self._run(engine)
        self.assertEqual([c[0] for c in engine.model.calls], ['transcribe'])
        self.assertEqual(engine.model.calls[-1][1]['language'], 'en')

    # Off, the main model itself, or an unknown model: no second model.
    def test_which_second_model_is_loaded(self):
        from koli.main import language_model_choice
        from koli.model_registry import ModelRegistry
        registry = ModelRegistry(whisper_models_config={'large-v3-turbo': {'label': 'T'},
                                                        'hebrew-large-v3-turbo': {'source': 'ivrit-ai/x', 'label': 'H'}})
        cfg = {'model': 'large-v3-turbo', 'language_model': {'language': 'he', 'model': 'hebrew-large-v3-turbo'}}
        self.assertEqual(language_model_choice(cfg, registry), ('he', 'hebrew-large-v3-turbo'))
        self.assertIsNone(language_model_choice({**cfg, 'language_model': {'language': '', 'model': 'x'}}, registry))
        self.assertIsNone(language_model_choice({**cfg, 'model': 'hebrew-large-v3-turbo'}, registry))
        self.assertIsNone(language_model_choice({**cfg, 'language_model': {'language': 'he', 'model': 'nope'}}, registry))

    # A swapped-out engine gives its graphics memory back at once: two turbo
    # models must fit on a 12 GB card after a few model switches.
    def test_unloading_frees_both_models(self):
        import unittest.mock as mock
        engine = self._engine()
        main, second = mock.Mock(), mock.Mock()
        engine.model, engine.language_model = main, second
        engine.unload()
        main.model.unload_model.assert_called_once()
        second.model.unload_model.assert_called_once()
        self.assertIsNone(engine.model)
        self.assertIsNone(engine.language_model)

    def test_a_replaced_engine_is_unloaded(self):
        import threading
        import unittest.mock as mock
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.logger = __import__('logging').getLogger('test')
        sm._state_lock = threading.Lock()
        old, new = mock.Mock(model_key='large'), mock.Mock(model_key='large-v3-turbo')
        sm.whisper_engine = old
        sm.replace_whisper_engine(new)
        self.assertIs(sm.whisper_engine, new)
        old.unload.assert_called_once()

    # main() must not keep the first engine alive for the whole session.
    def test_main_does_not_hold_the_startup_engine(self):
        source = (ROOT / 'src' / 'koli' / 'main.py').read_text(encoding='utf-8')
        body = source[source.index('state_manager = StateManager('):]
        self.assertIn('del whisper_engine', body[:body.index('state_manager.')])

    def test_the_ivrit_ai_models_are_in_the_list(self):
        from ruamel.yaml import YAML
        models = YAML().load((ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8'))['whisper']['models']
        self.assertEqual(models['hebrew-large-v3-turbo']['source'], 'ivrit-ai/whisper-large-v3-turbo-ct2')
        self.assertEqual(models['hebrew-large-v3']['source'], 'ivrit-ai/whisper-large-v3-ct2')


class VoiceLevelTests(unittest.TestCase):
    # "Only my voice", step 1: Koli learns how loud you are from your hotkey
    # dictations and leaves out speech much quieter than that (a TV across
    # the room).
    RATE = 16000

    def _tone(self, amplitude, seconds):
        import numpy as np
        t = np.arange(int(self.RATE * seconds)) / self.RATE
        return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    def test_speech_level_is_the_loud_part_in_dbfs(self):
        import numpy as np
        from koli.voice_filter import speech_level_db
        level = speech_level_db(np.concatenate([self._tone(0.1, 1.0), np.zeros(self.RATE, dtype=np.float32)]))
        self.assertAlmostEqual(level, -23.0, delta=0.5)      # a 0.1 sine is about -23 dBFS
        self.assertIsNone(speech_level_db(np.zeros(self.RATE, dtype=np.float32)))
        self.assertIsNone(speech_level_db(self._tone(0.1, 0.05)))   # too short to judge

    def test_a_boost_is_taken_back_out(self):
        from koli.voice_filter import speech_level_db
        plain = speech_level_db(self._tone(0.05, 1.0))
        boosted = speech_level_db(self._tone(0.2, 1.0), gain=4.0)
        self.assertAlmostEqual(plain, boosted, delta=0.2)

    def test_my_level_is_learned_per_microphone_and_kept(self):
        import tempfile
        from koli.voice_filter import VoiceLevels
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'voice_levels.json'
        levels = VoiceLevels(path)
        levels.learn('Headset', -20.0)
        levels.learn('Headset', -22.0)
        self.assertIsNone(levels.my_level('Headset'), 'too few dictations to know yet')
        levels.learn('Headset', -30.0)
        self.assertEqual(levels.my_level('Headset'), -22.0)   # the middle one
        self.assertIsNone(levels.my_level('Laptop mic'))
        for _ in range(40):
            levels.learn('Headset', -18.0)
        self.assertEqual(VoiceLevels(path).my_level('Headset'), -18.0, 'kept on disk, recent ones count')

    # Levels learned before 2026-10-12 were measured after noise reduction,
    # while the meter and the checks now use the raw microphone: they are
    # forgotten once and learned again from the next 3 hotkey dictations.
    def test_levels_from_processed_audio_are_forgotten(self):
        import json
        import tempfile
        from koli.voice_filter import VoiceLevels
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'voice_levels.json'
        path.write_text(json.dumps({'Microphone (EMEET Piko+)': [-21.0, -20.0, -22.0]}), encoding='utf-8')
        levels = VoiceLevels(path)
        self.assertIsNone(levels.my_level('Microphone (EMEET Piko+)'))
        for level in (-21.0, -20.0, -22.0):
            levels.learn('Microphone (EMEET Piko+)', level)
        self.assertEqual(VoiceLevels(path).my_level('Microphone (EMEET Piko+)'), -21.0)

    def test_pieces_much_quieter_than_me_are_left_out(self):
        import numpy as np
        from koli.voice_filter import keep_my_voice
        audio = np.concatenate([self._tone(0.1, 1.0), self._tone(0.01, 1.0)])   # me, then a TV 20 dB lower
        pieces = [{'text': ' Why you stop doing that?', 'start': 0.0, 'end': 1.0},
                  {'text': " Do you know what I'm saying?", 'start': 1.0, 'end': 2.0}]
        text, why = keep_my_voice('...', audio, pieces, my_level_db=-23.0, margin_db=12)
        self.assertEqual(text, 'Why you stop doing that?')
        self.assertIn('quieter', why)
        text, why = keep_my_voice('...', audio, pieces, my_level_db=-23.0, margin_db=25)
        self.assertEqual(why, '', 'within the margin both stay')

    # whisper.cpp gives no piece times: the clip is judged as a whole.
    def test_without_piece_times_the_whole_clip_is_judged(self):
        from koli.voice_filter import keep_my_voice
        self.assertEqual(keep_my_voice('hi', self._tone(0.01, 1.0), None, -23.0, 12)[0], '')
        self.assertEqual(keep_my_voice('hi', self._tone(0.1, 1.0), [], -23.0, 12)[0], 'hi')


class ConfidentSpeechTests(unittest.TestCase):
    # Whisper's own numbers for a clip nobody spoke into: unsure of the
    # language, pieces it rates as unlikely speech. Auto-dictation keeps only
    # what it is sure of.
    def _seg(self, text, no_speech=0.05, logprob=-0.3, ratio=1.3):
        return {'text': text, 'no_speech_prob': no_speech, 'avg_logprob': logprob, 'compression_ratio': ratio}

    def test_an_unsure_language_guess_drops_the_clip(self):
        from koli.dictation_cleanup import confident_speech
        info = {'language_probability': 0.48, 'segments': [self._seg(" Do you know what I'm saying?")]}
        text, reason = confident_speech("Do you know what I'm saying?", info, language_forced=False)
        self.assertEqual(text, '')
        self.assertIn('0.48', reason)

    def test_a_forced_language_skips_the_language_check(self):
        from koli.dictation_cleanup import confident_speech
        info = {'language_probability': 0.48, 'segments': [self._seg(' shalom')]}
        self.assertEqual(confident_speech('shalom', info, language_forced=True)[0], 'shalom')

    def test_unsure_pieces_are_dropped_and_sure_ones_kept(self):
        from koli.dictation_cleanup import confident_speech
        info = {'language_probability': 0.91, 'segments': [
            self._seg(' Why you stop doing that?'),
            self._seg(" Yes, I'm sick, I'm sick.", ratio=2.6),          # repetition
            self._seg(' Thank you.', no_speech=0.8),                       # not speech
            self._seg(' Mm hmm.', logprob=-1.4)]}                          # low confidence
        text, reason = confident_speech('...', info, language_forced=False)
        self.assertEqual(text, 'Why you stop doing that?')
        self.assertIn('3', reason)

    def test_nothing_sure_left_means_nothing(self):
        from koli.dictation_cleanup import confident_speech
        info = {'language_probability': 0.9, 'segments': [self._seg(' Thank you.', no_speech=0.9)]}
        self.assertEqual(confident_speech('Thank you.', info, language_forced=False)[0], '')

    # With "Languages I speak" the language's share of the list read 1.00 while
    # Whisper was only 0.36 sure, and made-up text was typed. The raw score counts.
    def test_the_raw_language_score_counts_not_the_share_of_the_list(self):
        from koli.dictation_cleanup import confident_speech
        info = {'language_probability': 1.0, 'language_raw_probability': 0.36,
                'segments': [self._seg(' Thank you for watching.')]}
        text, reason = confident_speech('Thank you for watching.', info, language_forced=False)
        self.assertEqual(text, '')
        self.assertIn('0.36', reason)

    # A 0.7 s scrap at the end of a session came out "Thank you." (-0.85).
    def test_a_short_clip_must_be_clearly_sure(self):
        from koli.dictation_cleanup import confident_speech
        made_up = {'language_raw_probability': 0.88, 'audio_seconds': 0.7,
                   'segments': [self._seg(' Thank you.', logprob=-0.85)]}
        self.assertEqual(confident_speech('Thank you.', made_up, False)[0], '')
        real = {'language_raw_probability': 0.95, 'audio_seconds': 1.4, 'segments': [self._seg(' Yes, send it.', logprob=-0.1)]}
        self.assertEqual(confident_speech('Yes, send it.', real, False)[0], 'Yes, send it.')
        longer = {'language_raw_probability': 0.9, 'audio_seconds': 4.0, 'segments': [self._seg(' Fine.', logprob=-0.85)]}
        self.assertEqual(confident_speech('Fine.', longer, False)[0], 'Fine.', 'only short clips need the closer bar')

    # Only my voice, step 2: auto-dictation types only what sounds like you on
    # this microphone; unknown (None) or switched off never blocks.
    def test_auto_dictation_needs_your_voiceprint_once_it_is_known(self):
        import types
        import numpy as np
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.logger = __import__('logging').getLogger('test')
        sm.config_manager = types.SimpleNamespace(config={'audio': {'voice_filter': {'voiceprint': True}}})
        sm.audio_recorder = types.SimpleNamespace(describe=lambda: {'name': 'Microphone (EMEET Piko+)'})
        answers = {'sim': 0.15}
        sm.meetings = types.SimpleNamespace(my_voice_similarity=lambda audio, mic: answers['sim'])
        audio = np.zeros(16000, dtype=np.float32)
        self.assertFalse(sm._sounds_like_me(audio), 'the TV, not you')
        answers['sim'] = 0.72
        self.assertTrue(sm._sounds_like_me(audio))
        answers['sim'] = None
        self.assertTrue(sm._sounds_like_me(audio), 'not learned yet: never blocks')
        answers['sim'] = 0.15
        sm.config_manager.config['audio']['voice_filter']['voiceprint'] = False
        self.assertTrue(sm._sounds_like_me(audio), 'switched off')

    # whisper.cpp and older info carry no per-piece numbers: only the language check.
    def test_without_piece_numbers_the_text_stands(self):
        from koli.dictation_cleanup import confident_speech
        self.assertEqual(confident_speech('hello', {'language_probability': 0.9}, False), ('hello', ''))
        self.assertEqual(confident_speech('hello', {}, False), ('hello', ''))


class PromptEchoTests(unittest.TestCase):
    PROMPT = 'Casual chat message. Conversational tone.'

    def test_words_all_taken_from_the_prompt_are_an_echo(self):
        from koli.dictation_cleanup import is_prompt_echo
        self.assertTrue(is_prompt_echo('Conversational chat message.', self.PROMPT))
        self.assertTrue(is_prompt_echo('casual chat message', self.PROMPT))
        self.assertTrue(is_prompt_echo('Conversational tone', self.PROMPT))

    def test_real_speech_is_not(self):
        from koli.dictation_cleanup import is_prompt_echo
        self.assertFalse(is_prompt_echo('See you in the chat tomorrow', self.PROMPT))
        self.assertFalse(is_prompt_echo('Our chat about the message went well', self.PROMPT))
        self.assertFalse(is_prompt_echo('', self.PROMPT))
        self.assertFalse(is_prompt_echo('chat message', ''))
        self.assertFalse(is_prompt_echo('chat message', None))


class AutoSendPlanTests(unittest.TestCase):
    # (auto_enter, may_wait, wait): only an app rule's automatic auto-send may
    # wait; `wait` is the rule's own seconds, None for the main setting.
    def test_only_the_rules_automatic_send_may_wait(self):
        from koli.state_manager import StateManager
        plan = StateManager._auto_send_plan
        self.assertEqual(plan(False, None), (False, False, None))
        self.assertEqual(plan(True, None), (True, False, None))                       # send key
        self.assertEqual(plan(False, {'auto_send': True}), (True, True, None))        # automatic
        self.assertEqual(plan(True, {'auto_send': True}), (True, False, None))        # send key wins
        self.assertEqual(plan(True, {'auto_send': False}), (False, False, None))      # rule says never
        self.assertEqual(plan(False, {'auto_paste': True}), (False, False, None))

    def test_a_rule_can_set_its_own_wait(self):
        from koli.state_manager import StateManager
        plan = StateManager._auto_send_plan
        self.assertEqual(plan(False, {'auto_send': True, 'auto_send_delay': 3}), (True, True, 3))
        self.assertEqual(plan(False, {'auto_send': True, 'auto_send_delay': 0}), (True, True, 0))
        # The send key still sends at once.
        self.assertEqual(plan(True, {'auto_send': True, 'auto_send_delay': 3}), (True, False, None))

    # Saved from the rule editor: a number of seconds within the main
    # setting's range, or nothing (the main setting).
    def test_saving_a_rule_keeps_a_clean_wait(self):
        import tempfile
        import unittest.mock as mock
        from koli.services import rules as svc
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(svc, '_rules_path', return_value=Path(tmp.name) / 'app_rules.yaml'):
            rule_id = svc.save_rule({'match': ['whatsapp.exe'], 'auto_send': True, 'auto_send_delay': '5'})
            saved = [r for r in svc.list_rules() if r['id'] == rule_id][0]['entry']
            self.assertEqual(saved['auto_send_delay'], 5)
            svc.save_rule({'match': ['whatsapp.exe'], 'auto_send': True, 'auto_send_delay': 'auto_slow'}, rule_id)
            saved = [r for r in svc.list_rules() if r['id'] == rule_id][0]['entry']
            self.assertEqual(saved['auto_send_delay'], 'auto_slow')
            for bad in (99, 'auto_turbo'):
                with self.assertRaises(ValueError):
                    svc.save_rule({'match': ['whatsapp.exe'], 'auto_send_delay': bad}, rule_id)


class DelayedSendStateManagerTests(unittest.TestCase):
    def _sm(self, recording):
        import unittest.mock as mock
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.clipboard_manager = mock.Mock()
        sm.clipboard_manager.delayed_send.cancel.return_value = True
        sm.get_current_state = mock.Mock(return_value='recording' if recording else 'idle')
        sm.cancel_active_recording = mock.Mock()
        return sm

    def test_esc_stops_a_pending_send(self):
        sm = self._sm(recording=False)
        self.assertTrue(sm.cancel_recording_hotkey_pressed())
        sm.clipboard_manager.delayed_send.cancel.assert_called_once_with('Esc')

    def test_esc_while_recording_still_cancels_the_recording(self):
        import unittest.mock as mock
        sm = self._sm(recording=True)
        with mock.patch('builtins.print'):
            self.assertTrue(sm.cancel_recording_hotkey_pressed())
        sm.cancel_active_recording.assert_called_once()

    def test_a_new_dictation_stops_a_pending_send(self):
        import unittest.mock as mock
        sm = self._sm(recording=False)
        sm.can_start_recording = mock.Mock(return_value=True)
        sm._begin_recording = mock.Mock()
        sm.start_recording()
        sm.clipboard_manager.delayed_send.cancel.assert_called_once_with('you started a new dictation')


# ── Settings search ─────────────────────────────────────────────────────────
# The Advanced Settings page filters its rows as you type. The matching lives
# in js/search.js (no imports), so Node can run it here.

class SettingsSearchMatchTests(unittest.TestCase):
    SEARCH_JS = ROOT / 'src' / 'koli' / 'ui' / 'web' / 'js' / 'search.js'

    def _run(self, script):
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is not installed')
        module = self.SEARCH_JS.resolve().as_uri()
        code = f"import * as s from '{module}';\n{script}"
        out = subprocess.run([node, '--input-type=module', '-e', code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_every_word_must_match_in_any_order_and_case(self):
        result = self._run(
            "const s1 = {label: 'Wait before sending', help: 'Pause before Enter', path: 'clipboard.auto_send_delay', group: ''};"
            "const hay = s.settingHaystack(s1, 'Delivery & clipboard');"
            "console.log(JSON.stringify(["
            "  s.matchesTerms(hay, s.searchTerms('send delay')),"
            "  s.matchesTerms(hay, s.searchTerms('DELAY Send')),"
            "  s.matchesTerms(hay, s.searchTerms('delivery')),"
            "  s.matchesTerms(hay, s.searchTerms('pause enter')),"
            "  s.matchesTerms(hay, s.searchTerms('send microphone')),"
            "  s.searchTerms('  two   words '),"
            "]));")
        self.assertEqual(result, '[true,true,true,true,false,["two","words"]]')

    def test_an_empty_query_matches_everything(self):
        self.assertEqual(self._run("console.log(s.matchesTerms('anything', s.searchTerms('   ')));"), 'true')


class SettingsSearchPageTests(unittest.TestCase):
    WEB = ROOT / 'src' / 'koli' / 'ui' / 'web'

    def test_settings_page_has_the_search_box(self):
        page = (self.WEB / 'js' / 'screens' / 'settings.js').read_text(encoding='utf-8')
        self.assertIn("from '../search.js'", page)
        self.assertIn('aria-label="Search settings"', page)
        self.assertIn('search-field', page)

    # One look for every in-page search box, defined once.
    def test_search_field_style_is_shared_not_inline(self):
        css = (self.WEB / 'css' / 'components.css').read_text(encoding='utf-8')
        self.assertIn('.search-field', css)
        history = (self.WEB / 'js' / 'screens' / 'history.js').read_text(encoding='utf-8')
        self.assertIn('search-field', history)
        self.assertNotIn('border:1px solid var(--line2);border-radius:9px;color:var(--mute)', history)
