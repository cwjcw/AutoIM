from __future__ import annotations

import os
import time


def send_keys(keys: str) -> None:
    """Send a pywinauto key sequence to the current foreground window."""
    if os.name != "nt":
        raise OSError("键盘控制仅支持 Windows")
    from pywinauto.keyboard import send_keys as pywinauto_send_keys

    pywinauto_send_keys(keys, pause=0.02)


def send_ctrl_c() -> None:
    send_keys("^c")


def send_ctrl_a() -> None:
    send_keys("^a")


def send_ctrl_v() -> None:
    send_keys("^v")


def send_escape() -> None:
    send_keys("{ESC}")


def send_ctrl_a_ctrl_c() -> None:
    send_keys("^a")
    time.sleep(0.12)
    send_keys("^c")
