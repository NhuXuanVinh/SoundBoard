import math
from pathlib import Path
import sys

import numpy as np
import soundfile as sf
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QVBoxLayout, QWidget,
)

from .audio import AudioCache, AudioEngine, audio_info, devices
from .hotkeys import (
    DEFAULT_STOP, STOP_ACTION, GlobalHotkeys, ShortcutError,
    assign_missing_shortcuts, compile_bindings,
)
from .library import DATA_DIR, load_state, new_clip, save_state
from .mic_test import MicrophoneTestDialog
from .shortcut_editor import ShortcutEditor


STYLE = '''
QWidget { color: #e7e9f3; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow, QWidget#root { background: #101219; }
QLabel { background: transparent; }
QLabel#brand { color: #c2adff; font-size: 14px; font-weight: 800; letter-spacing: 3px; }
QLabel#hero { font-size: 30px; font-weight: 700; }
QLabel#muted { color: #959aaf; }
QLabel#section { color: #959aaf; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
QLabel#title { font-size: 17px; font-weight: 600; }
QLabel#badge { color: #86e0c4; background: #1b302d; padding: 7px 12px; border-radius: 12px; }
QLabel#notice { color: #c5c9dd; background: #202433; border: 1px solid #33394f; border-radius: 9px; padding: 12px; }
QLabel#status { color: #a9afc6; padding: 6px 0; }
QFrame#panel { background: #181b25; border: 1px solid #2a2e3d; border-radius: 14px; }
QFrame#pad { background: #1c202c; border: 1px solid #303648; border-radius: 12px; }
QFrame#pad[playing="true"] { background: #242039; border: 1px solid #af90ff; }
QPushButton { background: #282d3e; border: 1px solid #3a4055; border-radius: 7px; padding: 9px 14px; font-weight: 600; }
QPushButton:hover { background: #343a50; border-color: #6c7089; }
QPushButton:pressed { background: #424965; }
QPushButton:disabled { color: #73798f; background: #222634; border-color: #2c3040; }
QPushButton#primary { color: #181124; background: #b99bff; border-color: #b99bff; }
QPushButton#primary:hover { background: #ccb6ff; }
QPushButton#danger { color: #ffb8c1; background: #38232e; border-color: #623643; }
QPushButton#play { text-align: left; background: transparent; border: none; font-size: 17px; padding: 10px 0; }
QPushButton#play:hover { color: #c9b3ff; }
QPushButton#more { border: none; background: transparent; padding: 0; font-size: 20px; }
QLineEdit, QComboBox { background: #12151e; border: 1px solid #34394d; border-radius: 7px; padding: 9px; selection-background-color: #7259ad; }
QLineEdit:focus, QComboBox:focus { border-color: #b99bff; }
QComboBox:disabled { color: #777d92; }
QComboBox QAbstractItemView { background: #222635; selection-background-color: #4d3b73; padding: 4px; }
QComboBox::drop-down { border: none; width: 22px; }
QSlider::groove:horizontal { height: 4px; background: #383e52; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #b99bff; border-radius: 2px; }
QSlider::handle:horizontal { background: #ded2ff; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
QCheckBox { spacing: 8px; color: #bbc0d2; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #58617c; background: #131722; border-radius: 4px; }
QCheckBox::indicator:checked { background: #b99bff; border-color: #b99bff; image: none; }
QProgressBar { background: #30364a; border: none; border-radius: 2px; max-height: 4px; }
QProgressBar::chunk { background: #aa8aef; border-radius: 2px; }
QProgressBar#meter { max-height: 7px; }
QProgressBar#meter::chunk { background: #82dfbd; }
QScrollArea { border: none; background: transparent; }
QWidget#padContainer, QWidget#shortcutRows { background: transparent; }
QScrollBar:vertical { background: #181b25; width: 7px; }
QScrollBar::handle:vertical { background: #424962; border-radius: 3px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QMenu { background: #252a3b; border: 1px solid #444c66; padding: 5px; }
QMenu::item { padding: 8px 22px; }
QMenu::item:selected { background: #4d3b73; }
QToolTip { color: #e7e9f3; background: #2d3347; border: 1px solid #59647c; }
QMessageBox, QInputDialog, QDialog { background: #181b25; }
'''


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback, name=None):
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(callback)
    return widget


def duration_text(seconds):
    if seconds < 60:
        return f'{seconds:.1f}s'
    return f'{int(seconds) // 60}:{int(seconds) % 60:02d}'


class TaskSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    done = Signal()


class Task(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = TaskSignals()

    def run(self):
        try:
            self.signals.result.emit(self.function())
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.done.emit()


class Pad(QFrame):
    def __init__(self, clip, index, window):
        super().__init__()
        self.clip = clip
        self.setObjectName('pad')
        self.setMinimumWidth(210)
        self.setFixedHeight(185)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(9)
        top = QHBoxLayout()
        hotkey = str(clip.get('shortcut') or 'No shortcut')
        self.shortcut_label = label(hotkey, 'muted')
        self.shortcut_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        unavailable = window.hotkeys.failed.get(clip['id']) if window.hotkeys else None
        self.shortcut_label.setToolTip(unavailable or hotkey)
        if unavailable:
            self.shortcut_label.setText(hotkey + ' (unavailable)')
        top.addWidget(self.shortcut_label, 1)
        top.addStretch()
        more = button('···', lambda: self.menu(window), 'more')
        more.setAccessibleName(f'Options for {clip["name"]}')
        more.setFixedSize(28, 22)
        top.addWidget(more)
        layout.addLayout(top)
        self.play = button('▶  ' + clip['name'], lambda: window.trigger(clip['id']), 'play')
        self.play.setToolTip(clip['path'])
        self.play.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.play.setMinimumWidth(0)
        self.play.setAccessibleName(f'Play or stop {clip["name"]}')
        layout.addWidget(self.play)
        self.detail = label(f'{duration_text(clip["duration"])}  ·  {Path(clip["path"]).suffix[1:].upper()}', 'muted')
        layout.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        bottom = QHBoxLayout()
        loop = QCheckBox('Loop')
        loop.setChecked(clip['loop'])
        loop.toggled.connect(lambda value: window.update_clip(clip, loop=value))
        bottom.addWidget(loop)
        bottom.addSpacing(12)
        gain = QSlider(Qt.Orientation.Horizontal)
        gain.setRange(0, 150)
        gain.setValue(round(clip['gain'] * 100))
        gain.setAccessibleName(f'{clip["name"]} volume')
        gain.setToolTip('Pad volume (0–150%)')
        gain.valueChanged.connect(lambda value: window.update_clip(clip, gain=value / 100))
        bottom.addWidget(gain)
        layout.addLayout(bottom)
        self._playing = False

    def menu(self, window):
        menu = QMenu(self)
        menu.addAction('Edit shortcut...', lambda: window.edit_shortcuts(self.clip['id']))
        menu.addAction('Rename', lambda: window.rename_clip(self.clip))
        menu.addAction('Remove from board', lambda: window.remove_clip(self.clip))
        menu.exec(self.mapToGlobal(self.rect().topRight()))

    def set_playing(self, progress, pending=False):
        playing = progress is not None
        if playing != self._playing:
            self._playing = playing
            self.setProperty('playing', playing)
            self.style().unpolish(self)
            self.style().polish(self)
        prefix = '…  ' if pending else ('■  ' if playing else '▶  ')
        self.play.setText(prefix + self.clip['name'])
        self.progress.setValue(int((progress or 0) * 1000))


class MainWindow(QMainWindow):
    def __init__(self, data_dir=DATA_DIR, register_hotkeys=True):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / 'library.json'
        self.state, warning = load_state(self.state_path)
        self.clips = self.state.get('clips', [])
        self.stop_shortcut = self.state.get('stop_shortcut', DEFAULT_STOP)
        assign_missing_shortcuts(self.clips, self.stop_shortcut)
        self.engine = AudioEngine()
        self.cache = AudioCache()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.tasks = set()
        self.pending = {}
        self.sequence = 0
        self.cards = {}
        self.connected = False
        self.closing = False
        self.columns = 0
        self.hotkeys = None
        self.setWindowTitle('Soundboard — Your sounds. On cue.')
        self.resize(1220, 830)
        self.setMinimumSize(1060, 740)
        self.setAcceptDrops(True)
        self.setStyleSheet(STYLE)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(400)
        self.save_timer.timeout.connect(self.persist)
        self.build_ui()
        self.refresh_devices(initial=True)
        self.render_cards()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(50)
        self.escape = QShortcut(QKeySequence('Escape'), self)
        self.escape.activated.connect(self.stop_sounds)
        if register_hotkeys:
            self.hotkeys = GlobalHotkeys(QApplication.instance(), self.activate_shortcut)
            self.sync_shortcuts()
            self.render_cards()
        if warning:
            self.notice(warning)

    def build_ui(self):
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 24, 28, 16)
        outer.setSpacing(20)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(8)
        titles.addWidget(label('◒  SOUNDBOARD', 'brand'))
        titles.addWidget(label('Your sounds. On cue.', 'hero'))
        titles.addWidget(label('A little personality for every conversation.', 'muted'))
        header.addLayout(titles)
        header.addStretch()
        badge = label('●  LOCAL AUDIO', 'badge')
        badge.setFixedHeight(32)
        header.addWidget(badge)
        header.addSpacing(10)
        header.addWidget(button('Routing guide ↗', self.show_guide))
        outer.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(22)
        left = QVBoxLayout()
        left.setSpacing(16)
        toolbar = QHBoxLayout()
        self.count_label = label('YOUR SOUNDS', 'section')
        toolbar.addWidget(self.count_label)
        toolbar.addStretch()
        toolbar.addWidget(button('■  Stop sounds', self.stop_sounds, 'danger'))
        toolbar.addWidget(button('+  Add sounds', self.choose_files, 'primary'))
        left.addLayout(toolbar)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search your sounds…')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.render_cards)
        left.addWidget(self.search)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pad_container = QWidget()
        self.pad_container.setObjectName('padContainer')
        self.grid = QGridLayout(self.pad_container)
        self.grid.setContentsMargins(0, 0, 5, 0)
        self.grid.setSpacing(14)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.pad_container)
        left.addWidget(self.scroll, 1)
        self.empty = QFrame()
        self.empty.setObjectName('panel')
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setContentsMargins(38, 38, 38, 38)
        empty_layout.addStretch()
        icon = label('♫', 'hero')
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(icon)
        self.empty_title = label('Make some noise.', 'hero')
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        self.empty_text = label('Drop audio files here to create your first sound pads.\nWAV, MP3, FLAC, OGG, AIFF · up to 5 minutes per clip', 'muted', True)
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_text)
        empty_layout.addSpacing(18)
        self.empty_add = button('Choose audio files', self.choose_files, 'primary')
        empty_layout.addWidget(self.empty_add, alignment=Qt.AlignmentFlag.AlignCenter)
        self.demo_button = button('Try three demo sounds', self.add_demos)
        empty_layout.addWidget(self.demo_button, alignment=Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch()
        left.addWidget(self.empty, 1)
        shortcuts = QHBoxLayout()
        self.shortcut_hint = label('', 'muted', True)
        self.refresh_shortcut_hint()
        shortcuts.addWidget(self.shortcut_hint, 1)
        shortcuts.addWidget(button('Shortcuts...', lambda: self.edit_shortcuts()))
        left.addLayout(shortcuts)
        body.addLayout(left, 1)

        panel = QFrame()
        panel.setObjectName('panel')
        self.routing_scroll = QScrollArea()
        self.routing_scroll.setFixedWidth(340)
        self.routing_scroll.setWidgetResizable(True)
        self.routing_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.routing_scroll.setWidget(panel)
        routing = QVBoxLayout(panel)
        routing.setContentsMargins(20, 20, 20, 20)
        routing.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(label('AUDIO ROUTING', 'section'))
        heading.addStretch()
        self.refresh_button = button('↻', self.refresh_devices)
        self.refresh_button.setToolTip('Refresh audio device list')
        self.refresh_button.setAccessibleName('Refresh audio devices')
        heading.addWidget(self.refresh_button)
        routing.addLayout(heading)
        routing.addWidget(label('Send sounds to', 'title'))
        self.output = QComboBox()
        self.output.setAccessibleName('Soundboard output device')
        self.output.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        routing.addWidget(self.output)
        self.route_hint = label('Choose CABLE Input to send audio into a virtual microphone.', 'muted', True)
        routing.addWidget(self.route_hint)
        self.route_button = button('Start output', self.toggle_route, 'primary')
        routing.addWidget(self.route_button)
        self.sound_slider = self.add_slider(routing, 'Sound volume', self.safe_level('sound_gain', 75), self.set_sound_gain)
        routing.addSpacing(5)
        self.mic_enabled = QCheckBox('Mix in my microphone')
        self.mic_enabled.setToolTip('Combine your real microphone with sound pads in the selected output.')
        routing.addWidget(self.mic_enabled)
        self.microphone = QComboBox()
        self.microphone.setAccessibleName('Physical microphone')
        routing.addWidget(self.microphone)
        self.mic_slider = self.add_slider(routing, 'Mic volume', self.safe_level('mic_gain', 100), self.set_mic_gain)
        self.mic_noise_reduction = QCheckBox('Reduce microphone noise')
        self.mic_noise_reduction.setChecked(self.state.get('mic_noise_reduction', True) is not False)
        self.mic_noise_reduction.setToolTip('Reduce fan noise and typing on your microphone only. '
                                          'Disconnect output to change this setting.')
        routing.addWidget(self.mic_noise_reduction)
        routing.addWidget(label('Filters your mic only. Turn off Discord’s noise suppression, '
                                'echo cancellation, and automatic gain.', 'muted', True))
        self.mic_test_button = button('Test microphone…', self.test_microphone)
        routing.addWidget(self.mic_test_button)
        self.monitor_enabled = QCheckBox('Hear sounds in my headphones')
        self.monitor_enabled.setToolTip('Monitor sound pads only. Your microphone is not played back to you.')
        routing.addWidget(self.monitor_enabled)
        self.monitor = QComboBox()
        self.monitor.setAccessibleName('Headphone monitoring device')
        routing.addWidget(self.monitor)
        routing.addWidget(label('Headphone monitoring plays sound pads only.', 'muted', True))
        routing.addStretch()
        routing.addWidget(label('OUTPUT LEVEL', 'section'))
        self.meter = QProgressBar()
        self.meter.setObjectName('meter')
        self.meter.setRange(0, 100)
        self.meter.setValue(0)
        self.meter.setTextVisible(False)
        routing.addWidget(self.meter)
        self.level_text = label('Output stopped', 'muted')
        routing.addWidget(self.level_text)
        body.addWidget(self.routing_scroll)
        outer.addLayout(body, 1)
        self.banner = label('', 'notice', True)
        self.banner.hide()
        outer.addWidget(self.banner)
        footer = QHBoxLayout()
        self.status = label('Ready when you are. Add sounds or try the demo pads.', 'status')
        footer.addWidget(self.status, 1)
        footer.addWidget(label('Files stay on your computer', 'muted'))
        outer.addLayout(footer)
        for combo in (self.output, self.microphone, self.monitor):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            combo.currentIndexChanged.connect(self.device_changed)
        self.mic_enabled.toggled.connect(self.device_changed)
        self.mic_noise_reduction.toggled.connect(self.device_changed)
        self.monitor_enabled.toggled.connect(self.device_changed)
        self.set_sound_gain(self.sound_slider.value())
        self.set_mic_gain(self.mic_slider.value())

    def safe_level(self, key, default):
        try:
            return max(0, min(100, int(self.state.get(key, default))))
        except (ValueError, TypeError):
            return default

    def add_slider(self, layout, title, initial, callback):
        row = QHBoxLayout()
        row.addWidget(label(title, 'muted'))
        row.addStretch()
        value = label(f'{initial}%', 'muted')
        row.addWidget(value)
        layout.addLayout(row)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(initial)
        slider.setAccessibleName(title)
        slider.valueChanged.connect(lambda number: value.setText(f'{number}%'))
        slider.valueChanged.connect(callback)
        layout.addWidget(slider)
        return slider

    def set_sound_gain(self, value):
        self.engine.mixer.sound_gain = value / 100
        self.save_timer.start()

    def set_mic_gain(self, value):
        self.engine.mixer.mic_gain = value / 100
        self.save_timer.start()

    def notice(self, text):
        self.banner.setText(text)
        self.banner.setVisible(bool(text))

    def refresh_devices(self, checked=False, initial=False):
        if self.connected:
            self.disconnect_route()
        previous = {}
        for name, combo in [('output', self.output), ('microphone', self.microphone), ('monitor', self.monitor)]:
            item = combo.currentData()
            previous[name] = item.key if item else self.state.get(name)
        try:
            available = devices()
        except Exception as exc:
            available = []
            self.notice(f'Could not list audio devices: {exc}')
        for name, combo in [('output', self.output), ('microphone', self.microphone), ('monitor', self.monitor)]:
            combo.blockSignals(True)
            combo.clear()
            choices = [d for d in available if (d.inputs if name == 'microphone' else d.outputs)]
            for device in choices:
                combo.addItem(device.label, device)
                combo.setItemData(combo.count() - 1, device.label, Qt.ItemDataRole.ToolTipRole)
            preferred = next((i for i, d in enumerate(choices) if d.key == previous[name]), None)
            if preferred is None and previous[name]:
                combo.setCurrentIndex(-1)
                combo.setPlaceholderText('Saved device unavailable — choose one')
            elif preferred is not None:
                combo.setCurrentIndex(preferred)
            elif name == 'output':
                cable = next((i for i, d in enumerate(choices) if 'cable input' in d.name.lower()), 0)
                combo.setCurrentIndex(cable if choices else -1)
            else:
                physical = next((i for i, d in enumerate(choices) if not any(
                    term in d.name.lower() for term in ('cable', 'voicemeeter', 'stereo mix'))), 0)
                combo.setCurrentIndex(physical if choices else -1)
            if not choices:
                combo.setPlaceholderText('No audio devices found')
            combo.blockSignals(False)
        self.device_changed()
        if not available:
            self.notice('No audio devices are available. Connect an audio device, then refresh. You can still build your sound library.')

    def device_changed(self, *args):
        self.microphone.setEnabled(self.mic_enabled.isChecked() and not self.connected)
        self.monitor.setEnabled(self.monitor_enabled.isChecked() and not self.connected)
        self.mic_slider.setEnabled(self.mic_enabled.isChecked())
        self.mic_noise_reduction.setEnabled(self.mic_enabled.isChecked() and not self.connected)
        self.mic_test_button.setEnabled(self.microphone.currentData() is not None and not self.connected)
        output = self.output.currentData()
        for combo in (self.output, self.microphone, self.monitor):
            device = combo.currentData()
            combo.setToolTip(device.label if device else 'Select an audio device')
        virtual = output and any(s in output.name.lower() for s in ('cable', 'voicemeeter', 'virtual'))
        self.route_hint.setText('In your voice app, select the matching virtual recording device (for VB-CABLE: CABLE Output).'
                                if virtual else 'For microphone input in another app, choose a virtual playback device such as CABLE Input.')
        self.save_timer.start()

    def toggle_route(self):
        if self.connected:
            self.disconnect_route()
        else:
            self.connect_route()

    def test_microphone(self):
        microphone = self.microphone.currentData()
        if self.connected or microphone is None:
            return
        try:
            dialog = MicrophoneTestDialog(microphone, devices(), self.monitor.currentData(), self)
            dialog.exec()
            dialog.deleteLater()
        except Exception as exc:
            self.notice(f'Could not open the microphone test: {exc}')

    def connect_route(self):
        output = self.output.currentData()
        microphone = self.microphone.currentData() if self.mic_enabled.isChecked() else None
        monitor = self.monitor.currentData() if self.monitor_enabled.isChecked() else None
        if not output:
            self.notice('Select an output device first. Install VB-CABLE if you need a virtual microphone.')
            return False
        if self.mic_enabled.isChecked() and not microphone:
            self.notice('Select your physical microphone or turn off microphone mixing.')
            return False
        if self.monitor_enabled.isChecked() and not monitor:
            self.notice('Select your headphones or turn off headphone monitoring.')
            return False
        if microphone and 'cable' in output.name.lower():
            source = microphone.name.lower().replace('output', 'input')
            if source == output.name.lower():
                self.notice('Choose your physical microphone. The selected cable feeds back into itself.')
                return False
        try:
            self.engine.start(output, microphone, monitor,
                              mic_noise_reduction=self.mic_noise_reduction.isChecked())
        except Exception as exc:
            self.notice(f'Could not start audio: {exc}. Check device availability and microphone access. If formats differ, set devices to the same sample rate in Windows Sound settings.')
            return False
        self.connected = True
        self.route_button.setText('Disconnect output')
        for control in (self.output, self.microphone, self.monitor, self.mic_enabled,
                        self.monitor_enabled, self.mic_noise_reduction, self.mic_test_button):
            control.setEnabled(False)
        self.notice('')
        filter_note = ' · Mic noise reduction on' if microphone and self.mic_noise_reduction.isChecked() else ''
        self.status.setText(f'Connected · {output.name} · {self.engine.rate:,} Hz{filter_note}')
        return True

    def disconnect_route(self):
        self.pending.clear()
        self.engine.stop()
        self.connected = False
        self.route_button.setText('Start output')
        for control in (self.output, self.mic_enabled, self.monitor_enabled):
            control.setEnabled(True)
        self.device_changed()
        self.status.setText('Output disconnected. Sounds and microphone are stopped.')

    def run_task(self, function, success, failure=None):
        if self.closing:
            return
        task = Task(function)
        self.tasks.add(task)
        task.signals.result.connect(success)
        task.signals.error.connect(failure or self.notice)
        task.signals.done.connect(lambda: self.tasks.discard(task))
        self.pool.start(task)

    def choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, 'Add audio files', '',
            'Audio files (*.wav *.mp3 *.flac *.ogg *.aif *.aiff);;All files (*)')
        self.import_files(files)

    def import_files(self, paths):
        if self.closing or not paths:
            return
        existing = {str(Path(c['path']).resolve()).casefold() for c in self.clips}
        pending_paths = []
        for path in paths:
            normalized = str(Path(path).resolve()).casefold()
            if normalized not in existing:
                existing.add(normalized)
                pending_paths.append(path)
        if not pending_paths:
            self.status.setText('Those sounds are already on your board.')
            return
        self.status.setText(f'Importing {len(pending_paths)} sound(s)…')

        def inspect():
            accepted, errors = [], []
            for path in pending_paths:
                try:
                    accepted.append(new_clip(path, audio_info(path).duration))
                except Exception as exc:
                    errors.append(f'{Path(path).name}: {exc}')
            return accepted, errors

        def imported(result):
            if self.closing:
                return
            accepted, errors = result
            known = {c['path'].casefold() for c in self.clips}
            accepted = [c for c in accepted if c['path'].casefold() not in known]
            self.clips.extend(accepted)
            assign_missing_shortcuts(self.clips, self.stop_shortcut)
            self.sync_shortcuts(report=False)
            self.search.clear()
            self.render_cards()
            self.save_timer.start()
            self.status.setText(f'Added {len(accepted)} sound(s). Click a pad to play.')
            self.notice('\n'.join(errors[:4]) + (f'\n…and {len(errors) - 4} more.' if len(errors) > 4 else ''))
            if not errors:
                self.report_shortcut_errors()
        self.run_task(inspect, imported)

    def add_demos(self):
        self.demo_button.setEnabled(False)

        def generate():
            folder = self.data_dir / 'demos'
            folder.mkdir(parents=True, exist_ok=True)
            rate = 48000
            paths = []
            for name, notes in [('Good vibes', [523.25, 659.25, 783.99]),
                                ('Level up', [392, 523.25, 659.25, 1046.5]),
                                ('Soft ping', [880])]:
                parts = []
                for frequency in notes:
                    t = np.arange(int(rate * 0.17)) / rate
                    envelope = np.minimum(t / 0.012, 1) * np.exp(-t * 14)
                    envelope[-240:] *= np.linspace(1, 0, 240)
                    parts.append((0.3 * np.sin(2 * np.pi * frequency * t) * envelope).astype(np.float32))
                path = folder / (name + '.wav')
                sf.write(str(path), np.concatenate(parts), rate)
                paths.append(str(path))
            return paths

        def complete(paths):
            self.demo_button.setEnabled(True)
            self.import_files(paths)

        def failed(message):
            self.demo_button.setEnabled(True)
            self.notice(message)
        self.run_task(generate, complete, failed)

    def trigger(self, clip_id):
        if self.closing:
            return
        if clip_id in self.pending:
            self.pending.pop(clip_id, None)
            return
        if clip_id in self.engine.mixer.progress():
            self.engine.mixer.stop(clip_id)
            return
        clip = next((c for c in self.clips if c['id'] == clip_id), None)
        if not clip:
            return
        if not self.connected and not self.connect_route():
            return
        self.sequence += 1
        token = self.sequence
        self.pending[clip_id] = token
        rate = self.engine.rate

        def ready(samples):
            if self.closing or self.pending.get(clip_id) != token:
                return
            self.pending.pop(clip_id, None)
            if self.connected and self.engine.running and rate == self.engine.rate:
                try:
                    self.engine.mixer.play(clip_id, samples, clip['gain'], clip['loop'])
                    self.status.setText('Playing · ' + clip['name'])
                except ValueError as exc:
                    self.notice(str(exc))

        def failed(message):
            if self.pending.get(clip_id) == token:
                self.pending.pop(clip_id, None)
                self.notice(f'Could not play {clip["name"]}: {message}')
        self.run_task(lambda: self.cache.load(clip['path'], rate), ready, failed)

    def shortcut_bindings(self):
        return {STOP_ACTION: self.stop_shortcut,
                **{clip['id']: clip.get('shortcut', '') for clip in self.clips}}

    def shortcut_names(self):
        return {STOP_ACTION: 'Stop sounds', **{clip['id']: clip['name'] for clip in self.clips}}

    def activate_shortcut(self, action):
        if self.closing:
            return
        if action == STOP_ACTION:
            self.stop_sounds()
        elif not QApplication.activeModalWidget():
            self.trigger(action)

    def refresh_shortcut_hint(self):
        stop = self.stop_shortcut or 'No global shortcut'
        self.shortcut_hint.setText(f'Stop sounds: {stop}  ·  Esc while focused')
        error = self.hotkeys.failed.get(STOP_ACTION) if self.hotkeys else None
        self.shortcut_hint.setToolTip(error or '')

    def report_shortcut_errors(self):
        if self.hotkeys and self.hotkeys.failed:
            names = self.shortcut_names()
            details = [f'{names.get(action, action)}: {error}'
                       for action, error in self.hotkeys.failed.items()]
            self.notice('Some shortcuts are unavailable. Open Shortcuts to change them.\n' + '\n'.join(details[:4]))

    def sync_shortcuts(self, report=True):
        if self.hotkeys:
            self.hotkeys.configure(self.shortcut_bindings())
        if report:
            self.report_shortcut_errors()

    def apply_shortcuts(self, changes):
        # Keep sounds that finished importing after the editor was opened.
        bindings = self.shortcut_bindings()
        bindings.update({action: text for action, text in changes.items() if action in bindings})
        valid, errors = compile_bindings(bindings, self.shortcut_names())
        if errors:
            action, message = next(iter(errors.items()))
            raise ShortcutError(f'{self.shortcut_names()[action]}: {message}')
        bindings = {action: valid[action].text if action in valid else '' for action in bindings}
        if self.hotkeys:
            self.hotkeys.configure(bindings, strict=True)
        self.stop_shortcut = bindings[STOP_ACTION]
        for clip in self.clips:
            clip['shortcut'] = bindings[clip['id']]
        self.refresh_shortcut_hint()
        self.render_cards()
        self.notice('')
        self.persist()

    def edit_shortcuts(self, selected=None):
        if self.hotkeys:
            self.hotkeys.suspend()
        try:
            names = self.shortcut_names()
            actions = [(action, names[action], text) for action, text in self.shortcut_bindings().items()]
            dialog = ShortcutEditor(self, actions, self.apply_shortcuts, selected)
            dialog.exec()
            dialog.deleteLater()
        finally:
            if self.hotkeys and self.hotkeys.paused and not self.closing:
                self.hotkeys.resume()
            self.refresh_shortcut_hint()
            self.render_cards()
            self.report_shortcut_errors()

    def stop_sounds(self):
        self.pending.clear()
        self.engine.mixer.stop()
        self.status.setText('All sounds stopped.' + (' Microphone is still connected.'
                            if self.connected and self.mic_enabled.isChecked() else ''))

    def update_clip(self, clip, **changes):
        clip.update(changes)
        self.engine.mixer.update(clip['id'], clip['gain'], clip['loop'])
        self.save_timer.start()

    def rename_clip(self, clip):
        name, ok = QInputDialog.getText(self, 'Rename sound', 'Sound name:', text=clip['name'])
        if ok and name.strip():
            clip['name'] = name.strip()[:100]
            self.render_cards()
            self.save_timer.start()

    def remove_clip(self, clip):
        self.pending.pop(clip['id'], None)
        self.engine.mixer.stop(clip['id'])
        self.clips.remove(clip)
        self.sync_shortcuts()
        self.render_cards()
        self.save_timer.start()

    def render_cards(self, *args):
        if not hasattr(self, 'grid'):
            return
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self.cards.clear()
        query = self.search.text().strip().casefold()
        visible = [(i, clip) for i, clip in enumerate(self.clips) if query in clip['name'].casefold()]
        self.columns = max(1, min(4, self.scroll.viewport().width() // 230))
        for position, (index, clip) in enumerate(visible):
            card = Pad(clip, index, self)
            self.cards[clip['id']] = card
            self.grid.addWidget(card, position // self.columns, position % self.columns)
        self.scroll.setVisible(bool(visible))
        self.empty.setVisible(not visible)
        self.empty_title.setText('No matching sounds.' if self.clips else 'Make some noise.')
        self.empty_text.setText('Try another search or add a new sound.' if self.clips else
                               'Drop audio files here to create your first sound pads.\nWAV, MP3, FLAC, OGG, AIFF · up to 5 minutes per clip')
        self.demo_button.setVisible(not self.clips)
        self.count_label.setText(f'YOUR SOUNDS  /  {len(self.clips):02d}')

    def tick(self):
        if self.closing:
            return
        if self.connected and not self.engine.healthy:
            error = self.engine.last_error or 'An audio device stopped or was disconnected.'
            self.disconnect_route()
            self.notice(error + ' Refresh devices, then start output again.')
        progress = self.engine.mixer.progress()
        for key, card in self.cards.items():
            card.set_playing(progress.get(key), key in self.pending)
        peak = self.engine.mixer.peak if self.connected else 0
        self.meter.setValue(int(min(1, peak) * 100))
        if not self.connected:
            self.level_text.setText('Output stopped')
        elif self.engine.mixer.clipped:
            self.level_text.setText('Peak limit reached · lower the volume')
        else:
            db = f'{20 * math.log10(peak):.0f} dB' if peak > 0.00001 else 'Silent'
            suffix = f' · {self.engine.xruns} audio dropouts' if self.engine.xruns else ''
            if self.engine.filter_drops:
                suffix += ' · Filter buffer overflow'
            self.level_text.setText(f'● Live · {db}{suffix}')

    def persist(self):
        state = dict(clips=self.clips, sound_gain=self.sound_slider.value(), mic_gain=self.mic_slider.value(),
                     mic_noise_reduction=self.mic_noise_reduction.isChecked(), stop_shortcut=self.stop_shortcut)
        for name, combo in [('output', self.output), ('microphone', self.microphone), ('monitor', self.monitor)]:
            item = combo.currentData()
            state[name] = item.key if item else self.state.get(name)
        try:
            save_state(self.state_path, state)
            self.state = state
        except OSError as exc:
            self.notice(f'Could not save your library: {exc}')

    def show_guide(self):
        box = QMessageBox(self)
        box.setWindowTitle('Send sounds into a microphone input')
        box.setText('Soundboard → CABLE Input → CABLE Output → your voice app')
        box.setInformativeText(
            '1. Install VB-CABLE from the official VB-Audio website and restart Windows.\n\n'
            '2. In Soundboard, choose CABLE Input as the output.\n\n'
            '3. In Discord, your game, or your recorder, select CABLE Output as the microphone/input.\n\n'
            '4. To include your voice, enable “Mix in my microphone” and choose your physical microphone. '
            'Use headphones when routing a live mic to avoid speaker feedback.\n\n'
            '5. Leave “Reduce microphone noise” enabled to filter your voice without filtering sound files. '
            'Use “Test microphone” before connecting to compare original and filtered audio.\n\n'
            '6. In Discord, turn off noise suppression (including Krisp), echo cancellation, and automatic gain control. '
            'If needed, select the Custom input profile to access these settings. '
            'Set input sensitivity low enough to pass quiet clips, or hold push-to-talk throughout playback.\n\n'
            '7. Start output and click a pad. Soundboard filters your microphone locally; Discord receives the combined audio.\n\n'
            'Stop sounds leaves the mic connected. Disconnect output stops both. '
            'The app does not install drivers or change Windows default devices.')
        download = box.addButton('Open VB-CABLE website', QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is download:
            QDesktopServices.openUrl(QUrl('https://vb-audio.com/Cable/'))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.import_files([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'scroll'):
            columns = max(1, min(4, self.scroll.viewport().width() // 230))
            if columns != self.columns:
                QTimer.singleShot(0, self.render_cards)

    def closeEvent(self, event):
        self.closing = True
        self.timer.stop()
        self.save_timer.stop()
        self.pending.clear()
        self.engine.stop()
        if self.hotkeys:
            self.hotkeys.close()
        self.persist()
        self.pool.clear()
        self.pool.waitForDone()
        event.accept()


def run():
    application = QApplication(sys.argv)
    application.setApplicationName('Soundboard')
    application.setStyle('Fusion')
    window = MainWindow()
    window.show()
    return application.exec()
