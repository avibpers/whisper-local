# tests/test_profiles.py
# Profiles sit on top of the global settings and never overwrite them. One applies per
# dictation: the one whose key is held, else the app's, else the active one. What
# matters most: your global settings and your settings file don't change, and after a
# held key everything is back as it was.

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli.config_manager import ConfigManager  # noqa: E402
from koli.profiles import ProfileLayer, ProfileManager  # noqa: E402
from koli.settings import schema  # noqa: E402

PROFILES = {
    'dictation': {'overrides': {}},
    'translate': {'hotkey': 'ctrl+win+y | macos: ctrl+option+t', 'overrides': {'ai': {'translate': 'he_en'}}},
    'hebrew': {'hotkey': 'ctrl+win+h', 'overrides': {'whisper': {'language': 'he', 'model': 'base'}, 'hotkey': {'recording_mode': 'toggle'}}},
    'copy': {'overrides': {'clipboard': {'auto_paste': False}}},
}


def config_with(folder) -> ConfigManager:
    with mock.patch('koli.config_manager.get_user_app_data_path', return_value=str(folder), create=True):
        cm = ConfigManager(use_user_settings=False)
    return cm


def manager_of(cm, active='dictation') -> ProfileManager:
    manager = ProfileManager.__new__(ProfileManager)
    manager.config_manager = cm
    manager.profiles = {k: dict(v) for k, v in PROFILES.items()}
    manager.active = active
    manager._persist_active = lambda: None
    return manager


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.cm = config_with(self.folder)
        self.manager = manager_of(self.cm)
        self.layer = ProfileLayer(self.cm, self.manager)


class LayerTests(Base):
    def test_a_profile_changes_what_koli_reads_and_not_the_global_setting(self):
        self.layer.use('translate')
        self.assertEqual(self.cm.get_path('ai.translate'), 'he_en')
        self.assertEqual(self.cm.config['ai']['translate'], 'he_en')
        self.assertEqual(self.cm.get_global('ai.translate'), 'off')
        self.assertEqual(self.cm.global_config['ai']['translate'], 'off')

    def test_what_a_profile_does_not_set_uses_global(self):
        global_language = self.cm.get_global('whisper.language')
        self.layer.use('translate')
        self.assertEqual(self.cm.get_path('whisper.language'), global_language)

    def test_it_is_gone_afterwards(self):
        self.layer.use('translate')
        self.layer.use()
        self.assertEqual(self.cm.get_path('ai.translate'), 'off')
        self.assertEqual(self.cm.overlay, {})

    def test_a_profile_is_never_saved_as_a_global_setting(self):
        self.layer.use('translate')
        self.assertNotIn('translate', (self.cm.user_overrides().get('ai') or {}))

    def test_a_global_change_during_a_profile_goes_to_the_global_setting(self):
        self.layer.use('translate')
        self.cm.set_path('ai.translate', 'off', save=False)
        self.cm.set_path('whisper.language', 'fr', save=False)
        self.assertEqual(self.cm.get_path('ai.translate'), 'he_en')    # the profile still decides
        self.assertEqual(self.cm.get_path('whisper.language'), 'fr')
        self.layer.use()
        self.assertEqual(self.cm.get_path('whisper.language'), 'fr')

    def test_the_active_profile_is_the_system_default(self):
        self.manager.active = 'translate'
        self.layer.use()
        self.assertEqual(self.cm.get_path('ai.translate'), 'he_en')
        self.layer.use('dictation')
        self.assertEqual(self.cm.get_path('ai.translate'), 'off')
        self.layer.use()
        self.assertEqual(self.cm.get_path('ai.translate'), 'he_en')

    def test_a_profile_that_is_gone_means_the_default(self):
        self.manager.active = 'translate'
        self.layer.use('deleted-yesterday')
        self.assertEqual(self.layer.current, 'translate')


class WhatAProfileCanSetTests(Base):
    def test_the_model_and_recording_mode_are_left_out(self):
        self.assertEqual(self.manager.overrides_of('hebrew'), {'whisper.language': 'he'})

    def test_every_setting_it_can_hold_exists_and_changes_between_dictations(self):
        for path in schema.PROFILE_PATHS:
            setting = schema.get(path)
            self.assertIsNotNone(setting, path)
            self.assertIn(setting.apply, (schema.LIVE, schema.NEXT_RECORDING), path)

    def test_the_page_is_told_which_ones(self):
        self.assertTrue(schema.get('ai.translate').to_dict()['profile'])
        self.assertNotIn('profile', schema.get('whisper.model').to_dict())

    def test_keys_are_listed_for_this_system(self):
        keys = dict(self.manager.hotkeys())
        self.assertEqual(keys['hebrew'], 'ctrl+win+h')
        self.assertEqual(keys['translate'], 'ctrl+win+y')
        self.assertNotIn('copy', keys)


class ComponentsTests(Base):
    def setUp(self):
        super().setUp()
        self.appliers = mock.Mock()
        self.layer.attach(self.appliers)

    def test_what_a_component_holds_is_set_and_set_back(self):
        self.layer.use('copy')
        self.appliers.apply_for_profile.assert_called_once_with('clipboard.auto_paste', False, True)
        self.appliers.reset_mock()
        self.layer.use()
        self.appliers.apply_for_profile.assert_called_once_with('clipboard.auto_paste', True, False)

    def test_the_same_profile_again_does_nothing(self):
        self.layer.use('copy')
        self.appliers.reset_mock()
        self.layer.use('copy')
        self.appliers.apply_for_profile.assert_not_called()

    def test_a_failing_component_does_not_stop_the_dictation(self):
        self.appliers.apply_for_profile.side_effect = RuntimeError('boom')
        self.layer.use('copy')
        self.assertFalse(self.cm.get_path('clipboard.auto_paste'))

    def test_a_default_set_before_the_window_opened_takes_effect_when_it_does(self):
        self.manager.active = 'copy'
        layer = ProfileLayer(self.cm, self.manager)
        appliers = mock.Mock()
        layer.attach(appliers)
        appliers.apply_for_profile.assert_called_once_with('clipboard.auto_paste', False, True)


class SettingsScreenTests(Base):
    def _service(self):
        from koli.settings.appliers import Appliers, AppContext
        from koli.settings.service import SettingsService
        appliers = Appliers(AppContext(mock.Mock(), self.cm))
        return SettingsService(self.cm, appliers), appliers

    def test_it_shows_and_edits_the_global_settings(self):
        service, _ = self._service()
        self.layer.use('translate')
        self.assertEqual(service.get('ai.translate'), 'off')
        self.assertEqual(service.snapshot()['ai']['translate'], 'off')

    def test_changing_a_setting_the_profile_holds_waits_for_the_profile_to_go(self):
        service, appliers = self._service()
        self.layer.use('copy')
        with mock.patch.object(appliers, 'apply_for_profile') as apply:
            service.set('clipboard.auto_paste', False)
            apply.assert_not_called()
        self.assertIs(self.cm.get_global('clipboard.auto_paste'), False)


# ── One dictation ────────────────────────────────────────────────────────────

class ChoosingTests(Base):
    def _manager(self, rule=None):
        from koli.state_manager import StateManager
        import threading
        sm = StateManager.__new__(StateManager)
        sm.logger = mock.Mock()
        sm.config_manager = self.cm
        sm.profile_manager = self.manager
        sm.profile_layer = self.layer
        sm.app_rules = mock.Mock()
        sm.app_rules.match_for_foreground.return_value = rule or {}
        sm.whisper_engine = mock.Mock()
        sm._state_lock = threading.RLock()
        sm._grab_selection_text = lambda: ''
        return sm

    def test_nothing_held_and_no_app_profile_means_the_default(self):
        self.manager.active = 'translate'
        sm = self._manager()
        sm._apply_recording_context()
        self.assertEqual(self.layer.current, 'translate')

    def test_the_apps_profile_beats_the_default(self):
        sm = self._manager({'profile': 'translate'})
        sm._apply_recording_context()
        self.assertEqual(self.cm.get_path('ai.translate'), 'he_en')

    def test_the_held_key_beats_the_apps_profile(self):
        sm = self._manager({'profile': 'translate'})
        sm._key_profile = 'dictation'
        sm._apply_recording_context()
        self.assertEqual(self.cm.get_path('ai.translate'), 'off')

    def test_the_engine_reads_the_profile(self):
        sm = self._manager({'profile': 'hebrew'})
        sm._apply_recording_context()
        self.assertEqual(sm.whisper_engine.language, 'he')

    def test_after_a_held_key_everything_is_back(self):
        sm = self._manager()
        sm._key_profile = 'translate'
        sm._apply_recording_context()
        self.assertEqual(self.cm.get_path('ai.translate'), 'he_en')
        sm._end_profile()
        self.assertIsNone(sm._key_profile)
        self.assertEqual(self.cm.get_path('ai.translate'), 'off')

    def test_the_key_starts_a_recording_with_its_profile(self):
        sm = self._manager()
        sm._cancel_pending_send = mock.Mock()
        sm.can_start_recording = lambda: True
        sm.start_recording = mock.Mock()
        sm.audio_recorder = mock.Mock()
        sm.audio_recorder.get_recording_status.return_value = True
        sm.start_profile_recording('translate')
        self.assertEqual(sm._key_profile, 'translate')
        sm.start_recording.assert_called_once()

    def test_a_recording_that_could_not_start_leaves_no_profile_behind(self):
        sm = self._manager()
        sm._cancel_pending_send = mock.Mock()
        sm.can_start_recording = lambda: True
        sm.start_recording = mock.Mock()
        sm.audio_recorder = mock.Mock()
        sm.audio_recorder.get_recording_status.return_value = False
        sm.start_profile_recording('translate')
        self.assertIsNone(sm._key_profile)

    def test_the_key_does_nothing_while_something_else_is_recording(self):
        sm = self._manager()
        sm._cancel_pending_send = mock.Mock()
        sm.can_start_recording = lambda: False
        sm.start_recording = mock.Mock()
        sm.start_profile_recording('translate')
        self.assertIsNone(sm._key_profile)
        sm.start_recording.assert_not_called()

    def test_activating_changes_no_global_setting(self):
        sm = self._manager()
        sm.system_tray = mock.Mock()
        sm._auto_dictation = None
        self.assertTrue(sm.activate_profile('translate'))
        self.assertEqual(self.manager.active, 'translate')
        self.assertEqual(self.cm.get_path('ai.translate'), 'he_en')
        self.assertEqual(self.cm.get_global('ai.translate'), 'off')


class HotkeyTests(unittest.TestCase):
    def test_each_key_starts_its_profile_and_letting_go_stops(self):
        from koli.hotkey_listener import HotkeyListener
        state = mock.Mock()
        state.profile_manager.hotkeys.return_value = [('translate', 'ctrl+win+y')]
        listener = HotkeyListener.__new__(HotkeyListener)
        listener.state_manager = state
        listener.logger = mock.Mock()
        self.assertEqual(listener._profile_hotkeys(), [('translate', 'ctrl+win+y')])
        listener._profile_key_pressed('translate')()
        state.start_profile_recording.assert_called_once_with('translate')
        self.assertFalse(listener.keys_armed)
        listener._profile_key_released()
        state.stop_recording.assert_called_once()
        self.assertTrue(listener.keys_armed)


class ScreenServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        from koli.services import rules
        self.rules = rules
        patcher = mock.patch.object(rules, 'get_user_app_data_path', return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_profile_keeps_its_key(self):
        self.rules.save_profile({'description': 'x', 'hotkey': 'ctrl+win+h', 'overrides': {'whisper': {'language': 'he'}}}, 'hebrew')
        saved = next(p for p in self.rules.list_profiles(None)['profiles'] if p['id'] == 'hebrew')
        self.assertEqual(saved['entry']['hotkey'], 'ctrl+win+h')

    def test_two_profiles_cant_share_a_key(self):
        with self.assertRaisesRegex(ValueError, 'translate'):
            self.rules.save_profile({'hotkey': 'win+ctrl+y'}, 'mine')

    def test_an_app_rule_keeps_its_profile(self):
        self.assertIn('profile', self.rules.RULE_KEYS)

    def test_the_shipped_translate_profile_translates_on_its_key(self):
        view = {p['id']: p for p in self.rules.list_profiles(None)['profiles']}
        self.assertEqual(view['translate']['entry']['overrides'], {'ai': {'translate': 'he_en'}})
        self.assertEqual(view['translate']['entry']['hotkey'], 'ctrl+win+y')     # this system's key only
        self.assertEqual(view['dictation']['entry']['overrides'], {})


if __name__ == '__main__':
    unittest.main()
