from dataclasses import replace
from datetime import timedelta
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

import numpy as np

from autoim.wecom.collection_incremental import compare_visible
from autoim.wecom.collection_navigation import CollectionNavigation, NavigationPaused
from autoim.wecom.collection_test import FULL_COLLECTION_SECONDS, TestClock, TestDeadline
from autoim.wecom.visual_contacts import GlobalUnreadObservation, _inside_avatar_art
from autoim.wecom.visual_b2 import B2Badge
from autoim.wecom.visual_layout import Box, SearchToolbar, VisualThresholds, detect_layout
from autoim.wecom.visual_messages import Container, analyze_messages
from autoim.wecom.visual_ocr import TextSpan
from autoim.wecom.visual_time import parse_message_time
from autoim.wecom.window_manager import WeComWindowManager
from test_wecom_collection_v35 import sample
from test_wecom_collection_v3535 import STAMP, geometry
from test_wecom_window_recovery import health


class WindowPreparation354Tests(TestCase):
    def setUp(self):
        self.gui = Mock(error=OSError)
        self.manager = WeComWindowManager()
        self.manager._windows_api = lambda: (self.gui, Mock())
        self.front = health()
        self.normal = replace(self.front, last=replace(self.front.last, zoomed=False,
                              placement=(0, 1, (0,0), (0,0), (0,0,1200,800))))

    def test_native_delayed_then_maximized(self):
        reports = []
        with patch("ctypes.windll.user32.ShowWindowAsync") as native:
            seen = self.manager.prepare_collection_window(
                Mock(side_effect=(self.normal, self.normal, self.normal, self.front)),
                lambda operation, **_: operation(), lambda: None, lambda: None,
                async_visibility=True, allow_restored=True, report=lambda *a, **kw: reports.append((a, kw)))
        self.assertEqual(seen, self.front)
        native.assert_called_once_with(self.front.window.hwnd, 3)
        self.assertEqual(sum(a[0] == "WINDOW_MAXIMIZE_OBSERVED" for a, _ in reports), 2)
        self.gui.PostMessage.assert_not_called()

    def test_asynchronous_no_effect_falls_back_to_system_command(self):
        with patch("ctypes.windll.user32.ShowWindowAsync"):
            seen = self.manager.prepare_collection_window(
                Mock(side_effect=(*([self.normal] * 9), self.front)),
                lambda operation, **_: operation(), lambda: None, lambda: None,
                async_visibility=True, allow_restored=True)
        self.assertEqual(seen, self.front)
        self.gui.PostMessage.assert_called_once_with(self.front.window.hwnd, 0x0112, 0xF030, 0)

    def test_unavailable_desktop_refuses_to_prepare(self):
        permit = Mock()
        navigation = CollectionNavigation(permit, manager=self.manager, desktop=lambda: False)
        with self.assertRaisesRegex(NavigationPaused, "输入桌面"):
            navigation.prepare_window(Event(), deadline_check=lambda: None)
        self.gui.ShowWindow.assert_not_called()

    def test_window_usable_after_both_maximization_methods_fail(self):
        desktop = replace(geometry((1200,800)), remote=True)
        restored = replace(self.normal, last=replace(self.normal.last, desktop=desktop))
        reports = []
        with patch("ctypes.windll.user32.ShowWindowAsync"):
            result = self.manager.prepare_collection_window(lambda: restored,
                        lambda operation, **_: operation(), lambda: None, lambda: None,
                        async_visibility=True, allow_restored=True,
                        report=lambda code, status, **_: reports.append((code, status)))
        self.assertIs(result, restored)
        self.assertIn(("WINDOW_MAXIMIZE_FALLBACK_RESTORED", "PARTIAL"), reports)
        self.assertIn(("WINDOW_MAXIMIZE", "FAILED"), reports)


class A2AddressTests(TestCase):
    def test_a2_hit_area_excludes_the_badge(self):
        raw = sample()
        unread = GlobalUnreadObservation(Box(20, 70, 95, 91), Box(70, 70, 99, 91), True)
        raw = replace(raw, global_unread=unread, frame=replace(raw.frame, coordinates=Mock(visual_unit=1.)))
        navigation = CollectionNavigation(Mock())
        with patch.object(navigation, "_act") as action:
            navigation.locate_unread(raw, fresh_capture=Mock(), deadline_check=Mock())
        self.assertEqual(action.call_args.args[2], Box(20, 70, 70, 91))
        self.assertTrue(action.call_args.kwargs["a2"])

    def test_a2_does_not_click_for_unknown_presence(self):
        raw = sample()
        navigation = CollectionNavigation(Mock())
        with patch.object(navigation, "_act") as action, self.assertRaises(NavigationPaused):
            navigation.locate_unread(raw, fresh_capture=Mock(), deadline_check=Mock())
        action.assert_not_called()

    def test_full_run_budget_and_original_target_limit(self):
        now = Mock(return_value=0.)
        clock = TestClock(Mock(), now, seconds=FULL_COLLECTION_SECONDS)
        now.return_value = 601.
        with self.assertRaisesRegex(TestDeadline, "600 秒"):
            clock.check()
        now.return_value = 0.
        clock.begin_target()
        now.return_value = 61.
        with self.assertRaisesRegex(TestDeadline, "60 秒"):
            clock.check()


class BadgeAppearanceTests(TestCase):
    def test_lower_right_art_is_excluded_with_two_neighbor_silhouettes(self):
        avatars = (Box(25, 10, 65, 50), Box(31, 75, 58, 102), Box(25, 125, 65, 165))
        art = B2Badge(Box(50, 88, 65, 102), "UNKNOWN_RED")
        true = B2Badge(Box(51, 69, 66, 84), "NORMAL_NUMBER")
        self.assertTrue(_inside_avatar_art(art, avatars, 1.))
        self.assertFalse(_inside_avatar_art(true, avatars, 1.))
        self.assertFalse(_inside_avatar_art(art, (avatars[1],), 1.))


class StructuredObjectTests(TestCase):
    def test_header_banner_uses_independent_b1_anchor_and_excludes_notice_from_c2(self):
        pixels = np.full((768, 1366, 3), 244, dtype=np.uint8)
        pixels[:, :160] = 207
        pixels[:, 420:] = (250, 247, 245)
        pixels[64:125, 420:] = (253, 235, 220)
        pixels[630:, 420:] = 219
        # A separator makes the neighboring panes distinguishable through
        # the entire height, including when their page backgrounds match.
        pixels[:, 417:420] = 216
        toolbar = SearchToolbar(Box(160, 0, 420, 150), Box(178, 16, 355, 52), bottom=64)
        with patch("autoim.wecom.visual_layout._search_toolbar", return_value=toolbar):
            result = detect_layout(pixels, {}, VisualThresholds(), 1.)
        self.assertLessEqual(abs(result.region("C1").bottom - 64), 4)
        self.assertGreaterEqual(result.region("C2").top, 124)
        self.assertIn("独立排除", next(item.evidence for item in result.regions if item.name == "C2"))

    def test_forwarded_record_owns_its_internal_text_and_preserves_following_message(self):
        region = Box(0, 0, 600, 340)
        pixels = np.full((340, 600, 3), 236, dtype=np.uint8)
        pixels[36:174, 337:575] = 250
        pixels[222:261, 20:123] = 218
        card = Container(Box(337, 36, 575, 174), False, quiet_outline=True, needs_text=True)
        text = Container(Box(20, 222, 123, 261), False)
        spans = (TextSpan("聊天记录", Box(352, 52, 427, 69), .99),
                 TextSpan("合成摘要甲", Box(352, 88, 453, 105), .99),
                 TextSpan("合成摘要乙", Box(352, 119, 453, 136), .99),
                 TextSpan("合成正文", Box(31, 230, 99, 251), .99))
        parsed = analyze_messages(pixels, region, (card, text), spans, 1., {}, VisualThresholds())
        self.assertEqual([(item.kind, item.direction) for item in parsed.messages],
                         [("CHAT_HISTORY_CARD", "OUTBOUND"), ("TEXT", "INBOUND")])
        self.assertEqual([entry.kind for entry in parsed.dispositions], ["MEDIA", "MEDIA", "MEDIA", "MESSAGE"])

    def test_closure_without_associated_structure_is_not_a_message(self):
        region = Box(0, 0, 600, 340)
        pixels = np.full((340, 600, 3), 241, dtype=np.uint8)
        card = Container(Box(337, 36, 575, 174), False, quiet_outline=True, needs_text=True)
        label = TextSpan("聊天记录", Box(354, 68, 427, 81), .99)
        parsed = analyze_messages(pixels, region, (card,), (label,), 1., {}, VisualThresholds())
        self.assertEqual(parsed.messages, ())
        self.assertTrue(parsed.dispositions[0].possible_message)


class ReliableSuffixTests(TestCase):
    def setUp(self):
        self.history = [{"first_seen": (STAMP - timedelta(hours=3)).isoformat(),
                         "body": "合成历史", "anchor": None, "direction": "INBOUND", "kind": "TEXT",
                         "time_text": "", "sender_name": ""}]
        self.message = Mock(kind="TEXT", quality="RELIABLE", direction="INBOUND", sender="", text="合成新增",
                            unread="UNREAD_CANDIDATE", hover=None, clock=parse_message_time(
                                "19:30", STAMP, source="GROUP_ANCHOR"))

    def test_historical_gap_allows_independent_newer_visible_suffix(self):
        decisions = compare_visible(self.history, (self.message,), gap=True, clear_tail=0)
        self.assertEqual(decisions, (["NEW"], 0, "AFTER_HISTORY_OBSERVATION"))

    def test_overlapping_observation_times_and_missing_coverage_block_current_suffix(self):
        self.assertEqual(compare_visible(self.history, (self.message,), gap=True)[0], ["REVIEW"])
        self.history[0]["first_seen"] = STAMP.isoformat()
        self.assertEqual(compare_visible(self.history, (self.message,), gap=True, clear_tail=0)[0], ["REVIEW"])
