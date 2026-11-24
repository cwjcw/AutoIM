from __future__ import annotations

import os
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from autoim.wecom.visual_capture import CaptureError
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_messages import VisibleMessageObservation
from autoim.wecom.visual_monitor import (MonitorObservation, MonitoredMessage, MonitorSnapshot, ViewRegions, VisualMonitor,
                                        compare_sequences, probe_frame, summarize)
from autoim.wecom.visual_observation import observe_frame
from autoim.wecom.visual_ocr import OcrUnavailable, TextSpan
from test_wecom_visual_capture import FixtureReader, fixture, span

AT = datetime(2026, 11, 22, tzinfo=timezone.utc)


def _named_reader(reader, left, title, *, title_label="", left_tag=None):
    changed = []
    for item in reader.spans:
        if item.box.top == 160 and item.text == "测试乙":
            changed.append(replace(item, text=left))
        elif item.box.top == 23:
            if title is not None:
                changed.append(replace(item, text=title))
        elif item.text == "＠微信":
            if title_label:
                changed.append(replace(item, text=title_label))
        else:
            changed.append(item)
    if left_tag:
        changed.append(left_tag)
    return FixtureReader(tuple(changed), ("7", "12"))


def _message(text, index, *, direction="INBOUND", kind="TEXT", shift=0):
    top = 83 + (index - 1) * 46 + shift
    left = 442 if direction == "INBOUND" else 1003
    box = Box(left, top, left + 120, top + 30)
    spans = (TextSpan(text, box, .96),) if text else ()
    message = VisibleMessageObservation(index, box, direction, kind, text, "", 0, None,
                                        "CANDIDATE", ("测试容器布局",), spans)
    return MonitoredMessage(message)


class MonitorFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame, reader = fixture()
        cls.result = replace(observe_frame(cls.frame, {}, reader), history_scrollbar=None)
        cls.observed = summarize(cls.result)
        if cls.observed.blocker:
            raise AssertionError(cls.observed.blocker)

    def sequence(self, names, *, offset=0, shift=0, bottom=False, **changes) -> MonitorObservation:
        messages = tuple(_message(text, index + offset, shift=shift) for index, text in enumerate(names, 1))
        return replace(self.observed, messages=messages, at_bottom=bottom, **changes)

    def changed_frame(self, version=1, *, cursor=None, hwnd=None):
        pixels = self.frame.pixels.copy()
        pixels[407:451, 930:1040] = 255 - version * 23
        capture = replace(self.frame.capture, image=pixels, hwnd=hwnd or self.frame.capture.hwnd)
        return replace(self.frame, capture=capture, mouse=cursor, captured_at=AT + timedelta(seconds=version))

    def recognized(self, frame, names, **changes):
        outcome = replace(self.result, frame=frame, messages=tuple(_message(text, i).message
                           for i, text in enumerate(names, 1)), **changes)
        return outcome


class SequenceTests(MonitorFixture):
    def test_one_inbound_message_after_preserved_prefix(self):
        delta = compare_sequences(self.sequence(["甲", "乙"]), self.sequence(["甲", "乙", "丙"]))
        self.assertEqual(delta.status, "VISUAL_NEW_CANDIDATE")
        self.assertEqual([(item.message.text, item.message.direction) for item in delta.appended], [("丙", "INBOUND")])

    def test_three_consecutive_messages_preserve_order(self):
        old = self.sequence(["甲", "乙"])
        new = self.sequence(["甲", "乙", "第一", "第二", "第三"])
        self.assertEqual([item.message.text for item in compare_sequences(old, new).appended], ["第一", "第二", "第三"])

    def test_consecutive_duplicate_content_remains_two_occurrences(self):
        old = self.sequence(["开头", "收到", "收到"])
        new = self.sequence(["开头", "收到", "收到", "收到", "收到"])
        self.assertEqual([item.message.sequence for item in compare_sequences(old, new).appended], [4, 5])

    def test_outbound_message_keeps_direction(self):
        old = self.sequence(["甲", "乙"])
        new = replace(old, messages=old.messages + (_message("我的回复", 3, direction="OUTBOUND"),))
        self.assertEqual(compare_sequences(old, new).appended[0].message.direction, "OUTBOUND")

    def test_common_small_relayout_without_new_content(self):
        old = self.sequence(["甲", "乙"])
        self.assertEqual(compare_sequences(old, self.sequence(["甲", "乙"], shift=-4)).status, "NO_CHANGE")

    def test_ocr_jitter_preserves_original_baseline(self):
        old = self.sequence(["这是测试会话中的一条足够长的文字内容", "另一条"])
        new = self.sequence(["这是测试会话中的一条足够长的文字内客", "另一条"])
        delta = compare_sequences(old, new)
        self.assertEqual(delta.status, "NO_CHANGE")
        self.assertTrue(delta.preserve_baseline)

    def test_image_appearance_is_a_visual_candidate(self):
        old = self.sequence(["甲", "乙"])
        image = replace(_message("", 3, kind="IMAGE"), media_pixels=np.full((14, 20, 3), 54, np.uint8))
        result = compare_sequences(old, replace(old, messages=old.messages + (image,)))
        self.assertEqual(result.appended[0].message.kind, "IMAGE")

    def test_attachment_appearance_is_a_visual_candidate(self):
        old = self.sequence(["甲", "乙"])
        result = compare_sequences(old, replace(old, messages=old.messages + (_message("样例.pdf 30KB", 3, kind="FILE"),)))
        self.assertEqual(result.appended[0].message.kind, "FILE")

    def test_history_scroll_down_with_shift_is_ambiguous_without_bottom(self):
        old = self.sequence(["甲", "乙", "丙", "丁"])
        new = self.sequence(["乙", "丙", "丁", "戊"])
        change = compare_sequences(old, new)
        self.assertEqual(change.status, "RESYNC_REQUIRED")
        self.assertIn("底部", change.reason)
        self.assertFalse(change.appended)

    def test_history_cursor_during_shift_blocks_even_with_bottom_thumb(self):
        old = self.sequence(["甲", "乙", "丙"], bottom=True)
        new = self.sequence(["乙", "丙", "丁"], bottom=True)
        self.assertEqual(compare_sequences(old, new, cursor_in_history=True).status, "RESYNC_REQUIRED")

    def test_full_prefix_motion_is_ambiguous_without_bottom_evidence(self):
        old = self.sequence(["甲", "乙", "丙"])
        new = self.sequence(["甲", "乙", "丙", "丁"], shift=-18)
        self.assertEqual(compare_sequences(old, new).status, "RESYNC_REQUIRED")

    def test_upward_retained_messages_with_positive_bottom_evidence(self):
        old = self.sequence(["甲", "乙", "丙", "丁"], bottom=True)
        new = self.sequence(["乙", "丙", "丁", "戊"], bottom=True)
        self.assertEqual([item.message.text for item in compare_sequences(old, new).appended], ["戊"])

    def test_history_appearing_at_the_top_is_not_an_append(self):
        old = self.sequence(["甲", "乙", "丙"])
        new = self.sequence(["更早", "甲", "乙", "丙"])
        self.assertEqual(compare_sequences(old, new).status, "RESYNC_REQUIRED")

    def test_disappearance_at_bottom_resynchronizes(self):
        old = self.sequence(["甲", "乙", "丙"])
        new = self.sequence(["甲", "乙"])
        self.assertEqual(compare_sequences(old, new).status, "RESYNC_REQUIRED")

    def test_short_and_repeated_overlap_resynchronizes(self):
        for old, new in ((["甲", "乙", "丙"], ["丙", "丁"]), (["甲", "乙", "乙"], ["乙", "乙", "丙"])):
            with self.subTest(old=old, new=new):
                delta = compare_sequences(self.sequence(old, bottom=True), self.sequence(new, bottom=True))
                self.assertEqual(delta.status, "RESYNC_REQUIRED")

    def test_changed_title_or_selected_row_breaks_continuity(self):
        old = self.sequence(["甲", "乙"])
        for change in ({"title": "另一窗口标题"}, {"selected_name": "另一选中行"}):
            with self.subTest(change=change):
                self.assertEqual(compare_sequences(old, self.sequence(["甲", "乙", "丙"], **change)).status,
                                 "RESYNC_REQUIRED")

    def test_same_name_but_changed_avatar_breaks_continuity(self):
        old = self.sequence(["甲", "乙"], contact_pixels=np.zeros((20, 20, 3), np.uint8))
        new = self.sequence(["甲", "乙", "丙"], contact_pixels=np.full((20, 20, 3), 179, np.uint8))
        self.assertEqual(compare_sequences(old, new).status, "RESYNC_REQUIRED")

    def test_same_name_with_no_avatar_and_changed_selected_row_breaks_continuity(self):
        old = self.sequence(["甲", "乙"], contact_pixels=None, selected_row_top=100)
        new = self.sequence(["甲", "乙", "丙"], contact_pixels=None, selected_row_top=160)
        self.assertEqual(compare_sequences(old, new).status, "RESYNC_REQUIRED")

    def test_visible_scrollbar_above_bottom_blocks_stationary_tail_appearance(self):
        old = self.sequence(["甲", "乙"], scroll_thumb=Box(1270, 155, 1274, 265))
        new = self.sequence(["甲", "乙", "丙"], scroll_thumb=old.scroll_thumb)
        self.assertEqual(compare_sequences(old, new).status, "RESYNC_REQUIRED")

    def test_moved_scrollbar_resynchronizes_even_when_text_looks_unchanged(self):
        old = self.sequence(["甲", "乙"], scroll_thumb=Box(1270, 155, 1274, 265))
        new = self.sequence(["甲", "乙"], scroll_thumb=Box(1270, 190, 1274, 300))
        self.assertEqual(compare_sequences(old, new).status, "RESYNC_REQUIRED")

    def test_visible_duplicate_contact_names_block_observation(self):
        row, *rest = self.result.contacts
        duplicate = replace(row, display_name=self.result.conversation.selected_name)
        result = summarize(replace(self.result, contacts=(duplicate, *rest)))
        self.assertIn("同名", result.blocker)
        self.assertFalse(result.identity_unique)

    def test_title_mismatch_and_unreliable_name_block_observation(self):
        conv = self.result.conversation
        mismatch = summarize(replace(self.result, conversation=replace(conv, match_status="CONVERSATION_MISMATCH")))
        weak = summarize(replace(self.result, conversation=replace(conv, selected_name_reliable=False)))
        self.assertEqual(mismatch.blocker_code, "NAME_MISMATCH")
        self.assertEqual(weak.blocker_code, "SELECTED_NAME_UNCERTAIN")


class ConversationLabelTests(MonitorFixture):
    def observe(self, left, title, **kwargs):
        frame, reader = fixture(thumbs=False)
        return observe_frame(frame, {}, _named_reader(reader, left, title, **kwargs))

    def assert_personal_label_match(self, left, labels):
        full = left.replace("＠", "@")
        conv = self.observe(left, full).conversation
        self.assertEqual(conv.selected_raw_name, left)
        self.assertEqual((conv.selected_name, conv.raw_title_line, conv.title), (full, full, full))
        self.assertEqual(conv.selected_identity_labels, labels)
        self.assertEqual((conv.selected_peer_type, conv.match_status), ("PERSONAL_WECHAT", "VISUAL_MATCH"))
        self.assertFalse(conv.identity_unique or conv.attribution_reliable)

    def test_ascii_personal_label_matches_full_title(self):
        self.assert_personal_label_match("样本员@微信", ("微信",))

    def test_full_width_personal_label_matches_full_title(self):
        self.assert_personal_label_match("样本员＠微信", ("微信",))

    def test_two_explicit_personal_labels_match_full_title(self):
        self.assert_personal_label_match("样本员@微信@微信联系人", ("微信", "微信联系人"))

    def test_separate_same_line_ocr_label_is_parsed(self):
        outcome = self.observe("张三", "张三@微信联系人", left_tag=span("＠微信联系人", 222, 160, 88))
        conv = outcome.conversation
        self.assertEqual(conv.selected_raw_name, "张三 ＠微信联系人")
        self.assertEqual((conv.selected_name, conv.selected_peer_type, conv.match_status),
                         ("张三@微信联系人", "PERSONAL_WECHAT", "VISUAL_MATCH"))

    def test_title_same_line_label_keeps_raw_ocr_and_differs_from_plain_selected_name(self):
        conv = self.observe("张三", "张三＠微信联系人").conversation
        self.assertEqual((conv.raw_title_line, conv.title, conv.title_peer_type, conv.match_status),
                         ("张三＠微信联系人", "张三@微信联系人", "PERSONAL_WECHAT", "CONVERSATION_MISMATCH"))

    def test_different_reliable_names_remain_mismatched(self):
        result = summarize(self.observe("张三", "李四"))
        self.assertEqual((result.match_status, result.blocker_code), ("CONVERSATION_MISMATCH", "NAME_MISMATCH"))
        self.assertTrue(result.messages_reliable)

    def test_explicit_conflicting_tags_remain_unknown(self):
        for left, title, title_label, blocker in (
            ("张三@微信@示例公司", "张三", "", "SELECTED_NAME_UNCERTAIN"),
            ("张三@微信", "张三@示例公司", "＠微信", "TITLE_UNCERTAIN"),
        ):
            with self.subTest(left=left, title=title, title_label=title_label):
                result = summarize(self.observe(left, title, title_label=title_label))
                self.assertEqual((result.match_status, result.blocker_code), ("UNKNOWN", blocker))
                self.assertIn("冲突", result.blocker)

    def test_different_complete_peer_labels_mismatch(self):
        result = summarize(self.observe("张三@微信", "张三@示例公司"))
        self.assertEqual((result.match_status, result.blocker_code), ("CONVERSATION_MISMATCH", "NAME_MISMATCH"))

    def test_external_organization_containing_wechat_is_not_personal(self):
        conv = self.observe("张三@示例微信科技有限公司", "张三@示例微信科技有限公司").conversation
        self.assertEqual(conv.selected_raw_name, "张三@示例微信科技有限公司")
        self.assertEqual(conv.selected_identity_labels, ("示例微信科技有限公司",))
        self.assertEqual((conv.selected_name, conv.selected_peer_type, conv.match_status),
                         ("张三@示例微信科技有限公司", "EXTERNAL_WECOM", "VISUAL_MATCH"))

    def test_unknown_suffix_is_never_silently_removed(self):
        for left, title, code in (
            ("张三", "张三@", "TITLE_UNCERTAIN"),
            ("张三@", "张三", "SELECTED_NAME_UNCERTAIN"),
            ("张三…@微信", "张三", "SELECTED_NAME_UNCERTAIN"),
        ):
            with self.subTest(left=left, title=title):
                result = summarize(self.observe(left, title))
                self.assertEqual((result.match_status, result.blocker_code), ("UNKNOWN", code))

    def test_low_confidence_selected_label_cannot_turn_into_a_plain_name_match(self):
        result = summarize(self.observe("张三", "张三", left_tag=span("@微信", 222, 160, 47, .45)))
        self.assertEqual((result.match_status, result.blocker_code), ("UNKNOWN", "SELECTED_NAME_UNCERTAIN"))
        self.assertEqual(result.conversation.selected_raw_name, "张三 @微信")

    def test_missing_title_ocr_reports_c1_and_blocks_baseline(self):
        observed = self.observe("张三@微信", None)
        service = VisualMonitor(capture=lambda: observed.frame, recognize=lambda _: observed)
        self.addCleanup(service.stop)
        self.assertTrue(service.start())
        state = service.poll()
        self.assertEqual((state.status, state.baseline_count, state.candidate_count), ("OBSERVATION_UNRELIABLE", 0, 0))
        self.assertEqual(state.latest_observation.blocker_code, "TITLE_UNCERTAIN")
        self.assertIn("C1 无标题 OCR", state.reason)


class SchedulingTests(MonitorFixture):
    def setUp(self):
        self.now = 0.0
        self.raw = self.frame
        self.names = ["甲", "乙"]
        self.capture = Mock(side_effect=lambda: self.raw)
        self.recognize = Mock(side_effect=lambda frame: self.recognized(frame, self.names))
        self.monitor = VisualMonitor(capture=self.capture, recognize=self.recognize, clock=lambda: self.now)
        self.addCleanup(self.monitor.stop)

    def step(self, elapsed=2.0):
        self.now += elapsed
        return self.monitor.poll()

    def draw_pending_messages(self, names):
        # Align the mocked OCR containers with visible pixels in the captured frame.
        pixels = self.raw.pixels.copy()
        for sequence, name in enumerate(names, len(self.names) + 1):
            _message(name, sequence).message.box.crop(pixels)[:] = (101, 109, 117)
        self.names.extend(names)
        self.raw = replace(self.raw, capture=replace(self.raw.capture, image=pixels))

    def test_first_observation_creates_baseline_without_history_replay(self):
        self.assertTrue(self.monitor.start())
        state = self.step()
        self.assertEqual((state.status, state.baseline_count, state.candidates), ("BASELINE_CREATED", 2, ()))
        self.assertEqual(state.latest_success.title, self.result.conversation.title)

    def test_plain_name_without_avatar_evidence_or_unique_id_can_establish_baseline(self):
        self.raw, reader = fixture(thumbs=False)
        self.recognize.side_effect = lambda frame: observe_frame(frame, {}, _named_reader(reader, "张三", "张三"))
        self.monitor.start()
        state = self.step()
        self.assertEqual((state.status, state.baseline_count, state.candidate_count), ("BASELINE_CREATED", 3, 0))
        self.assertEqual(state.stable_sample_count, 1)
        self.assertFalse(state.latest_success.identity_unique)
        self.assertIsNone(state.latest_success.contact_pixels)
        self.assertEqual(state.latest_success.identity_hint, ())

    def test_personal_wechat_baseline_then_only_new_inbound_candidate(self):
        self.raw, reader = fixture(thumbs=False)
        name_reader = _named_reader(reader, "样本员@微信", "样本员@微信")
        self.recognize.side_effect = lambda frame: observe_frame(frame, {}, name_reader)
        self.monitor.start()
        first = self.step()
        self.assertEqual((first.status, first.baseline_count, first.candidate_count), ("BASELINE_CREATED", 3, 0))
        pixels = self.raw.pixels.copy()
        pixels[320:355, 451:635] = 210
        self.raw = replace(self.raw, capture=replace(self.raw.capture, image=pixels))
        appended = FixtureReader(name_reader.spans + (span("新消息", 466, 328),), ("7", "12"))
        self.recognize.side_effect = lambda frame: observe_frame(frame, {}, appended)
        self.assertEqual(self.step().stable_sample_count, 1)
        result = self.step()
        self.assertEqual((result.status, result.stable_sample_count), ("VISUAL_NEW_CANDIDATE", 2))
        self.assertEqual([(item.message.text, item.message.direction) for item in result.candidates],
                         [("新消息", "INBOUND")])
        self.assertEqual(self.step().candidate_count, 1)

    def test_same_name_on_a_different_selected_row_discards_name_only_baseline(self):
        self.raw, reader = fixture(thumbs=False)
        initial = observe_frame(self.raw, {}, _named_reader(reader, "张三", "张三"))
        self.recognize.side_effect = lambda frame: replace(initial, frame=frame)
        self.monitor.start()
        self.assertEqual(self.step().status, "BASELINE_CREATED")
        self.raw = self.changed_frame()
        selected = next(row for row in initial.contacts if row.is_selected)
        moved_row = replace(selected, box=selected.box.offset(0, 64), avatar=None)
        self.recognize.side_effect = lambda frame: replace(initial, frame=frame, contacts=(moved_row,))
        self.step()
        state = self.step()
        self.assertEqual((state.status, state.baseline_count, state.candidate_count), ("RESYNC_REQUIRED", 0, 0))

    def test_unchanged_samples_do_not_repeat_full_ocr(self):
        self.monitor.start()
        self.step()
        for _ in range(25):
            state = self.step()
        self.assertEqual((state.recognition_count, state.sample_count), (1, 26))
        self.assertEqual(state.status, "NO_CHANGE")

    def test_input_and_scroll_gutter_changes_do_not_trigger_ocr(self):
        self.monitor.start()
        self.step()
        c2 = self.observed.regions.history
        pixels = self.frame.pixels.copy()
        pixels[100:510, c2.right-5:c2.right] = 25
        pixels[580:650, 500:1100] = 30
        self.raw = replace(self.frame, capture=replace(self.frame.capture, image=pixels), mouse=(1000, 300))
        state = self.step()
        self.assertEqual(state.recognition_count, 1)
        self.assertEqual(state.status, "NO_CHANGE")

    def test_localized_change_at_message_area_is_detected(self):
        regions = self.observed.regions
        baseline = probe_frame(self.frame, regions)
        pixels = self.frame.pixels.copy()
        pixels[289:301, 904:920] = 20
        modified = replace(self.frame, capture=replace(self.frame.capture, image=pixels))
        self.assertTrue(probe_frame(modified, regions).differs(baseline))

    def test_rapid_visual_changes_coalesce_until_two_matching_samples(self):
        self.monitor.start()
        self.step()
        for value in range(1, 7):
            self.raw = self.changed_frame(value)
            if value == 6:
                self.draw_pending_messages(["第三", "第四", "第五"])
            self.assertEqual(self.step().recognition_count, 1)
        end = self.step()
        self.assertEqual(end.recognition_count, 2)
        self.assertEqual(end.status, "VISUAL_NEW_CANDIDATE")
        self.assertEqual([c.message.text for c in end.candidates], ["第三", "第四", "第五"])
        self.assertTrue(all(c.first_seen == self.raw.captured_at for c in end.candidates))
        self.assertEqual(self.step().candidate_count, 3)

    def test_second_poll_during_inflight_capture_or_ocr_never_queues(self):
        entered = threading.Event()
        released = threading.Event()

        def blocked(_frame):
            entered.set()
            released.wait(2)
            return self.recognized(self.frame, self.names)

        self.recognize.side_effect = blocked
        self.monitor.start()
        worker = threading.Thread(target=self.monitor.poll)
        worker.start()
        try:
            self.assertTrue(entered.wait(1.5))
            for _ in range(25):
                self.step()
            self.assertEqual(self.capture.call_count, 1)
            self.assertEqual(self.recognize.call_count, 1)
            self.monitor.stop()
            self.assertFalse(self.monitor.start())
        finally:
            released.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.monitor.snapshot().status, "STOPPED")
        self.assertIsNone(self.monitor.snapshot().latest_success)

    def test_capture_failures_back_off_and_restart_baseline(self):
        for code in ("WINDOW_MINIMIZED", "WINDOW_UNAVAILABLE", "CAPTURE_FAILED"):
            with self.subTest(code=code):
                self.monitor.stop()
                self.monitor.start()
                self.capture.side_effect = CaptureError(code, "测试")
                first = self.step()
                self.assertEqual(first.status, "PAUSED")
                self.assertIn(code, first.reason)
                self.assertEqual(self.step(.01).sample_count, 1)
                self.capture.side_effect = lambda: self.raw
                self.assertEqual(self.step(4).candidate_count, 0)
                self.assertEqual(self.step().status, "BASELINE_CREATED")

    def test_hwnd_rebuild_discards_old_baseline_before_recovery(self):
        self.monitor.start()
        self.step()
        self.raw = self.changed_frame(hwnd=self.frame.capture.hwnd + 1)
        self.names = ["新窗口已有内容", "记录"]
        reset = self.step()
        self.assertEqual((reset.status, reset.baseline_count, reset.recognition_count),
                         ("RESYNC_REQUIRED", 0, 1))
        self.step()
        state = self.step()
        self.assertEqual((state.status, state.candidate_count), ("BASELINE_CREATED", 0))

    def test_ocr_failure_preserves_latest_success_and_blocks_new_events(self):
        self.monitor.start()
        first = self.step()
        self.raw = self.changed_frame()
        self.recognize.side_effect = OcrUnavailable("模拟 OCR 错误")
        self.step()
        failure = self.step()
        self.assertEqual(failure.status, "OBSERVATION_UNRELIABLE")
        self.assertIs(failure.latest_success, first.latest_success)
        self.assertEqual(failure.candidate_count, 0)
        self.assertEqual(self.step(.01).recognition_count, 2)

    def test_different_contact_rebaselines_without_mixing_messages(self):
        self.monitor.start()
        self.step()
        self.raw = self.changed_frame()
        alternative, reader = fixture(title="新同事")
        alternate_result = observe_frame(alternative, {}, reader)
        alternate_conv = replace(alternate_result.conversation, selected_name="新同事", match_status="VISUAL_MATCH")
        selected = next(row for row in alternate_result.contacts if row.is_selected)
        alternate_rows = tuple(replace(row, display_name="新同事") if row is selected else row
                               for row in alternate_result.contacts)
        self.recognize.side_effect = lambda frame: replace(
            self.recognized(frame, ["其他会话一", "其他会话二"]), conversation=alternate_conv, contacts=alternate_rows)
        self.step()
        self.assertEqual(self.step().status, "RESYNC_REQUIRED")
        self.step()
        state = self.step()
        self.assertEqual(state.status, "BASELINE_CREATED")
        self.assertEqual(state.candidate_count, 0)
        self.assertEqual(state.latest_success.title, "新同事")

    def test_stop_clear_events_and_no_new_capture(self):
        self.assertTrue(self.monitor.start())
        self.assertFalse(self.monitor.start())
        self.step()
        self.raw = self.changed_frame()
        self.draw_pending_messages(["新增"])
        self.step()
        self.assertEqual(len(self.step().candidates), 1)
        self.monitor.clear_events()
        cleared = self.monitor.snapshot()
        self.assertEqual((cleared.events, cleared.candidates, cleared.candidate_count), ((), (), 0))
        self.assertEqual(cleared.baseline_count, 3)
        self.monitor.stop()
        self.step(100)
        self.assertEqual(self.capture.call_count, 3)

    def test_sampling_interval_is_bounded_and_observation_has_no_full_frame(self):
        for invalid in (.01, 90):
            with self.assertRaises(ValueError):
                VisualMonitor(interval=invalid)
        self.monitor.start()
        observed = self.monitor.poll().latest_success
        for _ in range(100):
            self.monitor.poll()
        self.assertEqual(self.capture.call_count, 1)
        self.assertFalse(hasattr(observed, "frame"))
        self.assertLess(self.monitor._recognized_probe.history.nbytes, 200_000)


class GuiMonitorTests(MonitorFixture):
    def test_monitor_diagnostics_identify_each_distinct_baseline_blocker(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.visual_monitor_panel import VisualMonitorPanel

        app = QApplication.instance() or QApplication([])
        panel = VisualMonitorPanel(Mock(), Mock())
        self.addCleanup(panel.close)
        ok = self.result
        conv = ok.conversation
        row = next(item for item in ok.contacts if item.is_selected)
        cases = (
            (replace(ok, layout=replace(ok.layout, regions=())), "消息页面缺少可用的 C1、B2、C2 区域"),
            (replace(ok, contacts=()), "B2 未识别到选中行"),
            (replace(ok, contacts=(row, row)), "B2 存在多个选中行候选"),
            (replace(ok, contacts=(replace(row, status="PARTIALLY_VISIBLE"),)), "B2 选中行仅部分可见"),
            (replace(ok, conversation=replace(conv, selected_name_reliable=False)), "B2 选中姓名证据不足"),
            (replace(ok, conversation=replace(conv, title_name_reliable=False)), "C1 标题姓名无法核对"),
            (replace(ok, conversation=replace(conv, match_status="CONVERSATION_MISMATCH")), "B2 选中姓名与 C1 标题姓名不同"),
            (replace(ok, conversation=replace(conv, match_status="UNKNOWN", comparison_evidence="B2 与 C1 的明确身份标签冲突")),
             "B2 与 C1 的明确身份标签冲突"),
            (replace(ok, contacts=ok.contacts + (replace(row, is_selected=False),)), "B2 可见多个同名联系人"),
            (replace(ok, page="UNKNOWN"), "无法确认当前为消息页面"),
            (replace(ok, message_completeness="VISIBLE_MESSAGES_INCOMPLETE"), "C2 可见消息归属或 OCR 证据不完整"),
            (replace(ok, messages=()), "C2 缺少可靠、可比较的可见消息序列"),
        )
        for source, reason in cases:
            with self.subTest(reason=reason):
                observed = summarize(source)
                panel._render(MonitorSnapshot("OBSERVATION_UNRELIABLE", observed.blocker,
                                              latest_observation=observed, stable_sample_count=2))
                text = panel.summary.text()
                self.assertIn("基线未建立 · " + reason, text)
                self.assertIn("唯一客户身份未验证", text)
                self.assertIn("连续稳定采样 2 次", text)
        app.processEvents()

    def test_monitor_diagnostics_show_raw_names_labels_and_message_reliability(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.visual_monitor_panel import VisualMonitorPanel

        app = QApplication.instance() or QApplication([])
        panel = VisualMonitorPanel(Mock(), Mock())
        self.addCleanup(panel.close)
        frame, reader = fixture(thumbs=False)
        observed = summarize(observe_frame(frame, {}, _named_reader(reader, "张三＠微信", "张三@微信")))
        panel._render(MonitorSnapshot("BASELINE_CREATED", baseline_count=3, latest_observation=observed,
                                      stable_sample_count=1))
        text = panel.summary.text()
        for value in ("MESSAGES_CANDIDATE", "B2 选中行 CANDIDATE", "B2 原始名称 张三＠微信",
                      "显示姓名 张三@微信", "C1 原始标题 张三@微信", "标题姓名 张三@微信", "微信（PERSONAL_WECHAT）",
                      "VISUAL_MATCH", "可靠可见消息 已取得", "唯一客户身份未验证", "基线 3 条"):
            self.assertIn(value, text)
        self.assertNotIn("基线未建立", text)
        app.processEvents()

    def test_main_window_close_cancels_inflight_and_never_schedules_again(self):
        from PySide6.QtWidgets import QApplication
        from autoim.app import LogBus, MainWindow

        app = QApplication.instance() or QApplication([])
        entered = threading.Event()
        release = threading.Event()
        calls = Mock()

        def capture():
            calls()
            entered.set()
            release.wait(2)
            return self.frame

        recognize = Mock(return_value=self.result)
        service = VisualMonitor(capture=capture, recognize=recognize, interval=1)
        window = MainWindow(LogBus())
        panel = window.visual_capture_panel.monitor_panel
        panel._make_monitor = lambda interval: service
        panel.start_button.click()
        try:
            self.assertTrue(entered.wait(1.5))
            self.assertFalse(window.visual_capture_panel.read_button.isEnabled())
            window.close()
            self.assertEqual(service.snapshot().status, "STOPPED")
            self.assertFalse(panel._timer.isActive())
        finally:
            release.set()
            window.thread_pool.waitForDone(2500)
            app.processEvents()
        self.assertFalse(window._workers)
        self.assertFalse(panel._timer.isActive())
        self.assertEqual(calls.call_count, 1)
        recognize.assert_not_called()
