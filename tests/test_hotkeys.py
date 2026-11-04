import ctypes
from ctypes import wintypes
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
import unittest

from PySide6.QtWidgets import QApplication

from soundboard.hotkeys import (
    DEFAULT_STOP, STOP_ACTION, GlobalHotkeys, ShortcutError,
    assign_missing_shortcuts, compile_bindings, parse_shortcut,
)


class FakeHotkeyAPI:
    def __init__(self):
        self.registered = {}
        self.blocked = set()

    def RegisterHotKey(self, window, identifier, modifiers, key):
        signature = modifiers & 0xF, key
        if signature in self.blocked or signature in self.registered.values():
            return False
        if not modifiers & 0x4000:
            raise AssertionError('Key repeat must be disabled')
        self.registered[identifier] = signature
        return True

    def UnregisterHotKey(self, window, identifier):
        del self.registered[identifier]
        return True


class ShortcutTests(unittest.TestCase):
    def test_supported_keys_and_canonical_spelling(self):
        self.assertEqual(parse_shortcut('Alt+Ctrl+a').text, 'Ctrl+Alt+A')
        cases = {'Ctrl+Alt+1': (3, 0x31), 'Ctrl+Shift+S': (6, 0x53),
                 'F8': (0, 0x77), 'F24': (0, 0x87), 'Num+1': (0, 0x61),
                 'Ctrl+Num++': (2, 0x6B), 'Ctrl+Tab': (2, 0x09)}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_shortcut(text).signature, expected)
        self.assertIsNone(parse_shortcut(''))

    def test_rejects_ambiguous_or_reserved_combinations(self):
        for text in ['A', 'Shift+A', 'Escape', 'F12', 'Ctrl+F12', 'Meta+K',
                     'Ctrl+Alt+Del', 'Ctrl+A, Ctrl+B', 'bad shortcut', 'Num+Enter', None]:
            with self.subTest(text=text), self.assertRaises(ShortcutError):
                parse_shortcut(text)

    def test_duplicate_detection_includes_stop_and_windows_aliases(self):
        valid, errors = compile_bindings({STOP_ACTION: 'Ctrl+Alt+1', 'pad': 'Alt+Ctrl+1'},
                                        {STOP_ACTION: 'Stop sounds'})
        self.assertIn(STOP_ACTION, valid)
        self.assertIn('Stop sounds', errors['pad'])
        _, errors = compile_bindings({'one': 'Ctrl+Return', 'two': 'Ctrl+Enter'})
        self.assertIn('two', errors)

    def test_old_library_migrates_beyond_nine_without_shifting_on_remove(self):
        clips = [{'id': str(i)} for i in range(12)]
        assign_missing_shortcuts(clips)
        self.assertEqual([c['shortcut'] for c in clips[:9]], [f'Ctrl+Alt+{i}' for i in range(1, 10)])
        self.assertEqual(clips[9]['shortcut'], '')
        clips.pop(0)
        assign_missing_shortcuts(clips)
        self.assertEqual(clips[0]['shortcut'], 'Ctrl+Alt+2')
        self.assertEqual(clips[8]['shortcut'], '')
        clips.append({'id': 'new'})
        assign_missing_shortcuts(clips)
        self.assertEqual(clips[-1]['shortcut'], 'Ctrl+Alt+1')

    def test_migration_preserves_custom_and_cleared_and_reserves_stop(self):
        clips = [{'shortcut': ''}, {'shortcut': 'F8'}, {}, {'shortcut': 'Alt+Ctrl+2'}]
        assign_missing_shortcuts(clips, 'Ctrl+Alt+1')
        self.assertEqual([c['shortcut'] for c in clips], ['', 'F8', 'Ctrl+Alt+3', 'Alt+Ctrl+2'])


class RegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.api = FakeHotkeyAPI()
        self.actions = []
        self.manager = GlobalHotkeys(self.app, self.actions.append, api=self.api)
        self.original = {STOP_ACTION: DEFAULT_STOP, 'one': 'F8', 'two': 'F9'}
        self.manager.configure(self.original)

    def tearDown(self):
        self.manager.close()
        self.assertEqual(self.api.registered, {})

    def test_rebinding_swap_and_clear_releases_previous_keys(self):
        self.manager.suspend()
        self.assertEqual(self.api.registered, {})
        self.manager.configure({STOP_ACTION: '', 'one': 'F9', 'two': 'F8'}, strict=True)
        self.assertEqual(set(self.api.registered.values()), {(0, 0x78), (0, 0x77)})
        self.assertFalse(self.manager.paused)
        self.assertEqual(self.manager.failed, {})

    def test_conflict_rolls_back_entire_edit_and_cancel_restores_original(self):
        self.manager.suspend()
        self.api.blocked.add(parse_shortcut('F10').signature)
        with self.assertRaisesRegex(ShortcutError, 'F10.*unavailable'):
            self.manager.configure({'one': 'F11', 'two': 'F10'}, strict=True)
        self.assertEqual(self.manager.bindings, self.original)
        self.assertEqual(self.api.registered, {})
        self.assertTrue(self.manager.paused)
        self.manager.resume()
        self.assertEqual(len(self.api.registered), 3)

    def test_failed_edit_while_active_restores_original(self):
        self.api.blocked.add(parse_shortcut('F10').signature)
        with self.assertRaises(ShortcutError):
            self.manager.configure({'one': 'F10'}, strict=True)
        self.assertEqual(self.manager.bindings, self.original)
        self.assertEqual(len(self.api.registered), 3)

    def test_launch_keeps_available_keys_and_reports_invalid_or_busy_ones(self):
        self.api.blocked.add(parse_shortcut('F10').signature)
        self.manager.configure({'one': 'F10', 'two': 'F9', 'bad': ['invalid']})
        self.assertEqual(set(self.manager.failed), {'one', 'bad'})
        self.assertEqual([a for a, _ in self.manager.registered.values()], ['two'])

    def test_import_while_paused_does_not_resume_hotkeys(self):
        self.manager.suspend()
        bindings = dict(self.original, new='F11')
        self.manager.configure(bindings)
        self.assertTrue(self.manager.paused)
        self.assertEqual(self.api.registered, {})
        self.manager.resume()
        self.assertEqual(self.manager.bindings, bindings)
        self.assertEqual(len(self.api.registered), 4)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows message structure')
    def test_windows_message_dispatches_stable_action_and_drops_stale_events(self):
        identifier, (_, shortcut) = next((i, b) for i, b in self.manager.registered.items() if b[0] == 'two')
        msg = wintypes.MSG()
        msg.message, msg.wParam = 0x0312, identifier
        msg.lParam = (shortcut.key << 16) | shortcut.modifiers
        self.assertEqual(self.manager.nativeEventFilter(None, ctypes.addressof(msg)), (True, 0))
        self.app.processEvents()
        self.assertEqual(self.actions, ['two'])
        self.manager.nativeEventFilter(None, ctypes.addressof(msg))
        self.manager.suspend()
        self.app.processEvents()
        self.assertEqual(self.actions, ['two'])
        self.manager.resume()
        self.assertEqual(self.manager.nativeEventFilter(None, ctypes.addressof(msg)), (False, 0))


if __name__ == '__main__':
    unittest.main()
