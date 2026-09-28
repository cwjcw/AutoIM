from __future__ import annotations

import hashlib
import unittest
from unittest.mock import Mock, patch

from autoim.automation.clipboard import ClipboardSnapshot
from autoim.calibration import CalibrationStore
from autoim.vision import (
    NormalizedRect, WindowCapture, image_point_to_screen, relative_rect_from_pixels,
    relative_rect_to_pixels, validate_click_target,
)
from autoim.wecom.driver import WeComDriver
from autoim.wecom.window_manager import WeComWindowInfo, WindowResolutionError


class VisionCoordinateTests(unittest.TestCase):
    def test_normalized_round_trip_and_window_move_resize(self):
        rect = relative_rect_from_pixels(290, 100, 990, 780, 1000, 1000)
        self.assertEqual(relative_rect_to_pixels(rect, 1000, 1000), (290, 100, 990, 780))
        self.assertEqual(image_point_to_screen(500, 500, 1000, 1000, (100, 200, 2100, 1200)), (1100, 700))
        self.assertEqual(image_point_to_screen(500, 500, 1000, 1000, (300, 50, 1300, 1050)), (800, 550))

    def test_click_outside_window_and_chat_area_is_rejected(self):
        chat = NormalizedRect(.29, .10, .99, .78)
        with self.assertRaises(ValueError):
            validate_click_target(10, 800, 1000, 1000, (0, 0, 1000, 1000), chat)
        with self.assertRaises(ValueError):
            validate_click_target(999, 400, 1000, 1000, (0, 0, 1000, 1000), chat)

    def setUp(self):
        self.info = WeComWindowInfo(42, 420, "企业微信", "WeWorkWindow", rect=(100, 200, 1100, 1200))
        self.capture = WindowCapture(42, 420, "企业微信", "WeWorkWindow", self.info.rect, 1000, 1000, object())
        self.manager = Mock()
        self.manager.resolve_window.return_value = self.info
        self.manager.verify_foreground.return_value = self.info
        self.driver = WeComDriver(self.manager)
        self.chat = NormalizedRect(.29, .10, .99, .78)

    def test_dry_run_never_calls_mouse(self):
        click = Mock()
        point = self.driver.safe_click(self.capture, 500, 500, self.chat, dry_run=True, clicker=click)
        self.assertEqual(point, (600, 700))
        click.assert_not_called()
        self.manager.verify_foreground.assert_not_called()

    def test_foreground_failure_prevents_click(self):
        self.manager.verify_foreground.side_effect = WindowResolutionError("not foreground")
        click = Mock()
        with self.assertRaises(WindowResolutionError):
            self.driver.safe_click(self.capture, 500, 500, self.chat, dry_run=False, clicker=click)
        click.assert_not_called()

    def test_hwnd_change_and_moved_window_prevent_click(self):
        click = Mock()
        self.manager.resolve_window.return_value = WeComWindowInfo(42, 421, "企业微信", "WeWorkWindow", rect=self.info.rect)
        with self.assertRaisesRegex(RuntimeError, "PID/HWND"):
            self.driver.safe_click(self.capture, 500, 500, self.chat, dry_run=False, clicker=click)
        click.assert_not_called()
        self.manager.resolve_window.return_value = WeComWindowInfo(42, 420, "企业微信", "WeWorkWindow", rect=(101, 200, 1101, 1200))
        with self.assertRaisesRegex(RuntimeError, "位置或尺寸"):
            self.driver.safe_click(self.capture, 500, 500, self.chat, dry_run=False, clicker=click)

    def test_no_calibration_refuses_operation(self):
        click = Mock()
        with self.assertRaisesRegex(RuntimeError, "没有聊天消息区域"):
            self.driver.safe_click(self.capture, 500, 500, None, dry_run=False, clicker=click)
        click.assert_not_called()

    def test_clipboard_read_does_not_check_or_change_foreground(self):
        snapshot = ClipboardSnapshot(3, ("CF_UNICODETEXT",), "existing")
        with patch("autoim.wecom.driver.clipboard.read_clipboard", return_value=snapshot) as read:
            self.assertEqual(self.driver.read_clipboard(), snapshot)
        read.assert_called_once_with()
        self.manager.ensure_foreground.assert_not_called()
        self.manager.verify_foreground.assert_not_called()

    def test_copy_does_not_log_body_by_default(self):
        body = "sensitive visible message"
        snapshot = ClipboardSnapshot(5, ("CF_UNICODETEXT",), body)
        with patch("autoim.wecom.driver.clipboard.get_sequence_number", side_effect=[4, 5]), \
             patch("autoim.wecom.driver.clipboard.read_clipboard", return_value=snapshot), \
             patch("autoim.wecom.driver.keyboard.send_ctrl_c"), \
             patch("autoim.wecom.driver.mouse.click_screen_point") as click:
            with self.assertLogs("autoim.wecom.driver", level="INFO") as logs:
                result = self.driver.copy_visible_message(self.capture, 500, 500, self.chat)
        self.assertEqual(result["snapshot"].text, body)
        click.assert_called_once_with(600, 700)
        joined = "\n".join(logs.output)
        self.assertNotIn(body, joined)
        self.assertIn(hashlib.sha256(body.encode()).hexdigest(), joined)
        self.assertIn(f"length={len(body)}", joined)

    def test_calibration_persists_only_relative_regions(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            store = CalibrationStore(Path(temp) / "wecom-calibration.json")
            rect = NormalizedRect(.2, .1, .9, .8)
            store.save_region("chat_area", rect)
            raw = store.path.read_text(encoding="utf-8")
            self.assertNotIn("hwnd", raw.lower())
            self.assertNotIn("screen", raw.lower())
            self.assertEqual(store.load()["chat_area"], rect)
            store.delete_region("chat_area")
            self.assertEqual(store.load(), {})

    def test_legacy_clipboard_report_hides_body_unless_opted_in(self):
        from dataclasses import asdict
        from autoim.clipboard_diagnostics import CopyAttempt, _render_outputs
        body = "customer private text"
        result = CopyAttempt("Ctrl+C", True, True, True, body, len(body), "Unicode 文本", 12, "now")
        metadata = {"pid": 3, "hwnd": 0x45, "window_title": "企业微信", "class_name": "WeWorkWindow"}
        default_transcript, default_summary = _render_outputs([asdict(result)], metadata, "", "待核对", "待核对")
        self.assertNotIn(body, default_transcript)
        self.assertNotIn(body, default_summary)
        opted_transcript, _ = _render_outputs([asdict(result)], metadata, "", "待核对", "待核对", True)
        self.assertIn(body, opted_transcript)


if __name__ == "__main__":
    unittest.main()
