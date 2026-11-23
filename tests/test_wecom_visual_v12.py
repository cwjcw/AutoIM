from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QTableWidget

from autoim.vision import NormalizedRect, normalized_point_to_widget, widget_point_to_normalized
from autoim.vision_widgets import ScreenshotCanvas, fit_dialog_to_screen
from autoim.wecom.visual_capture import CaptureError, _UNPAINTED, _render_complete_client, capture_visible_window
from autoim.wecom.visual_layout import Box, REGION_NAMES
from autoim.wecom.visual_messages import find_containers
from autoim.wecom.visual_observation import observe_frame
from autoim.wecom.visual_ocr import OcrUnavailable
from test_wecom_visual_capture import BASE_STATE, INFO, THRESHOLDS, FixtureReader, fixture, span


class PreviewV12Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def canvas(self, source, viewport, dpr=1):
        pixmap = QPixmap(*source)
        pixmap.setDevicePixelRatio(1)
        pixmap.fill(QColor("#243040"))
        painter = QPainter(pixmap)
        w, h = source
        painter.fillRect(w * 4 // 5, 0, w - w * 4 // 5, h, QColor("#e65870"))
        painter.fillRect(0, h * 4 // 5, w, h - h * 4 // 5, QColor("#3ea971"))
        painter.end()
        pixmap.setDevicePixelRatio(dpr)
        canvas = ScreenshotCanvas(pixmap, "preview")
        self.addCleanup(canvas.close)
        canvas.resize(*viewport)
        canvas.show()
        self.app.processEvents()
        return canvas

    def sample(self, canvas, x, y):
        widget_x, widget_y = normalized_point_to_widget(x, y, canvas.transform)
        pixmap = canvas.grab()
        dpr = pixmap.devicePixelRatio()
        return pixmap.toImage().pixelColor(round(widget_x * dpr), round(widget_y * dpr)).name()

    def test_large_frame_starts_with_right_edge_and_bottom_visible(self):
        canvas = self.canvas((2696, 1436), (660, 263))
        self.assertEqual(self.sample(canvas, .97, .30), "#e65870")
        self.assertEqual(self.sample(canvas, .45, .97), "#3ea971")
        self.assertIn("整图在视口内", canvas.preview_geometry())

    def test_smaller_frame_can_fill_larger_viewport(self):
        canvas = self.canvas((160, 100), (640, 400))
        self.assertEqual(canvas.transform.displayed_width, 640)
        self.assertEqual(self.sample(canvas, .97, .3), "#e65870")

    def test_portrait_viewport_preserves_aspect_and_centers_letterbox(self):
        canvas = self.canvas((800, 400), (300, 600))
        self.assertEqual((canvas.transform.displayed_width, canvas.transform.displayed_height), (300, 150))
        self.assertEqual(canvas.transform.offset_y, 225)

    def test_source_metadata_dpr_does_not_crop_at_four_scales(self):
        for dpr in (1, 1.25, 1.5, 1.75):
            with self.subTest(dpr=dpr):
                canvas = self.canvas((1925, 1025), (668, 352), dpr)
                self.assertEqual(canvas.image.devicePixelRatio(), 1)
                self.assertEqual((canvas.source_width, canvas.source_height), (1925, 1025))
                self.assertEqual(self.sample(canvas, .97, .30), "#e65870")
                self.assertEqual(self.sample(canvas, .45, .97), "#3ea971")

    def test_overlay_pixels_and_source_coordinate_round_trip(self):
        canvas = self.canvas((1925, 1025), (664, 310), 1.75)
        box = NormalizedRect(.48, .2, .95, .88)
        canvas.overlays = (("", box, "#ff0000"),)
        canvas.update()
        self.app.processEvents()
        self.assertEqual(self.sample(canvas, .48, .54), "#ff0000")
        original = (.9, .92)
        mapped = normalized_point_to_widget(*original, canvas.transform)
        self.assertEqual(tuple(round(value, 6) for value in widget_point_to_normalized(*mapped, canvas.transform)),
                         original)

    def test_dialog_resizes_with_work_area_and_repaints_on_tab_switch(self):
        from autoim.wecom.visual_panel import VisualResultDialog

        frame, reader = fixture()
        dialog = VisualResultDialog(observe_frame(frame, {}, reader))
        self.addCleanup(dialog.close)
        dialog.show()
        work_area = QRect(0, 0, 650, 430)
        fit_dialog_to_screen(dialog, work_area)
        self.app.processEvents()
        dialog.resize(650, 430)
        self.app.processEvents()
        first_width = dialog.canvas.transform.displayed_width
        dialog.tabs.setCurrentIndex(1)
        dialog.resize(510, 350)
        dialog.tabs.setCurrentIndex(0)
        self.app.processEvents()
        self.assertLess(dialog.canvas.transform.displayed_width, first_width)
        self.assertLessEqual(dialog.frameGeometry().height(), work_area.height())
        self.assertIn("整图在视口内", dialog.preview_label.text())


class SourceCoverageV12Tests(unittest.TestCase):
    def test_exact_client_size_can_contain_untouched_right_and_bottom(self):
        frame, _ = fixture()
        pixels = frame.pixels.copy()
        pixels[:, -140:] = _UNPAINTED
        pixels[-80:, :] = _UNPAINTED
        manager = Mock()
        manager.resolve_window.return_value = INFO
        with self.assertRaisesRegex(CaptureError, "CAPTURE_INCOMPLETE") as captured:
            capture_visible_window(manager, inspect=lambda hwnd: BASE_STATE, render=lambda *args: pixels)
        self.assertEqual(captured.exception.diagnostics.raw_size, (1200, 720))
        self.assertIn("右缘", str(captured.exception))

    def test_exact_size_with_black_right_padding_is_rejected(self):
        frame, _ = fixture()
        pixels = frame.pixels.copy()
        pixels[:, -180:] = 0
        manager = Mock()
        manager.resolve_window.return_value = INFO
        with self.assertRaisesRegex(CaptureError, "宽黑边"):
            capture_visible_window(manager, inspect=lambda hwnd: BASE_STATE, render=lambda *args: pixels)

    def test_probe_recovers_same_window_content_beyond_its_physical_buffer(self):
        original_size = (420, 280)
        painted_size = (588, 392)

        def renderer(hwnd, width, height):
            self.assertEqual(hwnd, 81)
            pixels = np.full((height, width, 3), _UNPAINTED, np.uint8)
            pixels[:painted_size[1], :painted_size[0]] = (250, 248, 244)
            pixels[360:392, 510:588] = (120, 85, 190)
            return pixels

        rendered = _render_complete_client(81, *original_size, render=renderer)
        self.assertEqual(rendered.pixels.shape[:2], painted_size[::-1])
        self.assertEqual(tuple(rendered.pixels[-1, -1]), (120, 85, 190))
        self.assertGreater(rendered.probe_size[0], painted_size[0])

    def test_probe_reports_partial_paint_inside_larger_dib(self):
        def renderer(hwnd, width, height):
            pixels = np.full((height, width, 3), _UNPAINTED, np.uint8)
            pixels[:230, :400] = 80
            return pixels

        with self.assertRaisesRegex(CaptureError, "CAPTURE_INCOMPLETE.*400×230"):
            _render_complete_client(81, 420, 280, render=renderer)


class LayeredDiagnosticsV12Tests(unittest.TestCase):
    def test_ambiguous_a_b_boundary_preserves_independently_calibrated_title(self):
        frame, reader = fixture()
        frame.pixels[:, :391] = 226
        calibration = {"C1": Box(390, 0, 1200, 66).relative(1200, 720)}
        obs = observe_frame(frame, calibration, reader)
        self.assertEqual(obs.status, "LAYOUT_UNCERTAIN")
        self.assertEqual(obs.conversation.title, "测试乙")
        self.assertFalse(obs.conversation.attribution_reliable)
        self.assertIn("A", dict(obs.layout.failures))
        self.assertTrue(any(item.region == "C1 标题" and item.spans for item in obs.ocr_areas))

    def test_c2_marked_to_bottom_is_rejected_without_losing_c1(self):
        frame, reader = fixture()
        calibration = {"C2": Box(390, 66, 1200, 720).relative(1200, 720)}
        obs = observe_frame(frame, calibration, reader)
        self.assertIsNone(obs.layout.get("C2"))
        self.assertIn("包含输入区", dict(obs.layout.failures)["C2"])
        self.assertEqual(obs.conversation.title, "测试乙")
        self.assertFalse(any(item.region == "C2 历史消息" for item in obs.ocr_areas))

    def test_ocr_success_without_boxes_preserves_unassigned_text(self):
        frame, reader = fixture()
        frame.pixels[68:550, 391:1190] = 252
        obs = observe_frame(frame, {}, reader)
        self.assertFalse(obs.candidates)
        self.assertFalse(obs.messages)
        self.assertGreater(len(obs.context_text), 0)
        step = next(item for item in obs.ocr_areas if item.region.startswith("C2"))
        self.assertEqual(step.status, "TEXT_FOUND")
        self.assertTrue(step.spans)

    def test_c2_ocr_failure_keeps_boxes_contacts_and_title(self):
        frame, fixture_reader = fixture()

        class UnavailableC2(FixtureReader):
            def read(self, pixels, box, **kwargs):
                if box.width > 800 and 400 < box.height < 550:
                    raise OcrUnavailable("C2 测试异常")
                return super().read(pixels, box, **kwargs)

        obs = observe_frame(frame, {}, UnavailableC2(fixture_reader.spans, ("7", "12")))
        self.assertEqual(obs.ocr_status, "INITIALIZED")
        self.assertEqual(len(obs.candidates), 3)
        self.assertEqual(obs.conversation.title, "测试乙")
        self.assertEqual(len(obs.contacts), 3)
        self.assertEqual(next(item.reason for item in obs.ocr_areas if item.status == "FAILED"), "C2 测试异常")
        self.assertTrue(all(item.kind == "UNKNOWN" for item in obs.messages))

    def test_conflicting_alignment_is_kept_in_candidate_diagnostics(self):
        frame, reader = fixture()
        frame.pixels[300:345, 450:1140] = (205, 205, 205)
        result = observe_frame(frame, {}, reader)
        candidate = next(item for item in result.candidates if item.container.box.width > 600)
        self.assertEqual(candidate.direction, "UNKNOWN")
        self.assertIsNotNone(candidate.left_distance)
        self.assertIsNotNone(candidate.right_distance)

    def test_global_badge_number_and_muted_unread_are_independent(self):
        frame, reader = fixture()
        cv2.circle(frame.pixels, (63, 140), 8, (24, 29, 239), -1)
        cv2.line(frame.pixels, (62, 137), (62, 144), (255, 255, 255), 1)
        obs = observe_frame(frame, {}, FixtureReader((span("消息", 14, 132, 33), *reader.spans), ("3", "7", "12")))
        self.assertTrue(obs.global_unread.global_unread_count_reliable)
        self.assertEqual(obs.global_unread.global_unread_count, 3)
        self.assertTrue(obs.contacts[0].has_unread_indicator)
        self.assertTrue(obs.contacts[0].is_muted)
        self.assertTrue(obs.contacts[1].is_selected)
        self.assertFalse(obs.contacts[0].is_selected)

    def test_no_global_badge_does_not_hide_contact_unread(self):
        frame, reader = fixture()
        obs = observe_frame(frame, {}, FixtureReader((span("消息", 14, 132, 33), *reader.spans), ("7", "12")))
        self.assertFalse(obs.global_unread.global_unread_visible)
        self.assertTrue(obs.contacts[0].has_unread_indicator and obs.contacts[0].is_muted)

    def test_highlight_recovers_selected_row_when_avatar_has_no_contour(self):
        frame, reader = fixture()
        frame.pixels[152:200, 105:151] = (241, 212, 162)
        obs = observe_frame(frame, {}, reader)
        selected = [row for row in obs.contacts if row.is_selected]
        self.assertEqual(len(selected), 1)
        self.assertIsNone(selected[0].avatar)
        self.assertEqual(selected[0].display_name, "测试乙")

    def test_two_history_background_tones_do_not_mask_inner_bubbles(self):
        pixels = np.full((440, 800, 3), (226, 229, 232), np.uint8)
        pixels[:, 560:] = (246, 248, 250)
        pixels[65:110, 51:201] = (176, 182, 187)
        pixels[165:210, 587:760] = (214, 183, 152)
        boxes = find_containers(pixels, Box(0, 0, 800, 440), 1, THRESHOLDS)
        self.assertEqual(len(boxes), 2)
        self.assertGreater(boxes[1].box.left, 560)

    def test_dialog_reports_all_eight_rows_even_when_all_boundaries_fail(self):
        from autoim.wecom.visual_panel import VisualResultDialog

        app = QApplication.instance() or QApplication([])
        frame, _ = fixture()
        image = np.full(frame.pixels.shape, 244, dtype=np.uint8)
        obs = observe_frame(replace(frame, capture=replace(frame.capture, image=image)), {}, FixtureReader(()))
        dialog = VisualResultDialog(obs)
        dialog.show()
        app.processEvents()
        table = dialog.tabs.widget(1)
        self.assertIsInstance(table, QTableWidget)
        self.assertEqual(tuple(table.item(index, 0).text() for index in range(table.rowCount())), REGION_NAMES)
        self.assertTrue(all(table.item(index, 5).text() for index in range(table.rowCount())))
        self.assertIn("持续边缘覆盖", table.item(0, 5).text())
        dialog.close()
