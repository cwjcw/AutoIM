from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from autoim.wecom.collection_navigation import CollectionNavigation, CollectionPermit, NavigationPaused, row_target
from autoim.wecom.collection_service import CollectionService, page_transition, unread_targets
from autoim.wecom.visual_capture import CaptureError
from autoim.wecom.visual_contacts import GlobalUnreadObservation
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_messages import Container, analyze_messages
from autoim.wecom.visual_observation import observe_frame
from autoim.wecom.visual_store import SCHEMA_VERSION, SOURCE, VisualStore, _MIGRATIONS, database_path
from scripts.check_security_surface import inspect_collector_input
from test_wecom_visual_capture import FixtureReader, THRESHOLDS, fixture, span
from test_wecom_visual_files import _frame_with_file
from test_wecom_visual_partial import _history
from autoim.wecom.visual_observation import OcrAreaObservation

PROFILE = "合成测试账号"
NAMES = ("会话甲", "会话乙", "测试群")


def view(selected=0, unread=None, *, mismatch=False, group=False, partial_row=False):
    frame, reader = fixture(thumbs=False, hover=None)
    raw = observe_frame(frame, {}, reader)
    rows = []
    unread = unread or {}
    for index, row in enumerate(raw.contacts):
        has_badge = index in unread
        number, muted = unread.get(index, (None, False))
        rows.append(replace(row, display_name=NAMES[index], raw_name=NAMES[index],
                            kind="GROUP_CANDIDATE" if index == 2 else "CONTACT",
                            is_selected=index == selected, name_reliable=True,
                            status="PARTIALLY_VISIBLE" if partial_row and index == 2 else "CANDIDATE",
                            unread_count=number, unread_count_reliable=number is not None,
                            has_unread_indicator=has_badge, is_muted=muted,
                            badge=Box(row.box.left + 12, row.box.top + 9, row.box.left + 23, row.box.top + 20)
                            if has_badge else None))
    conv = replace(raw.conversation, title="错误会话" if mismatch else NAMES[selected],
                   selected_name=NAMES[selected], match_status="CONVERSATION_MISMATCH" if mismatch else "VISUAL_MATCH",
                   conversation_type="GROUP" if group or selected == 2 else "ONE_TO_ONE")
    return replace(raw, contacts=tuple(rows), conversation=conv,
                   global_unread=GlobalUnreadObservation(global_unread_count=0, global_unread_count_reliable=True))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "visual.sqlite3"
        self.store = VisualStore(self.path)

    def test_path_defaults_to_local_app_data_outside_the_repo(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": str(self.path.parent)}):
            self.assertEqual(database_path(), self.path.parent / "AutoIM" / "wecom-visual.sqlite3")

    def test_schema_and_transaction_commit(self):
        with self.store.transaction() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.store._account(db, PROFILE)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(self.path.exists())
        self.assertTrue({"accounts", "conversations", "messages", "attachments", "message_observations",
                         "capture_checkpoints", "scan_jobs"} <= tables)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)

    def test_rollback_removes_partial_messages_and_conversation(self):
        with patch.object(self.store, "_observation", side_effect=sqlite3.OperationalError("simulated failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.save(PROFILE, view())
        data = self.store.browse(PROFILE)
        self.assertEqual((data["message_count"], data["conversations"]), (0, []))

    def test_migration_preserves_v1_rows_and_adds_quote_columns(self):
        conn = sqlite3.connect(self.path)
        try:
            for op in _MIGRATIONS[1]:
                conn.execute(op)
            conn.execute("PRAGMA user_version=1")
            conn.execute("INSERT INTO accounts (profile, first_seen) VALUES (?,?)", (PROFILE, "2026-11-23"))
            conn.commit()
        finally:
            conn.close()
        with self.store.transaction() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            self.assertIn("quoted_message_id", columns)
            self.assertEqual(db.execute("SELECT profile FROM accounts").fetchone()[0], PROFILE)

    def test_first_read_saves_history_without_fake_new_messages_and_allows_repeats(self):
        result = self.store.save(PROFILE, view())
        self.assertEqual((result.inserted, result.new_candidates, result.outcome), (3, 0, "HISTORY_BASELINE"))
        messages = self.store.browse(PROFILE, result.conversation_id)["messages"]
        self.assertEqual(len(messages), 3)
        inbound = [item for item in messages if item["direction"] == "INBOUND"]
        self.assertEqual([item["body"] for item in inbound], ["你好", "你好"])
        self.assertNotEqual(inbound[0]["id"], inbound[1]["id"])
        self.assertTrue(all(item["source"] == SOURCE and item["actual_time"] is None for item in messages))

    def test_same_visible_sequence_links_repeated_observations(self):
        raw = view()
        first = self.store.save(PROFILE, raw)
        repeat = self.store.save(PROFILE, raw)
        self.assertEqual((repeat.conversation_id, repeat.inserted, repeat.repeated), (first.conversation_id, 0, 3))
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_observations WHERE message_id IS NOT NULL")
                             .fetchone()[0], 6)

    def test_reopen_keeps_history_without_assuming_restart_continuity(self):
        raw = view()
        saved = self.store.save(PROFILE, raw)
        reopened = VisualStore(self.path)
        self.assertEqual(reopened.browse(PROFILE, saved.conversation_id)["message_count"], 3)
        other = reopened.save(PROFILE, raw)
        self.assertEqual((other.outcome, other.inserted), ("CONVERSATION_UNCERTAIN", 0))
        self.assertEqual(reopened.browse(PROFILE, saved.conversation_id)["message_count"], 3)

    def test_reopen_with_distinct_avatar_still_requires_fresh_message_continuity(self):
        raw = view()
        icon = raw.contacts[0].avatar
        pixels = raw.frame.pixels.copy()
        icon.crop(pixels)[:, :icon.width // 2] = (45, 98, 177)
        icon.crop(pixels)[:, icon.width // 2:] = (207, 102, 49)
        raw = replace(raw, frame=replace(raw.frame, capture=replace(raw.frame.capture, image=pixels)))
        first = self.store.save(PROFILE, raw)
        reopened = VisualStore(self.path)
        second = reopened.save(PROFILE, raw)
        self.assertEqual((first.conversation_id, second.outcome, second.inserted),
                         (second.conversation_id, "CONTINUITY_UNVERIFIED", 0))

    def test_profile_boundary_prevents_cross_account_links(self):
        one = self.store.save(PROFILE, view())
        two = self.store.save("另一个本地账号", view())
        self.assertNotEqual(one.conversation_id, two.conversation_id)
        self.assertEqual(self.store.browse(PROFILE, two.conversation_id)["messages"], [])

    def test_saved_scan_job_cannot_be_completed_by_another_profile(self):
        target = unread_targets(view(unread={1: (None, True)}))[0]
        jid = self.store.create_job(PROFILE, "run", target.key, target.row)
        with self.assertRaises(ValueError):
            self.store.save("另一个账号", view(), job_id=jid)
        self.assertEqual(self.store.browse(PROFILE)["jobs"][0]["status"], "QUEUED")

    def test_pixel_change_inside_same_text_is_not_a_confirmed_duplicate(self):
        before = view()
        self.store.save(PROFILE, before)
        pixels = before.frame.pixels.copy()
        before.messages[0].box.crop(pixels)[:] = 177
        frame = replace(before.frame, capture=replace(before.frame.capture, image=pixels))
        result = self.store.save(PROFILE, replace(before, frame=frame))
        self.assertEqual((result.inserted, result.repeated), (0, 0))
        self.assertEqual(result.outcome, "ORDER_UNCERTAIN")

    def test_unverified_title_saves_observations_without_a_conversation_link(self):
        saved = self.store.save(PROFILE, view(mismatch=True))
        data = self.store.browse(PROFILE)
        self.assertIsNone(saved.conversation_id)
        self.assertEqual(data["conversations"], [])
        self.assertTrue(data["errors"])

    def test_group_sender_and_file_metadata_are_saved_independently(self):
        _, raw = _frame_with_file()
        raw = replace(raw, conversation=replace(raw.conversation, conversation_type="GROUP"),
                      messages=tuple(replace(item, sender_name="成员甲") if item.direction == "INBOUND" else item
                                     for item in raw.messages))
        saved = self.store.save(PROFILE, raw)
        history = self.store.browse(PROFILE, saved.conversation_id)
        self.assertEqual(history["conversations"][0]["kind"], "GROUP")
        file = next(row for row in history["messages"] if row["kind"] == "FILE")
        self.assertEqual((file["sender_name"], file["size_text"], file["extension"]), ("成员甲", "6.6K", "PDF"))
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT content_available FROM attachments").fetchone()[0], 0)

    def test_image_placeholder_and_quote_save_only_uncertain_evidence(self):
        _, raw = _history(("图片加载中", .98, 54), ("引用某条消息", .98, 52))
        saved = self.store.save(PROFILE, raw)
        data = self.store.browse(PROFILE, saved.conversation_id)
        errors = {row["kind"]: row for row in data["errors"]}
        self.assertIn("IMAGE_PLACEHOLDER", errors)
        self.assertEqual(json.loads(errors["COMPLEX_CANDIDATE"]["raw_ocr"])["quote"]["quoteParseStatus"], "UNPARSED_COMPLEX")
        self.assertNotIn("IMAGE_PLACEHOLDER", {item["kind"] for item in data["messages"]})

    def test_system_event_and_time_are_saved_as_context(self):
        self.store.save(PROFILE, view())
        with self.store.transaction() as db:
            kinds = {row[0] for row in db.execute(
                "SELECT kind FROM message_observations WHERE status='CONTEXT' AND message_id IS NULL")}
        self.assertEqual(kinds, {"TIME_SEPARATOR", "SYSTEM_EVENT"})

    def test_three_appended_messages_including_identical_occurrences(self):
        _, before = _history()
        _, after = _history(("你好", .98, 35), ("你好", .98, 35), ("第三条", .98, 35))
        self.store.save(PROFILE, before)
        saved = self.store.save(PROFILE, after)
        self.assertEqual((saved.outcome, saved.new_candidates), ("VISUAL_APPEND", 3))
        history = self.store.browse(PROFILE, saved.conversation_id)["messages"]
        self.assertEqual([row["body"] for row in history[-3:]], ["你好", "你好", "第三条"])

    def test_changed_order_records_ambiguity_without_inserting_as_new(self):
        before = view()
        self.store.save(PROFILE, before)
        after = replace(before, messages=tuple(replace(item, text=f"不同内容{item.sequence}") for item in before.messages))
        result = self.store.save(PROFILE, after)
        self.assertEqual((result.outcome, result.inserted, result.new_candidates), ("ORDER_UNCERTAIN", 0, 0))

    def test_recovery_marks_inflight_jobs_paused_and_retains_read_risk(self):
        target = unread_targets(view(unread={1: (None, True)}))[0]
        job = self.store.create_job(PROFILE, "before", target.key, target.row)
        self.store.job(job, "NAVIGATING", risk=True)
        reopened = VisualStore(self.path)
        reopened.recover_jobs(PROFILE, "after")
        row = reopened.browse(PROFILE)["jobs"][0]
        self.assertEqual((row["status"], row["read_risk"]), ("PAUSED", 1))
        self.assertIsNone(json.loads(row["unread_evidence"])["unreadCount"])


class DiscoveryAndLayoutTests(unittest.TestCase):
    def test_muted_dot_group_and_multiple_unread_do_not_depend_on_global_count(self):
        raw = view(unread={0: (4, False), 1: (None, True), 2: (2, False)})
        selected = unread_targets(raw)
        self.assertEqual(len(selected), 3)
        self.assertTrue(selected[1].row.is_muted)
        self.assertIsNone(selected[1].row.unread_count)
        self.assertEqual(selected[2].row.kind, "GROUP_CANDIDATE")
        self.assertEqual(raw.global_unread.global_unread_count, 0)

    def test_bottom_row_and_missing_scrollbar_are_not_variant_region_failures(self):
        raw = view(unread={2: (1, False)})
        target, = unread_targets(raw)
        self.assertFalse(target.ambiguous)
        self.assertIsNone(raw.list_scrollbar)
        self.assertTrue(raw.layout.get("B2").contains(row_target(raw, target.row)))

    def test_partial_bottom_row_is_recordable_but_not_clickable(self):
        raw = view(unread={2: (1, False)}, partial_row=True)
        target, = unread_targets(raw)
        self.assertTrue(target.ambiguous)
        with self.assertRaises(NavigationPaused):
            row_target(raw, target.row)

    def test_duplicate_names_are_not_navigation_targets(self):
        raw = view(unread={1: (1, False)})
        raw = replace(raw, contacts=(replace(raw.contacts[0], display_name=NAMES[1]), *raw.contacts[1:]))
        self.assertTrue(unread_targets(raw)[0].ambiguous)

    def test_unrecognized_name_with_badge_remains_an_uncertain_candidate(self):
        raw = view(unread={1: (None, False)})
        raw = replace(raw, contacts=(raw.contacts[0], replace(raw.contacts[1], display_name=""), raw.contacts[2]))
        target, = unread_targets(raw)
        self.assertTrue(target.ambiguous)

    def test_expanded_panel_is_excluded_and_group_sender_is_per_message(self):
        frame, reader = fixture(thumbs=False, hover=None)
        pixels = frame.pixels.copy()
        pixels[:, 981:] = (190, 195, 204)
        frame = replace(frame, capture=replace(frame.capture, image=pixels))
        ocr = (*reader.spans, span("群成员甲", 452, 104, 72), span("群板内容", 1012, 305, 127))
        raw = observe_frame(frame, {}, FixtureReader(ocr, ("7", "12")))
        self.assertEqual(raw.conversation.conversation_type, "GROUP")
        self.assertEqual(raw.layout.get("C2").right, raw.layout.get("D").left)
        self.assertFalse(any("群板内容" in item.span.text for item in raw.message_ocr))
        inbound = [item for item in raw.messages if item.direction == "INBOUND"]
        self.assertEqual([item.sender_name for item in inbound], ["群成员甲", ""])

    def test_gray_quote_marker_retains_composite_observation(self):
        pixels = np.full((400, 690, 3), 249, np.uint8)
        card = Box(50, 52, 305, 169)
        card.crop(pixels)[:] = 223
        pixels[62:120, 64:67] = 148
        spans = (span("样例成员", 74, 65, 90), span("这是上一段", 75, 91, 114),
                 span("这是本次回复", 63, 135, 125))
        result = analyze_messages(pixels, Box(0, 0, 690, 400), (Container(card, False),),
                                  spans, 1, {}, THRESHOLDS)
        message, = result.messages
        self.assertEqual((message.kind, message.status), ("COMPLEX_CANDIDATE", "UNCLASSIFIED"))
        self.assertEqual(len(message.ocr_spans), 3)

    def test_page_transition_covers_overlap_end_reorder_and_unconfirmable_state(self):
        for before, after, expected in (
                (("A", "B", "C", "D"), ("C", "D", "E", "F"), "SCROLLED"),
                (("A", "B", "C"), ("A", "B", "C"), "NO_SCROLL_CHANGE"),
                (("A", "B", "C", "D"), ("D", "C", "B", "A"), "SCROLL_UNCERTAIN"),
                (("A", "B", "C"), ("X", "Y", "Z"), "SCROLL_UNCERTAIN")):
            with self.subTest(before=before, after=after):
                self.assertEqual(page_transition(before, after), expected)


class ScanWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = VisualStore(Path(directory.name) / "test.sqlite3")
        self.raw = view(unread={1: (None, True), 2: (2, False)})
        self.navigator = Mock()
        self.permit = Mock()
        self.navigator.select.side_effect = self.switch
        self.service = CollectionService(PROFILE, self.store, lambda _: self.raw, capture=lambda: self.raw.frame,
                                         navigator=self.navigator, permit=self.permit)
        self.service.start()
        self.addCleanup(self.service.stop)

    def switch(self, _raw, row):
        # Verify the read-side-effect evidence has already committed.
        jobs = self.store.browse(PROFILE)["jobs"]
        self.assertTrue(any(job["display_name"] == row.display_name and job["status"] == "NAVIGATING"
                            and job["read_risk"] for job in jobs))
        remaining = {2: (2, False)} if row.display_name == NAMES[1] else {}
        self.raw = view(NAMES.index(row.display_name), remaining)

    def test_two_nonselected_unread_are_captured_serially_to_separate_conversations(self):
        first = self.service.tick()
        second = self.service.tick()
        self.assertEqual((first.status, first.target, second.status, second.target),
                         ("COMPLETED", NAMES[1], "COMPLETED", NAMES[2]))
        data = self.store.browse(PROFILE)
        self.assertEqual(data["message_count"], 6)
        self.assertEqual({row["display_name"] for row in data["conversations"]}, set(NAMES[1:]))
        self.assertEqual(self.navigator.select.call_count, 2)
        self.assertIn('"badge_after":false', data["jobs"][0]["transitions"])

    def test_b2_scan_is_completed_before_navigation_front_guard(self):
        from test_wecom_window_recovery import BASE, health
        background = health(replace(BASE, foreground_hwnd=999))
        self.service._window_probe = Mock(return_value=background)
        self.navigator.verify_view.side_effect = NavigationPaused("WINDOW_NOT_FOREGROUND · 等待切回")
        result = self.service.tick()
        self.assertEqual((result.scan_disposition, result.candidates, result.unread_marker_count),
                         ("B2_SCANNED_WAITING_NAVIGATION", 2, 2))
        self.assertEqual(result.contact_count, 3)
        self.assertIsNotNone(result.b2_scan_completed_at)
        self.assertEqual(result.awaiting_navigation, 2)
        self.assertEqual(result.foreground_changed_stage, "截图")
        self.navigator.select.assert_not_called()

    def test_returning_foreground_rechecks_fresh_contact_rows_before_navigation(self):
        from test_wecom_window_recovery import BASE, health
        background = health(replace(BASE, foreground_hwnd=999))
        foreground = health()
        self.service._window_probe = Mock(side_effect=(background, background, foreground, foreground))
        self.navigator.verify_view.side_effect = NavigationPaused("WINDOW_NOT_FOREGROUND · 等待切回")
        waiting = self.service.tick()
        self.assertEqual((waiting.candidates, waiting.queued, waiting.awaiting_navigation), (2, 2, 2))
        self.navigator.verify_view.side_effect = None
        self.navigator.select.side_effect = self.switch
        resumed = self.service.tick()
        self.assertEqual(resumed.status, "COMPLETED")
        self.assertEqual(self.navigator.select.call_count, 1)

    def test_foreground_change_while_ocr_runs_does_not_skip_b2_scan(self):
        from test_wecom_window_recovery import BASE, health
        self.service.read = lambda _: self.raw
        background = health(replace(BASE, foreground_hwnd=999))
        self.service._window_probe = Mock(side_effect=(health(), background))
        self.navigator.verify_view.side_effect = NavigationPaused("WINDOW_NOT_FOREGROUND")
        result = self.service.tick()
        self.assertEqual(result.foreground_changed_stage, "OCR")
        self.assertIsNotNone(result.b2_scan_completed_at)
        self.assertEqual(result.scan_disposition, "B2_SCANNED_WAITING_NAVIGATION")

    def test_invalid_incomplete_frame_cannot_create_unread_tasks(self):
        broken = replace(self.raw.frame, capture=replace(
            self.raw.frame.capture, image=self.raw.frame.pixels[:-12]))
        self.service.capture = Mock(return_value=broken)
        result = self.service.tick()
        self.assertEqual((result.scan_disposition, result.contact_count, result.queued),
                         ("B2_NOT_SCANNED", 0, 0))
        self.assertEqual(self.store.browse(PROFILE)["jobs"], [])

    def test_completed_b2_scan_with_no_unread_is_distinct_from_skipped_scan(self):
        self.raw = view()
        self.service.read = lambda _: self.raw
        result = self.service.tick()
        self.assertEqual((result.scan_disposition, result.candidates, result.unread_marker_count),
                         ("B2_SCANNED_NO_UNREAD", 0, 0))
        self.assertEqual(result.contact_count, 3)
        self.assertIsNotNone(result.b2_scan_attempt_at)

    def test_b2_ocr_failure_is_reported_as_unscanned(self):
        box = self.raw.layout.get("B2")
        self.raw = replace(self.raw, ocr_areas=(OcrAreaObservation("B2 联系人", box, "FAILED", reason="test OCR"),))
        self.service.read = lambda _: self.raw
        result = self.service.tick()
        self.assertEqual((result.scan_disposition, result.reliable_unread_count, result.queued),
                         ("B2_SCAN_FAILED", 0, 0))

    def test_red_badge_without_reliable_contact_identity_is_retained_as_unreliable(self):
        self.raw = view(unread={1: (None, True)})
        rows = list(self.raw.contacts)
        rows[1] = replace(rows[1], name_reliable=False)
        self.raw = replace(self.raw, contacts=tuple(rows))
        self.service.read = lambda _: self.raw
        result = self.service.tick()
        self.assertEqual((result.unread_marker_count, result.reliable_unread_count, result.unreliable_unread_count),
                         (1, 0, 1))
        self.assertEqual(result.scan_disposition, "B2_SCANNED_UNRELIABLE")
        self.assertTrue(result.unreliable_reasons)
        self.assertEqual(result.queued, 0)

    def set_unconfirmed_unread(self, *, activity="刚刚", preview="测试预览", count=1, move=0):
        raw = view(unread={1: (count, False)})
        rows = tuple(replace(row, is_selected=None, activity_text=activity, preview=preview,
                             box=row.box.offset(0, move)) if i == 1 else replace(row, is_selected=None)
                     for i, row in enumerate(raw.contacts))
        self.raw = replace(raw, contacts=rows)

    def test_persistent_unread_updates_one_waiting_job_despite_activity_time_and_position(self):
        self.set_unconfirmed_unread()
        self.service.tick()
        self.set_unconfirmed_unread(activity="5分钟前", move=3)
        self.service.tick()
        self.set_unconfirmed_unread(activity="08:15")
        state = self.service.tick()
        job, = self.store.browse(PROFILE)["jobs"]
        self.assertEqual((state.scan_disposition, state.errors), ("B2_SCANNED_UNRELIABLE", 0))
        self.assertEqual((job["status"], job["navigation_status"], job["read_risk"]),
                         ("WAITING_REVALIDATION", "NOT_STARTED", 0))
        waiting = json.loads(job["transitions"])[-1]
        self.assertEqual(waiting["detail"]["observations"], 3)
        self.assertEqual(waiting["detail"]["scan_id"], state.scan_id)
        self.navigator.select.assert_not_called()

    def test_preview_change_and_count_increase_start_new_observations(self):
        for preview, count in (("第一条", 1), ("第二条", 1), ("第二条", 2)):
            self.set_unconfirmed_unread(preview=preview, count=count)
            self.service.tick()
        jobs = self.store.browse(PROFILE)["jobs"]
        self.assertEqual(len(jobs), 3)
        self.assertEqual(sum(job["status"] == "WAITING_REVALIDATION" for job in jobs), 1)
        self.navigator.select.assert_not_called()

    def test_unreliable_preview_and_badge_ocr_do_not_create_new_waiting_jobs(self):
        self.set_unconfirmed_unread(count=2)
        self.service.tick()
        self.set_unconfirmed_unread(preview="单帧 OCR 抖动", count=None)
        rows = list(self.raw.contacts)
        rows[1] = replace(rows[1], preview_reliable=False)
        self.raw = replace(self.raw, contacts=tuple(rows))
        self.service.tick()
        self.set_unconfirmed_unread(count=3)
        self.service.tick()
        self.assertEqual(len(self.store.browse(PROFILE)["jobs"]), 2)

    def test_badge_reappearing_after_a_confirmed_absence_is_new(self):
        self.set_unconfirmed_unread()
        self.service.tick()
        self.raw = view()
        self.service.tick()
        self.set_unconfirmed_unread()
        self.service.tick()
        jobs = self.store.browse(PROFILE)["jobs"]
        self.assertEqual({job["status"] for job in jobs}, {"PAUSED", "WAITING_REVALIDATION"})
        self.assertEqual(len(jobs), 2)

    def test_waiting_job_is_promoted_on_foreground_while_old_uncertain_record_is_preserved(self):
        from test_wecom_window_recovery import BASE, health

        self.set_unconfirmed_unread()
        target, = unread_targets(self.raw)
        legacy = self.store.create_job(PROFILE, "legacy", "old-row-key", target.row)
        self.store.job(legacy, "UNCERTAIN", reason="旧版无法核对选中行")
        background = health(replace(BASE, foreground_hwnd=999))
        self.service._window_probe = Mock(side_effect=(background, background, health(), health()))
        waiting = self.service.tick()
        self.assertEqual((waiting.reliable_unread_count, waiting.unreliable_unread_count), (0, 1))
        self.assertEqual(waiting.queued, 0)
        self.navigator.select.assert_not_called()
        pending = next(job["id"] for job in self.store.browse(PROFILE)["jobs"] if job["id"] != legacy)
        raw = view(unread={1: (1, False)})
        rows = list(raw.contacts)
        rows[1] = replace(rows[1], preview="测试预览")
        self.raw = replace(raw, contacts=tuple(rows))
        resumed = self.service.tick()
        jobs = {job["id"]: job for job in self.store.browse(PROFILE)["jobs"]}
        self.assertEqual(resumed.status, "COMPLETED")
        self.assertEqual(set(jobs), {legacy, pending})
        self.assertEqual(jobs[legacy]["status"], "UNCERTAIN")
        self.assertEqual(jobs[pending]["status"], "COMPLETED")
        self.navigator.select.assert_called_once()

    def test_scanning_round_does_not_inherit_previous_completion_timestamp(self):
        self.set_unconfirmed_unread()
        first = self.service.tick()
        captured = []

        def read(_):
            captured.append(self.service.snapshot())
            return self.raw

        self.service.read = read
        second = self.service.tick()
        scanning, = captured
        self.assertEqual(scanning.scan_id, second.scan_id)
        self.assertNotEqual(scanning.scan_id, first.scan_id)
        self.assertEqual(scanning.scan_disposition, "B2_SCANNING")
        self.assertIsNone(scanning.b2_scan_completed_at)
        self.assertIsNone(scanning.scan_ocr_completed_at)
        self.assertIn(first.scan_id, scanning.last_b2_summary)
        self.assertEqual([item.scan_id for item in self.service.recent_scans()], [first.scan_id, second.scan_id])

    def test_already_selected_task_needs_no_click(self):
        self.raw = view(unread={0: (3, False)})
        self.assertEqual(self.service.tick().status, "COMPLETED")
        self.navigator.select.assert_not_called()

    def test_title_mismatch_is_never_written_under_the_target(self):
        self.navigator.select.side_effect = lambda *_: setattr(self, "raw", view(1, mismatch=True))
        result = self.service.tick()
        self.assertEqual(result.status, "NAVIGATION_UNCERTAIN")
        data = self.store.browse(PROFILE)
        self.assertEqual(data["message_count"], 0)
        self.assertTrue(any(job["status"] == "UNCERTAIN" and job["read_risk"] for job in data["jobs"]))

    def test_occluded_or_recent_input_pauses_without_click(self):
        for reason in ("检测到弹窗", "用户正在移动鼠标或操作键盘"):
            self.navigator.verify_view.side_effect = NavigationPaused(reason)
            self.assertEqual(self.service.tick().status, "PAUSED")
        self.navigator.select.assert_not_called()

    def test_stale_row_guard_pauses_and_retries_only_with_fresh_frame(self):
        self.navigator.select.side_effect = NavigationPaused("列表已重排")
        first = self.service.tick()
        self.assertEqual(first.status, "PAUSED")
        self.assertTrue(all(not job["read_risk"] for job in self.store.browse(PROFILE)["jobs"]))
        self.navigator.select.side_effect = self.switch
        self.assertEqual(self.service.tick().status, "COMPLETED")

    def test_navigation_success_followed_by_ocr_failure_retains_uncertain_read_risk(self):
        def failed(*_):
            raw = view(1)
            self.raw = replace(raw, messages=(), message_completeness="VISIBLE_MESSAGES_INCOMPLETE")
        self.navigator.select.side_effect = failed
        self.assertEqual(self.service.tick().status, "UNCERTAIN")
        self.assertTrue(any(job["status"] == "UNCERTAIN" and job["read_risk"]
                            for job in self.store.browse(PROFILE)["jobs"]))

    def test_database_failure_after_click_stops_run(self):
        with patch.object(self.store, "save", side_effect=sqlite3.OperationalError("locked")):
            result = self.service.tick()
        self.assertEqual(result.status, "FAILED")
        self.assertIn("可能已清除", result.reason)
        self.assertFalse(self.service.active)
        self.assertEqual(self.service.tick().status, "STOPPED")
        self.assertEqual(self.navigator.select.call_count, 1)

    def test_stop_or_unavailable_window_prevents_navigation(self):
        self.service.capture = Mock(side_effect=CaptureError("WINDOW_MINIMIZED", "最小化"))
        self.assertEqual(self.service.tick().status, "PAUSED")
        self.service.stop()
        self.assertEqual(self.service.tick().status, "STOPPED")
        self.navigator.select.assert_not_called()

    def test_scroll_disabled_until_explicitly_granted(self):
        self.raw = view()
        self.service.tick()
        self.navigator.scroll_visible_contacts.assert_not_called()

    def test_scroll_requires_new_overlapping_page_and_stops_if_unchanged(self):
        self.service.stop()
        self.service.start(scroll=True)
        raw = view()
        self.raw = replace(raw, list_scrollbar=Box(384, 150, 387, 250))
        after = replace(self.raw, contacts=tuple(replace(row, display_name=name)
                        for row, name in zip(self.raw.contacts, (NAMES[1], NAMES[2], "翻页目标"))))
        self.navigator.scroll_visible_contacts.side_effect = lambda _: setattr(self, "raw", after)
        first, second = self.service.tick(), self.service.tick()
        self.assertEqual((first.status, first.pages, second.status), ("SCROLLED", 2, "NO_SCROLL_CHANGE"))
        self.service.tick()
        self.assertEqual(self.navigator.scroll_visible_contacts.call_count, 2)

    def test_scroll_c2_movement_stops_further_scroll(self):
        self.service.stop()
        self.service.start(scroll=True)
        raw = view()
        self.raw = replace(raw, list_scrollbar=Box(384, 150, 387, 250))
        pixels = raw.frame.pixels.copy()
        raw.layout.get("C2").crop(pixels)[40:95, 25:160] = 173
        frame = replace(raw.frame, capture=replace(raw.frame.capture, image=pixels))
        after = replace(self.raw, frame=frame, contacts=tuple(replace(row, display_name=name)
                        for row, name in zip(self.raw.contacts, (NAMES[1], NAMES[2], "翻页目标"))))
        self.navigator.scroll_visible_contacts.side_effect = lambda _: setattr(self, "raw", after)
        self.assertEqual(self.service.tick().status, "SCROLL_UNCERTAIN")
        self.service.tick()
        self.assertEqual(self.navigator.scroll_visible_contacts.call_count, 1)

    def test_window_replacement_rebinds_with_same_native_permission(self):
        self.service.tick()
        c = self.raw.frame.capture
        new_hwnd = c.hwnd + 1
        self.raw = replace(self.raw, frame=replace(self.raw.frame, capture=replace(c, hwnd=new_hwnd)))

        def switch_on_new_window(raw, row):
            self.switch(raw, row)
            self.raw = replace(self.raw, frame=replace(
                self.raw.frame, capture=replace(self.raw.frame.capture, hwnd=new_hwnd)))

        self.navigator.select.side_effect = switch_on_new_window
        result = self.service.tick()
        self.assertEqual((result.status, result.bound_hwnd), ("COMPLETED", new_hwnd))
        self.assertIn("WINDOW_RECREATED", result.rebind_result)
        self.assertIsNotNone(result.resumed_at)
        self.permit.revoke.assert_not_called()
        self.assertEqual(self.navigator.select.call_args.args[0].frame.capture.hwnd, new_hwnd)
        self.assertTrue(any(row["status"] == "PAUSED" and row["navigation_status"] == "REVALIDATE_WINDOW"
                            for row in self.store.browse(PROFILE)["jobs"]))

    def test_queued_target_relocates_after_rows_reorder(self):
        original = self.raw.contacts[2].box
        def rearrange(raw, row):
            self.switch(raw, row)
            if row.display_name == NAMES[1]:
                current = self.raw
                a, b, c = current.contacts
                self.raw = replace(current, contacts=(replace(c, box=a.box), b, replace(a, box=c.box)))
        self.navigator.select.side_effect = rearrange
        self.assertEqual(self.service.tick().status, "COMPLETED")
        self.assertEqual(self.service.tick().status, "COMPLETED")
        self.assertNotEqual(self.navigator.select.call_args.args[1].box, original)
        self.assertEqual(len(self.store.browse(PROFILE)["jobs"]), 2)

    def test_stop_after_click_keeps_audited_side_effect(self):
        def cancel(raw, row):
            self.switch(raw, row)
            self.service.stop()
        self.navigator.select.side_effect = cancel
        self.service.tick()
        self.assertTrue(any(job["status"] == "UNCERTAIN" and job["read_risk"]
                            for job in self.store.browse(PROFILE)["jobs"]))
        self.service.tick()
        self.assertEqual(self.navigator.select.call_count, 1)


class NativeBoundaryTests(unittest.TestCase):
    def test_native_permit_starts_closed(self):
        permit, action = CollectionPermit(), Mock()
        with self.assertRaises(NavigationPaused):
            permit.perform(action, scroll=False)
        action.assert_not_called()

    def test_b2_bounds_reject_search_input_and_forged_row(self):
        raw = view(unread={1: (1, False)})
        real = raw.contacts[1]
        for forbidden in ("B1", "C3"):
            with self.subTest(region=forbidden), self.assertRaises(NavigationPaused):
                row_target(raw, replace(real, box=raw.layout.get(forbidden)))

    def test_native_revalidates_fresh_b2_pixels_and_emits_no_input_on_change(self):
        raw = view(unread={1: (1, False)})
        pixels = raw.frame.pixels.copy()
        raw.layout.get("B2").crop(pixels)[0, 0] = 1
        fresh = replace(raw.frame, capture=replace(raw.frame.capture, image=pixels))
        nav = CollectionNavigation(CollectionPermit(), capture=lambda: fresh)
        with patch.object(nav, "verify_view", return_value=(lambda point: point, 1)), \
                patch("win32api.mouse_event") as send, self.assertRaises(NavigationPaused):
            nav.select(raw, raw.contacts[1])
        send.assert_not_called()

    def test_mocked_native_click_and_scroll_are_confined_to_b2(self):
        import win32con

        raw = view(unread={1: (2, False)})
        targets = []
        permit = CollectionPermit()
        permit._handle = Mock()
        permit._scroll = True
        self.addCleanup(permit.revoke)
        nav = CollectionNavigation(permit, capture=lambda: raw.frame, idle=lambda: (7, 4000))
        with (patch.object(nav, "verify_view", return_value=(lambda xy: xy, 7)),
              patch("win32api.SetCursorPos", side_effect=targets.append),
              patch("win32gui.GetCursorPos", side_effect=lambda: targets[-1]),
              patch("win32gui.GetForegroundWindow", return_value=raw.frame.capture.hwnd),
              patch("win32api.mouse_event") as send):
            nav.select(raw, raw.contacts[1])
            nav.scroll_visible_contacts(raw)
        area = raw.layout.get("B2")
        self.assertTrue(all(area.contains(xy) for xy in targets))
        self.assertFalse(any(raw.layout.get("C2").contains(xy) for xy in targets))
        self.assertEqual([call.args[0] for call in send.call_args_list],
                         [win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP, win32con.MOUSEEVENTF_WHEEL])
        self.assertEqual(send.call_args.args[3], -120)

    def test_security_scanner_rejects_legacy_activation_and_unscoped_input(self):
        for call in ("window.activate()", "win32api.mouse_event(1,0,0,0,0)", "keyboard.send_shortcut('x')"):
            self.assertTrue(inspect_collector_input(Path("collection_service.py"), call))


class CollectionGuiTests(unittest.TestCase):
    def test_start_requires_explicit_confirmation_and_gates_other_visual_modes(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from autoim.wecom.visual_panel import VisualCapturePanel

        app = QApplication.instance() or QApplication([])
        panel = VisualCapturePanel(Path("unused-calibration.json"), Mock())
        self.addCleanup(panel.close)
        collection = panel.collection_panel
        from autoim.wecom.collection_service import ScanStatus

        service = Mock()
        service.active = False
        service.snapshot.return_value = ScanStatus("RUNNING")
        service.start.side_effect = lambda **_: setattr(service, "active", True)
        service.stop.side_effect = lambda: setattr(service, "active", False)
        collection.make_service = Mock(return_value=service)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            collection.start()
        collection.make_service.assert_not_called()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            collection.start()
        collection.make_service.assert_called_once_with(None, collection.store)
        service.start.assert_called_once_with(scroll=False)
        self.assertFalse(panel.read_button.isEnabled())
        self.assertFalse(panel.monitor_panel.start_button.isEnabled())
        collection.stop()
        self.assertTrue(panel.read_button.isEnabled())
        app.processEvents()

    def test_history_dialog_uses_database_rows_not_independent_observations(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.collection_panel import CollectionHistory

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            store = VisualStore(Path(directory) / "history.sqlite3")
            raw = view()
            stored = store.save(PROFILE, raw)
            store.save(PROFILE, raw)
            def run(function, *, on_result, on_error):
                on_result(function())
            dialog = CollectionHistory(run, store, PROFILE, store.browse(PROFILE, stored.conversation_id), 0)
            self.assertEqual(dialog.history.rowCount(), 3)
            self.assertEqual(dialog.history.item(0, 4).text(), "你好")
            dialog.close()
            app.processEvents()
