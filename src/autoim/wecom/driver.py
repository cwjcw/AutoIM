from __future__ import annotations

import time

from autoim.automation import clipboard, keyboard
from autoim.wecom.window_manager import WeComWindowInfo, WeComWindowManager


class WeComDriver:
    """Standard desktop operations guarded by current-window verification."""

    def __init__(self, window_manager: WeComWindowManager | None = None) -> None:
        self.window_manager = window_manager or WeComWindowManager()

    def resolve_window(self) -> WeComWindowInfo:
        return self.window_manager.resolve_window()

    def activate(self) -> WeComWindowInfo:
        return self.window_manager.activate()

    def ensure_foreground(self) -> WeComWindowInfo:
        return self.window_manager.ensure_foreground()

    def get_rect(self) -> tuple[int, int, int, int]:
        return self.window_manager.get_rect()

    def send_shortcut(self, shortcut: str) -> WeComWindowInfo:
        actions = {
            "ctrl_c": keyboard.send_ctrl_c,
            "ctrl_a": keyboard.send_ctrl_a,
            "ctrl_v": keyboard.send_ctrl_v,
            "escape": keyboard.send_escape,
        }
        if shortcut == "ctrl_a_c":
            self.ensure_foreground()
            keyboard.send_ctrl_a()
            self.window_manager.verify_foreground()
            keyboard.send_ctrl_c()
            return self.window_manager.verify_foreground()
        try:
            action = actions[shortcut]
        except KeyError as exc:
            raise ValueError(f"不支持的快捷键动作：{shortcut}") from exc
        info = self.ensure_foreground()
        action()
        return self.window_manager.verify_foreground()

    def read_clipboard(self) -> tuple[WeComWindowInfo, clipboard.ClipboardSnapshot]:
        info = self.ensure_foreground()
        return info, clipboard.read_clipboard()

    def clear_clipboard(self) -> tuple[WeComWindowInfo, clipboard.ClipboardSnapshot]:
        self.ensure_foreground()
        clipboard.clear_clipboard()
        info = self.window_manager.verify_foreground()
        return info, clipboard.read_clipboard()

    def write_clipboard(self, text: str) -> WeComWindowInfo:
        info = self.ensure_foreground()
        clipboard.write_unicode_text(text)
        return self.window_manager.verify_foreground()

    def clipboard_sequence_number(self) -> tuple[WeComWindowInfo, int]:
        info = self.ensure_foreground()
        return info, clipboard.get_sequence_number()

    def clipboard_after_change(self, baseline: int, timeout: float) -> tuple[WeComWindowInfo, clipboard.ClipboardChange]:
        # Poll only the sequence counter while waiting. Clipboard contents are
        # retrieved only after a fresh foreground verification.
        if timeout < 0:
            raise ValueError("timeout 必须非负")
        started = time.perf_counter()
        deadline = started + timeout
        self.window_manager.verify_foreground()
        current = clipboard.get_sequence_number()
        while current == baseline and time.perf_counter() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.perf_counter())))
            self.window_manager.verify_foreground()
            current = clipboard.get_sequence_number()
        info = self.window_manager.verify_foreground()
        snapshot = clipboard.read_clipboard()
        change = clipboard.ClipboardChange(
            changed=current != baseline,
            before_sequence=baseline,
            after_sequence=snapshot.sequence_number,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            snapshot=snapshot,
        )
        return info, change
