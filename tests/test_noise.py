import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from soundboard.audio import AudioEngine, Device
from soundboard.noise import (
    FRAME_SIZE, MicrophoneProcessor, MonoBuffer, RNNoiseFilter, filter_recording,
)
from test_audio import FakeStream


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.001)
    if not predicate():
        raise AssertionError('Microphone worker did not finish')


class HalfVolumeFilter:
    def __init__(self):
        self.closed = False

    def process(self, samples):
        if self.closed:
            raise RuntimeError('Used after close')
        return samples * 0.5

    def close(self):
        self.closed = True


class BufferTests(unittest.TestCase):
    def test_overflow_keeps_latest_samples_and_counts_drops(self):
        buffer = MonoBuffer(capacity=5)
        buffer.write(np.arange(8, dtype=np.float32))
        buffer.write([9, 10])
        self.assertEqual(buffer.available, 5)
        self.assertEqual(buffer.dropped, 5)
        np.testing.assert_array_equal(buffer.read(7), [5, 6, 7, 9, 10, 0, 0])

    def test_close_discards_audio_and_wakes_waiter(self):
        buffer = MonoBuffer()
        buffer.write(np.ones(200))
        results = []
        thread = threading.Thread(target=lambda: results.append(buffer.read_frame()))
        thread.start()
        buffer.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(buffer.available, 0)
        buffer.write(np.ones(480))
        self.assertEqual(buffer.available, 0)


class WorkerTests(unittest.TestCase):
    def test_arbitrary_callback_sizes_preserve_frame_continuity(self):
        samples = np.random.default_rng(3).uniform(-0.8, 0.8, 1730).astype(np.float32)
        worker = MicrophoneProcessor(HalfVolumeFilter)
        try:
            offset = 0
            for count in [1, 203, 129, 777, 620]:
                worker.submit(samples[offset:offset + count])
                offset += count
            wait_for(lambda: worker.output.available == 1440)
            result = worker.read(1500)
            np.testing.assert_array_equal(result[:1440, 0], samples[:1440] * 0.5)
            np.testing.assert_array_equal(result[1440:], 0)
            self.assertEqual(worker.input.available, 290)
        finally:
            worker.stop()
        self.assertFalse(worker.healthy)
        self.assertTrue(worker._filter.closed)
        self.assertEqual(worker.output.available, 0)
        worker.stop()  # Idempotent cleanup.

    def test_worker_exception_never_returns_unfiltered_audio(self):
        class BrokenFilter(HalfVolumeFilter):
            def process(self, samples):
                raise RuntimeError('inference failed')

        worker = MicrophoneProcessor(BrokenFilter)
        try:
            worker.submit(np.ones(480, np.float32))
            wait_for(lambda: worker.error is not None)
            with self.assertRaisesRegex(RuntimeError, 'inference failed'):
                worker.read(480)
            self.assertFalse(worker.healthy)
        finally:
            worker.stop()
        self.assertTrue(worker._filter.closed)

    def test_stopping_idle_worker_does_not_wait_for_more_input(self):
        worker = MicrophoneProcessor(HalfVolumeFilter)
        worker.stop()
        self.assertFalse(worker._thread.is_alive())

    def test_test_take_handles_partial_frame_and_cancel(self):
        samples = np.ones(501, np.float32)
        result = filter_recording(samples, filter_factory=HalfVolumeFilter)
        np.testing.assert_array_equal(result[:501], 0.5)
        np.testing.assert_array_equal(result[501:], 0)
        stopped = threading.Event()
        stopped.set()
        self.assertIsNone(filter_recording(samples, stopped, HalfVolumeFilter))


class NativeFilterTests(unittest.TestCase):
    def test_native_library_silence_finite_output_and_lifecycle(self):
        with RNNoiseFilter() as processor:
            for _ in range(5):
                np.testing.assert_array_equal(processor.process(np.zeros(480, np.float32)), 0)
            extreme = np.resize([np.nan, np.inf, -np.inf, 2, -2], 480)
            result = processor.process(extreme)
            self.assertEqual(result.dtype, np.float32)
            self.assertTrue(np.isfinite(result).all())
            self.assertLessEqual(float(abs(result).max()), 1)
            with self.assertRaises(ValueError):
                processor.process(np.zeros(479))
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            processor.process(np.zeros(480))
        processor.close()

    def test_native_stationary_noise_is_reduced(self):
        samples = np.random.default_rng(4).normal(0, 0.015, 48000).astype(np.float32)
        with RNNoiseFilter() as processor:
            result = np.concatenate([processor.process(samples[i:i + FRAME_SIZE])
                                     for i in range(0, len(samples), FRAME_SIZE)])
        self.assertLess(float(np.mean(result[24000:] ** 2)),
                        float(np.mean(samples[24000:] ** 2)) * 0.5)


class FilterRoutingTests(unittest.TestCase):
    def setUp(self):
        FakeStream.created = []
        self.engine = AudioEngine()
        self.output = Device(1, 'Cable', 'Fake', 0, 2, 192000)
        self.mic = Device(2, 'Mic', 'Fake', 1, 0, 16000)
        self.monitor = Device(3, 'Headphones', 'Fake', 0, 2, 44100)
        self.patches = [patch('soundboard.audio.sd.check_input_settings'),
                        patch('soundboard.audio.sd.InputStream', FakeStream),
                        patch('soundboard.audio.sd.OutputStream', FakeStream),
                        patch('soundboard.noise.RNNoiseFilter', HalfVolumeFilter)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        self.engine.stop()
        for item in reversed(self.patches):
            item.stop()

    def test_only_microphone_is_filtered_and_monitor_keeps_exact_sound(self):
        self.engine.start(self.output, self.mic, self.monitor, mic_noise_reduction=True)
        self.assertEqual(self.engine.rate, 48000)
        capture, monitor, output = FakeStream.created
        self.assertTrue(all(stream.options['samplerate'] == 48000 for stream in FakeStream.created))
        capture.options['callback'](np.full((480, 1), 0.2, np.float32), 480, None, False)
        wait_for(lambda: self.engine._mic_processor.output.available == 480)
        sound = np.random.default_rng(8).uniform(-0.2, 0.2, (480, 2)).astype(np.float32)
        self.engine.mixer.play('pad', sound)
        result = np.empty((480, 2), np.float32)
        output.options['callback'](result, 480, None, False)
        np.testing.assert_allclose(result, sound * 0.75 + 0.1, atol=1e-7)
        monitor.options['callback'](result, 480, None, False)
        np.testing.assert_array_equal(result, sound * 0.75)
        worker = self.engine._mic_processor
        self.engine.stop()
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(worker._filter.closed)
        self.assertTrue(all(stream.closed for stream in FakeStream.created))

    def test_filter_off_or_no_microphone_retains_native_rate(self):
        for microphone in (None, self.mic):
            self.engine.start(self.output, microphone, mic_noise_reduction=microphone is None)
            self.assertEqual(self.engine.rate, 192000)
            self.assertIsNone(self.engine._mic_processor)
            self.engine.stop()

    def test_failed_stream_start_cleans_filter_and_retry_gets_fresh_state(self):
        created = []

        def make_worker():
            worker = MicrophoneProcessor(HalfVolumeFilter)
            created.append(worker)
            return worker

        with patch('soundboard.audio.MicrophoneProcessor', side_effect=make_worker):
            with patch('soundboard.audio.sd.OutputStream', side_effect=RuntimeError('device failed')):
                with self.assertRaisesRegex(RuntimeError, 'device failed'):
                    self.engine.start(self.output, self.mic, mic_noise_reduction=True)
            self.assertFalse(created[0]._thread.is_alive())
            self.assertTrue(created[0]._filter.closed)
            self.engine.start(self.output, self.mic, mic_noise_reduction=True)
            self.assertTrue(self.engine.healthy)
            self.assertIsNot(created[0], created[1])

    def test_filter_load_failure_does_not_open_capture(self):
        with patch('soundboard.audio.MicrophoneProcessor', side_effect=RuntimeError('missing library')):
            with self.assertRaisesRegex(RuntimeError, 'missing library'):
                self.engine.start(self.output, self.mic, mic_noise_reduction=True)
        self.assertFalse(FakeStream.created)
        self.assertFalse(self.engine.running)

    def test_worker_failure_marks_route_unhealthy(self):
        self.engine.start(self.output, self.mic, mic_noise_reduction=True)
        self.engine._mic_processor.error = 'Filter failed'
        self.assertFalse(self.engine.healthy)
        self.assertEqual(self.engine.last_error, 'Filter failed')


if __name__ == '__main__':
    unittest.main()
