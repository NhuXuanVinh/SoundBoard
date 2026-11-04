"""Explicit, memory-only microphone comparison; never uses the live cable route."""
import threading
import time

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QSizePolicy, QSlider, QVBoxLayout,
)

from .audio import device_settings, is_virtual_device
from .noise import SAMPLE_RATE, filter_recording


class TestRecording:
    def __init__(self, microphone, seconds=10):
        self.limit = int(seconds * SAMPLE_RATE)
        self.count = 0
        self.error = None
        self.dropouts = 0
        self._samples = np.zeros(self.limit, dtype=np.float32)
        self._stream = None
        self.started_at = time.monotonic()

        def capture(indata, frames, timing, status):
            try:
                if status:
                    self.dropouts += 1
                take = min(frames, self.limit - self.count)
                self._samples[self.count:self.count + take] = indata[:take, 0]
                self.count += take
            except Exception as exc:
                self.error = str(exc)
                raise sd.CallbackAbort
            if self.count == self.limit:
                raise sd.CallbackStop

        try:
            self._stream = sd.InputStream(device=microphone.index, samplerate=SAMPLE_RATE,
                channels=1, dtype='float32', blocksize=0, latency='low', callback=capture,
                extra_settings=device_settings(microphone))
            self._stream.start()
            self.started_at = time.monotonic()
        except Exception:
            self.close()
            raise

    @property
    def active(self):
        return bool(self._stream and self._stream.active)

    def finish(self):
        self._close_stream()
        result = self._samples[:self.count].copy()
        self._samples = np.empty(0, dtype=np.float32)
        return result

    def _close_stream(self):
        stream, self._stream = self._stream, None
        if stream:
            try:
                stream.abort()
            except Exception as exc:
                self.error = f'Microphone stopped: {exc}'
            try:
                stream.close()
            except Exception as exc:
                self.error = f'Could not close the microphone: {exc}'

    def close(self):
        self._close_stream()
        self._samples = np.empty(0, dtype=np.float32)


class TestPlayback:
    def __init__(self):
        self.gain = 0.5
        self.error = None
        self._stream = None
        self._samples = None
        self._position = 0

    @property
    def active(self):
        return bool(self._stream and self._stream.active)

    def play(self, samples, device):
        self.close()
        if device is None or not device.outputs or is_virtual_device(device):
            raise ValueError('Select physical headphones for the microphone test, not a virtual audio device.')
        self.error = None
        self._samples = samples
        self._position = 0

        def render(outdata, frames, timing, status):
            outdata.fill(0)
            try:
                take = min(frames, len(self._samples) - self._position)
                outdata[:take] = self._samples[self._position:self._position + take, None] * self.gain
                self._position += take
            except Exception as exc:
                self.error = str(exc)
                raise sd.CallbackAbort
            if self._position == len(self._samples):
                raise sd.CallbackStop

        try:
            self._stream = sd.OutputStream(device=device.index, samplerate=SAMPLE_RATE,
                channels=min(2, device.outputs), dtype='float32', blocksize=0, latency='low',
                callback=render, extra_settings=device_settings(device))
            self._stream.start()
        except Exception:
            self.close()
            raise

    def close(self):
        stream, self._stream = self._stream, None
        if stream:
            try:
                stream.abort()
            except Exception as exc:
                self.error = f'Headphone output stopped: {exc}'
            try:
                stream.close()
            except Exception as exc:
                self.error = f'Could not close headphone output: {exc}'
        self._samples = None


class FilterTake(QThread):
    ready = Signal(object)
    error = Signal(str)

    def __init__(self, samples, parent):
        super().__init__(parent)
        self.samples = samples
        self.cancelled = threading.Event()

    def run(self):
        try:
            result = filter_recording(self.samples, self.cancelled)
            if result is not None and not self.cancelled.is_set():
                self.ready.emit(result)
        except Exception as exc:
            if not self.cancelled.is_set():
                self.error.emit(str(exc))
        finally:
            self.samples = None


class MicrophoneTestDialog(QDialog):
    def __init__(self, microphone, available_devices, preferred_output=None, parent=None):
        super().__init__(parent)
        self.microphone = microphone
        self.recording = None
        self.player = TestPlayback()
        self.task = None
        self.original = None
        self.filtered = None
        self.closing = False
        self.processing = False
        self._was_playing = False
        self.setWindowTitle('Test microphone noise reduction')
        self.setMinimumWidth(500)
        self.resize(560, 660)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        title = QLabel('Hear the difference')
        title.setObjectName('hero')
        layout.addWidget(title)
        instructions = QLabel('Record up to 10 seconds. Speak normally, pause, then type while speaking. '
                              'Compare both versions using headphones.\n\n'
                              'The sample stays in memory and is discarded when you close this window. '
                              'Nothing is sent to your voice app.')
        instructions.setWordWrap(True)
        instructions.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        instructions.setMinimumHeight(instructions.fontMetrics().lineSpacing() * 7)
        instructions.setObjectName('muted')
        layout.addWidget(instructions)
        source = QLabel('Microphone: ' + microphone.name)
        source.setTextFormat(Qt.TextFormat.PlainText)
        source.setWordWrap(True)
        layout.addWidget(source)
        layout.addWidget(QLabel('Listen through'))
        self.output = QComboBox()
        self.output.setAccessibleName('Microphone test headphone output')
        for device in available_devices:
            if device.outputs and not is_virtual_device(device):
                self.output.addItem(device.label, device)
        chosen = next((i for i in range(self.output.count()) if preferred_output and
                       self.output.itemData(i).key == preferred_output.key), None)
        if chosen is None:
            chosen = next((i for i in range(self.output.count())
                           if 'headphone' in self.output.itemData(i).name.lower()), 0)
        self.output.setCurrentIndex(chosen if self.output.count() else -1)
        self.output.setPlaceholderText('Connect headphones, then reopen this test')
        self.output.currentIndexChanged.connect(self.stop_playback)
        layout.addWidget(self.output)
        self.record_button = QPushButton('Record 10 seconds')
        self.record_button.setObjectName('primary')
        self.record_button.clicked.connect(self.toggle_recording)
        layout.addWidget(self.record_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        playback = QHBoxLayout()
        self.original_button = QPushButton('▶ Original')
        self.original_button.clicked.connect(lambda: self.play(False))
        self.filtered_button = QPushButton('▶ Filtered')
        self.filtered_button.clicked.connect(lambda: self.play(True))
        self.stop_button = QPushButton('Stop playback')
        self.stop_button.clicked.connect(self.stop_playback)
        for control in (self.original_button, self.filtered_button, self.stop_button):
            playback.addWidget(control)
        layout.addLayout(playback)
        self.volume_label = QLabel('Playback volume: 50%')
        layout.addWidget(self.volume_label)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(50)
        self.volume.setAccessibleName('Microphone test playback volume')
        self.volume.valueChanged.connect(self.set_volume)
        layout.addWidget(self.volume)
        self.status = QLabel('Ready. Recording starts only when you press Record.')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close_button = QPushButton('Close and discard sample')
        close_button.clicked.connect(self.reject)
        layout.addWidget(close_button)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.update_controls()

    def set_volume(self, value):
        self.player.gain = value / 100
        self.volume_label.setText(f'Playback volume: {value}%')

    def update_controls(self):
        busy = self.recording is not None or self.processing
        has_output = self.output.currentData() is not None
        self.record_button.setEnabled(not self.processing)
        self.record_button.setText('Stop recording' if self.recording else 'Record 10 seconds')
        self.output.setEnabled(not busy)
        self.original_button.setEnabled(not busy and has_output and self.original is not None)
        self.filtered_button.setEnabled(not busy and has_output and self.filtered is not None)
        self.stop_button.setEnabled(self.player.active)

    def toggle_recording(self):
        if self.recording:
            self.finish_recording()
            return
        if self.processing:
            return
        self.stop_playback()
        self.original = self.filtered = None
        self.progress.setValue(0)
        try:
            self.recording = TestRecording(self.microphone)
            self.status.setText('Recording… speak, pause, and try typing.')
        except Exception as exc:
            self.status.setText(f'Could not record: {exc}. Check microphone access and device availability.')
        self.update_controls()

    def finish_recording(self):
        recording = self.recording
        self.recording = None
        try:
            self.original = recording.finish()
        except Exception as exc:
            self.status.setText(f'Could not finish recording: {exc}')
            self.update_controls()
            return
        if recording.error or not len(self.original):
            self.status.setText(recording.error or 'No audio was captured. Try again.')
            self.original = None
        elif recording.dropouts:
            self.status.setText('Audio dropped out during the test. Try recording again.')
            self.original = None
        else:
            self.processing = True
            self.status.setText('Preparing the filtered version…')
            if self.task:
                self.task.wait()
                self.task.deleteLater()
            self.task = FilterTake(self.original, self)
            self.task.ready.connect(self.filtered_ready)
            self.task.error.connect(self.filter_failed)
            self.task.start()
        self.update_controls()

    def filtered_ready(self, samples):
        if self.closing:
            return
        self.filtered = samples
        self.processing = False
        self.status.setText('Compare Original and Filtered. Listen for clear quiet words and reduced fan noise or typing.')
        self.update_controls()

    def filter_failed(self, message):
        if self.closing:
            return
        self.processing = False
        self.status.setText(message)
        self.update_controls()

    def play(self, filtered):
        if self.recording or self.processing or self.closing:
            return
        samples = self.filtered if filtered else self.original
        if samples is None:
            return
        try:
            self.player.play(samples, self.output.currentData())
            self._was_playing = True
            self.status.setText('Playing filtered microphone.' if filtered else 'Playing original microphone.')
        except Exception as exc:
            self.status.setText(f'Could not play the sample: {exc}')
        self.update_controls()

    def stop_playback(self, *args):
        if self._was_playing and hasattr(self, 'status'):
            self.status.setText('Playback stopped.')
        self.player.close()
        self._was_playing = False
        if hasattr(self, 'stop_button'):
            self.update_controls()

    def tick(self):
        if self.recording:
            self.progress.setValue(int(self.recording.count / self.recording.limit * 1000))
            elapsed = time.monotonic() - self.recording.started_at
            if (self.recording.count == self.recording.limit or self.recording.error
                    or elapsed >= self.recording.limit / SAMPLE_RATE + 0.5):
                self.finish_recording()
            elif not self.recording.active:
                self.recording.close()
                self.recording = None
                self.status.setText('Microphone stopped before the test finished. Check the device and try again.')
                self.update_controls()
        if self._was_playing and not self.player.active:
            error = self.player.error
            self.stop_playback()
            self.status.setText(f'Playback stopped: {error}' if error else 'Playback finished. Try the other version.')

    def done(self, result):
        self.closing = True
        self.timer.stop()
        if self.recording:
            self.recording.close()
            self.recording = None
        self.player.close()
        if self.task:
            self.task.cancelled.set()
            self.task.wait()
        self.original = self.filtered = None
        super().done(result)
