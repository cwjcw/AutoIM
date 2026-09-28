from __future__ import annotations

import ctypes
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger(__name__)

_FORMAT_NAMES = {
    1: "CF_TEXT",
    2: "CF_BITMAP",
    3: "CF_METAFILEPICT",
    8: "CF_DIB",
    13: "CF_UNICODETEXT",
    14: "CF_ENHMETAFILE",
    15: "CF_HDROP",
    17: "CF_DIBV5",
}


@dataclass(frozen=True, slots=True)
class ClipboardSnapshot:
    sequence_number: int
    formats: tuple[str, ...]
    text: str | None

    @property
    def content_type(self) -> str:
        if not self.formats:
            return "空"
        if self.text is not None:
            other = [name for name in self.formats if name != "CF_UNICODETEXT"]
            return "Unicode 文本" + (f" + {', '.join(other)}" if other else "")
        return "非文本（" + ", ".join(self.formats) + ")"


@dataclass(frozen=True, slots=True)
class ClipboardChange:
    changed: bool
    before_sequence: int
    after_sequence: int
    elapsed_ms: int
    snapshot: ClipboardSnapshot


def _modules():
    if os.name != "nt":
        raise OSError("Windows 剪贴板仅支持 Windows")
    import win32clipboard
    import win32con
    import win32api

    return win32clipboard, win32con, win32api


def _sequence_number() -> int:
    return int(ctypes.windll.user32.GetClipboardSequenceNumber())


@contextmanager
def _opened_clipboard(retries: int = 30, interval: float = 0.05) -> Iterator[tuple[object, object]]:
    clipboard, constants, _ = _modules()
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            clipboard.OpenClipboard()
            break
        except Exception as exc:
            last_error = exc
            time.sleep(interval)
    else:
        raise TimeoutError(f"无法打开 Windows 剪贴板：{last_error}")
    try:
        yield clipboard, constants
    finally:
        clipboard.CloseClipboard()


def _format_name(clipboard, format_id: int) -> str:
    if format_id in _FORMAT_NAMES:
        return _FORMAT_NAMES[format_id]
    try:
        return clipboard.GetClipboardFormatName(format_id) or f"Format {format_id}"
    except Exception:
        return f"Format {format_id}"


def get_sequence_number() -> int:
    """Return Windows' clipboard sequence number for change detection."""
    _modules()
    return _sequence_number()


def read_clipboard() -> ClipboardSnapshot:
    """Read Unicode text and describe formats without interpreting images."""
    clipboard, constants, _ = _modules()
    with _opened_clipboard() as (clipboard, constants):
        formats: list[str] = []
        current = clipboard.EnumClipboardFormats(0)
        while current:
            formats.append(_format_name(clipboard, current))
            current = clipboard.EnumClipboardFormats(current)
        text: str | None = None
        if clipboard.IsClipboardFormatAvailable(constants.CF_UNICODETEXT):
            value = clipboard.GetClipboardData(constants.CF_UNICODETEXT)
            if isinstance(value, bytes):
                text = value.decode("utf-16-le", errors="replace").rstrip("\x00")
            elif value is not None:
                text = str(value)
        return ClipboardSnapshot(_sequence_number(), tuple(formats), text)


def read_unicode_text() -> str | None:
    return read_clipboard().text


def write_unicode_text(text: str) -> None:
    if not isinstance(text, str):
        raise TypeError("剪贴板文本必须是 str")
    clipboard, constants, _ = _modules()
    with _opened_clipboard() as (clipboard, constants):
        clipboard.EmptyClipboard()
        clipboard.SetClipboardData(constants.CF_UNICODETEXT, text)
    logger.info("已写入 Unicode 文本到 Windows 剪贴板（%d 字符）", len(text))


def clear_clipboard() -> int:
    clipboard, _, _ = _modules()
    with _opened_clipboard() as (clipboard, _):
        clipboard.EmptyClipboard()
    sequence = _sequence_number()
    logger.info("Windows 剪贴板已清空")
    return sequence


def has_changed(previous_sequence: int) -> bool:
    return get_sequence_number() != previous_sequence


def wait_for_change(previous_sequence: int, timeout: float = 2.5,
                    poll_interval: float = 0.05) -> ClipboardChange:
    """Wait until the Windows clipboard sequence number changes."""
    if timeout < 0 or poll_interval <= 0:
        raise ValueError("timeout 必须非负，poll_interval 必须大于 0")
    started = time.perf_counter()
    deadline = started + timeout
    current = _sequence_number()
    while current == previous_sequence and time.perf_counter() < deadline:
        time.sleep(min(poll_interval, max(0.0, deadline - time.perf_counter())))
        current = _sequence_number()
    changed = current != previous_sequence
    snapshot = read_clipboard()
    return ClipboardChange(
        changed=changed,
        before_sequence=previous_sequence,
        after_sequence=snapshot.sequence_number,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
        snapshot=snapshot,
    )
