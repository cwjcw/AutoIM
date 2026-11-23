"""Synthetic file cards and explicit diagnostic-copy regressions."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np

from autoim.wecom.visual_diagnostics import build_visual_report
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_messages import Container, analyze_messages, find_containers
from autoim.wecom.visual_monitor import MonitorEvent, MonitorSnapshot, NewCandidate, VisualMonitor, summarize
from autoim.wecom.visual_observation import observe_frame
from test_wecom_visual_capture import FixtureReader, THRESHOLDS, fixture, span

REGION = Box(0, 0, 800, 460)
LEFT = Box(56, 48, 306, 152)
RIGHT = Box(496, 174, 746, 274)


def _paint(box, pixels, *, contrast=4):
    box.crop(pixels)[:] = 250 + contrast


def _card_ocr(box, names, size, icon=None):
    records = [span(name, box.left + 14, box.top + 10 + i * 19, min(152, 12 * len(name)))
               for i, name in enumerate(names)]
    if size:
        records.append(span(size, box.left + 14, box.top + 10 + max(1, len(names)) * 19, 42))
    if icon:
        records.append(span(icon, box.right - 46, box.top + 22, 26))
    return tuple(records)


def _analyze(cards, extra=(), *, containers=None):
    pixels = np.full((REGION.height, REGION.width, 3), 250, np.uint8)
    all_spans = list(extra)
    for box, rows, size, icon in cards:
        _paint(box, pixels)
        all_spans.extend(_card_ocr(box, rows, size, icon))
    if containers is None:
        containers = find_containers(pixels, REGION, 1, THRESHOLDS)
    return analyze_messages(pixels, REGION, containers, tuple(all_spans), 1, {}, THRESHOLDS)


def _frame_with_file(*, incomplete_old=False):
    frame, reader = fixture(thumbs=False, hover=None)
    pixels = frame.pixels.copy()
    box = Box(451, 328, 685, 415)
    # Existing fixture uses a 252 background, so the low-contrast card is darker.
    box.crop(pixels)[:] = 248
    frame = replace(frame, capture=replace(frame.capture, image=pixels))
    values = tuple(replace(item, score=.35) if incomplete_old and item.box.top == 134 else item
                   for item in reader.spans)
    file_spans = _card_ocr(box, ("测 试+#[2]", "中文报告.PDF"), "6.6K", "PDF")
    unassigned = span("仅完整明细可见☎", 791, 449, 150)
    return frame, observe_frame(frame, {}, FixtureReader((*values, *file_spans, unassigned), ("7", "12")))


class FileRecognitionTests(unittest.TestCase):
    def test_low_contrast_left_md_has_one_inbound_file(self):
        outcome = _analyze(((LEFT, ("检查提示-样例.md",), "6.6K", "M↓"),))
        card, = outcome.messages
        self.assertEqual((card.kind, card.direction, card.status), ("FILE", "INBOUND", "CANDIDATE"))
        self.assertEqual((card.text, card.file.file_extension, card.file.file_size_text),
                         ("检查提示-样例.md", "md", "6.6K"))
        self.assertFalse(card.file.content_read)
        self.assertIn("浅色", card.evidence[0])

    def test_right_two_line_pdf_preserves_ocr_rows_and_special_characters(self):
        names = ("北区 + 样例[07]", "工单#2（修订）.PDF")
        card, = _analyze(((RIGHT, names, "2.4 MB", "PDF"),)).messages
        self.assertEqual((card.direction, card.kind, card.file.file_size_text), ("OUTBOUND", "FILE", "2.4 MB"))
        self.assertEqual(card.file.file_name, "".join(names))
        self.assertEqual(card.file.name_rows, names)
        self.assertEqual(card.file.file_extension, "PDF")
        self.assertEqual([item.text for item in card.file.name_ocr_blocks], list(names))

    def test_three_line_name_joins_even_when_extension_is_split(self):
        names = ("Delivery_附件第", "三次复核.", "pdf")
        card, = _analyze(((RIGHT, names, "16K", "PDF"),)).messages
        self.assertEqual(card.file.file_name, "Delivery_附件第三次复核.pdf")
        self.assertEqual(card.file.name_rows, names)

    def test_icon_is_separate_even_with_low_ocr_confidence(self):
        pixels = np.full((460, 800, 3), 250, np.uint8)
        ocr = _card_ocr(LEFT, ("示例组.md",), "48K", "M↓")
        result = analyze_messages(pixels, REGION, (Container(LEFT, False),),
                                  (*ocr[:-1], replace(ocr[-1], score=.38)), 1, {}, THRESHOLDS)
        message, = result.messages
        self.assertEqual(message.status, "CANDIDATE")
        self.assertEqual([item.text for item in message.file.icon_evidence], ["M↓"])
        self.assertEqual([entry.kind for entry in result.dispositions], ["MEDIA"] * 3)
        self.assertIn("图标区", result.dispositions[-1].reason)

    def test_near_adjacent_cards_are_kept_separate(self):
        second = LEFT.offset(0, LEFT.height + 5)
        result = _analyze(((LEFT, ("资料-A.md",), "22K", "M↓"), (second, ("资料 B.pdf",), "3MB", "PDF")))
        self.assertEqual([(item.file.file_name, item.file.file_size_text) for item in result.messages],
                         [("资料-A.md", "22K"), ("资料 B.pdf", "3MB")])
        self.assertEqual({entry.container_number for entry in result.dispositions}, {1, 2})

    def test_inner_icon_contour_does_not_become_a_second_message(self):
        pixels = np.full((460, 800, 3), 250, np.uint8)
        _paint(LEFT, pixels)
        icon = Box(LEFT.right - 58, LEFT.top + 15, LEFT.right - 13, LEFT.top + 64)
        icon.crop(pixels)[:] = (90, 106, 162)
        contours = find_containers(pixels, REGION, 1, THRESHOLDS)
        self.assertGreaterEqual(len(contours), 2)
        result = analyze_messages(pixels, REGION, contours, _card_ocr(LEFT, ("样例.md",), "7K", "M↓"),
                                  1, {}, THRESHOLDS)
        message, = result.messages
        self.assertEqual(message.file.file_name, "样例.md")
        self.assertEqual({entry.container_number for entry in result.dispositions}, {1})

    def test_inline_name_and_size_still_split_if_ocr_returns_one_block(self):
        message, = _analyze(((LEFT, ("测产 + 日志.md 5 KB",), None, None),)).messages
        self.assertEqual((message.file.file_name, message.file.file_size_text), ("测产 + 日志.md", "5 KB"))

    def test_neighbor_text_and_file_are_not_merged(self):
        text = Box(56, 164, 198, 198)
        result = _analyze(((LEFT, ("工序数据.csv",), "4K", "CSV"),),
                          extra=(span("请看上面的附件", 72, 173, 108),),
                          containers=(Container(LEFT, False), Container(text, False)))
        self.assertEqual([item.kind for item in result.messages], ["FILE", "TEXT"])
        self.assertEqual(result.messages[-1].text, "请看上面的附件")

    def test_standalone_body_with_extension_is_not_a_confirmed_attachment(self):
        result = _analyze((), extra=(span("说明文档.pdf", 66, 64, 85),))
        self.assertEqual(result.messages, ())
        self.assertEqual(result.dispositions[0].kind, "UNASSIGNED")

    def test_file_metadata_can_parse_with_direction_unknown(self):
        middle = Box(276, 125, 526, 216)
        message, = _analyze(((middle, ("资料.md",), "3K", "M↓"),)).messages
        self.assertEqual((message.kind, message.direction, message.status), ("FILE", "UNKNOWN", "UNCLASSIFIED"))
        self.assertEqual(message.file.file_name, "资料.md")

    def test_clipped_file_does_not_invent_a_complete_name(self):
        box = Box(56, 0, 306, 68)
        message, = _analyze(((box, ("抽检说明.md",), "3K", "M↓"),),
                            containers=(Container(box, True),)).messages
        self.assertEqual((message.direction, message.status, message.file.file_name),
                         ("UNKNOWN", "PARTIALLY_VISIBLE", None))
        self.assertEqual(message.file.name_rows, ("抽检说明.md",))

    def test_missing_filename_keeps_visible_size_and_icon(self):
        result = _analyze(((LEFT, (), "7KB", "PDF"),))
        message, = result.messages
        self.assertEqual((message.kind, message.file.file_name, message.file.file_size_text),
                         ("FILE_CANDIDATE", None, "7KB"))
        self.assertEqual([item.text for item in message.file.icon_evidence], ["PDF"])

    def test_visible_ellipsis_preserves_uncertain_raw_rows(self):
        result = _analyze(((RIGHT, ("部分文…字.PDF",), "6.6K", "PDF"),))
        message, = result.messages
        self.assertIsNone(message.file.file_name)
        self.assertEqual(message.file.name_rows, ("部分文…字.PDF",))
        self.assertIn("省略", " ".join(message.file.failure_reasons))

    def test_competing_ocr_owners_cannot_both_create_confirmed_files(self):
        boxes = (Container(LEFT, False), Container(LEFT.offset(3, 4), False))
        result = _analyze(((LEFT, ("资料-重叠.md",), "7K", "M↓"),), containers=boxes)
        self.assertTrue(result.messages)
        self.assertTrue(all(item.file.file_name is None and item.status != "CANDIDATE" for item in result.messages))
        self.assertTrue(any(item.kind == "UNASSIGNED" for item in result.dispositions))

    def test_centered_time_stays_context_beside_the_card(self):
        labels = (span("昨天 23:21", 346, 28, 110), span("8:37", 377, 158, 45),
                  span("星期三 14:40", 342, 287, 116))
        result = _analyze(((LEFT, ("附件.md",), "7K", "M↓"), (RIGHT, ("附件2.pdf",), "8K", "PDF")), labels)
        self.assertEqual([entry.kind for entry in result.dispositions[:3]], ["TIME", "TIME", "TIME"])
        self.assertEqual(len(result.messages), 2)
        self.assertEqual([item.direction for item in result.messages], ["INBOUND", "OUTBOUND"])

    def test_centered_time_does_not_enter_file_fields_even_if_bounds_overlap(self):
        wide = Box(55, 66, 452, 168)
        time = span("8:37", 375, 82, 40)
        result = _analyze(((wide, ("资料.md",), "6K", None),), (time,))
        message, = result.messages
        self.assertEqual(message.file.file_name, "资料.md")
        self.assertEqual(result.dispositions[0].kind, "TIME")
        self.assertNotIn(time, message.ocr_spans)

    def test_file_stays_reliable_in_partial_baseline_without_reading_contents(self):
        frame, observation = _frame_with_file(incomplete_old=True)
        monitor = VisualMonitor(capture=lambda: frame, recognize=lambda _: observation)
        self.addCleanup(monitor.stop)
        monitor.start()
        state = monitor.poll()
        self.assertEqual((state.status, state.candidate_count), ("PARTIAL_BASELINE", 0))
        file, = state.latest_observation.file_messages
        self.assertIn(file.file.file_name, [item.message.text for item in state.latest_observation.reliable_messages])
        self.assertFalse(file.file.content_read)
        self.assertEqual({entry.span.text for entry in state.latest_observation.unassigned_ocr},
                         {"你好", "仅完整明细可见☎"})


class VisualCopyTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.visual_monitor_panel import VisualMonitorPanel

        self.app = QApplication.instance() or QApplication([])
        self.panel = VisualMonitorPanel(None, None)
        self.addCleanup(self.panel.close)
        self.frame, raw = _frame_with_file()
        self.observation = replace(summarize(raw), conversation=replace(
            raw.conversation, selected_raw_name="敏感客户 13900000000", raw_title_line="敏感标题"))
        self.state = MonitorSnapshot(
            status="UNCONFIRMED_VISUAL_CHANGE", reason="内部备注 13900000000", baseline_type="PARTIAL",
            latest_observation=self.observation, latest_sample=self.frame.captured_at, recognition_ms=118.0,
            events=(MonitorEvent(self.frame.captured_at, "UNCONFIRMED_VISUAL_CHANGE", "追加区域证据不足"),),
            unconfirmed_count=1,
        )
        self.panel._render(self.state)

    def test_summary_only_copies_sanitized_window_and_count_data(self):
        with (patch("autoim.automation.clipboard.write_unicode_text") as write,
              patch("autoim.automation.clipboard.read_clipboard") as read,
              patch("autoim.wecom.window_manager.WeComWindowManager.activate") as activate):
            write.assert_not_called()
            self.panel.copy_controls.summary_button.click()
            payload, = write.call_args.args
            read.assert_not_called()
            activate.assert_not_called()
        for field in ("PC-V2.2", "WINDOW_VISUAL_OCR", "HWND 0x51", "PrintWindow", "DPI", "B2 选中行",
                      "C1 标题", "基线 PARTIAL", "118 ms", "文件卡片候选 1", "未归属 OCR 1", "未确认变化 1"):
            self.assertIn(field, payload)
        for private in ("敏感客户", "敏感标题", "13900000000", "中文报告.PDF", "仅完整明细可见☎", "你好"):
            self.assertNotIn(private, payload)
        self.assertIn("已复制", self.panel.copy_controls.feedback.text())

    def test_complete_copy_requires_sensitive_information_confirmation(self):
        from PySide6.QtWidgets import QMessageBox

        with (patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm,
              patch("autoim.automation.clipboard.write_unicode_text") as write):
            self.panel.copy_controls.full_button.click()
            write.assert_not_called()
            self.assertIn("聊天正文", confirm.call_args.args[2])
            self.assertEqual(confirm.call_args.args[-1], QMessageBox.StandardButton.No)

    def test_full_copy_contains_unicode_file_details_and_separate_events(self):
        from PySide6.QtWidgets import QMessageBox

        file, = self.observation.file_messages
        candidate = NewCandidate(self.frame.captured_at, "试验", "试验", "VISUAL_MATCH", False, file, "稳定尾部")
        self.panel._render(replace(self.state, candidates=(candidate,), candidate_count=1))
        with (patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
              patch("autoim.automation.clipboard.write_unicode_text") as write):
            self.panel.copy_controls.full_button.click()
            payload, = write.call_args.args
        for field in ("测 试+#[2]中文报告.PDF", "6.6K", "文件名原始 OCR 块", "附件内容未读取",
                      "仅完整明细可见☎", "新增消息候选", "未确认变化记录", "追加区域证据不足"):
            self.assertIn(field, payload)
        self.assertEqual(payload, payload.encode("utf-8").decode("utf-8"))

    def test_empty_diagnostics_are_explicit_and_can_be_copied(self):
        self.panel._render(MonitorSnapshot())
        with patch("autoim.automation.clipboard.write_unicode_text") as write:
            self.panel.copy_controls.summary_button.click()
            payload, = write.call_args.args
        self.assertIn("尚无有效观察", payload)
        self.assertIn("文件卡片候选 0", payload)

    def test_clipboard_failure_reports_reason(self):
        with patch("autoim.automation.clipboard.write_unicode_text", side_effect=TimeoutError("剪贴板被占用")):
            self.panel.copy_controls.summary_button.click()
        self.assertIn("复制失败 · 剪贴板被占用", self.panel.copy_controls.feedback.text())

    def test_single_capture_dialog_exposes_the_same_report_and_file_rows(self):
        from autoim.wecom.visual_panel import VisualResultDialog

        _, raw = _frame_with_file()
        dialog = VisualResultDialog(raw)
        self.addCleanup(dialog.close)
        with patch("autoim.automation.clipboard.write_unicode_text") as write:
            dialog.copy_controls.summary_button.click()
            self.assertEqual(write.call_args.args[0], build_visual_report(summarize(raw)))
        headers = [self.panel.observed_table.horizontalHeaderItem(i).text()
                   for i in range(self.panel.observed_table.columnCount())]
        self.assertIn("文件大小", headers)
