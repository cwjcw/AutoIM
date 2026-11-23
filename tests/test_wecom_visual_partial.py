"""Synthetic C2 gaps and their effect on PC visual monitoring."""

from __future__ import annotations

import unittest
from dataclasses import replace

from autoim.wecom.visual_capture import CaptureError
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_monitor import VisualMonitor, compare_sequences, summarize
from autoim.wecom.visual_observation import OcrAreaObservation, observe_frame
from test_wecom_visual_capture import FixtureReader, fixture, span


def _history(*appended, first_score=.98):
    frame, reader = fixture(thumbs=False, hover=None)
    pixels = frame.pixels.copy()
    spans = [replace(item, score=first_score) if item.box.top == 134 else item for item in reader.spans]
    for i, (text, score, height) in enumerate(appended):
        top = 319 + i * 71
        pixels[top:top + height, 451:635] = 209
        spans.append(span(text, 466, top + 8, 145, score))
    frame = replace(frame, capture=replace(frame.capture, image=pixels))
    return frame, observe_frame(frame, {}, FixtureReader(tuple(spans), ("7", "12")))


def _shift(observation, dy):
    return replace(observation, messages=tuple(replace(item, message=replace(
        item.message, box=item.message.box.offset(0, dy))) for item in observation.messages),
        gaps=tuple(replace(gap, box=gap.box.offset(0, dy)) if gap.box else gap for gap in observation.gaps))


class PartialEvidenceTests(unittest.TestCase):
    def test_five_of_six_messages_and_low_confidence_ocr_are_retained(self):
        _, result = _history(("第四条", .98, 35), ("第五条", .98, 35), ("第六条", .98, 35), first_score=.35)
        observed = summarize(result)
        self.assertEqual((observed.baseline_type, len(observed.messages), len(observed.reliable_messages)),
                         ("PARTIAL", 6, 5))
        gap, = observed.gaps
        self.assertEqual((gap.direction, gap.kind, gap.spans[0].text, gap.spans[0].score),
                         ("INBOUND", "UNKNOWN", "你好", .35))
        self.assertEqual(gap.box, observed.messages[0].message.box)
        self.assertIn("置信度不足", gap.reason)

    def test_file_card_missing_size_keeps_filename_without_promoting_to_text(self):
        _, result = _history(("测试报告.pdf", .98, 52))
        observed = summarize(result)
        gap, = observed.gaps
        self.assertEqual((gap.kind, gap.spans[0].text), ("FILE_CANDIDATE", "测试报告.pdf"))
        self.assertIn("大小证据不足", gap.reason)
        self.assertEqual(len(observed.reliable_messages), 3)

    def test_image_placeholder_retains_media_evidence_without_reliable_message(self):
        _, result = _history(("图片加载中", .98, 54))
        observed = summarize(result)
        gap, = observed.gaps
        self.assertEqual(gap.kind, "IMAGE_PLACEHOLDER")
        self.assertIn("图片占位", gap.reason)
        self.assertEqual(observed.baseline_type, "PARTIAL")

    def test_quote_card_stays_an_uncertain_observation(self):
        _, result = _history(("引用测试文字", .98, 42))
        gap, = summarize(result).gaps
        self.assertEqual(gap.kind, "COMPLEX_CANDIDATE")
        self.assertIn("正文边界", gap.reason)

    def test_centered_time_and_system_text_do_not_create_message_gaps(self):
        _, result = _history()
        observed = summarize(result)
        self.assertEqual((observed.baseline_type, len(observed.reliable_messages), observed.gaps), ("RELIABLE", 3, ()))
        self.assertEqual([entry.kind for entry in observed.context_ocr], ["TIME", "SYSTEM"])

    def test_unassigned_text_is_preserved_with_unknown_direction(self):
        frame, reader = fixture(thumbs=False, hover=None)
        unassigned = span("没有对应容器的文字", 473, 372, 160, .92)
        observation = observe_frame(frame, {}, FixtureReader((*reader.spans, unassigned), ("7", "12")))
        gap, = summarize(observation).gaps
        self.assertEqual((gap.box, gap.direction, gap.kind, gap.spans), (
            unassigned.box, "UNKNOWN", "UNASSIGNED_OCR", (unassigned,)))
        self.assertIn("候选框", gap.reason)

    def test_region_ocr_failure_blocks_partial_append_even_with_local_gap(self):
        _, old = _history(first_score=.35)
        _, new = _history(("新内容", .98, 35), first_score=.35)
        failed = OcrAreaObservation("C2 历史消息", new.layout.region("C2"), "FAILED", reason="模拟 C2 OCR 失败")
        result = compare_sequences(summarize(old), summarize(replace(new, ocr_areas=(failed,))))
        self.assertEqual((result.status, result.appended), ("UNCONFIRMED_VISUAL_CHANGE", ()))
        self.assertIn("无法定位", result.reason)

    def test_partial_motion_requires_bottom_evidence_and_rejects_manual_scroll(self):
        _, old = _history(first_score=.35)
        _, new = _history(("新内容", .98, 35), first_score=.35)
        before = summarize(old)
        after = _shift(summarize(new), -44)
        self.assertEqual(compare_sequences(before, after).status, "RESYNC_REQUIRED")
        bottom = compare_sequences(replace(before, at_bottom=True), replace(after, at_bottom=True))
        self.assertEqual(bottom.status, "VISUAL_NEW_CANDIDATE")
        hovered = compare_sequences(replace(before, at_bottom=True), replace(after, at_bottom=True),
                                    cursor_in_history=True)
        self.assertEqual(hovered.status, "RESYNC_REQUIRED")

    def test_partial_return_from_history_cannot_report_an_append(self):
        _, old = _history(first_score=.35)
        _, new = _history(("底部已存在的消息", .98, 35), first_score=.35)
        before, after = summarize(old), summarize(new)
        gutter = before.regions.history.right - 4
        before = replace(before, scroll_thumb=Box(gutter, 110, gutter + 2, 200))
        after = replace(after, scroll_thumb=Box(gutter, 430, gutter + 2, 550), at_bottom=True)
        outcome = compare_sequences(before, after)
        self.assertEqual((outcome.status, outcome.appended), ("RESYNC_REQUIRED", ()))


class PartialMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.frame, self.observation = _history(first_score=.35)
        self.monitor = VisualMonitor(capture=lambda: self.frame, recognize=lambda _: self.observation,
                                     clock=lambda: self.now)
        self.addCleanup(self.monitor.stop)
        self.monitor.start()

    def poll(self, elapsed=2):
        self.now += elapsed
        return self.monitor.poll()

    def changed(self, *appended, **kwargs):
        self.frame, self.observation = _history(*appended, **kwargs)
        self.poll()
        return self.poll()

    def test_first_partial_observation_never_replays_history(self):
        first = self.poll()
        self.assertEqual((first.status, first.baseline_type, first.baseline_count, first.candidates),
                         ("PARTIAL_BASELINE", "PARTIAL", 2, ()))
        self.assertIs(first.latest_observation, first.observations[-1])
        self.assertEqual((first.latest_observation.page, first.latest_observation.selected_row_status),
                         ("MESSAGES_CANDIDATE", "CANDIDATE"))
        unchanged = self.poll()
        self.assertEqual((unchanged.recognition_count, unchanged.baseline_type, unchanged.candidate_count),
                         (1, "PARTIAL", 0))

    def test_no_reliable_messages_keeps_diagnostics_without_creating_partial_baseline(self):
        self.observation = replace(self.observation, messages=tuple(
            replace(item, status="OCR_UNCERTAIN") for item in self.observation.messages))
        state = self.poll()
        self.assertEqual((state.status, state.baseline_type, state.baseline_count, state.candidates),
                         ("OBSERVATION_UNRELIABLE", "NONE", 0, ()))
        self.assertEqual(len(state.latest_observation.gaps), 3)

    def test_partial_old_gap_allows_independently_proven_inbound_tail(self):
        self.poll()
        state = self.changed(("新消息", .98, 35), first_score=.35)
        self.assertEqual((state.status, state.baseline_type, state.baseline_count),
                         ("VISUAL_NEW_CANDIDATE", "PARTIAL", 3))
        one, = state.candidates
        self.assertEqual((one.message.text, one.message.direction), ("新消息", "INBOUND"))
        self.assertIn("前段识别曾不完整", one.integrity_risk)
        self.assertFalse(one.auto_reply_ready)

    def test_ocr_only_recovery_updates_baseline_without_reporting_history(self):
        self.poll()
        self.frame, self.observation = _history()
        state = self.poll(elapsed=10)
        self.assertEqual((state.status, state.baseline_type, state.candidate_count, state.recognition_count),
                         ("BASELINE_CREATED", "RELIABLE", 0, 2))

    def test_partial_gap_in_new_area_is_unconfirmed_and_not_queued(self):
        self.poll()
        state = self.changed(("图片加载中", .98, 54), first_score=.35)
        self.assertEqual((state.status, state.candidates, state.unconfirmed_count),
                         ("UNCONFIRMED_VISUAL_CHANGE", (), 1))
        self.assertIn("新增区域", state.reason)
        self.assertEqual([len(item.gaps) for item in state.observations], [1, 2])

    def test_old_gap_at_tail_prevents_using_newly_read_old_text_as_anchor(self):
        frame, base = _history()
        old = tuple(replace(item, status="OCR_UNCERTAIN") if i == 2 else item
                    for i, item in enumerate(base.messages))
        self.frame, self.observation = frame, replace(base, messages=old, message_completeness="VISIBLE_MESSAGES_INCOMPLETE")
        self.poll()
        state = self.changed(("可能新增", .98, 35))
        self.assertEqual((state.status, state.candidate_count), ("UNCONFIRMED_VISUAL_CHANGE", 0))
        self.assertIn("尾部重叠", state.reason)

    def test_unrelated_pixels_cannot_promote_tail_ocr_recovery_to_candidate(self):
        old, _ = _history(first_score=.35)
        self.poll()
        _, self.observation = _history(("补读容器", .98, 35), first_score=.35)
        pixels = old.pixels.copy()
        pixels[365:413, 890:1010] = 151
        self.frame = replace(old, capture=replace(old.capture, image=pixels))
        self.poll()
        state = self.poll()
        self.assertEqual((state.status, state.candidate_count), ("UNCONFIRMED_VISUAL_CHANGE", 0))
        self.assertIn("追加区像素", state.reason)

    def test_reliable_baseline_also_requires_local_append_pixel_evidence(self):
        self.frame, self.observation = _history()
        self.assertEqual(self.poll().baseline_type, "RELIABLE")
        _, self.observation = _history(("补读容器", .98, 35))
        pixels = self.frame.pixels.copy()
        pixels[365:413, 890:1010] = 151
        self.frame = replace(self.frame, capture=replace(self.frame.capture, image=pixels))
        self.poll()
        state = self.poll()
        self.assertEqual((state.status, state.candidate_count), ("UNCONFIRMED_VISUAL_CHANGE", 0))
        self.assertIn("追加区像素", state.reason)

    def test_two_identical_tail_messages_keep_separate_occurrences(self):
        self.poll()
        state = self.changed(("你好", .98, 35), ("你好", .98, 35), first_score=.35)
        self.assertEqual(state.status, "VISUAL_NEW_CANDIDATE")
        self.assertEqual([item.message.text for item in state.candidates], ["你好", "你好"])
        self.assertLess(state.candidates[0].message.box.top, state.candidates[1].message.box.top)

    def test_three_tail_messages_are_published_in_visual_order(self):
        self.poll()
        state = self.changed(("第一条", .98, 35), ("第二条", .98, 35), ("第三条", .98, 35), first_score=.35)
        self.assertEqual([item.message.text for item in state.candidates], ["第一条", "第二条", "第三条"])

    def test_window_loss_and_resync_keep_previous_diagnostic_observations(self):
        first = self.poll()

        def closed():
            raise CaptureError("WINDOW_UNAVAILABLE", "模拟关闭")

        self.monitor._capture = closed
        paused = self.poll()
        self.assertEqual((paused.status, paused.baseline_type, paused.observations),
                         ("PAUSED", "NONE", first.observations))
        self.assertIsNone(paused.latest_observation)

    def test_gui_separates_partial_observations_from_new_candidates(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.visual_monitor_panel import VisualMonitorPanel

        app = QApplication.instance() or QApplication([])
        panel = VisualMonitorPanel(None, None)
        self.addCleanup(panel.close)
        state = self.poll()
        panel._render(state)
        self.assertIn("可见消息候选 3 · 可靠识别 2 · 不可靠 1", panel.summary.text())
        self.assertIn("部分基线 · 可靠基线尚未建立", panel.summary.text())
        self.assertEqual(panel.table.rowCount(), 0)
        rows = [[panel.observed_table.item(i, j).text() for j in range(panel.observed_table.columnCount())]
                for i in range(panel.observed_table.rowCount())]
        self.assertEqual(sum(row[4] == "不可靠" for row in rows), 1)
        self.assertTrue(any("35%" in row[3] and "置信度不足" in row[5] for row in rows))
        app.processEvents()
