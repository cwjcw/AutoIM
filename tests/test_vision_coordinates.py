from __future__ import annotations

import hashlib
import unittest
from unittest.mock import Mock, patch

from autoim.automation.clipboard import ClipboardSnapshot
from autoim.calibration import CalibrationStore
from autoim.vision import (
    NormalizedRect, WindowCapture, create_display_transform, image_point_to_screen,
    normalized_point_to_widget, normalized_rect_to_widget, relative_rect_from_pixels,
    relative_rect_to_pixels, validate_click_target, widget_point_to_normalized,
    widget_rect_to_normalized,
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

    def test_source_and_display_same_size(self):
        transform = create_display_transform(800, 600, 800, 600)
        self.assertEqual((transform.displayed_width, transform.displayed_height), (800, 600))
        self.assertEqual((transform.offset_x, transform.offset_y), (0, 0))
        self.assertEqual(widget_point_to_normalized(248, 153, transform), (.31, .255))

    def test_half_scale_round_trip(self):
        transform = create_display_transform(1600, 1000, 800, 500)
        norm = widget_rect_to_normalized(100, 80, 650, 420, transform)
        self.assertEqual((norm.left, norm.top, norm.right, norm.bottom), (.125, .16, .8125, .84))
        self.assertEqual(normalized_rect_to_widget(norm, transform), (100, 80, 650, 420))

    def test_non_integer_scale_round_trip_error_within_two_pixels(self):
        transform = create_display_transform(1920, 1080, 997, 613)
        dragged = (177.25, 91.5, 801.75, 510.25)
        norm = widget_rect_to_normalized(*dragged, transform)
        mapped = normalized_rect_to_widget(norm, transform)
        self.assertLessEqual(max(abs(a-b) for a, b in zip(dragged, mapped)), 2)

    def test_vertical_letterbox_is_accounted_for(self):
        transform = create_display_transform(1600, 900, 800, 800)
        self.assertAlmostEqual(transform.offset_y, 175)
        self.assertEqual(widget_point_to_normalized(400, 400, transform), (.5, .5))
        with self.assertRaisesRegex(ValueError, "留白"):
            widget_point_to_normalized(400, 100, transform)

    def test_horizontal_letterbox_is_accounted_for(self):
        transform = create_display_transform(900, 1600, 800, 800)
        self.assertAlmostEqual(transform.offset_x, 175)
        self.assertEqual(widget_point_to_normalized(400, 400, transform), (.5, .5))
        with self.assertRaisesRegex(ValueError, "留白"):
            widget_point_to_normalized(100, 400, transform)

    def test_16_9_source_inside_square_widget(self):
        transform = create_display_transform(1920, 1080, 1000, 1000)
        self.assertAlmostEqual(transform.displayed_width, 1000)
        self.assertAlmostEqual(transform.displayed_height, 562.5)
        self.assertAlmostEqual(transform.offset_y, 218.75)
        self.assertEqual(widget_point_to_normalized(500, 500, transform), (.5, .5))

    def test_point_forward_and_inverse_conversion(self):
        transform = create_display_transform(1920, 1080, 1000, 700)
        widget = normalized_point_to_widget(.31, .42, transform)
        normalized = widget_point_to_normalized(*widget, transform)
        self.assertAlmostEqual(normalized[0], .31)
        self.assertAlmostEqual(normalized[1], .42)

    def test_rect_forward_and_inverse_conversion_error_under_two_pixels(self):
        transform = create_display_transform(1440, 900, 1024, 768)
        original = (225.2, 188.7, 805.6, 622.3)
        mapped = normalized_rect_to_widget(widget_rect_to_normalized(*original, transform), transform)
        self.assertLessEqual(max(abs(a-b) for a, b in zip(original, mapped)), 2)

    def test_points_outside_image_are_rejected(self):
        transform = create_display_transform(1600, 900, 800, 800)
        for point in ((10, 10), (10, 100), (400, 10), (400, 790)):
            with self.subTest(point=point), self.assertRaises(ValueError):
                widget_point_to_normalized(*point, transform)

    def test_dpi_values_are_reported_without_compensation_factor(self):
        for dpr in (1.0, 1.25, 1.5, 2.0):
            with self.subTest(dpr=dpr):
                transform = create_display_transform(2000, 1200, 800, 600, dpr)
                self.assertEqual(widget_point_to_normalized(400, 300, transform), (.5, .5))
                self.assertEqual(transform.device_pixel_ratio, dpr)

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
        with patch("autoim.wecom.driver.REAL_INPUT_ENABLED", True), self.assertRaises(WindowResolutionError):
            self.driver.safe_click(self.capture, 500, 500, self.chat, dry_run=False, clicker=click)
        click.assert_not_called()

    def test_hwnd_change_and_moved_window_prevent_click(self):
        click = Mock()
        self.manager.resolve_window.return_value = WeComWindowInfo(42, 421, "企业微信", "WeWorkWindow", rect=self.info.rect)
        with patch("autoim.wecom.driver.REAL_INPUT_ENABLED", True), self.assertRaisesRegex(RuntimeError, "PID/HWND"):
            self.driver.safe_click(self.capture, 500, 500, self.chat, dry_run=False, clicker=click)
        click.assert_not_called()
        self.manager.resolve_window.return_value = WeComWindowInfo(42, 420, "企业微信", "WeWorkWindow", rect=(101, 200, 1101, 1200))
        with patch("autoim.wecom.driver.REAL_INPUT_ENABLED", True), self.assertRaisesRegex(RuntimeError, "位置或尺寸"):
            self.driver.safe_click(self.capture, 500, 500, self.chat, dry_run=False, clicker=click)

    def test_real_click_is_fail_closed_by_default(self):
        click = Mock()
        with self.assertRaisesRegex(RuntimeError, "Fail Closed"):
            self.driver.safe_click(self.capture, 500, 500, self.chat, dry_run=False, clicker=click)
        click.assert_not_called()

    def test_real_copy_is_fail_closed_by_default_before_activation_or_input(self):
        with patch("autoim.wecom.driver.mouse.click_screen_point") as click, \
             patch("autoim.wecom.driver.keyboard.send_ctrl_c") as ctrl_c, \
             self.assertRaisesRegex(RuntimeError, "Fail Closed"):
            self.driver.copy_visible_message(self.capture, 500, 500, self.chat)
        click.assert_not_called()
        ctrl_c.assert_not_called()
        self.manager.ensure_foreground.assert_not_called()

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
        with patch("autoim.wecom.driver.REAL_INPUT_ENABLED", True), \
             patch("autoim.wecom.driver.clipboard.get_sequence_number", side_effect=[4, 5]), \
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

    def test_legacy_calibration_version_is_rejected(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wecom-calibration.json"
            path.write_text('{"version":1,"regions":{"chat_area":{"left":0.178,"top":0.05,"right":0.56,"bottom":0.48}}}', encoding="utf-8")
            self.assertEqual(CalibrationStore(path).load(), {})

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
