import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from soundboard.audio import Device
from soundboard.hotkeys import DEFAULT_STOP, STOP_ACTION, GlobalHotkeys, parse_shortcut
from soundboard.library import new_clip, save_state
from soundboard.shortcut_editor import ShortcutEditor
from soundboard.ui import MainWindow
from test_hotkeys import FakeHotkeyAPI


class ShortcutEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        clips = [new_clip(self.folder / f'Sound {i + 1:02}.wav', 1) for i in range(12)]
        save_state(self.folder / 'library.json', {'clips': clips})
        self.devices = patch('soundboard.ui.devices', return_value=[
            Device(1, 'Headphones', 'Fake', 0, 2, 48000)])
        self.devices.start()
        self.window = MainWindow(self.folder, register_hotkeys=False)
        self.api = FakeHotkeyAPI()
        self.window.hotkeys = GlobalHotkeys(self.app, self.window.activate_shortcut, api=self.api)
        self.window.sync_shortcuts()
        self.window.show()
        self.app.processEvents()
        self.dialog = None

    def tearDown(self):
        if self.dialog:
            self.dialog.close()
        self.window.close()
        self.app.processEvents()
        self.assertEqual(self.api.registered, {})
        self.devices.stop()
        self.directory.cleanup()

    def editor(self, selected=None):
        self.window.hotkeys.suspend()
        names = self.window.shortcut_names()
        actions = [(key, names[key], text) for key, text in self.window.shortcut_bindings().items()]
        self.dialog = ShortcutEditor(self.window, actions, self.window.apply_shortcuts, selected)
        self.dialog.show()
        self.app.processEvents()
        return self.dialog

    def drive_modal(self, interaction):
        errors = []

        def drive():
            dialog = self.app.activeModalWidget()
            try:
                self.assertIsInstance(dialog, ShortcutEditor)
                interaction(dialog)
            except Exception as exc:
                errors.append(exc)
            finally:
                if dialog:
                    dialog.reject()
        QTimer.singleShot(0, drive)
        self.window.edit_shortcuts()
        if errors:
            raise errors[0]

    def test_records_custom_keys_and_saves_tenth_pad_clear_and_stop_across_restart(self):
        tenth = self.window.clips[9]['id']
        first = self.window.clips[0]['id']
        dialog = self.editor(tenth)
        edit = dialog.editors[tenth]
        edit.setFocus()
        QTest.keyClick(edit, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(edit.keySequence().toString(), 'Ctrl+Shift+K')
        dialog.editors[first].clear()
        dialog.editors[STOP_ACTION].setKeySequence(QKeySequence('F8'))
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.window.cards[tenth].shortcut_label.text(), 'Ctrl+Shift+K')
        self.assertIn('F8', self.window.shortcut_hint.text())
        self.assertEqual(self.window.cards[first].shortcut_label.text(), 'No shortcut')
        saved = json.loads(self.window.state_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['clips'][9]['shortcut'], 'Ctrl+Shift+K')
        self.assertEqual(saved['clips'][0]['shortcut'], '')
        self.assertEqual(saved['stop_shortcut'], 'F8')
        reopened = MainWindow(self.folder, register_hotkeys=False)
        try:
            self.assertEqual(reopened.clips[9]['shortcut'], 'Ctrl+Shift+K')
            self.assertEqual(reopened.clips[0]['shortcut'], '')
            self.assertEqual(reopened.stop_shortcut, 'F8')
        finally:
            reopened.close()

    def test_duplicate_with_stop_is_explained_and_not_saved(self):
        first = self.window.clips[0]['id']
        dialog = self.editor()
        dialog.editors[first].setKeySequence(QKeySequence(DEFAULT_STOP))
        dialog.save()
        self.assertTrue(dialog.isVisible())
        self.assertIn('Stop sounds', dialog.error.text())
        self.assertEqual(self.window.clips[0]['shortcut'], 'Ctrl+Alt+1')
        self.assertEqual(self.api.registered, {})

    def test_cancel_restores_registered_keys_without_applying_edits(self):
        original = self.window.shortcut_bindings()

        def interact(dialog):
            self.assertEqual(self.api.registered, {})
            dialog.editors[STOP_ACTION].setKeySequence(QKeySequence('F8'))
        self.drive_modal(interact)
        self.assertEqual(self.window.shortcut_bindings(), original)
        self.assertEqual(len(self.api.registered), 10)
        self.assertFalse(self.window.hotkeys.paused)

    def test_windows_conflict_keeps_editor_open_and_cancel_restores_original(self):
        first = self.window.clips[0]['id']
        self.api.blocked.add(parse_shortcut('F8').signature)

        def interact(dialog):
            dialog.editors[first].setKeySequence(QKeySequence('F8'))
            dialog.save()
            self.assertTrue(dialog.isVisible())
            self.assertIn('unavailable', dialog.error.text())
            self.assertEqual(self.window.clips[0]['shortcut'], 'Ctrl+Alt+1')
            self.assertEqual(self.api.registered, {})
        self.drive_modal(interact)
        self.assertEqual(len(self.api.registered), 10)

    def test_remove_and_search_do_not_shift_shortcuts_or_actions(self):
        second = self.window.clips[1]['id']
        self.window.remove_clip(self.window.clips[0])
        self.window.search.setText('Sound 02')
        self.assertEqual(self.window.cards[second].shortcut_label.text(), 'Ctrl+Alt+2')
        self.window.search.setText('No match')
        with patch.object(self.window, 'trigger') as trigger:
            self.window.activate_shortcut(second)
            trigger.assert_called_once_with(second)
        with patch.object(self.window, 'stop_sounds') as stop:
            self.window.activate_shortcut(STOP_ACTION)
            stop.assert_called_once()

    def test_save_keeps_sound_imported_while_editor_was_open(self):
        dialog = self.editor()
        clip = new_clip(self.folder / 'New sound.wav', 1)
        clip['shortcut'] = 'F9'
        self.window.clips.append(clip)
        self.window.sync_shortcuts()
        self.assertEqual(self.api.registered, {})
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.window.clips[-1]['shortcut'], 'F9')
        self.assertIn(clip['id'], self.window.hotkeys.bindings)


if __name__ == '__main__':
    unittest.main()
