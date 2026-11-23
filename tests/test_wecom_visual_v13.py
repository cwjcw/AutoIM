"""De-identified geometry regressions for Windows PC-V1.3."""

from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from autoim.wecom.visual_contacts import detect_contacts
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_messages import Container, analyze_messages, find_containers
from autoim.wecom.visual_observation import observe_frame
from autoim.wecom.visual_ocr import TextSpan
from test_wecom_visual_capture import FixtureReader, THRESHOLDS, fixture, span


def _title_result(value, *, same_name=False, score=.98):
    frame, reader = fixture(same_name=same_name)
    spans = tuple(item for item in reader.spans if item.box != Box(418, 23, 538, 38))
    spans += (span(value, 418, 23, 305, score),)
    return observe_frame(frame, {}, FixtureReader(spans, ("7", "12"))).conversation


class ConversationV13Tests(unittest.TestCase):
    def test_same_name_with_phone_and_metadata_on_one_line(self):
        result = _title_result("测试乙 13900000000 示例业务部")
        self.assertEqual(result.match_status, "VISUAL_MATCH")
        self.assertEqual(result.title, "测试乙")
        self.assertIn("按手机号", result.comparison_evidence)
        self.assertIn("示例业务部", result.visible_identity[0])

    def test_same_name_with_organization_only(self):
        result = _title_result("测试乙 示例集团")
        self.assertEqual(result.match_status, "VISUAL_MATCH")
        self.assertIn("组织", result.comparison_evidence)

    def test_organization_suffix_does_not_hide_reliable_mismatch(self):
        result = _title_result("另一方 示例集团")
        self.assertEqual(result.match_status, "CONVERSATION_MISMATCH")
        self.assertTrue(result.title_name_reliable)

    def test_same_name_does_not_verify_unique_identity(self):
        result = _title_result("测试乙 示例中心", same_name=True)
        self.assertEqual(result.match_status, "VISUAL_MATCH")
        self.assertFalse(result.identity_unique or result.attribution_reliable)

    def test_weak_or_ambiguous_title_returns_unknown(self):
        for title, score in (("另一个名字", .39), ("测试乙 示例标签", .98), ("测试乙…", .99)):
            with self.subTest(title=title):
                result = _title_result(title, score=score)
                self.assertEqual(result.match_status, "UNKNOWN")
                self.assertFalse(result.title_name_reliable)

    def test_multiple_ocr_pieces_can_share_name_line_with_metadata(self):
        frame, reader = fixture()
        spans = tuple(item for item in reader.spans if item.box != Box(418, 23, 538, 38))
        spans += (span("测试乙", 418, 23, 62), span("13900000000", 496, 23, 109),
                  span("示例中心", 620, 23, 67))
        result = observe_frame(frame, {}, FixtureReader(spans, ("7", "12"))).conversation
        self.assertEqual(result.title, "测试乙")
        self.assertEqual(result.match_status, "VISUAL_MATCH")

    def test_full_group_title_with_organization_words_is_preserved(self):
        frame, reader = fixture(title="交付组 示例集团")
        spans = tuple(replace(item, text="交付组 示例集团") if item.text == "测试乙" else item
                      for item in reader.spans)
        result = observe_frame(frame, {}, FixtureReader(spans, ("7", "12"))).conversation
        self.assertEqual(result.title, "交付组 示例集团")
        self.assertEqual(result.match_status, "VISUAL_MATCH")


def _contact_rows(*, bottom=417, diagnostics=None):
    pixels = np.full((460, 560, 3), 237, dtype=np.uint8)
    region = Box(40, 70, 430, bottom)
    for y in (110, 173, 236):
        pixels[y-20:y+20, 55:95] = (135, 100, 156)
    # Different icons at the bottom and one row with no preview.
    pixels[290:298, 65:77] = (90, 75, 70)
    pixels[352:358, 65:78] = (90, 75, 70)
    values = ("普通同事", "交付群(8人)", "外部联系人", "值班机器人", "企业微信系统通知", "待处理会话")
    spans = []
    for index, name in enumerate(values):
        y = 110 + index * 63
        spans.append(span(name, 115, y - 20, 125))
        if index < 3:
            spans.append(span("预览摘要", 115, y + 5))
    return detect_contacts(pixels, region, FixtureReader(tuple(spans)), 1, THRESHOLDS, diagnostics)


class ContactV13Tests(unittest.TestCase):
    def test_person_and_group_rows_preserve_type_candidates(self):
        rows = _contact_rows()
        self.assertEqual((rows[0].display_name, rows[0].kind), ("普通同事", "CONTACT"))
        self.assertEqual((rows[1].display_name, rows[1].kind), ("交付群(8人)", "GROUP_CANDIDATE"))

    def test_bot_and_system_without_avatar_or_preview_are_retained(self):
        rows = _contact_rows()
        self.assertEqual([row.display_name for row in rows[3:5]], ["值班机器人", "企业微信系统通知"])
        self.assertEqual([row.kind for row in rows[3:5]], ["BOT_CANDIDATE", "SYSTEM_CANDIDATE"])
        self.assertTrue(all(not row.preview and row.avatar is None for row in rows[3:5]))

    def test_last_row_position_and_partial_contact_are_preserved(self):
        rows = _contact_rows(bottom=425)
        self.assertEqual(rows[-1].display_name, "待处理会话")
        self.assertEqual(rows[-1].status, "PARTIALLY_VISIBLE")
        self.assertEqual(rows[-1].box.bottom, 425)

    def test_low_confidence_text_has_contact_filter_reason(self):
        pixels = np.full((250, 500, 3), 246, dtype=np.uint8)
        area = Box(10, 10, 480, 240)
        text = span("候选行", 101, 164, score=.39)
        diagnostics = []
        rows = detect_contacts(pixels, area, FixtureReader((text,)), 1, THRESHOLDS, diagnostics)
        self.assertFalse(rows)
        self.assertEqual(diagnostics[0].span, text)
        self.assertIn("低置信", diagnostics[0].reason)

    def test_partially_clipped_avatar_with_no_name_keeps_row_candidate(self):
        pixels = np.full((250, 500, 3), 246, dtype=np.uint8)
        region = Box(10, 10, 480, 240)
        pixels[222:240, 26:71] = (142, 90, 95)
        rows = detect_contacts(pixels, region, FixtureReader(()), 1, THRESHOLDS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "PARTIALLY_VISIBLE")
        self.assertFalse(rows[0].name_reliable)


def _six_messages():
    pixels = np.full((700, 1180, 3), (248, 246, 244), np.uint8)
    region = Box(140, 40, 1120, 655)
    boxes = (Box(330, 106, 1068, 150), Box(186, 180, 513, 228),
             Box(953, 262, 1068, 310), Box(186, 336, 445, 385),
             Box(1009, 415, 1068, 466), Box(186, 510, 384, 558))
    values = ("这是一条长度不同的测试消息", "收到", "请核对", "回复内容", "好", "收到")
    spans = []
    for i, (box, value) in enumerate(zip(boxes, values)):
        color = (255, 247, 242) if i % 2 == 0 else (213, 214, 217)
        box.crop(pixels)[:] = color
        right = box.right + 4 if i == 0 else box.right - 7
        spans.append(TextSpan(value, Box(box.left + 7, box.top + 12, right, box.bottom - 11), .99))
    spans += [span("12:34", 594, 72), span("以下是新消息", 582, 483, 112)]
    return pixels, region, values, tuple(spans)


class MessageV13Tests(unittest.TestCase):
    def test_six_messages_keep_order_and_both_blue_width_extremes(self):
        pixels, region, values, spans = _six_messages()
        containers = find_containers(pixels, region, 1, THRESHOLDS)
        result = analyze_messages(pixels, region, containers, spans, 1, {}, THRESHOLDS)
        self.assertEqual(tuple(item.text for item in result.messages), values)
        self.assertEqual([item.direction for item in result.messages], ["OUTBOUND", "INBOUND"] * 3)
        self.assertEqual([item.sequence for item in result.messages], list(range(1, 7)))
        self.assertEqual(sum(item.text == "收到" for item in result.messages), 2)

    def test_near_edge_ocr_spill_is_linked_to_the_actual_container_once(self):
        pixels, region, _, spans = _six_messages()
        result = analyze_messages(pixels, region, find_containers(pixels, region, 1, THRESHOLDS),
                                  spans, 1, {}, THRESHOLDS)
        self.assertEqual(result.dispositions[0].kind, "MESSAGE")
        self.assertEqual(result.dispositions[0].container_number, 1)
        self.assertEqual(len(result.dispositions), len(spans))

    def test_time_and_system_have_separate_dispositions(self):
        pixels, region, _, spans = _six_messages()
        result = analyze_messages(pixels, region, find_containers(pixels, region, 1, THRESHOLDS),
                                  spans, 1, {}, THRESHOLDS)
        self.assertEqual([item.kind for item in result.dispositions[-2:]], ["TIME", "SYSTEM"])
        self.assertEqual(result.messages[0].time_evidence, "12:34")
        self.assertNotIn("以下是新消息", [item.text for item in result.messages])

    def test_time_value_in_a_genuine_side_bubble_is_dialogue(self):
        pixels, region, _, spans = _six_messages()
        revised = (replace(spans[0], text="12:34"), *spans[1:])
        result = analyze_messages(pixels, region, find_containers(pixels, region, 1, THRESHOLDS),
                                  revised, 1, {}, THRESHOLDS)
        self.assertEqual(result.dispositions[0].kind, "MESSAGE")

    def test_file_and_picture_ocr_go_to_whole_media_card(self):
        pixels = np.full((330, 750, 3), 240, dtype=np.uint8)
        region = Box(0, 0, 750, 330)
        file = Container(Box(45, 35, 280, 115), False)
        image = Container(Box(455, 140, 705, 270), False)
        rng = np.random.default_rng(22)
        image.box.crop(pixels)[:] = rng.integers(15, 198, (130, 250, 3), dtype=np.uint8)
        spans = (span("资料.pdf", 65, 46), span("48 KB", 64, 80),
                 span("图片里的文字", 490, 170, 103))
        result = analyze_messages(pixels, region, (file, image), spans, 1, {}, THRESHOLDS)
        self.assertEqual([item.kind for item in result.messages], ["FILE", "IMAGE"])
        self.assertEqual([item.kind for item in result.dispositions], ["MEDIA"] * 3)
        self.assertEqual(result.messages[1].text, "")

    def test_unmatched_ocr_has_nearest_candidate_reason_and_never_an_inferred_direction(self):
        frame, reader = fixture()
        extra = span("独立未关联文字", 1014, 350, 146, .99)
        result = observe_frame(frame, {}, FixtureReader((*reader.spans, extra), ("7", "12")))
        record = next(item for item in result.message_ocr if item.span == extra)
        self.assertEqual(record.kind, "UNASSIGNED")
        self.assertIsNotNone(record.nearest_container)
        self.assertTrue(record.possible_message)
        self.assertIn("未达到", record.reason)
        self.assertEqual(result.message_completeness, "VISIBLE_MESSAGES_INCOMPLETE")
        self.assertFalse(any(extra in item.ocr_spans for item in result.messages))

    def test_center_inside_but_excessive_spill_does_not_silently_disappear(self):
        pixels, region, _, spans = _six_messages()
        revised = (replace(spans[0], box=Box(335, 118, 1114, 139)), *spans[1:])
        result = analyze_messages(pixels, region, find_containers(pixels, region, 1, THRESHOLDS),
                                  revised, 1, {}, THRESHOLDS)
        self.assertEqual(result.dispositions[0].kind, "UNASSIGNED")
        self.assertIn(revised[0], result.context)

    def test_two_close_overlapping_containers_do_not_both_claim_same_ocr(self):
        pixels = np.full((300, 750, 3), 240, dtype=np.uint8)
        region = Box(0, 0, 750, 300)
        containers = (Container(Box(510, 50, 706, 110), False),
                      Container(Box(524, 56, 705, 117), False))
        result = analyze_messages(pixels, region, containers, (span("重叠正文", 559, 67, 88),),
                                  1, {}, THRESHOLDS)
        diagnostic = result.dispositions[0]
        self.assertEqual(diagnostic.kind, "UNASSIGNED")
        self.assertIn("多个", diagnostic.reason)
        self.assertTrue(all(not msg.text for msg in result.messages))

    def test_blue_square_without_associated_text_does_not_supply_direction(self):
        pixels = np.full((300, 800, 3), (248, 246, 244), np.uint8)
        region = Box(0, 0, 800, 300)
        pixels[50:94, 750:794] = (255, 247, 242)
        candidates = find_containers(pixels, region, 1, THRESHOLDS)
        self.assertTrue(candidates[0].needs_text)
        result = analyze_messages(pixels, region, candidates, (), 1, {}, THRESHOLDS)
        self.assertFalse(result.messages)
        self.assertEqual(result.anchors, (None, None))

    def test_diagnostic_dialog_exposes_ledger_and_incomplete_overlay(self):
        from PySide6.QtWidgets import QApplication, QCheckBox, QTableWidget
        from autoim.wecom.visual_panel import VisualResultDialog

        app = QApplication.instance() or QApplication([])
        frame, reader = fixture()
        extra = span("待排查正文", 1017, 345, 123)
        outcome = observe_frame(frame, {}, FixtureReader((*reader.spans, extra), ("7", "12")))
        dialog = VisualResultDialog(outcome)
        self.addCleanup(dialog.close)
        dialog.show()
        app.processEvents()
        tab = next(dialog.tabs.widget(i) for i in range(dialog.tabs.count())
                   if dialog.tabs.tabText(i) == "C2 文字归属")
        table = tab.findChild(QTableWidget)
        self.assertEqual(table.rowCount(), len(outcome.message_ocr))
        row = next(i for i in range(table.rowCount()) if table.item(i, 0).text() == extra.text)
        self.assertEqual(table.item(row, 3).text(), "UNASSIGNED")
        old = len(dialog.canvas.overlays)
        toggle = next(item for item in dialog.findChildren(QCheckBox) if item.text().startswith("显示未归属"))
        toggle.setChecked(True)
        self.assertGreater(len(dialog.canvas.overlays), old)
