"""Windows registered hotkeys, without a keyboard hook or administrator rights."""
import ctypes
from ctypes import wintypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QTimer


class GlobalHotkeys(QAbstractNativeEventFilter):
    def __init__(self, application, play, stop):
        super().__init__()
        self.application = application
        self.play = play
        self.stop = stop
        self.registered = []
        self.failed = []
        if sys.platform == 'win32':
            self.api = ctypes.WinDLL('user32', use_last_error=True)
            self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self.api.RegisterHotKey.restype = wintypes.BOOL
            self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self.api.UnregisterHotKey.restype = wintypes.BOOL
            application.installNativeEventFilter(self)
            for number in range(10):
                key = ord(str(number))
                if self.api.RegisterHotKey(None, 0x4100 + number, 0x4003, key):
                    self.registered.append(number)
                else:
                    self.failed.append(number)

    def nativeEventFilter(self, event_type, message):
        if sys.platform == 'win32':
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312:
                number = int(msg.wParam) - 0x4100
                if number in self.registered:
                    # Defer UI work until the Windows message has been handled.
                    if number == 0:
                        QTimer.singleShot(0, self.stop)
                    else:
                        QTimer.singleShot(0, lambda n=number: self.play(n - 1))
                    return True, 0
        return False, 0

    def close(self):
        if sys.platform == 'win32':
            for number in self.registered:
                self.api.UnregisterHotKey(None, 0x4100 + number)
            self.registered.clear()
            self.application.removeNativeEventFilter(self)
