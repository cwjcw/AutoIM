from __future__ import annotations

import time

import hashlib
import logging
import time

from autoim.automation import clipboard, keyboard, mouse
from autoim.vision import NormalizedRect, WindowCapture, validate_click_target
from autoim.wecom.window_manager import WeComWindowInfo, WeComWindowManager

logger = logging.getLogger(__name__)


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

    def read_clipboard(self) -> clipboard.ClipboardSnapshot:
        """Read the system clipboard without changing focus or requiring WeCom."""
        return clipboard.read_clipboard()

    def safe_click(self, capture: WindowCapture, x: int, y: int,
                   chat_area: NormalizedRect | None, *, dry_run: bool = True,
                   clicker=None) -> tuple[int, int]:
        """Validate a screenshot point against fresh identity, focus, and calibration.

        Dry Run computes and returns the planned point but never calls the mouse API.
        """
        if chat_area is None:
            raise RuntimeError("没有聊天消息区域标定；操作已取消")
        current = self.window_manager.resolve_window()
        if current.pid != capture.pid or current.hwnd != capture.hwnd:
            raise RuntimeError("企业微信 PID/HWND 已变化；必须重新截图定位")
        if current.rect != capture.rect:
            raise RuntimeError("企业微信窗口位置或尺寸已变化；必须重新截图定位")
        point = validate_click_target(x, y, capture.width, capture.height, current.rect, chat_area)
        if dry_run:
            return point
        foreground = self.window_manager.verify_foreground()
        if (foreground.pid, foreground.hwnd, foreground.rect) != (capture.pid, capture.hwnd, capture.rect):
            raise RuntimeError("企业微信不在前台或窗口身份已变化；点击已取消")
        (clicker or mouse.click_screen_point)(*point)
        return point

    def copy_visible_message(self, capture: WindowCapture, x: int, y: int,
                             chat_area: NormalizedRect | None, timeout: float = 2.5) -> dict:
        """Click a user-selected visible point, issue normal Ctrl+C, and read changed clipboard."""
        if timeout < 0:
            raise ValueError("timeout 必须非负")
        if chat_area is None:
            raise RuntimeError("没有聊天消息区域标定；真实操作已取消")
        current = self.window_manager.resolve_window()
        if (current.pid, current.hwnd, current.rect) != (capture.pid, capture.hwnd, capture.rect):
            raise RuntimeError("企业微信窗口身份、位置或尺寸已变化；必须重新截图定位")
        self.window_manager.ensure_foreground()
        self.window_manager.verify_foreground()
        baseline = clipboard.get_sequence_number()
        point = self.safe_click(capture, x, y, chat_area, dry_run=False)
        self._verify_capture_foreground(capture)
        keyboard.send_ctrl_c()
        started = time.perf_counter()
        deadline = started + timeout
        current = clipboard.get_sequence_number()
        while current == baseline and time.perf_counter() < deadline:
            self._verify_capture_foreground(capture)
            time.sleep(min(0.05, max(0.0, deadline - time.perf_counter())))
            current = clipboard.get_sequence_number()
        self._verify_capture_foreground(capture)
        snapshot = clipboard.read_clipboard()
        change = clipboard.ClipboardChange(
            changed=current != baseline,
            before_sequence=baseline,
            after_sequence=snapshot.sequence_number,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            snapshot=snapshot,
        )
        snapshot = change.snapshot if change.changed else clipboard.ClipboardSnapshot(
            change.snapshot.sequence_number, change.snapshot.formats, None
        )
        text = snapshot.text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else ""
        logger.info("可见消息复制%s length=%d method=Clipboard sha256=%s verified=UNVERIFIED",
                    "成功" if text else "未取得新文本", len(text or ""), digest)
        return {"snapshot": snapshot, "changed": change.changed, "point": point,
                "operation_time": time.strftime("%Y-%m-%d %H:%M:%S"), "sha256": digest}

    def _verify_capture_foreground(self, capture: WindowCapture) -> WeComWindowInfo:
        current = self.window_manager.verify_foreground()
        if (current.pid, current.hwnd, current.rect) != (capture.pid, capture.hwnd, capture.rect):
            raise RuntimeError("复制期间企业微信身份或窗口 Rectangle 已变化；操作已取消")
        return current

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
