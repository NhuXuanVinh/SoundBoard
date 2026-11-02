"""Audio decoding, bounded mixing, and PortAudio device routing.

File I/O and resampling happen on a worker, never in an audio callback.
"""
from collections import OrderedDict, deque
from dataclasses import dataclass
from math import gcd
from pathlib import Path
import threading

import numpy as np
from scipy.signal import resample_poly
import sounddevice as sd
import soundfile as sf


MAX_CLIP_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    api: str
    inputs: int
    outputs: int
    rate: int

    @property
    def key(self):
        return f"{self.api}|{self.name}"

    @property
    def label(self):
        return f"{self.name}  [{self.api}]"


def devices():
    apis = sd.query_hostapis()
    result = [Device(i, d['name'], apis[d['hostapi']]['name'],
                     d['max_input_channels'], d['max_output_channels'],
                     int(d['default_samplerate']))
              for i, d in enumerate(sd.query_devices())]
    # WASAPI gives full endpoint names and avoids duplicate MME/DirectSound entries.
    wasapi = [d for d in result if d.api == 'Windows WASAPI']
    return wasapi or result


def audio_info(path):
    info = sf.info(str(path))
    if info.frames < 1:
        raise ValueError('This audio file is empty.')
    if info.channels not in (1, 2):
        raise ValueError('Use a mono or stereo file; surround audio is not supported.')
    if info.duration > 300 or info.frames * 8 > MAX_CLIP_BYTES:
        raise ValueError('Use a clip under 5 minutes and 128 MB of decoded stereo audio.')
    return info


class AudioCache:
    """A worker-owned LRU cache, capped by decoded size."""
    def __init__(self, limit=256 * 1024 * 1024):
        self.limit = limit
        self.items = OrderedDict()
        self.size = 0

    def load(self, path, rate):
        path = Path(path)
        key = (str(path.resolve()), path.stat().st_mtime_ns, rate)
        if key in self.items:
            self.items.move_to_end(key)
            return self.items[key]
        info = audio_info(path)
        if int(info.duration * rate + 1) * 8 > MAX_CLIP_BYTES:
            raise ValueError('This clip is too large at the selected output sample rate.')
        samples, source_rate = sf.read(str(path), dtype='float32', always_2d=True)
        if source_rate != rate:
            divisor = gcd(source_rate, rate)
            samples = resample_poly(samples, rate // divisor, source_rate // divisor, axis=0)
        if samples.shape[1] == 1:
            samples = np.repeat(samples, 2, axis=1)
        samples = np.ascontiguousarray(samples, dtype=np.float32)
        np.nan_to_num(samples, copy=False, nan=0, posinf=1, neginf=-1)
        np.clip(samples, -1, 1, out=samples)
        while self.items and self.size + samples.nbytes > self.limit:
            _, old = self.items.popitem(last=False)
            self.size -= old.nbytes
        if samples.nbytes <= self.limit:
            self.items[key] = samples
            self.size += samples.nbytes
        return samples


@dataclass
class Voice:
    samples: np.ndarray
    gain: float = 1.0
    loop: bool = False
    position: int = 0


class Mixer:
    def __init__(self):
        self._lock = threading.Lock()
        self._voices = {}
        self.sound_gain = 0.75
        self.mic_gain = 1.0
        self.peak = 0.0
        self.clipped = False

    def play(self, clip_id, samples, gain=1.0, loop=False):
        if len(samples) == 0:
            raise ValueError('Cannot play an empty clip.')
        with self._lock:
            if len(self._voices) >= 32 and clip_id not in self._voices:
                raise ValueError('A maximum of 32 pads can play at once.')
            self._voices[clip_id] = Voice(samples, gain, loop)

    def update(self, clip_id, gain, loop):
        with self._lock:
            if clip_id in self._voices:
                self._voices[clip_id].gain = gain
                self._voices[clip_id].loop = loop

    def stop(self, clip_id=None):
        with self._lock:
            if clip_id is None:
                self._voices.clear()
            else:
                self._voices.pop(clip_id, None)

    def progress(self):
        with self._lock:
            return {key: voice.position / len(voice.samples)
                    for key, voice in self._voices.items()}

    def render(self, frames, microphone=None):
        sound = np.zeros((frames, 2), dtype=np.float32)
        with self._lock:
            for key, voice in list(self._voices.items()):
                written = 0
                while written < frames:
                    count = min(frames - written, len(voice.samples) - voice.position)
                    sound[written:written + count] += (
                        voice.samples[voice.position:voice.position + count] * voice.gain)
                    written += count
                    voice.position += count
                    if voice.position == len(voice.samples):
                        if voice.loop:
                            voice.position = 0
                        else:
                            del self._voices[key]
                            break
        sound *= self.sound_gain
        mixed = sound.copy()
        if microphone is not None:
            mixed += microphone * self.mic_gain
        self.peak = float(np.max(np.abs(mixed))) if frames else 0.0
        self.clipped = self.peak > 1.0
        np.clip(mixed, -1, 1, out=mixed)
        np.clip(sound, -1, 1, out=sound)
        return mixed, sound


class SampleQueue:
    """Small stereo FIFO between independently clocked devices.

    Bound latency by dropping the oldest samples when capture runs ahead.
    Underruns are filled with silence; this is a practical shared-mode bridge,
    not a clock-synchronizing studio audio engine.
    """
    def __init__(self, max_frames):
        self.max_frames = max_frames
        self._blocks = deque()
        self._frames = 0
        self._lock = threading.Lock()

    def push(self, samples):
        block = samples[-self.max_frames:].copy()
        if block.shape[1] == 1:
            block = np.repeat(block, 2, axis=1)
        with self._lock:
            self._blocks.append(block)
            self._frames += len(block)
            excess = self._frames - self.max_frames
            while excess > 0:
                first = self._blocks[0]
                count = min(len(first), excess)
                if count == len(first):
                    self._blocks.popleft()
                else:
                    self._blocks[0] = first[count:]
                excess -= count
                self._frames -= count

    def read(self, frames):
        result = np.zeros((frames, 2), dtype=np.float32)
        with self._lock:
            offset = 0
            while self._blocks and offset < frames:
                first = self._blocks[0]
                count = min(len(first), frames - offset)
                result[offset:offset + count] = first[:count]
                if count == len(first):
                    self._blocks.popleft()
                else:
                    self._blocks[0] = first[count:]
                self._frames -= count
                offset += count
        return result


class AudioEngine:
    def __init__(self):
        self.mixer = Mixer()
        self.rate = 48000
        self._streams = []
        self._output = None
        self.last_error = None
        self.xruns = 0

    @property
    def running(self):
        return bool(self._output and self._output.active)

    @property
    def healthy(self):
        return bool(self._streams) and all(stream.active for stream in self._streams)

    def start(self, output, microphone=None, monitor=None):
        self.stop()
        if monitor and (monitor.index == output.index or monitor.name == output.name):
            raise ValueError('Choose a different device for headphone monitoring.')
        self.rate = output.rate
        self.last_error = None
        self.xruns = 0
        mic_queue = SampleQueue(int(self.rate * 0.12))
        monitor_queue = SampleQueue(int(self.rate * 0.12))
        channels = min(2, output.outputs)

        def capture(indata, frames, timing, status):
            try:
                if status:
                    self.xruns += 1
                mic_queue.push(indata)
            except Exception as exc:
                self.last_error = str(exc)
                raise sd.CallbackAbort

        def render(outdata, frames, timing, status):
            try:
                if status:
                    self.xruns += 1
                mixed, sounds = self.mixer.render(
                    frames, mic_queue.read(frames) if microphone else None)
                outdata[:] = mixed if channels == 2 else mixed.mean(axis=1, keepdims=True)
                if monitor:
                    monitor_queue.push(sounds)
            except Exception as exc:
                outdata.fill(0)
                self.last_error = str(exc)
                raise sd.CallbackAbort

        def listen(outdata, frames, timing, status):
            try:
                if status:
                    self.xruns += 1
                samples = monitor_queue.read(frames)
                outdata[:] = samples if outdata.shape[1] == 2 else samples.mean(axis=1, keepdims=True)
            except Exception as exc:
                outdata.fill(0)
                self.last_error = str(exc)
                raise sd.CallbackAbort

        common = dict(samplerate=self.rate, dtype='float32', blocksize=0, latency='low')

        def settings(device):
            # Shared-mode Windows conversion also supports headsets whose native
            # capture rate differs from the virtual cable's playback rate.
            return sd.WasapiSettings(auto_convert=True) if device.api == 'Windows WASAPI' else None

        try:
            if microphone:
                sd.check_input_settings(device=microphone.index, channels=1,
                                        dtype='float32', samplerate=self.rate,
                                        extra_settings=settings(microphone))
                self._streams.append(sd.InputStream(device=microphone.index, channels=1,
                                                     callback=capture,
                                                     extra_settings=settings(microphone), **common))
            if monitor:
                self._streams.append(sd.OutputStream(device=monitor.index,
                                                     channels=min(2, monitor.outputs),
                                                     callback=listen,
                                                     extra_settings=settings(monitor), **common))
            self._output = sd.OutputStream(device=output.index, channels=channels,
                                           callback=render, extra_settings=settings(output), **common)
            self._streams.append(self._output)
            for stream in self._streams:
                stream.start()
        except Exception:
            self.stop()
            raise

    def stop(self):
        self.mixer.stop()
        for stream in reversed(self._streams):
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self._streams.clear()
        self._output = None
        self.mixer.peak = 0
