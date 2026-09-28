from __future__ import annotations

import os


def click_screen_point(x: int, y: int) -> None:
    """Perform one ordinary visible Windows left-click at a validated point."""
    if os.name != "nt":
        raise OSError("鼠标控制仅支持 Windows")
    import win32api
    import win32con

    win32api.SetCursorPos((int(x), int(y)))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
