from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PySide6.QtWidgets import QApplication, QMessageBox

from autoim.wecom.collection_core import MAX_READ_POLLS, CoreView, analyze_read
from autoim.wecom.collection_incremental import SOURCE, CoreAccount
from autoim.wecom.collection_navigation import NavigationPaused
from autoim.wecom.collection_panel import VisualCollectionPanel
from autoim.wecom.collection_runtime import CollectionRuntime
from autoim.wecom.collection_service import CollectionScope, CollectionService, ScanStatus
from autoim.wecom.visual_account import LoginEvidence
from autoim.wecom.visual_files import FileCardObservation
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_messages import OcrDisposition
from autoim.wecom.visual_observation import OcrAreaObservation, observe_frame
from autoim.wecom.visual_store import VisualStore
from test_wecom_collection import NAMES, view
from test_wecom_visual_capture import fixture, span
from test_wecom_window_recovery import BASE, health


ACCOUNT = "合成登录账号"
BODY = "合成消息 27Q"


def sample(selected=0, unread=None, *, tone=220, highlight=True, extra=(), name=ACCOUNT, avatar="synthetic"):
    raw = view(selected, unread=unread, group=selected == 2)
    pixels = raw.frame.pixels.copy()
    raw.layout.get("C2").crop(pixels)[:] = tone
    frame = replace(raw.frame, capture=replace(raw.frame.capture, image=pixels))
    rows = tuple(replace(row, is_selected=row.is_selected if highlight else None,
                         preview=BODY, preview_reliable=True)
                 for row in raw.contacts)
    conversation = raw.conversation
    if not highlight:
        conversation = replace(conversation, selected_name="", selected_name_reliable=False, match_status="UNKNOWN")
    return replace(raw, frame=frame, contacts=rows, conversation=conversation,
                   login=LoginEvidence(name, avatar, Box(0, 0, 24, 24), "synthetic"),
                   messages=(*extra, bubble(raw, BODY)))


def bubble(raw, body, *, y=330, kind="TEXT", status="CANDIDATE", score=.99, direction="INBOUND"):
    box = Box(490, y, 680, y + 35)
    return replace(raw.messages[0], sequence=y, box=box, text=body, kind=kind, status=status, direction=direction,
                   time_evidence="", ocr_spans=(replace(span(body, 503, y + 5, 620, y + 25), score=score),))


def with_avatar_texture(raw, index, *, different=False):
    box = raw.contacts[index].avatar
    pixels = raw.frame.pixels.copy()
    y, x = np.indices((box.height, box.width))
    texture = np.stack((x * 3 + 20, y * 3 + 30, (x + y) * 2 + 40), axis=-1)
    if different:
        texture = 245 - texture
    box.crop(pixels)[:] = texture
    return replace(raw, frame=replace(raw.frame, capture=replace(raw.frame.capture, image=pixels)))


class CoreFlowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.formal = VisualStore(self.root / "formal.sqlite3")
        self.isolated = VisualStore(self.root / "isolated.sqlite3")
        self.runtime = CollectionRuntime(self.root / "logs", revision="test")
        self.addCleanup(self.runtime.close)
        self.before = sample(unread={1: (1, False), 2: (1, False)})
        self.current = self.before
        self.window_generation = 1
        self.navigator = Mock()
        self.permit = Mock()
        self.service = CollectionService(None, self.formal, lambda _: self.current,
                                         capture=lambda: self.current.frame, navigator=self.navigator, permit=self.permit,
                                         require_email=True, isolated_store=self.isolated)
        self.service._lifetime = lambda _: (20, 100, self.window_generation)
        self.service.scope = CollectionScope()
        self.service.configure_runtime(self.runtime, "test")
        avatars = patch("autoim.wecom.collection_service.avatar_key", side_effect=lambda _, row: row.display_name)
        avatars.start()
        self.addCleanup(avatars.stop)
        self.addCleanup(self.service.stop)

    def start(self, *, failure_crops=False):
        self.service.start(core_test=True, failure_crops=failure_crops)

    def poll(self, observation=None, count=1):
        if observation is not None:
            self.current = observation
        for _ in range(count):
            state = self.service.tick()
            if self.service._core and self.service._core.presentation:
                self.service.confirm_core_displayed(state.core.messages)
        return state

    def events(self):
        self.runtime.sync()
        return [json.loads(line) for file in self.root.glob("logs/*/events.jsonl")
                for line in file.read_text(encoding="utf-8").splitlines()]

    def test_core_without_email_reads_b_and_never_opens_profile_or_database(self):
        self.start()
        first = self.poll()
        self.assertEqual(first.core.clicks, 1)
        self.assertIsNotNone(first.b2_scan_completed_at)
        state = self.poll(sample(1, {2: (1, False)}, tone=160), count=2)
        self.assertEqual((state.account_state, state.core.clicks, state.core.verified), ("TEST_A1_OBSERVED", 1, 1))
        self.assertEqual([(item.text, item.unread) for item in state.core.messages], [(BODY, "CONFIRMED_UNREAD")])
        self.navigator.profile_start.assert_not_called()
        self.assertFalse(self.formal.path.exists())
        self.assertFalse(self.isolated.path.exists())
        self.assertIsNone(state.notice)
        self.service.confirm_core_displayed(state.core.messages)
        events = self.events()
        codes = [event["code"] for event in events]
        for code in ("UNREAD_DETECTED", "TARGET_IN_SCOPE", "TARGET_SELECTED", "NAVIGATION_PRECHECK", "CONTACT_CLICKED",
                     "CONVERSATION_CHANGED", "CONVERSATION_VERIFIED", "C2_CAPTURED", "LATEST_POSITION_CHECKED",
                     "MESSAGE_OCR_COMPLETED", "UNREAD_RANGE_CHECKED", "RESULT_DISPLAYED", "TARGET_COMPLETED", "NEXT_TARGET"):
            self.assertIn(code, codes)
        selected = next(event["details"] for event in events if event["code"] == "TARGET_SELECTED")
        self.assertEqual(selected["unread_count"], 1)
        self.assertIsNotNone(selected["badge_box"])
        self.assertNotIn(BODY, repr(events))
        self.assertNotIn(ACCOUNT, repr(events))
        self.assertNotIn(NAMES[1], repr(events))

    def test_production_preserves_email_gate(self):
        self.service.configure_runtime(self.runtime, "production")
        with self.assertRaises(NavigationPaused):
            self.start()
        self.service.start()
        with patch.object(self.service, "_profile_tick", return_value=ScanStatus()) as profile:
            self.poll()
        profile.assert_called_once()
        self.navigator.select.assert_not_called()

    def test_confirmed_core_flow_commits_then_displays_and_never_writes_formal_store(self):
        self.service._core_account = CoreAccount("合成采集账号", "core@example.test")
        self.start()
        self.poll()
        result = self.poll(sample(1, {2: (1, False)}, tone=160), count=2)
        self.assertEqual((result.core.clicks, result.core.verified, result.core.first_saved), (1, 1, 1))
        self.assertEqual((result.core.messages[0].text, result.core.messages[0].save_status), (BODY, "SAVED"))
        self.assertFalse(self.formal.path.exists())
        with self.isolated.transaction() as db:
            self.assertEqual(db.execute("SELECT body FROM messages WHERE source=?", (SOURCE,)).fetchone()[0], BODY)
        events = self.events()
        codes = [event["code"] for event in events]
        self.assertLess(codes.index("CORE_DB_COMMITTED"), codes.index("RESULT_DISPLAYED"))
        self.assertNotIn(BODY, repr(events))

    def test_confirmed_core_flow_displays_body_after_isolated_commit_failure(self):
        self.service._core_account = CoreAccount("合成采集账号", "core@example.test")
        self.start()
        self.poll()
        with patch.object(self.isolated, "transaction", side_effect=OSError):
            result = self.poll(sample(1, {2: (1, False)}, tone=160), count=2)
        self.assertEqual((result.core.first_saved, result.core.save_failures), (0, 1))
        self.assertEqual((result.core.messages[0].text, result.core.messages[0].save_status), (BODY, "FAILED"))
        self.assertFalse(self.formal.path.exists())
        self.assertFalse(self.isolated.path.exists())

    def test_startup_a1_preview_does_not_touch_databases(self):
        status = self.service.identify_account(visual_only=True)
        self.assertEqual((status.account_name, status.account_state), (ACCOUNT, "TEST_A1_PREVIEW"))
        self.assertFalse(self.formal.path.exists())
        self.assertFalse(self.isolated.path.exists())

    def test_missing_a1_is_diagnostic_and_does_not_block_the_core_read(self):
        missing = LoginEvidence(reason="合成 A1 OCR 不可用")
        self.start()
        first = self.poll(replace(self.before, login=missing))
        self.assertEqual((first.account_state, first.core.clicks), ("TEST_A1_UNAVAILABLE", 1))
        result = self.poll(replace(sample(1, tone=135), login=missing), count=2)
        self.assertEqual(result.core.messages[0].text, BODY)
        self.assertFalse(self.formal.path.exists())
        self.assertFalse(self.isolated.path.exists())

    def test_unverified_window_lifetime_blocks_native_input(self):
        self.start()
        self.window_generation = None
        status = self.poll()
        self.assertIn("WINDOW_STATE_UNCERTAIN", status.reason)
        self.navigator.select.assert_not_called()
        self.window_generation = 1
        self.assertEqual(self.poll().core.clicks, 1)

    def test_same_named_rows_are_not_clicked(self):
        self.start()
        original = self.before.contacts
        duplicate = replace(original[2], display_name=original[1].display_name)
        self.poll(replace(self.before, contacts=(*original[:2], duplicate)), count=2)
        self.navigator.select.assert_not_called()

    def test_contact_avatar_is_taken_from_current_scan_not_a_previous_roster(self):
        self.start()
        early = sample(unread={1: (1, False)})
        self.poll(replace(early, contacts=tuple(replace(row, is_muted=None) if row.has_unread_indicator else row
                                                for row in early.contacts)))
        with patch("autoim.wecom.collection_service.avatar_key", side_effect=lambda _, row: "new-" + row.display_name):
            status = self.poll(self.before)
        self.assertEqual(status.core.clicks, 1)
        self.assertEqual(self.service._core.task.target.avatar, "new-" + NAMES[1])

    def test_unknown_muted_status_is_skipped_and_next_in_scope_is_clicked(self):
        self.start()
        rows = self.before.contacts
        status = self.poll(replace(self.before, contacts=(rows[0], replace(rows[1], is_muted=None), rows[2])))
        self.assertEqual(status.unknown_muted_unread, 1)
        self.assertEqual(self.navigator.select.call_args.args[1].display_name, NAMES[2])

    def test_default_scope_skips_muted_and_folded_and_applies_configured_muted(self):
        self.start()
        personal, muted, folded = self.before.contacts
        raw = replace(self.before, contacts=(personal, replace(muted, is_muted=True),
                                             replace(folded, kind="FOLDED_CHAT_CONTAINER")))
        status = self.poll(raw)
        self.assertEqual((status.policy_skipped_unread, status.unsupported_unread), (1, 1))
        self.navigator.select.assert_not_called()
        self.service.stop()
        self.service.scope = CollectionScope(muted=True)
        self.start()
        self.poll(raw)
        self.assertEqual(self.navigator.select.call_args.args[1].display_name, NAMES[1])

    def test_multiple_contacts_use_fresh_b2_order_and_preserve_message_content(self):
        self.start()
        self.poll()
        self.poll(sample(1, {2: (1, False)}, tone=147), count=2)
        next_view = sample(1, {0: (1, False), 2: (1, False)}, tone=147)
        a, b, group = next_view.contacts
        fresh_group = replace(group, box=a.box, badge=a.badge, avatar=a.avatar)
        fresh_a = replace(a, box=b.box, badge=a.badge.offset(0, b.box.top - a.box.top), avatar=b.avatar)
        self.poll(replace(next_view, contacts=(fresh_a, fresh_group)))
        result = self.poll(sample(2, tone=101), count=2)
        targets = [call.args[1] for call in self.navigator.select.call_args_list]
        self.assertEqual([row.display_name for row in targets], [NAMES[1], NAMES[2]])
        self.assertEqual(targets[1].box, fresh_group.box)
        self.assertEqual([item.contact for item in result.core.messages], [NAMES[1], NAMES[2]])
        self.assertFalse(self.formal.path.exists())

    def test_no_initial_highlight_and_no_post_highlight_use_transition_evidence(self):
        self.start()
        self.poll(sample(unread={1: (1, False)}, highlight=False))
        result = self.poll(sample(1, tone=130, highlight=False), count=2)
        self.assertEqual((result.core.clicks, result.core.verified), (1, 1))
        self.assertEqual(result.core.messages[0].unread, "CONFIRMED_UNREAD")

    def test_wrong_conversation_does_not_expose_body_and_next_target_can_proceed(self):
        self.start()
        self.poll()
        failed = self.poll(sample(0, {1: (1, False), 2: (1, False)}, tone=99), count=MAX_READ_POLLS)
        self.assertEqual((failed.core.failures, failed.core.read_unfinished, failed.core.messages), (1, 1, ()))
        independent = self.poll()
        self.assertEqual(independent.core.messages[0].unread, "INDEPENDENT_OCR")
        self.assertNotEqual(independent.core.messages[0].contact, NAMES[1])
        self.poll()
        self.assertEqual([call.args[1].display_name for call in self.navigator.select.call_args_list], [NAMES[1], NAMES[2]])
        self.assertTrue(any(event["details"].get("scope_decision") == "UNREAD_ALREADY_HANDLED" for event in self.events()))

    def test_unchanged_c2_never_confirms_messages_after_click(self):
        self.start()
        self.poll()
        result = self.poll(sample(1), count=MAX_READ_POLLS)
        self.assertEqual((result.core.verified, result.core.read_unfinished, result.core.messages), (1, 1, ()))
        self.navigator.select.assert_called_once()

    def test_nonconsecutive_correct_title_does_not_establish_stability(self):
        self.start()
        self.poll()
        self.poll(sample(1, tone=100))
        self.poll(sample(0, tone=100))
        self.assertFalse(self.poll(sample(1, tone=100)).core.messages)
        self.assertEqual(self.poll().core.messages[0].text, BODY)

    def test_changed_avatar_hash_can_be_corroborated_after_the_actual_click(self):
        self.start()
        before = with_avatar_texture(sample(unread={1: (1, False)}), 1)
        self.poll(before)
        after = with_avatar_texture(sample(1, tone=120), 1)
        with patch("autoim.wecom.collection_service.avatar_key", return_value="different-hash"):
            result = self.poll(after, count=2)
        self.assertEqual((result.core.clicks, result.core.verified), (1, 1))
        self.assertEqual(result.core.messages[0].text, BODY)
        self.assertEqual(result.core.messages[0].unread, "CONFIRMED_UNREAD")
        self.assertTrue(any(event["details"].get("match_evidence") == "CLICK_TITLE_ROW_BADGE_REFRESH_AND_AVATAR"
                            for event in self.events()))

    def test_matching_title_cannot_override_a_conflicting_avatar_after_click(self):
        self.start()
        self.poll(with_avatar_texture(sample(unread={1: (1, False)}), 1))
        after = with_avatar_texture(sample(1, tone=120), 1, different=True)
        with patch("autoim.wecom.collection_service.avatar_key", return_value="different-hash"):
            result = self.poll(after, count=MAX_READ_POLLS)
        self.assertEqual((result.core.verified, result.core.read_unfinished, result.core.messages), (0, 1, ()))
        self.assertEqual(self.poll().core.messages[0].unread, "INDEPENDENT_OCR")

    def test_selected_row_with_missing_avatar_contour_uses_before_click_crop(self):
        self.start()
        self.poll(with_avatar_texture(sample(unread={1: (1, False)}), 1))
        after = with_avatar_texture(sample(1, tone=120), 1)
        rows = tuple(replace(row, avatar=None) if row.is_selected else row for row in after.contacts)
        with patch("autoim.wecom.collection_service.avatar_key",
                   side_effect=lambda _, row: row.display_name if row.avatar else ""):
            result = self.poll(replace(after, contacts=rows), count=2)
        self.assertEqual(result.core.verified, 1)
        self.assertEqual(result.core.messages[0].unread, "CONFIRMED_UNREAD")

    def test_core_retries_ocr_without_reclick_and_preserves_reliable_body_on_incomplete_page(self):
        self.start()
        self.poll()
        unknown = bubble(view(), "", y=425, kind="UNKNOWN", status="UNCLASSIFIED")
        partial = sample(1, tone=120)
        partial = replace(partial, messages=(*partial.messages, unknown), message_completeness="VISIBLE_MESSAGES_INCOMPLETE")
        result = self.poll(partial, count=MAX_READ_POLLS)
        self.navigator.select.assert_called_once()
        self.assertEqual([item.quality for item in result.core.messages], ["RELIABLE", "UNKNOWN_TYPE"])
        self.assertEqual(result.core.messages[0].text, BODY)
        self.assertEqual(result.core.completeness, "VISIBLE_MESSAGES_INCOMPLETE")
        self.assertEqual((result.core.phase, result.core.read_unfinished), ("TARGET_PARTIAL", 1))

    def test_window_recreation_and_changed_a1_both_stop_navigation(self):
        self.start()
        self.poll()
        result = self.poll(sample(1, tone=120, avatar="other"))
        self.assertEqual(result.core.phase, "WINDOW_IDENTITY_CHANGED")
        self.assertEqual(result.core.read_unfinished, 1)
        self.permit.revoke.assert_called_once()
        self.service.stop()
        self.start()
        self.poll(sample(unread={1: (1, False)}))
        self.window_generation += 1
        result = self.poll()
        self.assertEqual(result.core.phase, "WINDOW_IDENTITY_CHANGED")
        self.assertEqual(self.navigator.select.call_count, 2)

    def test_native_gate_failure_is_bounded_and_next_target_is_attempted(self):
        self.start()
        self.navigator.select.side_effect = [NavigationPaused("遮挡"), NavigationPaused("用户正在操作"), None]
        self.poll(count=4)
        self.assertEqual([call.args[1].display_name for call in self.navigator.select.call_args_list],
                         [NAMES[1], NAMES[1], NAMES[2]])
        self.assertEqual(self.service.snapshot().core.failures, 1)

    def test_background_scans_unread_without_click_then_foreground_revalidates(self):
        background = health(replace(BASE, foreground_hwnd=999))
        foreground = health()
        self.service._window_probe = Mock(return_value=background)
        self.start()
        result = self.poll()
        self.assertEqual((result.core.phase, result.core.discovered, result.core.waiting), ("WAITING_FOREGROUND", 2, 2))
        self.navigator.select.assert_not_called()
        self.service._window_probe.return_value = foreground
        self.poll(sample(unread={1: (2, False)}))
        attempted = self.navigator.select.call_args.args[1]
        self.assertEqual(attempted.unread_count, 2)

    def test_user_idle_gate_does_not_exhaust_target_retry_budget(self):
        self.start()
        self.navigator.verify_view.side_effect = NavigationPaused("等待用户空闲")
        result = self.poll(count=4)
        self.assertEqual((result.core.phase, result.core.failures), ("WAITING_NAVIGATION_SAFETY", 0))
        self.navigator.select.assert_not_called()
        self.navigator.verify_view.side_effect = None
        self.poll()
        self.navigator.select.assert_called_once()

    def test_b2_ocr_failure_cannot_schedule_native_input(self):
        self.start()
        failed = OcrAreaObservation("B2 联系人", self.before.layout.get("B2"), "FAILED")
        status = self.poll(replace(self.before, ocr_areas=(failed,)), count=2)
        self.assertEqual(status.scan_disposition, "B2_SCAN_FAILED")
        self.navigator.select.assert_not_called()

    def test_b2_deadline_discards_late_results_and_continues_independent_c2(self):
        self.start()
        clock = self.service._core.clock
        def slow(_frame):
            end = clock.phase.started + 15.1
            clock.now = lambda: end
            return self.before

        with patch.object(self.service, "read", side_effect=slow):
            self.poll()
        self.navigator.select.assert_not_called()
        self.assertEqual(clock.outcomes[2], "TIMEOUT")
        result = self.poll(sample())
        self.assertTrue(result.core.messages)
        self.assertEqual(result.core.messages[0].unread, "INDEPENDENT_OCR")
        self.assertEqual(self.poll().core.phase, "TEST_FINISHED")
        self.assertLess(clock.finished - clock.started, 180)

    def test_no_visible_unread_can_read_and_finish_without_navigation(self):
        self.start()
        self.poll(sample())
        status = self.poll()
        self.assertEqual(status.core.messages[0].unread, "INDEPENDENT_OCR")
        self.assertEqual(self.poll().core.phase, "TEST_FINISHED")
        self.navigator.select.assert_not_called()
        self.assertFalse(self.formal.path.exists())
        self.assertFalse(self.isolated.path.exists())

    def test_muted_unknown_has_two_samples_then_independent_c2(self):
        self.start()
        raw = sample(unread={1: (1, False)})
        rows = tuple(replace(row, is_muted=None) if row.has_unread_indicator else row for row in raw.contacts)
        frame = replace(raw.frame, state=replace(raw.frame.state, dpi=168))
        self.poll(replace(raw, frame=frame, contacts=rows), count=2)
        self.assertEqual(max(self.service._core.mute_trials.values()), 2)
        result = self.poll()
        self.assertTrue(result.core.messages)
        self.assertEqual(self.poll().core.phase, "TEST_FINISHED")
        self.navigator.select.assert_not_called()

    def test_same_recent_mute_crop_can_recover_an_unknown_result(self):
        self.start()
        self.service._window_probe = Mock(return_value=health(replace(BASE, foreground_hwnd=999)))
        raw = sample(unread={1: (1, False)})
        self.poll(raw)
        self.service._window_probe.return_value = health()
        rows = tuple(replace(row, is_muted=None) if row.has_unread_indicator else row for row in raw.contacts)
        revised = self.poll(replace(raw, contacts=rows))
        self.assertEqual(revised.core.clicks, 1)
        self.assertTrue(any(event["details"].get("method") == "RECENT_SAME_IDENTITY_UNCHANGED_ICON_AREA"
                            for event in self.events()))

    def test_run_watchdog_ends_without_waiting_for_read_worker_to_return(self):
        self.start()
        self.service.begin_worker()
        clock = self.service._core.clock
        clock.now = lambda: clock.end
        self.service.check_core_watchdog()
        self.assertFalse(self.service.active)
        self.assertTrue(clock.snapshot()["finished"])
        self.assertEqual(self.service.snapshot().stop_reason, "timeout")
        self.service.end_worker()
        self.navigator.select.assert_not_called()

    def test_stop_retains_results_and_clear_only_affects_display(self):
        self.start()
        self.poll()
        self.poll(sample(1, tone=120), count=2)
        self.service.begin_worker()
        self.service.stop(source="hotkey")
        self.assertEqual(self.runtime.report(self.service.detection_id)["stages"]["STOP"]["status"], "PARTIAL")
        self.service.end_worker()
        self.assertEqual(self.runtime.report(self.service.detection_id)["stages"]["STOP"]["status"], "AUTO_PASS")
        self.assertEqual(self.poll(self.before).core.messages[0].text, BODY)
        self.navigator.select.assert_called_once()
        self.assertEqual(self.service.clear_core_results().core.messages, ())
        self.assertFalse(self.formal.path.exists())

    def test_stop_from_button_completes_and_late_pointer_changes_fail_audit(self):
        self.start()
        self.service.stop()
        self.assertEqual(self.runtime.report(self.service.detection_id)["stages"]["STOP"]["status"], "AUTO_PASS")
        self.runtime.emit("test", self.service.detection_id, "WINDOW", "CURSOR_MOVED")
        self.assertEqual(self.runtime.report(self.service.detection_id)["stages"]["STOP"]["status"], "FAILED")

    def test_opt_in_failed_title_crop_excluded_from_diagnostics(self):
        self.start(failure_crops=True)
        self.poll()
        private = self.root / "private-crops"
        with patch("autoim.wecom.collection_service.failure_crop_directory", return_value=private):
            self.poll(sample(0, tone=99), count=MAX_READ_POLLS)
        self.assertEqual(len(list(private.glob("*.png"))), 1)
        report = self.root / "report.zip"
        self.runtime.export(report, "detection", self.service.detection_id, {})
        with zipfile.ZipFile(report) as archive:
            self.assertTrue(archive.namelist())
            self.assertFalse(any(name.endswith(".png") for name in archive.namelist()))

    def test_repeated_observation_of_same_task_does_not_add_messages(self):
        self.start()
        self.poll()
        task = self.service._core.task
        result = self.poll(sample(1, tone=120), count=2)
        self.assertEqual(len(result.core.messages), 1)
        analysis = analyze_read(self.current, task.id, NAMES[1], 1, BODY,
                                verified=True, badge_cleared=True, refreshed=True)
        self.service._core.accept_messages(task, analysis)
        self.assertEqual(len(self.service._core.view.messages), 1)
        self.assertEqual(self.service._core.view.repeated, 1)


class CoreReadTests(unittest.TestCase):
    def test_separator_and_bottom_aligned_visible_range_support_multiple_and_identical_unreads(self):
        raw = sample(1)
        separator = OcrDisposition(span("以下是新消息", 625, 170, 738, 194), "SYSTEM", None, None, "")
        before = bubble(raw, "历史", y=130)
        items = (before, bubble(raw, BODY, y=220), bubble(raw, BODY, y=275), raw.messages[0])
        c2 = raw.layout.get("C2")
        raw = replace(raw, messages=items, message_ocr=(separator,),
                      history_scrollbar=Box(c2.right-7, c2.bottom-120, c2.right-3, c2.bottom-2))
        result = analyze_read(raw, "test", NAMES[1], 3, BODY, verified=True, badge_cleared=True, refreshed=True)
        self.assertEqual([item.unread for item in result.messages], ["HISTORY_VISIBLE", *["CONFIRMED_UNREAD"]*3])
        self.assertEqual(len({item.id for item in result.messages}), 4)

    def test_file_metadata_and_text_remain_readable_despite_unassigned_ocr(self):
        raw = sample(1)
        file = bubble(raw, "", y=200, kind="FILE")
        file = replace(file, file=FileCardObservation("sample.txt", "txt", "10 KB", file.box, (), (), (), .96, "COMPLETE", ()))
        unknown_ocr = OcrDisposition(span("合成未归属", 518, 410, 611, 439), "UNASSIGNED", None, None, "", True)
        result = analyze_read(replace(raw, messages=(file, *raw.messages), message_ocr=(unknown_ocr,),
                                      message_completeness="VISIBLE_MESSAGES_INCOMPLETE"),
                              "test", NAMES[1], 1, BODY, verified=True, badge_cleared=True, refreshed=True)
        self.assertEqual([item.text for item in result.messages], ["sample.txt · 10 KB", BODY])
        self.assertTrue(all(item.quality == "RELIABLE" for item in result.messages))
        self.assertEqual(result.gaps[0]["reason"], "UNASSIGNED_MESSAGE_OCR")
        self.assertNotIn("CONFIRMED_UNREAD", [item.unread for item in result.messages])

    def test_away_from_bottom_and_unknown_attribution_are_explicit(self):
        raw = sample(1)
        c2 = raw.layout.get("C2")
        raw = replace(raw, history_scrollbar=Box(c2.right-9, c2.top+10, c2.right-4, c2.top+80))
        for verified, unread in ((True, "UNREAD_CANDIDATE"), (False, "ATTRIBUTION_UNCERTAIN")):
            result = analyze_read(raw, "test", NAMES[1], 1, BODY, verified=verified, badge_cleared=True, refreshed=True)
            self.assertEqual((result.latest, result.messages[0].unread), ("UNKNOWN", unread))

    def test_low_confidence_clipping_and_outbound_do_not_create_unread(self):
        raw = sample(1)
        items = (bubble(raw, BODY, y=205, score=.45), bubble(raw, "合成裁剪", y=255, status="PARTIALLY_VISIBLE"),
                 replace(raw.messages[0], direction="OUTBOUND"))
        result = analyze_read(replace(raw, messages=items), "test", NAMES[1], 3, BODY,
                              verified=True, badge_cleared=True, refreshed=True)
        self.assertEqual([item.quality for item in result.messages], ["LOW_CONFIDENCE", "CLIPPED", "RELIABLE"])
        self.assertTrue(all(item.unread == "UNKNOWN" for item in result.messages))

    def test_header_only_gate_prevents_c2_and_composer_text_ocr(self):
        frame, reader = fixture()
        gate = Mock(return_value=False)
        raw = observe_frame(frame, {}, reader, message_gate=gate)
        self.assertEqual(raw.status, "HEADER_ONLY")
        gate.assert_called_once()
        self.assertFalse(any(area.region.startswith(("C2", "C3")) for area in raw.ocr_areas))

    def test_header_only_gate_accepts_b2_without_c1_title(self):
        frame, reader = fixture(title="")
        raw = observe_frame(frame, {}, reader, message_gate=lambda _: False)
        self.assertEqual(raw.page, "MESSAGES_CANDIDATE")
        self.assertFalse(raw.conversation.title_name_reliable)
        self.assertTrue(any(row.has_unread_indicator for row in raw.contacts))


class CorePanelTests(unittest.TestCase):
    def test_start_button_uses_core_without_prompt_and_displays_ocr_body(self):
        app = QApplication.instance() or QApplication([])
        service = Mock()
        service.active = False
        service.snapshot.return_value = ScanStatus("RUNNING")
        service.start.side_effect = lambda **_: setattr(service, "active", True)
        service.stop.side_effect = lambda **_: setattr(service, "active", False)
        panel = VisualCollectionPanel(Mock(), Mock(return_value=service))
        panel.enable_runtime(Mock())
        with patch.object(QMessageBox, "question") as prompt:
            panel.start_button.click()
        prompt.assert_not_called()
        service.start.assert_called_once_with(core_test=True, failure_crops=False)
        messages = analyze_read(sample(1), "synthetic-task", NAMES[1], 1, BODY,
                                verified=True, badge_cleared=True, refreshed=True).messages
        panel._render(ScanStatus(core=CoreView(messages=messages)))
        self.assertEqual(panel.core_table.item(0, 5).text(), BODY)
        self.assertEqual(panel.core_table.item(0, 7).text(), "已确认未读")
        service.confirm_core_displayed.assert_called_once_with(messages)
        panel.stop()
        panel.close()
        app.processEvents()

    def test_first_hotkey_starts_core_without_prompt_and_second_stops(self):
        app = QApplication.instance() or QApplication([])
        service = Mock()
        service.active = False
        service.snapshot.return_value = ScanStatus("RUNNING")
        service.start.side_effect = lambda **_: setattr(service, "active", True)
        service.stop.side_effect = lambda **_: setattr(service, "active", False)
        panel = VisualCollectionPanel(Mock(), Mock(return_value=service))
        panel.enable_runtime(Mock())
        with patch.object(QMessageBox, "question") as prompt:
            panel.toggle()
            self.assertTrue(panel._timer.isActive())
            panel.toggle()
        prompt.assert_not_called()
        service.start.assert_called_once_with(core_test=True, failure_crops=False)
        service.stop.assert_called_once_with(source="hotkey")
        self.assertFalse(panel._timer.isActive())
        panel.close()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
