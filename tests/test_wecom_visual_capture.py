from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np

from autoim.vision import WindowCapture
from autoim.wecom.visual_capture import CaptureError, VisualFrame, WindowState, _render_client, capture_visible_window
from autoim.wecom.visual_layout import Box, LEAF_REGIONS, VisualThresholds, detect_layout
from autoim.wecom.visual_messages import classify_messages, direction_evidence, find_containers
from autoim.wecom.visual_observation import observe_frame, read_visible_conversation
from autoim.wecom.visual_ocr import LocalTextRecognizer, OcrUnavailable, TextSpan
from autoim.wecom.window_manager import WeComWindowInfo, WindowResolutionError

THRESHOLDS = VisualThresholds()
BASE_STATE = WindowState(False, True, 96, 1200, 720, (110, 170))
INFO = WeComWindowInfo(42, 81, "企业微信", "WeWorkWindow", rect=(-100, 2, 1100, 722))


class FixtureReader:
    """Inject outputs only for detector tests. A separate test runs real OCR."""

    def __init__(self, spans, digits=()):
        self.spans = spans
        self.digits = iter(digits)
        self.regions = []

    def read(self, pixels, box, *, single_line=False):
        self.regions.append(box)
        if single_line:
            value = next(self.digits, "")
            return (TextSpan(value, box, .98),) if value else ()
        return tuple(span for span in self.spans if box.contains(span.box.center))


def span(text, x, y, width=60, score=.96):
    return TextSpan(text, Box(x, y, x + width, y + 15), score)


def fixture(*, title="测试乙", hover=(110, 170), thumbs=True, same_name=False,
            activities=("5分钟前", "17:18", "昨天"), variation=0):
    pixels = np.full((720, 1200, 3), 252, dtype=np.uint8)
    pixels[:, :86] = (72 + variation, 64, 55)
    pixels[:, 86:390] = (222 + variation, 223, 224)
    pixels[:64, 86:390] = (200 + variation, 202, 204)
    pixels[:66, 390:] = (227 + variation, 227, 228)
    pixels[554:, 390:] = (231 + variation, 231, 231)
    pixels[18:46, 105:328] = (181, 181, 181)
    pixels[18:46, 342:370] = (163, 163, 163)
    cv2.line(pixels, (350, 32), (361, 32), (40, 40, 40), 1)
    cv2.line(pixels, (355, 27), (355, 37), (40, 40, 40), 1)
    pixels[144:214, 86:388] = (241, 212, 162)
    for top in (90, 160, 230):
        pixels[top:top + 40, 105:145] = (109, 138, 166)
    cv2.circle(pixels, (144, 91), 8, (25, 30, 240), -1)
    cv2.line(pixels, (143, 87), (143, 94), (255, 255, 255), 1)
    cv2.circle(pixels, (144, 161), 5, (25, 30, 240), -1)
    cv2.ellipse(pixels, (143, 230), (12, 8), 0, 0, 360, (25, 30, 240), -1)
    cv2.line(pixels, (140, 226), (140, 232), (255, 255, 255), 1)
    cv2.line(pixels, (146, 226), (146, 232), (255, 255, 255), 1)
    cv2.rectangle(pixels, (344, 91), (351, 99), (85, 85, 85), -1)
    cv2.line(pixels, (342, 87), (356, 101), (85, 85, 85), 2)
    if thumbs:
        pixels[232:295, 385:389] = (118, 118, 118)
        pixels[219:301, 1195:1199] = (115, 115, 115)
    pixels[126:160, 451:635] = (210, 210, 210)
    pixels[179:215, 927:1139] = (247, 204, 174)
    pixels[232:267, 451:635] = (210, 210, 210)
    for x in (410, 435, 460, 485):
        pixels[564:575, x:x + 11] = (70, 70, 70)
    pixels[668:704, 1081:1168] = (190, 114, 56)
    spans = [
        span("搜索 联系人", 120, 22), span(title, 418, 23, 120), span("＠微信", 420, 45),
        span("测试乙" if same_name else "测试甲名称很长依然保留", 157, 89, 188),
        span("客户发送了一条很长的消息预览…", 158, 113, 157),
        span(activities[0], 326, 113, 48),
        span("测试乙", 157, 160), span("上次预览", 159, 186),
        span(activities[1], 329, 184, 40),
        span("保险测试群(8人)", 159, 230, 144), span("有人发送了文件", 159, 254, 90),
        span(activities[2], 325, 253, 44),
        span("11/22 17:39", 760, 90, 106), span("你好", 466, 134),
        span("同意", 943, 187), span("你好", 466, 240),
        span("某某撤回了一条消息", 722, 279, 139),
        span("未发送的草稿", 423, 617, 200), span("发送(S)", 1102, 677, 49),
    ]
    state = replace(BASE_STATE, mouse=hover)
    frame = VisualFrame(WindowCapture(42, 81, "企业微信", "WeWorkWindow", INFO.rect,
                                     1200, 720, pixels), datetime.now(), state, hover)
    return frame, FixtureReader(spans, ("7", "12"))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.manager = Mock()
        self.manager.resolve_window.return_value = INFO
        self.inspect = Mock(return_value=BASE_STATE)
        self.render = Mock(return_value=fixture()[0].pixels)

    def capture(self):
        return capture_visible_window(self.manager, inspect=self.inspect, render=self.render)

    def test_maximized_capture_passes_only_verified_hwnd(self):
        result = self.capture()
        self.assertEqual(result.capture.hwnd, INFO.hwnd)
        self.render.assert_called_once_with(INFO.hwnd, 1200, 720)
        self.assertEqual(self.manager.resolve_window.call_count, 2)
        self.assertFalse(result.pixels.flags.writeable)

    def test_dpi_is_metadata_and_capture_pixels_remain_native(self):
        self.inspect.return_value = replace(BASE_STATE, dpi=144)
        result = self.capture()
        self.assertEqual(result.capture.device_pixel_ratio, 1.5)
        self.assertEqual(result.pixels.shape, (720, 1200, 3))

    def test_non_maximized_and_minimized_do_not_render(self):
        for state, code in ((replace(BASE_STATE, maximized=False), "NOT_MAXIMIZED"),
                            (replace(BASE_STATE, minimized=True), "WINDOW_MINIMIZED")):
            with self.subTest(code=code):
                self.inspect.return_value = state
                with self.assertRaisesRegex(CaptureError, code):
                    self.capture()
        self.render.assert_not_called()

    def test_failed_or_black_capture(self):
        for image in (None, np.zeros((720, 1200, 3), dtype=np.uint8), np.zeros((10, 10, 3))):
            with self.subTest(shape=getattr(image, "shape", None)):
                self.render.return_value = image
                with self.assertRaisesRegex(CaptureError, "CAPTURE_FAILED"):
                    self.capture()

    def test_invalid_window_and_client_closed_after_capture(self):
        self.manager.resolve_window.side_effect = WindowResolutionError()
        with self.assertRaisesRegex(CaptureError, "WINDOW_UNAVAILABLE"):
            self.capture()
        self.render.assert_not_called()
        self.manager.resolve_window.side_effect = [INFO, WindowResolutionError()]
        with self.assertRaisesRegex(CaptureError, "WINDOW_CHANGED"):
            self.capture()

    def test_recreated_or_moved_window_discards_frame(self):
        for replacement in (replace(INFO, hwnd=82), replace(INFO, rect=(0, 0, 1200, 720))):
            self.manager.resolve_window.side_effect = [INFO, replacement]
            with self.subTest(replacement=replacement), self.assertRaisesRegex(CaptureError, "WINDOW_CHANGED"):
                self.capture()

    def test_maximize_status_or_dpi_changed_during_grab(self):
        for replacement in (replace(BASE_STATE, maximized=False), replace(BASE_STATE, dpi=144)):
            self.inspect.side_effect = [BASE_STATE, replacement]
            with self.subTest(replacement=replacement), self.assertRaisesRegex(CaptureError, "WINDOW_CHANGED"):
                self.capture()

    def test_cursor_change_discards_only_hover_evidence(self):
        self.inspect.side_effect = [BASE_STATE, replace(BASE_STATE, mouse=(900, 400))]
        self.assertIsNone(self.capture().mouse)

    def test_native_hwnd_render_uses_only_own_synthetic_client(self):
        import uuid

        import win32api
        import win32con
        import win32gui

        def draw(hwnd, message, wparam, lparam):
            if message in (0x0014, 0x0317, 0x0318):  # WM_ERASEBKGND / WM_PRINT / WM_PRINTCLIENT
                for rect, color in (((0, 0, 100, 200), (200, 30, 10)),
                                    ((100, 0, 220, 200), (20, 80, 190))):
                    brush = win32gui.CreateSolidBrush(win32api.RGB(*color))
                    try:
                        win32gui.FillRect(wparam, rect, brush)
                    finally:
                        win32gui.DeleteObject(brush)
                return 1
            return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

        spec = win32gui.WNDCLASS()
        spec.hInstance = win32api.GetModuleHandle(None)
        spec.lpszClassName = f"AutoIMVisualFixture{uuid.uuid4().hex}"
        spec.lpfnWndProc = draw
        atom = win32gui.RegisterClass(spec)
        self.addCleanup(win32gui.UnregisterClass, spec.lpszClassName, spec.hInstance)
        # PrintWindow may skip a hidden window. Show the fixture beyond the
        # virtual desktop, without taking foreground focus.
        x = win32api.GetSystemMetrics(76) + win32api.GetSystemMetrics(78) + 500
        y = win32api.GetSystemMetrics(77) + win32api.GetSystemMetrics(79) + 500
        hwnd = win32gui.CreateWindow(atom, "Synthetic window", win32con.WS_POPUP,
                                     x, y, 220, 200, 0, 0, spec.hInstance, None)
        self.addCleanup(win32gui.DestroyWindow, hwnd)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        pixels = _render_client(hwnd, 220, 200)
        self.assertEqual(tuple(pixels[35, 36]), (10, 30, 200))
        self.assertEqual(tuple(pixels[35, 185]), (190, 80, 20))


class LayoutAndContactsTests(unittest.TestCase):
    def observe(self, **kwargs):
        frame, reader = fixture(**kwargs)
        return observe_frame(frame, {}, reader)

    def test_all_eight_regions_across_dimensions_and_colors(self):
        for width, height, variation in ((1200, 720, 0), (1920, 1023, 8), (1600, 900, -9)):
            frame, _ = fixture(variation=variation)
            pixels = cv2.resize(frame.pixels, (width, height), interpolation=cv2.INTER_NEAREST)
            layout = detect_layout(pixels, {}, THRESHOLDS)
            with self.subTest(width=width, height=height):
                self.assertEqual({item.name for item in layout.regions}, set(LEAF_REGIONS) | {"B", "C"})
                self.assertAlmostEqual(layout.region("C2").left / width, 390 / 1200, delta=.008)
                self.assertAlmostEqual(layout.region("C2").bottom / height, 554 / 720, delta=.008)

    def test_ambiguous_edges_refuse_region_assignment(self):
        pixels = np.full((720, 1200, 3), 242, dtype=np.uint8)
        pixels[:, :90] = 70
        pixels[:, 300:390] = 170
        self.assertEqual(detect_layout(pixels, {}, THRESHOLDS).status, "LAYOUT_UNCERTAIN")

    def test_calibration_reuses_relative_coordinates_and_rejects_bad_partition(self):
        frame, _ = fixture()
        auto = detect_layout(frame.pixels, {}, THRESHOLDS)
        calibration = {item.name: item.normalized for item in auto.regions if item.name in LEAF_REGIONS}
        pixels = np.full((1023, 1920, 3), 245, dtype=np.uint8)
        result = detect_layout(pixels, calibration, THRESHOLDS)
        self.assertEqual(result.status, "CANDIDATE")
        self.assertEqual(result.region("C2").left, round(calibration["C2"].left * 1920))
        calibration["C3"] = calibration["C2"]
        self.assertEqual(detect_layout(pixels, calibration, THRESHOLDS).status, "LAYOUT_UNCERTAIN")

    def test_local_threshold_configuration_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wecom-pc-visual-thresholds.json"
            self.assertEqual(VisualThresholds.load(path), THRESHOLDS)
            path.write_text('{"anchor_tolerance": 0.02}', encoding="utf-8")
            self.assertEqual(VisualThresholds.load(path).anchor_tolerance, .02)
            path.write_text('{"anchor_tolerance": 0.8}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "配置无效"):
                VisualThresholds.load(path)

    def test_search_and_plus_stay_outside_contact_rows(self):
        result = self.observe()
        self.assertIsNotNone(result.search_box)
        self.assertIsNotNone(result.add_button)
        self.assertEqual(len(result.contacts), 3)
        self.assertTrue(all(row.box.top >= result.layout.region("B2").top for row in result.contacts))

    def test_contacts_keep_long_name_preview_and_group_display_name(self):
        a, _, group = self.observe().contacts
        self.assertEqual(a.display_name, "测试甲名称很长依然保留")
        self.assertEqual(a.preview, "客户发送了一条很长的消息预览…")
        self.assertEqual(group.display_name, "保险测试群(8人)")

    def test_unread_number_dot_multiple_digits_mute_and_selected_are_independent(self):
        a, b, c = self.observe().contacts
        self.assertEqual((a.has_unread_indicator, a.unread_count, a.is_muted, a.is_selected),
                         (True, 7, True, False))
        self.assertEqual((b.has_unread_indicator, b.unread_count, b.unread_count_reliable, b.is_selected),
                         (True, None, False, True))
        self.assertEqual((c.has_unread_indicator, c.unread_count, c.unread_count_reliable), (True, 12, True))
        self.assertEqual(self.observe().conversation.selected_name, "测试乙")

    def test_red_inside_avatar_is_not_a_badge(self):
        frame, reader = fixture()
        frame.pixels[80:105, 128:161] = (222, 223, 224)
        frame.pixels[90:130, 105:145] = (109, 138, 166)
        cv2.circle(frame.pixels, (123, 111), 7, (20, 30, 240), -1)
        self.assertFalse(observe_frame(frame, {}, reader).contacts[0].has_unread_indicator)

    def test_activity_formats_remain_raw(self):
        for values in (("5分钟前", "17:18", "昨天"), ("11/04", "星期五", "23:59")):
            rows = self.observe(activities=values).contacts
            self.assertEqual(tuple(row.activity_text for row in rows), values)
            self.assertTrue(all(row.activity_time_reliable for row in rows))

    def test_unknown_activity_ocr_is_preserved_with_no_time_claim(self):
        row = self.observe(activities=("I7:18", "17:18", "昨天")).contacts[0]
        self.assertEqual(row.activity_text, "I7:18")
        self.assertFalse(row.activity_time_reliable)
        self.assertNotIn("I7:18", row.preview)

    def test_scroll_thumbs_are_optional_and_independent(self):
        tracked = self.observe()
        self.assertEqual(tracked.hovered_region, "B2")
        self.assertIsNotNone(tracked.list_scrollbar)
        self.assertIsNotNone(tracked.history_scrollbar)
        absent = self.observe(thumbs=False, hover=(500, 25))
        self.assertEqual(absent.hovered_region, "C1")
        self.assertIsNone(absent.list_scrollbar)
        self.assertIsNone(absent.history_scrollbar)
        self.assertEqual(len(absent.contacts), len(tracked.contacts))

    def test_same_name_is_never_unique_or_official_identity(self):
        result = self.observe(same_name=True)
        self.assertEqual([row.display_name for row in result.contacts[:2]], ["测试乙", "测试乙"])
        self.assertEqual(result.conversation.match_status, "VISUAL_MATCH")
        self.assertFalse(result.conversation.identity_unique)
        self.assertFalse(result.conversation.attribution_reliable)

    def test_mismatch_retains_right_side_observation_without_attribution(self):
        result = self.observe(title="不一致")
        self.assertEqual(result.status, "CONVERSATION_MISMATCH")
        self.assertFalse(result.conversation.attribution_reliable)
        self.assertGreater(len(result.messages), 0)

    def test_no_page_semantics_does_not_read_c2(self):
        frame, _ = fixture()
        reader = FixtureReader([])
        result = observe_frame(frame, {}, reader)
        self.assertEqual(result.status, "PAGE_UNCERTAIN")
        self.assertNotIn(result.layout.region("C2"), reader.regions)

    def test_ocr_exception_retains_layout_diagnostics(self):
        frame, _ = fixture()
        with patch("autoim.wecom.visual_observation.LocalTextRecognizer", side_effect=OcrUnavailable("本地模型不可用")):
            result = observe_frame(frame, {})
        self.assertEqual(result.status, "OCR_UNAVAILABLE")
        self.assertEqual(len(result.layout.regions), 8)

    def test_high_dpi_preserves_contact_and_direction_geometry(self):
        frame, reader = fixture()
        pixels = cv2.resize(frame.pixels, (1800, 1080), interpolation=cv2.INTER_NEAREST)
        state = replace(frame.state, dpi=144, width=1800, height=1080)
        frame = replace(frame, state=state, capture=replace(frame.capture, width=1800,
                                                           height=1080, image=pixels, device_pixel_ratio=1.5))
        scaled = tuple(replace(item, box=Box(*(round(value * 1.5) for value in
                                              (item.box.left, item.box.top, item.box.right, item.box.bottom))))
                       for item in reader.spans)
        result = observe_frame(frame, {}, FixtureReader(scaled, ("7", "12")))
        self.assertEqual(result.conversation.match_status, "VISUAL_MATCH")
        self.assertEqual([message.direction for message in result.messages], ["INBOUND", "OUTBOUND", "INBOUND"])


class MessageVisionTests(unittest.TestCase):
    def test_text_container_direction_duplicates_time_system_and_draft_exclusion(self):
        frame, reader = fixture()
        result = observe_frame(frame, {}, reader)
        self.assertEqual([message.direction for message in result.messages], ["INBOUND", "OUTBOUND", "INBOUND"])
        self.assertEqual([message.kind for message in result.messages], ["TEXT"] * 3)
        self.assertEqual([message.text for message in result.messages], ["你好", "同意", "你好"])
        self.assertEqual([message.time_evidence for message in result.messages], ["11/22 17:39"] * 3)
        self.assertTrue(result.composer.has_visible_draft)
        self.assertIsNotNone(result.composer.toolbar)
        self.assertIsNotNone(result.composer.send_button)
        self.assertIn("某某撤回了一条消息", [item.text for item in result.context_text])

    def test_file_and_image_types_on_each_side_do_not_use_bubble_color(self):
        pixels = np.full((480, 800, 3), 243, dtype=np.uint8)
        region = Box(0, 0, 800, 480)
        rectangles = (Box(55, 45, 255, 108), Box(545, 125, 745, 189),
                      Box(55, 206, 199, 278), Box(606, 291, 745, 380))
        for box in rectangles[:2]:
            pixels[box.top:box.bottom, box.left:box.right] = 255
            cv2.rectangle(pixels, (box.left, box.top), (box.right - 1, box.bottom - 1), (125, 125, 125), 1)
        rng = np.random.default_rng(11)
        for box in rectangles[2:]:
            box.crop(pixels)[:] = rng.integers(2, 210, (box.height, box.width, 3), dtype=np.uint8)
        spans = (span("客户资料.pdf", 70, 55, 120), span("12 KB", 70, 81),
                 span("条款.docx", 553, 133, 120), span("3 MB", 553, 157))
        containers = find_containers(pixels, region, 1, THRESHOLDS)
        messages, _ = classify_messages(pixels, region, containers, spans, 1, {}, THRESHOLDS)
        self.assertEqual([(item.kind, item.direction) for item in messages],
                         [("FILE", "INBOUND"), ("FILE", "OUTBOUND"), ("IMAGE", "INBOUND"), ("IMAGE", "OUTBOUND")])
        self.assertEqual([item.text for item in messages[2:]], ["", ""])

    def test_long_multiline_ocr_preserves_text_and_order(self):
        pixels = np.full((260, 800, 3), 252, dtype=np.uint8)
        region = Box(0, 0, 800, 260)
        pixels[30:109, 40:567] = 210
        containers = find_containers(pixels, region, 1, THRESHOLDS)
        line = "这是完整的第一行长消息文字" * 5
        messages, _ = classify_messages(pixels, region, containers,
                                        (span(line, 52, 46, 495), span("第二行", 52, 72, 120)), 1, {}, THRESHOLDS)
        self.assertEqual(messages[0].text, line + "\n第二行")

    def test_point_text_without_container_cannot_create_message(self):
        pixels = np.full((260, 800, 3), 250, dtype=np.uint8)
        region = Box(0, 0, 800, 260)
        messages, context = classify_messages(pixels, region, find_containers(pixels, region, 1, THRESHOLDS),
                                             (span("只有文字坐标", 40, 30),), 1, {}, THRESHOLDS)
        self.assertFalse(messages)
        self.assertEqual(len(context), 1)

    def test_conflicting_or_missing_anchors_return_unknown(self):
        region = Box(0, 0, 800, 260)
        box = Box(40, 50, 758, 90)
        self.assertEqual(direction_evidence(box, region, (40, 758), THRESHOLDS)[0], "UNKNOWN")
        self.assertEqual(direction_evidence(box, region, (None, None), THRESHOLDS)[0], "UNKNOWN")
        self.assertEqual(direction_evidence(box, region, (40, None), THRESHOLDS)[0], "UNKNOWN")

    def test_low_confidence_and_clipped_message_are_explicit(self):
        pixels = np.full((260, 800, 3), 251, dtype=np.uint8)
        region = Box(0, 0, 800, 260)
        pixels[0:36, 40:180] = 205
        pixels[77:114, 40:180] = 205
        containers = find_containers(pixels, region, 1, THRESHOLDS)
        spans = (span("截断", 55, 9), span("OCR候选", 55, 86, score=.42))
        messages, _ = classify_messages(pixels, region, containers, spans, 1, {}, THRESHOLDS)
        self.assertEqual((messages[0].status, messages[0].direction), ("PARTIALLY_VISIBLE", "UNKNOWN"))
        self.assertEqual((messages[1].status, messages[1].kind), ("OCR_UNCERTAIN", "UNKNOWN"))

    def test_real_local_ocr_reads_three_rendered_chinese_messages_in_memory(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont, QFontDatabase, QImage, QPainter
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        frame, _ = fixture()
        canvas = cv2.cvtColor(frame.pixels, cv2.COLOR_BGR2RGB)
        image = QImage(canvas.data, 1200, 720, canvas.strides[0], QImage.Format.Format_RGB888)
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        font_id = QFontDatabase.addApplicationFont(str(fonts / "msyh.ttc"))
        families = QFontDatabase.applicationFontFamilies(font_id)
        self.assertTrue(families, "Windows 中文 OCR 用例需要 Microsoft YaHei 字体")
        painter = QPainter(image)
        painter.setPen(Qt.GlobalColor.black)
        font = QFont(families[0])
        font.setPixelSize(23)
        painter.setFont(font)
        painter.drawText(420, 43, "测试乙")
        painter.drawText(467, 151, "你好企业微信")
        painter.drawText(944, 206, "已收到消息")
        painter.drawText(467, 258, "今天天气好")
        painter.drawText(1100, 695, "发送")
        painter.end()
        raster = cv2.cvtColor(np.frombuffer(image.constBits(), dtype=np.uint8).reshape(720, 1200, 3),
                              cv2.COLOR_RGB2BGR)
        frame = replace(frame, capture=replace(frame.capture, image=raster))
        with patch("requests.sessions.Session.request", side_effect=AssertionError("OCR must stay offline")):
            result = observe_frame(frame, {}, LocalTextRecognizer())
        self.assertEqual([item.text.replace(" ", "") for item in result.messages],
                         ["你好企业微信", "已收到消息", "今天天气好"])
        self.assertEqual([item.direction for item in result.messages], ["INBOUND", "OUTBOUND", "INBOUND"])
        self.assertTrue(all(item.ocr_spans[0].score > .5 for item in result.messages))


class GuiAndSafetyTests(unittest.TestCase):
    def test_visual_read_path_never_calls_input_clipboard_or_disk_output(self):
        frame, reader = fixture()
        with (patch("autoim.wecom.visual_observation.capture_visible_window", return_value=frame),
              patch("autoim.wecom.visual_observation.LocalTextRecognizer", return_value=reader),
              patch("autoim.wecom.driver.mouse.click_screen_point") as click,
              patch("autoim.automation.keyboard.send_keys") as key,
              patch("autoim.automation.clipboard.read_clipboard") as clipboard,
              patch("autoim.wecom.driver.WeComDriver.send_shortcut") as send,
              patch("autoim.wecom.window_manager.WeComWindowManager.activate") as activate,
              patch("pathlib.Path.write_bytes") as write,
              patch("pathlib.Path.write_text") as write_text,
              patch("builtins.open") as open_file):
            result = read_visible_conversation(None)
        self.assertEqual(len(result.messages), 3)
        for call in (click, key, clipboard, send, activate, write, write_text, open_file):
            call.assert_not_called()

    def test_button_dispatches_and_renders_read_only_diagnostic(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.visual_panel import VisualCapturePanel

        app = QApplication.instance() or QApplication([])
        frame, reader = fixture()
        worker = Mock()
        panel = VisualCapturePanel(Path("unused.json"), worker)
        panel.read_button.click()
        self.assertIs(worker.call_args.args[0], read_visible_conversation)
        self.assertFalse(panel.read_button.isEnabled())
        worker.call_args.kwargs["on_result"](observe_frame(frame, {}, reader))
        self.assertIn("3 条消息", panel.status.text())
        self.assertTrue(panel.read_button.isEnabled())
        panel.result_dialog.close()
        panel.close()
        app.processEvents()
