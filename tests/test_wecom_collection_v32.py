from __future__ import annotations

import ctypes
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from ctypes import wintypes
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from autoim.wecom.collection_hotkey import CollectionHotkey, parse_hotkey
from autoim.wecom.collection_navigation import CollectionNavigation, CollectionPermit, NavigationPaused
from autoim.wecom.collection_panel import VisualCollectionPanel
from autoim.wecom.collection_profile import ProfileSurface, card_close, email_from_spans, menu_action, normalize_email
from autoim.wecom.collection_service import (CollectionScope, CollectionService, ScanStatus, _verify_transition,
                                             unread_targets)
from autoim.wecom.visual_account import LoginEvidence, _signature
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_store import VisualStore, _MIGRATIONS
from test_wecom_collection import PROFILE, view
from autoim.wecom.visual_ocr import TextSpan
from test_wecom_window_recovery import health


def span(text, left, top, right, bottom):
    return TextSpan(text, Box(left, top, right, bottom), .96)


def _unknown_selection(raw):
    return replace(raw, contacts=tuple(replace(row, is_selected=None) for row in raw.contacts))


class ScopeAndNavigationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = VisualStore(Path(directory.name) / "test.sqlite3")
        self.raw = view(unread={1: (1, False), 2: (1, False)})
        self.navigator = Mock()
        self.permit = Mock()
        self.service = CollectionService(PROFILE, self.store, lambda _: self.raw, capture=lambda: self.raw.frame,
                                         navigator=self.navigator, permit=self.permit)
        self.service.scope = CollectionScope()
        self.service.start()
        self.addCleanup(self.service.stop)

    def test_folded_and_muted_are_skipped_and_do_not_block_normal_row(self):
        first, second, third = view(unread={0: (1, False), 1: (1, True), 2: (1, False)}).contacts
        self.raw = replace(self.raw, contacts=(replace(first, kind="FOLDED_CHAT_CONTAINER"), second, third))
        self.navigator.select.side_effect = lambda *_: setattr(self, "raw", view(2))
        result = self.service.tick()
        self.assertEqual((result.status, result.unread_marker_count, result.skipped_unread, result.errors),
                         ("COMPLETED", 3, 2, 0))
        self.assertEqual(len(self.store.browse(PROFILE)["jobs"]), 1)

    def test_unknown_mute_and_special_entry_remain_distinct(self):
        first, second, third = view(unread={0: (2, False), 1: (1, False), 2: (1, True)}).contacts
        raw = replace(self.raw, contacts=(replace(first, kind="UNKNOWN_ENTRY"), replace(second, is_muted=None), third))
        results = unread_targets(raw, CollectionScope())
        self.assertEqual(results[0].scope_decision, "WAITING_REVALIDATION")
        self.assertIn("静音状态", results[1].blockers[0])
        self.assertTrue(results[2].skip_reason)
        self.assertFalse(unread_targets(raw, CollectionScope(muted=True))[2].skip_reason)

    def test_click_with_no_selected_highlight_and_save_after_independent_transition(self):
        self.raw = _unknown_selection(view(unread={1: (1, False)}))
        self.navigator.select.side_effect = lambda *_: setattr(self, "raw", _unknown_selection(view(1)))
        with patch("autoim.wecom.collection_service.avatar_key",
                   side_effect=lambda _, row: row.display_name):
            result = self.service.tick()
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(self.store.browse(PROFILE)["message_count"], 3)
        self.assertTrue(all(row.is_selected is None for row in self.raw.contacts))

    def test_title_without_badge_transition_cannot_verify_capture(self):
        before = _unknown_selection(view(unread={1: (1, False)}))
        after = _unknown_selection(view(1, unread={1: (1, False)}))
        with patch("autoim.wecom.collection_service.avatar_key",
                   side_effect=lambda _, row: row.display_name):
            target, = unread_targets(before)
            self.assertIs(_verify_transition(before, after, target), after)

    def test_local_block_yields_to_later_row_at_next_scan(self):
        def click(raw, row):
            if row == raw.contacts[1]:
                raise NavigationPaused("被某个窗口遮挡", local=True)
            self.raw = view(2, unread={1: (1, False)})
        self.navigator.select.side_effect = click
        first = self.service.tick()
        self.assertEqual(first.status, "PAUSED")
        self.assertFalse(first.waiting_for_window)
        second = self.service.tick()
        self.assertEqual(second.status, "COMPLETED")
        self.assertEqual(second.queued, 1)
        jobs = self.store.browse(PROFILE)["jobs"]
        waiting = next(item for item in jobs if item["status"] == "WAITING_REVALIDATION")
        self.assertEqual(waiting["read_risk"], 0)
        self.assertEqual(json.loads(waiting["transitions"])[-1]["detail"]["observations"], 2)

    def test_global_block_does_not_yield_to_later_row(self):
        self.navigator.verify_view.side_effect = NavigationPaused("等待用户空闲")
        for _ in range(3):
            self.assertEqual(self.service.tick().status, "PAUSED")
        self.navigator.select.assert_not_called()
        jobs = self.store.browse(PROFILE)["jobs"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(sum(len(json.loads(job["transitions"])) for job in jobs), 5)

    def test_success_resets_navigation_failure_budget(self):
        self.service._operations = 11
        self.navigator.select.side_effect = lambda *_: setattr(self, "raw", view(1))
        self.assertEqual(self.service.tick().status, "COMPLETED")
        self.assertEqual(self.service._operations, 0)


class NativeBoundaryTests(unittest.TestCase):
    def test_exact_point_ignores_unrelated_windows_and_reports_hit_on_block(self):
        info = health()
        raw = view()
        capture = replace(raw.frame.capture, hwnd=info.window.hwnd, pid=info.window.pid, rect=info.last.rect)
        state = replace(raw.frame.state, width=info.last.client_size[0], height=info.last.client_size[1],
                        dpi=info.last.dpi)
        raw = replace(raw, frame=replace(raw.frame, capture=capture, state=state))
        nav = CollectionNavigation(Mock(), desktop=lambda: True, idle=lambda: (8, 4000), window_probe=lambda: info)
        with (patch("win32gui.IsWindowEnabled", return_value=True),
              patch("win32gui.ClientToScreen", return_value=(0, 0)),
              patch("win32gui.WindowFromPoint", return_value=99),
              patch("win32gui.GetAncestor", return_value=info.window.hwnd) as ancestor,
              patch("win32gui.EnumWindows", side_effect=RuntimeError("callback detail")) as enum):
            check, _ = nav.verify_view(raw, need_idle=True)
            check((1, 1))
            enum.assert_not_called()
            ancestor.return_value = 99
            with self.assertRaisesRegex(NavigationPaused, "WindowFromPoint 0x63.*EnumWindows.*callback detail") as cm:
                check((1, 1))
            self.assertTrue(cm.exception.local)

    def test_idle_reports_held_key_and_wrap(self):
        from autoim.wecom.collection_navigation import _input_state

        with (patch("win32api.GetLastInputInfo", return_value=0xFFFFFF00),
              patch("win32api.GetTickCount", return_value=0x800),
              patch("win32api.GetAsyncKeyState", side_effect=lambda key: 0x8000 if key == 18 else 0)):
            self.assertEqual(_input_state().milliseconds, 2304)
            nav = CollectionNavigation(Mock())
            nav.mark_started()
            nav._own_input = 0xFFFFFF00
            with self.assertRaisesRegex(NavigationPaused, "空闲 2304.*按住 Alt.*AutoIM True.*启动等待 True"):
                nav._activity(True)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.sqlite3"
        self.store = VisualStore(self.path)
        self.raw = view()
        self.box = Box(10, 10, 82, 82)
        self.person = LoginEvidence("测试使用者", "fingerprint", self.box,
                                    _signature(self.box.crop(self.raw.frame.pixels)), avatar_box=self.box)
        self.life = (41, 82, (41, 82, 1.0, "wxwork.exe"))

    def test_migration_reuses_confirmed_legacy_id_and_keeps_messages_jobs(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            for version in (1, 2, 3):
                for statement in _MIGRATIONS[version]:
                    db.execute(statement)
            db.execute("PRAGMA user_version=3")
            db.execute("INSERT INTO accounts(id,profile,first_seen,login_name,login_key,login_avatar) "
                       "VALUES (19, 'legacy', 'today', ?, ?, ?)", (self.person.name, self.person.key, self.person.avatar))
        self.store.save(19, self.raw)
        job = self.store.create_job(19, "legacy", "row", self.raw.contacts[0])
        account, choices = self.store.bind_email(self.person, "UP@Example.Test", self.life)
        self.assertIsNone(account)
        self.assertEqual([row.id for row in choices], [19])
        self.assertEqual(self.store.bind_email(self.person, "UP@Example.Test", self.life,
                                               confirmed=True, account_id=19)[0], 19)
        data = VisualStore(self.path).browse(19)
        self.assertEqual(data["message_count"], 3)
        self.assertEqual(data["jobs"][0]["id"], job)

    def test_email_is_unique_and_rename_updates_only_same_id(self):
        a, _ = self.store.bind_email(self.person, "a@example.test", self.life)
        b, _ = self.store.bind_email(self.person, "b@example.test", self.life)
        renamed = replace(self.person, name="新名字")
        self.assertNotEqual(a, b)
        self.assertEqual(self.store.bind_email(renamed, "A@EXAMPLE.TEST", self.life)[0], a)
        self.assertEqual(self.store.accounts()[0].label, "新名字")
        with self.assertRaises(ValueError):
            self.store.bind_email(renamed, "a@example.test", self.life, confirmed=True, account_id=b)

    def test_quick_check_requires_matching_process_hwnd_name_and_avatar(self):
        account, _ = self.store.bind_email(self.person, "a@example.test", self.life)
        self.assertEqual(VisualStore(self.path).verified_login(self.person, self.life), (account, "a@example.test"))
        for evidence, lifetime in (
            (replace(self.person, name="改名"), self.life),
            (replace(self.person, avatar="new"), self.life),
            (self.person, (41, 83, (41, 83, 1.0, "wxwork.exe"))),
            (self.person, (41, 82, (41, 82, 2.0, "wxwork.exe"))),
        ):
            self.assertIsNone(self.store.verified_login(evidence, lifetime))

    def profile_fixture(self):
        image = np.full((500, 520, 3), 245, np.uint8)
        image[40:103, 42:104] = np.random.default_rng(25).integers(0, 255, size=(63, 62, 3), dtype=np.uint8)
        menu = ProfileSurface(100, image, Box(0, 0, 520, 500), ((0, 0), 520, 500))
        menu_text = (span("测试使用者", 114, 58, 225, 84), span("收藏", 90, 155, 153, 177),
                     span("消息管理器", 90, 204, 207, 227), span("设置", 90, 250, 130, 273))
        card_image = np.full_like(image, 245)
        for ends in (((479, 20), (497, 38)), ((497, 20), (479, 38))):
            cv2.line(card_image, *ends, (20, 20, 20), 2)
        card = replace(menu, hwnd=101, pixels=card_image)
        card_text = (span("邮箱", 106, 238, 154, 261), span("A@example.test", 180, 239, 387, 262),
                     span("部门", 106, 278, 151, 302), span("不保留", 178, 280, 310, 304),
                     span("测试使用者", 114, 66, 225, 92))
        return menu, menu_text, card, card_text

    def test_menu_card_and_same_line_email_at_175_percent(self):
        menu, menu_text, card, card_text = self.profile_fixture()
        self.assertIsNotNone(menu_action(menu, menu_text, self.person.name, 168))
        self.assertIsNone(menu_action(menu, menu_text[:2], self.person.name, 168))
        self.assertEqual(email_from_spans(card, card_text), "a@example.test")
        self.assertIsNotNone(card_close(card, 168))
        self.assertEqual(email_from_spans(card, (card_text[0], card_text[3])), "")
        self.assertEqual(normalize_email("a..b@example.test"), "")

    def drive_profile(self, *, change_value=False, stop_at_menu=False):
        menu, menu_text, card, card_text = self.profile_fixture()
        reader = Mock()
        frame = self.raw.frame
        reference = replace(self.raw, login=self.person)
        reader.identify.return_value = reference
        other = replace(card_text[1], text="b@example.test")
        frames = [(frame, (menu,), (100,)), (frame, (card,), (100, 101)),
                  (replace(frame, captured_at=frame.captured_at + timedelta(seconds=1)), (card,), (100, 101)),
                  (frame, (menu,), (100,)), (frame, (), ())]
        nav = Mock()
        nav.profile_start.return_value = ()
        nav.profile_surfaces.side_effect = frames
        reader.profile_text.side_effect = [menu_text, card_text,
                                           (card_text[0], other, *card_text[2:]) if change_value else card_text, menu_text]
        service = CollectionService(None, self.store, reader, permit=Mock(), capture=lambda: frame, navigator=nav,
                                    require_email=True)
        service._lifetime = lambda _: self.life
        service.start()
        self.addCleanup(service.stop)
        first = service._profile_tick(frame)
        self.assertEqual(first.account_state, "OPENING_PROFILE_MENU")
        if stop_at_menu:
            service.stop()
            service._profile_tick(frame)
            nav.profile_surfaces.assert_not_called()
            self.assertEqual(nav.profile_click.call_count, 1)
            return None
        for _ in frames:
            state = service._profile_tick(frame)
        self.assertEqual([call.args[1].stage for call in nav.profile_click.call_args_list],
                         ["A1_OPEN", "MENU_AVATAR", "CARD_CLOSE", "A1_CLOSE"])
        return state

    def test_profile_flow_binds_after_closing_both_layers(self):
        state = self.drive_profile()
        self.assertEqual((state.account_state, state.account_email), ("IDENTITY_VERIFIED", "a@example.test"))

    def test_mismatching_email_closes_profile_without_binding(self):
        state = self.drive_profile(change_value=True)
        self.assertEqual(state.account_state, "IDENTITY_VERIFICATION_FAILED")
        self.assertFalse(self.store.accounts())

    def test_stop_while_profile_is_open_prevents_more_mouse_actions(self):
        self.drive_profile(stop_at_menu=True)


class HotkeyAndGuiTests(unittest.TestCase):
    def test_hotkey_registration_conflict_debounce_and_unregistration(self):
        app, api, toggle = Mock(), Mock(), Mock()
        api.RegisterHotKey.side_effect = [True, False, True]
        hotkey = CollectionHotkey(app, 77, toggle, native=api)
        hotkey.set_shortcut("F10")
        self.assertEqual(api.RegisterHotKey.call_args.args[-2:], (0x4000, 0x79))
        with self.assertRaisesRegex(OSError, "注册失败"):
            hotkey.set_shortcut("F9")
        message = wintypes.MSG(hWnd=77, message=0x0312, wParam=hotkey.active_id)
        for now in (1., 1.01, 2.):
            with patch("autoim.wecom.collection_hotkey.monotonic", return_value=now):
                self.assertEqual(hotkey.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)), (True, 0))
        self.assertEqual(toggle.call_count, 2)
        hotkey.set_shortcut("Ctrl+Alt+M")
        self.assertEqual(parse_hotkey("Ctrl+Alt+M"), (0x4003, ord("M")))
        first, final = message.wParam, hotkey.active_id
        hotkey.close()
        self.assertEqual([call.args for call in api.UnregisterHotKey.call_args_list], [(77, first), (77, final)])
        app.removeNativeEventFilter.assert_called_once_with(hotkey)

    def test_scope_and_collapsible_sections_are_restored(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "test.ini"), QSettings.Format.IniFormat)
            panel = VisualCollectionPanel(Mock(), Mock(), settings=settings)
            self.assertEqual([box.isChecked() for box in panel.scope_checks.values()], [True, True, False, False])
            self.assertFalse(panel.scope_checks["folded"].isEnabled())
            self.assertFalse(panel.diagnostics_toggle.isChecked())
            self.assertFalse(panel.advanced_toggle.isChecked())
            panel.scope_checks["muted"].setChecked(True)
            settings.sync()
            restarted = VisualCollectionPanel(Mock(), Mock(), settings=settings)
            self.assertTrue(restarted.scope_checks["muted"].isChecked())
            restarted.close()
            panel.close()
            app.processEvents()

    def test_hotkey_stops_worker_and_cannot_restart_until_drained(self):
        app = QApplication.instance() or QApplication([])
        service = Mock()
        service.active = False
        service.snapshot.return_value = ScanStatus("RUNNING")
        service.start.side_effect = lambda **_: setattr(service, "active", True)
        service.stop.side_effect = lambda: setattr(service, "active", False)
        panel = VisualCollectionPanel(Mock(), Mock(return_value=service))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as prompt:
            panel.toggle()
            service.start.assert_not_called()
            panel.start()
            panel._in_flight = True
            panel.toggle()
            service.stop.assert_called_once()
            panel.toggle()
            service.start.assert_called_once()
            panel._finished((ScanStatus("RUNNING"), []))
            self.assertFalse(panel._timer.isActive())
            panel.toggle()
            self.assertEqual(service.start.call_count, 2)
            prompt.assert_called_once()
        panel.stop()
        panel.close()
        app.processEvents()
