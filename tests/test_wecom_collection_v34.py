import unittest
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from autoim.wecom.collection_events import CollectionTrace, SaveNotice
from autoim.wecom.collection_navigation import CollectionNavigation, NavigationPaused
from autoim.wecom.collection_notifications import CollectionNotifier
from autoim.wecom.collection_profile import ProfileCheck, email_from_spans, owner_chain, unique_cards
from autoim.wecom.collection_runtime import CollectionRuntime
from autoim.wecom.collection_service import CollectionScope, CollectionService, unread_targets
from autoim.wecom.visual_account import LoginEvidence, _signature
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_store import VisualStore
from test_wecom_collection import PROFILE, view
import test_wecom_collection_v32 as prior

from autoim.wecom.collection_profile import ProfileWindows, PopupEvidence


class ProfileWindowTests(unittest.TestCase):
    def test_detached_menu_can_own_new_detail_card(self):
        context = ProfileWindows((20, 100, 1., "wxwork.exe"), frozenset())
        menu = PopupEvidence(200, 20, 33, "WXworkWindow", 0, 0, 200, 200, (), ((30, 20), 320, 280))
        card = PopupEvidence(300, 20, 33, "UserInfoWindow", 200, 0, 300, 200, (200,), ((75, 65), 450, 510))
        self.assertIsNone(context.relation(card, detached=False))
        context.menu = menu
        context.before_card = frozenset({200})
        context.card_click = "click-event"
        self.assertEqual(context.relation(card, detached=False), "VERIFIED_MENU_DESCENDANT")

    def popup(self, hwnd=300, pid=20, chain=(200,), owner=200):
        return PopupEvidence(hwnd, pid, 33, "UserInfoWindow", owner, 0, hwnd, owner, chain, ((75, 65), 450, 510))

    def test_wrong_process_and_old_popup_are_rejected(self):
        context = ProfileWindows((20, 100, 1., "wxwork.exe"), frozenset({301}), card_click="click")
        context.menu = self.popup(200, chain=(), owner=0)
        for popup in (self.popup(pid=21), self.popup(hwnd=301), self.popup(chain=(777,), owner=777)):
            self.assertIsNone(context.relation(popup, detached=False))

    def test_main_owned_card_is_admitted_but_only_after_click(self):
        popup = self.popup(chain=(100,), owner=100)
        context = ProfileWindows((20, 100, 1., "wxwork.exe"), frozenset())
        self.assertIsNone(context.relation(popup, detached=False))
        context.card_click = "click"
        self.assertEqual(context.relation(popup, detached=False), "MAIN_DESCENDANT")
        context.valid = False
        self.assertIsNone(context.relation(popup, detached=False))

    def test_preexisting_card_and_reused_observed_handle_fail_closed(self):
        context = ProfileWindows((20, 100, 1., "wxwork.exe"), frozenset(), card_click="click")
        popup = self.popup(chain=(100,), owner=100)
        context.observe({300: popup})
        self.assertIsNone(context.relation(replace(popup, thread=777), detached=False))
        self.assertIsNone(context.relation(popup, detached=False))
        context.before_card = frozenset({301})
        self.assertIsNone(context.relation(replace(popup, hwnd=301), detached=False))

    def test_missing_handle_is_retired_but_hidden_owner_remains(self):
        menu = self.popup(200, chain=(), owner=0)
        card = self.popup()
        context = ProfileWindows((20, 100, 1., "wxwork.exe"), frozenset(), card_click="click", menu=menu)
        context.observe({200: menu})
        context.observe({300: card}, present={200, 300})
        self.assertEqual(context.relation(card, detached=False), "VERIFIED_MENU_DESCENDANT")
        context.observe({}, present={200})
        self.assertIsNone(context.relation(card, detached=False))

    def test_owner_cycles_invalid_handles_and_depth_fail_closed(self):
        self.assertEqual(owner_chain(1, {1: 2, 2: 0}.get, lambda _: 9, 9), ((2,), True))
        self.assertFalse(owner_chain(1, {1: 2, 2: 1}.get, lambda _: 9, 9)[1])
        self.assertFalse(owner_chain(1, {1: 2}.get, lambda _: None, 9)[1])
        self.assertFalse(owner_chain(1, lambda hwnd: hwnd + 1, lambda _: 9, 9)[1])

    def test_pending_foreground_card_can_be_read_but_other_app_cannot(self):
        from test_wecom_window_recovery import health
        sample = health()
        context = ProfileWindows(sample.identity, frozenset(), card_click="test")
        nav = CollectionNavigation(Mock())
        nav.report = Mock()
        with (patch("win32gui.GetForegroundWindow", return_value=300) as foreground,
              patch("win32gui.IsWindow", return_value=True),
              patch("win32gui.GetClassName", return_value="UserInfoWindow"),
              patch("win32gui.GetWindow", return_value=200),
              patch("win32gui.GetParent", return_value=200),
              patch("win32gui.GetAncestor", return_value=300),
              patch("win32process.GetWindowThreadProcessId", return_value=(33, sample.window.pid)) as pid):
            self.assertEqual(nav._profile_foreground(sample, [(300, ())], context), 300)
            self.assertEqual(nav.report.call_args.kwargs["details"]["relation"], "PENDING_PROFILE")
            foreground.return_value = 400
            pid.return_value = 33, sample.window.pid + 1
            with self.assertRaisesRegex(NavigationPaused, "OTHER_APP"):
                nav._profile_foreground(sample, [(300, ())], context)

    def test_native_enumeration_correlates_detached_menu_child(self):
        from test_wecom_window_recovery import health
        sample = health()
        root, pid = sample.window.hwnd, sample.window.pid
        ctx = ProfileWindows(sample.identity, frozenset(), card_click="synthetic-click")
        menu = self.popup(200, pid, (), 0)
        card = self.popup(300, pid)
        ctx.menu = menu
        ctx.before_card = frozenset({200})
        nav = CollectionNavigation(Mock())
        nav.report = Mock()
        with patch.object(nav, "_profile_inventory", return_value={
                200: (menu, True, "VISIBILITY"), 300: (card, False, ""),
                301: (replace(card, hwnd=301, root_owner=777, chain=(777,)), False, "")}):
            self.assertEqual(nav._owned_profile_windows(sample, ctx), ((300, card.geometry),))
        admitted = next(call.kwargs["details"] for call in nav.report.call_args_list
                        if call.args[1] == "CARD_WINDOW_ADMITTED")
        self.assertEqual((admitted["upstream_click"], admitted["root_owner"], admitted["chain"]),
                         ("synthetic-click", 200, (200,)))


class CardTests(unittest.TestCase):
    def test_adjacent_fragments_join_but_far_text_does_not(self):
        _, _, card, _ = prior.IdentityTests.profile_fixture(self)
        span = prior.span
        label = span("邮箱", 100, 100, 135, 119)
        left = span("synthetic@", 155, 100, 205, 119)
        right = span("example.test", 208, 101, 300, 120)
        facts = {}
        self.assertEqual(email_from_spans(card, (label, left, right), evidence=facts), "synthetic@example.test")
        self.assertEqual(facts["value_fragments"], 2)
        self.assertNotIn("synthetic", repr(facts))
        self.assertEqual(email_from_spans(card, (label, left, replace(right, box=Box(270, 101, 400, 120)))), "")

    def test_card_duplicate_uses_physical_extent_and_prefers_native(self):
        _, _, card, spans = prior.IdentityTests.profile_fixture(self)
        embedded = replace(card, hwnd=1, geometry=())
        separate = replace(card, hwnd=111)
        locations = {card.hwnd: (220, 220, 620, 620), 1: (225, 225, 619, 625), 111: (650, 240, 910, 580)}
        result = unique_cards([(embedded, spans), (card, spans), (separate, spans)],
                              lambda item: locations[item.hwnd])
        self.assertEqual([item.hwnd for item, _ in result], [card.hwnd, separate.hwnd])


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.formal = VisualStore(root / "formal.sqlite3")
        self.test = VisualStore(root / "test-data" / "test.sqlite3")
        self.runtime = CollectionRuntime(root / "logs", revision="synthetic")
        self.addCleanup(self.runtime.close)
        self.before = self.with_login(view(unread={1: (1, False), 2: (1, False)}))
        self.after = self.with_login(view(1))
        self.navigator = Mock()
        self.reader = Mock(return_value=self.before)
        self.service = CollectionService(None, self.formal, self.reader, capture=lambda: self.reader.return_value.frame,
                                         navigator=self.navigator, permit=Mock(), require_email=True, isolated_store=self.test)
        self.service._lifetime = lambda _: (20, 100, (20, 100, 1., "wxwork.exe"))
        self.service.configure_runtime(self.runtime, "test")
        self.service.start()
        self.service.assist_identity("skip")
        self.addCleanup(self.service.stop)
        self.avatar = patch("autoim.wecom.collection_service.avatar_key", side_effect=lambda _, row: row.display_name)
        self.avatar.start()
        self.addCleanup(self.avatar.stop)

    @staticmethod
    def with_login(raw):
        box = Box(10, 10, 82, 82)
        return replace(raw, login=LoginEvidence(
            "测试操作员", "synthetic-avatar", box, _signature(box.crop(raw.frame.pixels)), avatar_box=box))

    def test_no_authorization_observes_c1_c2_without_clicking_or_storing(self):
        self.service.tick()
        self.navigator.select.assert_not_called()
        self.assertFalse(self.formal.path.exists())
        self.assertFalse(self.test.path.exists())
        report = self.runtime.report(self.service.detection_id)
        self.assertEqual(report["stages"]["TEST_C1"]["status"], "AUTO_PASS")
        self.assertEqual(report["stages"]["TEST_C2"]["status"], "AUTO_PASS")
        self.assertEqual(report["stages"]["C2"]["status"], "BLOCKED_BY_DEPENDENCY")

    def test_authorized_target_only_and_dedup_in_isolated_store(self):
        state = self.service.tick()
        option = next(item for item in state.isolated_choices if item.target.row.sequence == 2)
        self.service.authorize_isolated(option)
        self.navigator.select.side_effect = lambda *_: setattr(self.reader, "return_value", self.after)
        self.service.tick()
        self.service.tick()
        self.service.tick()
        self.navigator.select.assert_called_once()
        self.assertEqual(self.navigator.select.call_args.args[1].display_name, option.target.row.display_name)
        self.assertFalse(self.formal.path.exists())
        self.assertEqual(len(self.test.accounts()), 1)
        account = self.test.accounts()[0]
        self.assertEqual(VisualStore(self.test.path).browse(account.id)["message_count"], 3)

    def test_login_change_invalidates_authorization_and_does_not_click(self):
        option = self.service.tick().isolated_choices[0]
        self.service.authorize_isolated(option)
        self.reader.return_value = replace(self.before, login=replace(self.before.login, avatar="changed"))
        self.service.tick()
        self.navigator.select.assert_not_called()
        self.assertIsNone(self.service._isolated_run)

    def test_isolated_transaction_probe_rolls_back_without_touching_formal_db(self):
        self.service.test_database_probe()
        with self.test.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM synthetic_transaction_probe").fetchone()[0], 1)
        self.assertFalse(self.formal.path.exists())
        self.assertFalse(self.test.accounts())

    def test_stop_request_does_not_claim_drained_worker(self):
        self.service.begin_worker()
        self.service.stop(source="hotkey")
        report = self.runtime.report(self.service.detection_id)
        self.assertEqual(report["stages"]["STOP"]["final_status"], "PARTIAL")
        self.assertEqual(report["substeps"]["STOP_WORKER"]["status"], "NOT_RUN")
        self.service.end_worker()
        report = self.runtime.report(self.service.detection_id)
        self.assertEqual(report["stages"]["STOP"]["final_status"], "AUTO_PASS")
        self.assertEqual(report["substeps"]["STOP_INPUT"]["status"], "AUTO_PASS")

    def test_restore_accepts_business_changes_with_verified_layout_and_a1(self):
        ctx = ProfileWindows(self.service._lifetime(self.before.frame)[-1], frozenset())
        known = ProfileCheck(self.before, ctx)
        self.reader.return_value = self.after
        self.navigator.verify_view.return_value = (Mock(), None)
        restored = self.service._restored_message_view(known, self.after.frame, (), ())
        self.assertIs(restored, self.after)
        self.assertIsNone(self.service._restored_message_view(known, self.after.frame, (300,), ()))


class ReportTests(unittest.TestCase):
    def test_recovery_and_unresolved_are_distinct_and_keep_history(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = CollectionRuntime(Path(folder), revision="synthetic")
            try:
                for result in ("AUTO_PASS", "BLOCKED_BY_DEPENDENCY", "BLOCKED_BY_DEPENDENCY"):
                    runtime.emit("test", "r", "WINDOW", "WINDOW_UPDATE", result)
                stage = runtime.report("r")["stages"]["WINDOW"]
                self.assertEqual((stage["verification_status"], stage["final_status"], stage["unresolved_blockers"]),
                                 ("AUTO_PASS", "BLOCKED_BY_DEPENDENCY", 2))
                runtime.emit("test", "r", "WINDOW", "WINDOW_RECOVERED", "AUTO_PASS")
                stage = runtime.report("r")["stages"]["WINDOW"]
                self.assertEqual((stage["final_status"], stage["recovered_failures"], stage["unresolved_blockers"]),
                                 ("AUTO_PASS", 2, 0))
            finally:
                runtime.close()

    def test_all_three_filtered_with_distinct_codes_and_accounted_total(self):
        raw = view(unread={0: (1, True), 1: (2, True), 2: (1, False)})
        raw = replace(raw, contacts=tuple(replace(row, kind="FOLDED_CHAT_CONTAINER") if i == 2 else row
                                          for i, row in enumerate(raw.contacts)))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = CollectionRuntime(root / "logs", revision="synthetic")
            try:
                service = CollectionService(PROFILE, VisualStore(root / "db.sqlite3"), lambda _: raw,
                                            capture=lambda: raw.frame, navigator=Mock(), permit=Mock())
                service.configure_runtime(runtime, "test")
                service.scope = CollectionScope()
                service.start()
                state = service.tick()
                self.assertEqual((state.unread_marker_count, state.policy_skipped_unread,
                                  state.unsupported_unread, state.reliable_unread_count), (3, 2, 1, 0))
                service.tick()
                events = [item["details"] for item in runtime.events("all") if item["code"] == "CONTACT_SCOPE_OBSERVED"]
                self.assertEqual(len(events), 6)
                self.assertTrue(all(first["target_id"] == second["target_id"] for first, second in zip(events[:3], events[3:])))
                self.assertTrue(all(item["exclusion_reason"] and not item["navigation_eligible"] for item in events))
                service.stop()
            finally:
                runtime.close()

    def test_unknown_type_and_mute_are_not_policy_exclusions(self):
        raw = view(unread={0: (1, False), 1: (1, False)})
        rows = (replace(raw.contacts[0], kind="UNKNOWN_ENTRY"), replace(raw.contacts[1], is_muted=None), raw.contacts[2])
        targets = unread_targets(replace(raw, contacts=rows), CollectionScope())
        self.assertEqual([(item.scope_decision, item.uncertainty) for item in targets],
                         [("WAITING_REVALIDATION", "type"), ("WAITING_REVALIDATION", "muted")])

    def test_notifications_keep_test_and_formal_batches_distinct(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tray = Mock()
        factory = Mock(return_value=tray)
        notifier = CollectionNotifier(tray_factory=factory)
        notifier.enqueue(SaveNotice("isolated", "synthetic", 1, CollectionTrace(), test_run="test-run"))
        notifier.enqueue(SaveNotice("formal", "synthetic2", 2, CollectionTrace()))
        notifier.flush()
        self.assertEqual(tray.showMessage.call_count, 2)
        self.assertFalse(tray.showMessage.call_args_list[0].args[0].startswith("TEST"))
        self.assertTrue(tray.showMessage.call_args_list[1].args[0].startswith("TEST"))
        app.processEvents()
