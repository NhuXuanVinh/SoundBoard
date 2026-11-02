from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from soundboard.audio import AudioCache, AudioEngine, Device, Mixer, SampleQueue, audio_info
from soundboard.library import load_state, new_clip, save_state


class MixerTests(unittest.TestCase):
    def setUp(self):
        self.mixer = Mixer()
        self.mixer.sound_gain = 1

    def test_overlap_and_end_of_clip(self):
        self.mixer.play('one', np.full((3, 2), 0.2, np.float32))
        self.mixer.play('two', np.full((5, 2), 0.3, np.float32))
        mixed, _ = self.mixer.render(4)
        np.testing.assert_allclose(mixed[:3], 0.5)
        np.testing.assert_allclose(mixed[3:], 0.3)
        self.assertEqual(set(self.mixer.progress()), {'two'})
        mixed, _ = self.mixer.render(4)
        np.testing.assert_allclose(mixed[0], 0.3)
        np.testing.assert_array_equal(mixed[1:], 0)
        self.assertEqual(self.mixer.progress(), {})

    def test_loop_fills_blocks_longer_than_clip(self):
        self.mixer.play('loop', np.array([[0.1, 0.2], [0.3, 0.4]], np.float32), loop=True)
        mixed, _ = self.mixer.render(7)
        np.testing.assert_allclose(mixed[:, 0], [0.1, 0.3, 0.1, 0.3, 0.1, 0.3, 0.1])
        self.assertAlmostEqual(self.mixer.progress()['loop'], 0.5)

    def test_mic_is_mixed_but_excluded_from_monitor(self):
        self.mixer.sound_gain = 0.5
        self.mixer.mic_gain = 0.25
        self.mixer.play('one', np.full((8, 2), 0.6, np.float32), gain=0.5)
        mixed, monitor = self.mixer.render(4, np.full((4, 2), 0.4, np.float32))
        np.testing.assert_allclose(monitor, 0.15)
        np.testing.assert_allclose(mixed, 0.25)

    def test_stop_sounds_preserves_live_mic(self):
        self.mixer.play('one', np.ones((10, 2), np.float32))
        self.mixer.stop()
        mixed, _ = self.mixer.render(3, np.full((3, 2), 0.2, np.float32))
        np.testing.assert_allclose(mixed, 0.2)

    def test_peak_protection(self):
        self.mixer.play('loud', np.ones((4, 2), np.float32), gain=1.5)
        mixed, _ = self.mixer.render(4)
        self.assertTrue(self.mixer.clipped)
        self.assertEqual(self.mixer.peak, 1.5)
        np.testing.assert_array_equal(mixed, 1)

    def test_live_gain_and_loop_update(self):
        self.mixer.play('one', np.ones((2, 2), np.float32), loop=True)
        self.mixer.update('one', 0.25, False)
        mixed, _ = self.mixer.render(3)
        np.testing.assert_allclose(mixed[:2], 0.25)
        np.testing.assert_array_equal(mixed[2:], 0)
        self.assertFalse(self.mixer.progress())

    def test_retrigger_restarts_and_limit_is_enforced(self):
        for number in range(32):
            self.mixer.play(str(number), np.zeros((4, 2), np.float32))
        with self.assertRaises(ValueError):
            self.mixer.play('extra', np.zeros((4, 2), np.float32))
        self.mixer.render(2)
        self.mixer.play('0', np.zeros((4, 2), np.float32))
        self.assertEqual(self.mixer.progress()['0'], 0)
        self.mixer.stop('0')
        self.assertNotIn('0', self.mixer.progress())

    def test_empty_clip_rejected(self):
        with self.assertRaises(ValueError):
            self.mixer.play('empty', np.empty((0, 2), np.float32), loop=True)


class QueueTests(unittest.TestCase):
    def test_fifo_and_underrun(self):
        queue = SampleQueue(10)
        queue.push(np.array([[1], [2], [3]], np.float32))
        np.testing.assert_array_equal(queue.read(2), [[1, 1], [2, 2]])
        queue.push(np.array([[4, 5]], np.float32))
        np.testing.assert_array_equal(queue.read(4), [[3, 3], [4, 5], [0, 0], [0, 0]])

    def test_overflow_keeps_recent_samples(self):
        queue = SampleQueue(3)
        queue.push(np.arange(5, dtype=np.float32).reshape(-1, 1))
        queue.push(np.array([[5]], np.float32))
        np.testing.assert_array_equal(queue.read(3)[:, 0], [3, 4, 5])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_decode_mono_and_resample_without_pitch_change(self):
        path = self.folder / 'tone.wav'
        source_rate = 22050
        t = np.arange(source_rate) / source_rate
        sf.write(str(path), np.sin(2 * np.pi * 440 * t) * 0.5, source_rate)
        cache = AudioCache()
        samples = cache.load(path, 48000)
        self.assertEqual(samples.shape, (48000, 2))
        self.assertEqual(samples.dtype, np.float32)
        np.testing.assert_array_equal(samples[:, 0], samples[:, 1])
        self.assertEqual(np.argmax(abs(np.fft.rfft(samples[:, 0]))), 440)
        self.assertIs(cache.load(path, 48000), samples)
        self.assertEqual(cache.load(path, 44100).shape, (44100, 2))

    def test_cache_evicts_by_decoded_size(self):
        cache = AudioCache(limit=1000)
        for number in range(3):
            path = self.folder / f'{number}.wav'
            sf.write(str(path), np.zeros(100), 48000)
            cache.load(path, 48000)
        self.assertLessEqual(cache.size, 1000)
        self.assertEqual(len(cache.items), 1)

    def test_surround_and_empty_rejected(self):
        path = self.folder / 'surround.wav'
        sf.write(str(path), np.zeros((30, 6)), 48000)
        with self.assertRaisesRegex(ValueError, 'mono or stereo'):
            audio_info(path)
        sf.write(str(path), np.empty(0), 48000)
        with self.assertRaisesRegex(ValueError, 'empty'):
            audio_info(path)

    def test_library_roundtrip_and_corrupt_recovery(self):
        path = self.folder / 'library.json'
        state = {'clips': [new_clip(self.folder / 'âm thanh.wav', 1.5)]}
        save_state(path, state)
        loaded, warning = load_state(path)
        self.assertEqual(loaded, state)
        self.assertIsNone(warning)
        path.write_text('broken json', encoding='utf-8')
        loaded, warning = load_state(path)
        self.assertEqual(loaded, {})
        self.assertTrue(warning)
        backup = list(self.folder.glob('library.recovery-*.json'))
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_text(), 'broken json')


class FakeStream:
    created = []

    def __init__(self, **kwargs):
        self.options = kwargs
        self.active = False
        self.closed = False
        self.created.append(self)

    def start(self):
        self.active = True

    def abort(self):
        self.active = False

    def close(self):
        self.closed = True


class RoutingTests(unittest.TestCase):
    def setUp(self):
        FakeStream.created = []
        self.engine = AudioEngine()
        self.output = Device(1, 'Cable', 'Fake', 0, 2, 48000)
        self.mic = Device(2, 'Mic', 'Fake', 1, 0, 44100)
        self.monitor = Device(3, 'Headphones', 'Fake', 0, 2, 48000)

    @patch('soundboard.audio.sd.check_input_settings')
    @patch('soundboard.audio.sd.InputStream', FakeStream)
    @patch('soundboard.audio.sd.OutputStream', FakeStream)
    def test_full_route_and_cleanup(self, check):
        self.engine.start(self.output, self.mic, self.monitor)
        self.assertTrue(self.engine.healthy)
        capture, monitor, output = FakeStream.created
        capture.options['callback'](np.full((4, 1), 0.2, np.float32), 4, None, False)
        self.engine.mixer.play('pad', np.full((4, 2), 0.4, np.float32))
        result = np.empty((4, 2), np.float32)
        output.options['callback'](result, 4, None, False)
        np.testing.assert_allclose(result, 0.5)
        monitor.options['callback'](result, 4, None, False)
        np.testing.assert_allclose(result, 0.3)
        capture.active = False
        self.assertFalse(self.engine.healthy)
        self.engine.stop()
        self.assertFalse(self.engine.running)
        self.assertTrue(all(stream.closed for stream in FakeStream.created))

    @patch('soundboard.audio.sd.check_input_settings')
    @patch('soundboard.audio.sd.InputStream', FakeStream)
    @patch('soundboard.audio.sd.OutputStream', side_effect=RuntimeError('unavailable'))
    def test_failed_start_cleans_up_partial_streams(self, output, check):
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            self.engine.start(self.output, self.mic)
        self.assertFalse(self.engine.running)
        self.assertTrue(FakeStream.created[0].closed)

    def test_monitor_cannot_duplicate_output(self):
        with self.assertRaisesRegex(ValueError, 'different device'):
            self.engine.start(self.output, monitor=self.output)


if __name__ == '__main__':
    unittest.main()
