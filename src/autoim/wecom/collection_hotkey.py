"""One native Windows hotkey, owned by the Qt main window."""
import ctypes
import sys
from ctypes import wintypes
from time import monotonic

from PySide6.QtCore import QAbstractNativeEventFilter


def parse_hotkey(text):
    parts = [part.strip().upper() for part in text.split("+")]
    mods = {"ALT": 1, "CTRL": 2, "SHIFT": 4}
    prefixes, key = parts[:-1], parts[-1]
    if len(set(prefixes)) != len(prefixes) or any(part not in mods for part in prefixes):
        raise ValueError("使用 F9、F10 或 Ctrl+Alt+M 等快捷键")
    if key in {f"F{i}" for i in range(1, 12)}:
        vk = 0x6F + int(key[1:])
    elif prefixes and len(key) == 1 and key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        vk = ord(key)
    else:
        raise ValueError("使用 F1–F11 或带组合键的字母、数字")
    return sum(mods[part] for part in prefixes) | 0x4000, vk


class CollectionHotkey(QAbstractNativeEventFilter):
    def __init__(self, app, hwnd, toggle, *, native=None):
        super().__init__()
        self.app, self.hwnd, self.toggle = app, hwnd, toggle
        self.native = native or (ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None)
        self.active_id = None
        self.current = None
        self.last = float("-inf")
        if self.native:
            if native is None:
                self.native.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
                self.native.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
            app.installNativeEventFilter(self)
            app.aboutToQuit.connect(self.close)

    def set_shortcut(self, text):
        code = parse_hotkey(text)
        if not self.native:
            raise OSError("Windows 全局快捷键不可用")
        if code == self.current:
            return
        next_id = 0x4151 if self.active_id == 0x4150 else 0x4150
        if not self.native.RegisterHotKey(self.hwnd, next_id, *code):
            error = ctypes.get_last_error()
            raise OSError(f"快捷键注册失败，可能已被占用（Win32 {error}）")
        if self.active_id is not None and not self.native.UnregisterHotKey(self.hwnd, self.active_id):
            self.native.UnregisterHotKey(self.hwnd, next_id)
            raise OSError("无法注销原有快捷键")
        self.active_id, self.current = next_id, code

    def nativeEventFilter(self, event_type, address):
        if self.active_id is None or bytes(event_type) != b"windows_generic_MSG":
            return False, 0
        msg = wintypes.MSG.from_address(int(address))
        if msg.message != 0x0312 or msg.hWnd != self.hwnd or msg.wParam != self.active_id:
            return False, 0
        now = monotonic()
        if now - self.last >= .25:
            self.last = now
            self.toggle()
        return True, 0

    def close(self):
        if self.native:
            if self.active_id is not None:
                self.native.UnregisterHotKey(self.hwnd, self.active_id)
                self.active_id = self.current = None
            self.app.removeNativeEventFilter(self)
            self.native = None
