import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
import sounddevice as sd
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from soundboard.audio import Device
from soundboard.mic_test import FilterTake, MicrophoneTestDialog, TestPlayback, TestRecording
from test_audio import FakeStream


MIC = Device(1, 'Microphone', 'Fake', 1, 0, 48000)
CABLE = Device(2, 'CABLE Input', 'Fake', 0, 2, 48000)
HEADPHONES = Device(3, 'Headphones', 'Fake', 0, 2, 48000)


class PreviewAudioTests(unittest.TestCase):
    @patch('soundboard.mic_test.sd.InputStream', FakeStream)
    def test_recording_is_bounded_and_closes_before_copying(self):
        recording = TestRecording(MIC, seconds=0.01)
        stream = recording._stream
        with self.assertRaises(sd.CallbackStop):
            stream.options['callback'](np.ones((700, 1), np.float32), 700, None, False)
        samples = recording.finish()
        self.assertEqual(samples.shape, (480,))
        self.assertTrue(stream.closed)
        self.assertEqual(len(recording._samples), 0)
        recording.close()

    @patch('soundboard.mic_test.sd.OutputStream', FakeStream)
    def test_playback_rejects_virtual_devices_and_finishes_with_silence(self):
        player = TestPlayback()
        with self.assertRaisesRegex(ValueError, 'physical headphones'):
            player.play(np.ones(480), CABLE)
        player.play(np.ones(100, np.float32), HEADPHONES)
        stream = player._stream
        result = np.empty((480, 2), np.float32)
        with self.assertRaises(sd.CallbackStop):
            stream.options['callback'](result, 480, None, False)
        np.testing.assert_array_equal(result[:100], 0.5)
        np.testing.assert_array_equal(result[100:], 0)
        player.close()
        self.assertTrue(stream.closed)
        self.assertIsNone(player._samples)

    def test_failed_recording_start_closes_stream(self):
        class FailingStream(FakeStream):
            def start(self):
                raise RuntimeError('disconnected')

        with patch('soundboard.mic_test.sd.InputStream', FailingStream):
            with self.assertRaisesRegex(RuntimeError, 'disconnected'):
                TestRecording(MIC)
        self.assertTrue(FakeStream.created[-1].closed)

    def test_device_loss_during_close_still_discards_samples(self):
        class DisconnectedStream(FakeStream):
            def abort(self):
                raise RuntimeError('device unplugged')

        with patch('soundboard.mic_test.sd.InputStream', DisconnectedStream):
            recording = TestRecording(MIC)
            stream = recording._stream
            recording.close()
            self.assertTrue(stream.closed)
            self.assertEqual(len(recording._samples), 0)
        with patch('soundboard.mic_test.sd.OutputStream', DisconnectedStream):
            player = TestPlayback()
            player.play(np.ones(480, np.float32), HEADPHONES)
            stream = player._stream
            player.close()
            self.assertTrue(stream.closed)
            self.assertIsNone(player._samples)


class PreviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = MicrophoneTestDialog(MIC, [MIC, CABLE, HEADPHONES], CABLE)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.app.processEvents()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            # qWait can keep the Python GIL while waiting on Windows; let the
            # filter thread import its native dependency as in the real event loop.
            time.sleep(0.005)
        self.assertTrue(predicate())

    @patch('soundboard.mic_test.sd.InputStream', FakeStream)
    def test_explicit_capture_filtering_and_discard(self):
        self.assertIsNone(self.dialog.recording)
        self.assertEqual(self.dialog.output.count(), 1)
        self.assertEqual(self.dialog.output.currentData(), HEADPHONES)
        self.dialog.toggle_recording()
        recording = self.dialog.recording
        recording._stream.options['callback'](np.zeros((960, 1), np.float32), 960, None, False)
        self.dialog.toggle_recording()
        self.wait_for(lambda: self.dialog.filtered is not None)
        self.assertEqual(len(self.dialog.original), 960)
        self.assertTrue(self.dialog.original_button.isEnabled())
        self.dialog.reject()
        self.assertIsNone(self.dialog.original)
        self.assertIsNone(self.dialog.filtered)
        self.assertFalse(self.dialog.task.isRunning())

    @patch('soundboard.mic_test.sd.InputStream', FakeStream)
    def test_escape_cancels_recording_and_discards_buffer(self):
        self.dialog.toggle_recording()
        recording = self.dialog.recording
        stream = recording._stream
        self.dialog.reject()
        self.assertTrue(stream.closed)
        self.assertEqual(len(recording._samples), 0)
        self.assertIsNone(self.dialog.recording)

    def test_close_cancels_inference(self):
        entered = threading.Event()

        def slow_filter(samples, cancelled):
            entered.set()
            cancelled.wait(2)
            return None

        with patch('soundboard.mic_test.filter_recording', side_effect=slow_filter):
            self.dialog.original = np.ones(480, np.float32)
            self.dialog.task = FilterTake(self.dialog.original, self.dialog)
            self.dialog.task.start()
            self.wait_for(entered.is_set)
            self.dialog.reject()
        self.assertFalse(self.dialog.task.isRunning())
        self.assertIsNone(self.dialog.task.samples)
        self.assertIsNone(self.dialog.original)

    def test_unavailable_microphone_is_reported(self):
        with patch('soundboard.mic_test.sd.InputStream', side_effect=RuntimeError('unplugged')):
            self.dialog.toggle_recording()
        self.assertIn('unplugged', self.dialog.status.text())
        self.assertTrue(self.dialog.record_button.isEnabled())

    @patch('soundboard.mic_test.sd.InputStream', FakeStream)
    def test_capture_timeout_releases_device_without_more_callbacks(self):
        self.dialog.toggle_recording()
        stream = self.dialog.recording._stream
        self.dialog.recording.started_at = time.monotonic() - 11
        self.dialog.tick()
        self.assertIsNone(self.dialog.recording)
        self.assertTrue(stream.closed)
        self.assertIn('No audio', self.dialog.status.text())


if __name__ == '__main__':
    unittest.main()
