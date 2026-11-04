"""Editable Windows hotkeys, without a keyboard hook or administrator rights."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QTimer, Qt
from PySide6.QtGui import QKeySequence


STOP_ACTION = '__stop_sounds__'
DEFAULT_STOP = 'Ctrl+Alt+0'


class ShortcutError(ValueError):
    pass


@dataclass(frozen=True)
class Shortcut:
    text: str
    modifiers: int
    key: int

    @property
    def signature(self):
        return self.modifiers, self.key


def parse_shortcut(value):
    """Convert one portable Qt key combination to an unambiguous Windows key."""
    if not isinstance(value, str):
        raise ShortcutError('Record a shortcut or use Clear.')
    if not value.strip():
        return None
    sequence = QKeySequence.fromString(value, QKeySequence.SequenceFormat.PortableText)
    if sequence.count() != 1 or not sequence.toString():
        raise ShortcutError('Use a single key combination, not a sequence.')
    combination = sequence[0]
    key = int(combination.key())
    flags = combination.keyboardModifiers()
    allowed = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier |
               Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.KeypadModifier)
    if flags.value & ~allowed.value:
        raise ShortcutError('Windows/Meta keys are reserved. Use Ctrl, Alt, or Shift instead.')
    modifiers = (2 if flags & Qt.KeyboardModifier.ControlModifier else 0)
    modifiers |= (1 if flags & Qt.KeyboardModifier.AltModifier else 0)
    modifiers |= (4 if flags & Qt.KeyboardModifier.ShiftModifier else 0)
    keypad = bool(flags & Qt.KeyboardModifier.KeypadModifier)
    function = int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24)
    if key == int(Qt.Key.Key_F12):
        raise ShortcutError('F12 is reserved by Windows. Choose another key.')
    if keypad:
        if ord('0') <= key <= ord('9'):
            virtual_key = 0x60 + key - ord('0')
        elif key in map(ord, '*+-./'):
            virtual_key = {ord('*'): 0x6A, ord('+'): 0x6B, ord('-'): 0x6D,
                           ord('.'): 0x6E, ord('/'): 0x6F}[key]
        else:
            raise ShortcutError('For the number pad, use a digit or +, -, *, /, decimal with Num Lock on.')
    elif function:
        virtual_key = 0x70 + key - int(Qt.Key.Key_F1)
    elif ord('A') <= key <= ord('Z') or ord('0') <= key <= ord('9'):
        virtual_key = key
    else:
        virtual_key = {
            int(Qt.Key.Key_Backspace): 0x08, int(Qt.Key.Key_Tab): 0x09,
            int(Qt.Key.Key_Return): 0x0D, int(Qt.Key.Key_Enter): 0x0D,
            int(Qt.Key.Key_Escape): 0x1B, int(Qt.Key.Key_Space): 0x20,
            int(Qt.Key.Key_PageUp): 0x21, int(Qt.Key.Key_PageDown): 0x22,
            int(Qt.Key.Key_End): 0x23, int(Qt.Key.Key_Home): 0x24,
            int(Qt.Key.Key_Left): 0x25, int(Qt.Key.Key_Up): 0x26,
            int(Qt.Key.Key_Right): 0x27, int(Qt.Key.Key_Down): 0x28,
            int(Qt.Key.Key_Insert): 0x2D, int(Qt.Key.Key_Delete): 0x2E,
        }.get(key)
    if virtual_key is None:
        raise ShortcutError('Use a letter, digit, navigation key, F-key, or number-pad key.')
    if not function and not keypad and not modifiers & 3:
        raise ShortcutError('Include Ctrl or Alt, or use an F-key or number-pad key on its own.')
    if virtual_key == 0x2E and modifiers & 3 == 3:
        raise ShortcutError('Ctrl+Alt+Delete is reserved by Windows.')
    return Shortcut(sequence.toString(QKeySequence.SequenceFormat.PortableText), modifiers, virtual_key)


def compile_bindings(bindings, names=None):
    valid, errors, used = {}, {}, {}
    names = names or {}
    for action, text in bindings.items():
        try:
            shortcut = parse_shortcut(text)
            if shortcut is None:
                continue
            if shortcut.signature in used:
                owner = used[shortcut.signature]
                raise ShortcutError(f'{shortcut.text} is also assigned to {names.get(owner, owner)}.')
            valid[action] = shortcut
            used[shortcut.signature] = action
        except ShortcutError as exc:
            errors[action] = str(exc)
    return valid, errors


def assign_missing_shortcuts(clips, stop_shortcut=DEFAULT_STOP):
    """Migrate old libraries and give new pads free defaults; keep explicit clears."""
    used = set()
    for text in [stop_shortcut] + [c['shortcut'] for c in clips if 'shortcut' in c]:
        try:
            shortcut = parse_shortcut(text)
            if shortcut:
                used.add(shortcut.signature)
        except ShortcutError:
            pass  # Keep invalid saved values visible so the editor can repair them.
    for clip in clips:
        if 'shortcut' in clip:
            continue
        clip['shortcut'] = ''
        for number in range(1, 10):
            shortcut = parse_shortcut(f'Ctrl+Alt+{number}')
            if shortcut.signature not in used:
                clip['shortcut'] = shortcut.text
                used.add(shortcut.signature)
                break


class GlobalHotkeys(QAbstractNativeEventFilter):
    def __init__(self, application, activate, api=None):
        super().__init__()
        self.application = application
        self.activate = activate
        self.bindings = {}
        self.registered = {}  # Windows identifier -> (stable action, Shortcut)
        self.failed = {}
        self.paused = False
        self.generation = 0
        self.next_id = 0x4100
        self.api = api
        if api is None and sys.platform == 'win32':
            self.api = ctypes.WinDLL('user32', use_last_error=True)
            self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self.api.RegisterHotKey.restype = wintypes.BOOL
            self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self.api.UnregisterHotKey.restype = wintypes.BOOL
        application.installNativeEventFilter(self)

    def configure(self, bindings, strict=False):
        """Best effort on launch; save edits only if every requested key is available."""
        valid, errors = compile_bindings(bindings)
        if strict and errors:
            raise ShortcutError(next(iter(errors.values())))
        if self.paused and not strict:
            # An asynchronous import can finish while the editor is open.
            self.bindings = dict(bindings)
            return
        was_paused = self.paused
        self._unregister()
        for action, shortcut in valid.items():
            identifier = self.next_id
            self.next_id = 0x4100 if identifier == 0xBFFF else identifier + 1
            if self.api and self.api.RegisterHotKey(None, identifier, 0x4000 | shortcut.modifiers, shortcut.key):
                self.registered[identifier] = action, shortcut
            else:
                errors[action] = f'{shortcut.text} is unavailable (already in use or reserved by Windows).'
        if strict and errors:
            self._unregister()
            if not was_paused:
                self.configure(self.bindings)
            raise ShortcutError(next(iter(errors.values())))
        self.bindings = dict(bindings)
        self.failed = errors
        self.paused = False

    def _unregister(self):
        self.generation += 1
        for identifier in self.registered:
            self.api.UnregisterHotKey(None, identifier)
        self.registered.clear()

    def suspend(self):
        self._unregister()
        self.paused = True

    def resume(self):
        self.paused = False
        self.configure(self.bindings)

    def nativeEventFilter(self, event_type, message):
        if sys.platform == 'win32':
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312:
                binding = self.registered.get(int(msg.wParam))
                if binding:
                    action, shortcut = binding
                    if (int(msg.lParam) & 0xF, (int(msg.lParam) >> 16) & 0xFFFF) == shortcut.signature:
                        generation = self.generation
                        # Drop deferred events if bindings change before Qt delivers them.
                        QTimer.singleShot(0, lambda: self._activate(action, generation))
                        return True, 0
        return False, 0

    def _activate(self, action, generation):
        if generation == self.generation and not self.paused:
            self.activate(action)

    def close(self):
        self.suspend()
        self.application.removeNativeEventFilter(self)
