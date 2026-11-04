"""Local microphone suppression. Sound pads never enter this module."""
from collections import deque
import ctypes
import threading
import time

import numpy as np


SAMPLE_RATE = 48000
FRAME_SIZE = 480
QUEUE_SAMPLES = 2880  # 60 ms per queue


class RNNoiseFilter:
    """Own one native RNNoise state; accept normalized float32 mono frames.

    Use the package's native API directly: its convenience wrapper converts
    output to int16 without saturating, which can wrap peaks around zero.
    No file reader, resampler, automatic gain, or hard voice gate is involved.
    """
    def __init__(self):
        self._state = None
        try:
            from pyrnnoise.rnnoise import lib
            self._lib = lib
            if lib.rnnoise_get_frame_size() != FRAME_SIZE:
                raise RuntimeError('Unexpected RNNoise frame size.')
            self._state = lib.rnnoise_create(None)
            if not self._state:
                raise RuntimeError('Could not allocate the microphone filter.')
        except Exception as exc:
            self.close()
            raise RuntimeError(
                'Microphone noise reduction is unavailable. Close Soundboard and run '
                f'start.bat to install its dependencies. Details: {exc}') from exc

    def process(self, samples):
        if self._state is None:
            raise RuntimeError('Microphone filter is closed.')
        if np.shape(samples) != (FRAME_SIZE,):
            raise ValueError('RNNoise requires exactly 480 mono samples.')
        frame = np.array(samples, dtype=np.float32, copy=True, order='C')
        np.nan_to_num(frame, copy=False, nan=0, posinf=1, neginf=-1)
        np.clip(frame, -1, 1, out=frame)
        frame *= 32768.0
        output = np.empty(FRAME_SIZE, dtype=np.float32)
        pointer = ctypes.POINTER(ctypes.c_float)
        self._lib.rnnoise_process_frame(
            self._state, output.ctypes.data_as(pointer), frame.ctypes.data_as(pointer))
        output /= 32768.0
        if not np.isfinite(output).all():
            raise RuntimeError('Microphone filter produced invalid audio.')
        np.clip(output, -1, 1, out=output)
        return output

    def close(self):
        if self._state is not None:
            self._lib.rnnoise_destroy(self._state)
            self._state = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class MonoBuffer:
    """Bounded FIFO with short copy-only critical sections.

    Only the worker waits for a complete frame; audio callbacks never wait for
    inference or a producer. Overflow drops oldest samples to cap latency.
    """
    def __init__(self, capacity=QUEUE_SAMPLES):
        self.capacity = capacity
        self.dropped = 0
        self._blocks = deque()
        self._count = 0
        self._condition = threading.Condition()
        self._closed = False

    @property
    def available(self):
        with self._condition:
            return self._count

    def write(self, samples):
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not len(samples):
            return
        block = samples[-self.capacity:].copy()
        with self._condition:
            if self._closed:
                return
            self.dropped += max(0, len(samples) - self.capacity)
            self._blocks.append(block)
            self._count += len(block)
            excess = max(0, self._count - self.capacity)
            self.dropped += excess
            while excess:
                count = min(excess, len(self._blocks[0]))
                if count == len(self._blocks[0]):
                    self._blocks.popleft()
                else:
                    self._blocks[0] = self._blocks[0][count:]
                self._count -= count
                excess -= count
            self._condition.notify()

    def _read(self, count):
        result = np.zeros(count, dtype=np.float32)
        offset = 0
        while self._blocks and offset < count:
            block = self._blocks[0]
            take = min(count - offset, len(block))
            result[offset:offset + take] = block[:take]
            if take == len(block):
                self._blocks.popleft()
            else:
                self._blocks[0] = block[take:]
            self._count -= take
            offset += take
        return result

    def read(self, count):
        """Read immediately, padding an underrun with silence."""
        with self._condition:
            return self._read(count)

    def read_frame(self):
        """Worker-only read; closing the buffer wakes the waiting worker."""
        with self._condition:
            self._condition.wait_for(lambda: self._closed or self._count >= FRAME_SIZE)
            if self._closed:
                return None
            return self._read(FRAME_SIZE)

    def close(self):
        with self._condition:
            self._closed = True
            self._blocks.clear()
            self._count = 0
            self._condition.notify_all()


class MicrophoneProcessor:
    """One worker and native state per live microphone route."""
    def __init__(self, filter_factory=None):
        self.input = MonoBuffer()
        self.output = MonoBuffer()
        self.error = None
        self.timings_ms = deque(maxlen=1000)
        self._stop = threading.Event()
        self._filter = (filter_factory or RNNoiseFilter)()
        self._thread = threading.Thread(target=self._run, name='microphone-filter', daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._filter.close()
            raise

    @property
    def healthy(self):
        return self._thread.is_alive() and self.error is None and not self._stop.is_set()

    @property
    def dropped_samples(self):
        return self.input.dropped + self.output.dropped

    def submit(self, samples):
        if self.error:
            raise RuntimeError(self.error)
        self.input.write(samples)

    def read(self, frames):
        if self.error:
            raise RuntimeError(self.error)
        return self.output.read(frames).reshape(-1, 1)

    def _run(self):
        try:
            while not self._stop.is_set():
                frame = self.input.read_frame()
                if frame is None:
                    break
                started = time.perf_counter()
                result = self._filter.process(frame)
                self.timings_ms.append((time.perf_counter() - started) * 1000)
                if not self._stop.is_set():
                    self.output.write(result)
        except Exception as exc:
            self.error = f'Microphone noise reduction stopped: {exc}'
            self.output.close()
        finally:
            self._filter.close()

    def stop(self):
        self._stop.set()
        self.input.close()
        self._thread.join(timeout=2)
        self.output.close()
        if self._thread.is_alive():
            # The worker alone owns the state: never free it during inference.
            self.error = 'Microphone filter did not stop. Restart Soundboard.'


def filter_recording(samples, cancelled=None, filter_factory=None):
    """Filter an in-memory test take, retaining a short tail to finish speech."""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    padding = (-len(samples)) % FRAME_SIZE + 2 * FRAME_SIZE
    padded = np.pad(samples, (0, padding))
    result = np.empty_like(padded)
    processor = (filter_factory or RNNoiseFilter)()
    try:
        for offset in range(0, len(padded), FRAME_SIZE):
            if cancelled is not None and cancelled.is_set():
                return None
            result[offset:offset + FRAME_SIZE] = processor.process(padded[offset:offset + FRAME_SIZE])
    finally:
        processor.close()
    return result
