import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from soundboard.audio import Device
from soundboard.ui import MainWindow
from test_audio import FakeStream


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.device_patch = patch('soundboard.ui.devices', return_value=[
            Device(1, 'CABLE Input', 'Fake', 0, 2, 48000),
            Device(2, 'Physical mic', 'Fake', 1, 0, 48000),
            Device(3, 'Headphones', 'Fake', 0, 2, 48000)])
        self.device_patch.start()
        self.stream_patch = patch('soundboard.audio.sd.OutputStream', FakeStream)
        self.stream_patch.start()
        self.window = MainWindow(self.folder, register_hotkeys=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.device_patch.stop()
        self.stream_patch.stop()
        self.directory.cleanup()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(predicate(), 'Background UI operation did not finish')

    def import_sound(self):
        path = self.folder / 'sample.wav'
        sf.write(str(path), np.zeros(4800), 48000)
        self.window.import_files([str(path)])
        self.wait_until(lambda: len(self.window.clips) == 1)
        return path

    def test_import_play_stop_search_and_persistence(self):
        path = self.import_sound()
        self.assertFalse(self.window.mic_enabled.isChecked())
        self.assertEqual(self.window.output.currentData().name, 'CABLE Input')
        clip = self.window.clips[0]
        self.window.trigger(clip['id'])
        self.wait_until(lambda: clip['id'] in self.window.engine.mixer.progress())
        self.assertTrue(self.window.connected)
        self.window.trigger(clip['id'])
        self.assertFalse(self.window.engine.mixer.progress())
        self.window.search.setText('missing')
        self.assertEqual(len(self.window.cards), 0)
        self.assertTrue(self.window.empty.isVisible())
        self.window.search.clear()
        self.assertEqual(len(self.window.cards), 1)
        self.window.import_files([str(path)])
        self.assertEqual(len(self.window.clips), 1)
        self.window.persist()
        self.assertTrue((self.folder / 'library.json').exists())
        self.window.remove_clip(clip)
        self.assertEqual(len(self.window.clips), 0)

    def test_stop_cancels_pending_decode(self):
        self.import_sound()
        clip = self.window.clips[0]
        self.window.trigger(clip['id'])
        self.window.stop_sounds()
        self.wait_until(lambda: not self.window.tasks)
        self.assertFalse(self.window.engine.mixer.progress())

    def test_disconnect_cancels_pending_decode(self):
        self.import_sound()
        self.window.trigger(self.window.clips[0]['id'])
        self.window.disconnect_route()
        self.wait_until(lambda: not self.window.tasks)
        self.assertFalse(self.window.engine.running)
        self.assertFalse(self.window.engine.mixer.progress())

    def test_missing_file_is_reported_without_crashing(self):
        path = self.import_sound()
        path.unlink()
        self.window.trigger(self.window.clips[0]['id'])
        self.wait_until(lambda: 'Could not play' in self.window.banner.text())
        self.assertFalse(self.window.pending)

    def test_demo_generation(self):
        self.window.add_demos()
        self.wait_until(lambda: len(self.window.clips) == 3)
        self.assertEqual(len(self.window.cards), 3)
        self.assertTrue(all(Path(clip['path']).exists() for clip in self.window.clips))

    def test_no_devices_is_recoverable(self):
        with patch('soundboard.ui.devices', return_value=[]):
            self.window.refresh_devices()
        self.assertFalse(self.window.connect_route())
        self.assertTrue(self.window.banner.isVisible())
        self.window.refresh_devices()
        self.assertTrue(self.window.connect_route())

    def test_cable_cannot_feed_back_into_itself(self):
        self.window.microphone.clear()
        self.window.microphone.addItem('CABLE Output', Device(4, 'CABLE Output', 'Fake', 2, 0, 48000))
        self.window.mic_enabled.setChecked(True)
        self.assertFalse(self.window.connect_route())
        self.assertIn('feeds back', self.window.banner.text())

    def test_missing_saved_output_is_not_silently_replaced(self):
        self.window.state['output'] = 'Fake|Disconnected cable'
        self.window.output.clear()
        self.window.refresh_devices()
        self.assertIsNone(self.window.output.currentData())
        self.assertFalse(self.window.connect_route())


if __name__ == '__main__':
    unittest.main()
