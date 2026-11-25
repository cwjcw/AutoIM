from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import cv2
import numpy as np

from autoim.wecom.collection_core import analyze_read
from autoim.wecom.collection_incremental import SOURCE, CoreAccount, save_visible
from autoim.wecom.collection_service import CollectionScope, _core_geometry_continuity, _core_selection_evidence, _Target, unread_targets
from autoim.wecom.visual_b2 import BELL, activity_text, divider_rows, mute_evidence, structural_span, typical_pitch, unread_badges
from autoim.wecom.visual_contacts import detect_contacts
from autoim.wecom.visual_layout import Box, VisualThresholds
from autoim.wecom.visual_observation import compare_conversation
from autoim.wecom.visual_ocr import TextSpan
from autoim.wecom.visual_store import VisualStore, name_key
from autoim.wecom.visual_time import parse_message_time
from test_wecom_collection_v35 import sample, with_avatar_texture


NOW = datetime(2026, 11, 24, 17, tzinfo=timezone(timedelta(hours=8)))
ACCOUNT = CoreAccount("合成默认账号", "synthetic@example.test")


class VisualEvidenceTests(TestCase):
    def test_activity_reads_full_width_clock_and_rejects_preview(self):
        self.assertEqual(activity_text(" １４ ： ０２ "), "14:02")
        self.assertEqual(activity_text("５ 分钟前"), "5分钟前")
        self.assertEqual(activity_text("截图 14:02"), "")

    def test_thin_gray_dividers_and_blue_band_seed_distinct_rows(self):
        pixels = np.full((310, 380, 3), 232, dtype=np.uint8)
        for y in (105, 205):
            pixels[y:y + 2] = 222
        bounds = Box(0, 0, 380, 310)
        highlight = Box(0, 205, 380, 305)
        lines = divider_rows(pixels, bounds, 1.75)
        self.assertEqual(lines, (105, 205))
        pitch = typical_pitch((55, 155, 255), lines, highlight, 1.75)
        self.assertEqual(structural_span(155, bounds, pitch, lines, highlight), (105, 205, "GRAY_DIVIDERS"))
        self.assertEqual(structural_span(254, bounds, pitch, lines, highlight), (205, 305, "BLUE_BAND"))

    def test_normal_and_small_red_badges_detected_independently(self):
        pixels = np.full((300, 430, 3), 232, dtype=np.uint8)
        for x, y, radius in ((80, 20, 16), (80, 130, 10), (300, 200, 16)):
            cv2.circle(pixels, (x, y), radius, (78, 90, 252), -1)
            pixels[y - 5:y + 6, x - 1:x + 2] = 255
        badges = unread_badges(pixels, Box(0, 0, 430, 300), 1.75, 50)
        self.assertEqual([badge.kind for badge in badges], ["NORMAL_NUMBER", "SMALL_RED"])

    def test_avatar_content_is_rejected_by_aligned_badge_column(self):
        pixels = np.full((260, 430, 3), 232, dtype=np.uint8)
        for x in (63, 96):
            cv2.circle(pixels, (x, 27), 15, (78, 90, 252), -1)
            pixels[23:31, x - 2:x + 2] = 255
        bounds = Box(0, 0, 430, 260)
        badges = unread_badges(pixels, bounds, 1.75, 50, (Box(28, 29, 96, 97), Box(28, 130, 96, 198)))
        self.assertEqual([(badge.box.center[0], badge.kind) for badge in badges], [(96, "NORMAL_NUMBER")])

    def test_divider_and_badge_can_seed_missing_name_avatar_and_activity(self):
        bounds = Box(0, 0, 430, 310)
        pixels = np.full((bounds.height, bounds.width, 3), 232, dtype=np.uint8)
        pixels[105:107] = pixels[205:207] = 222
        cv2.circle(pixels, (98, 126), 16, (78, 90, 252), -1)
        pixels[120:133, 96:100] = 255

        class Reader:
            def read(self, pixels, box, *, single_line=False):
                if single_line:
                    return (TextSpan("2", box, .99),)
                return (TextSpan("合成预览", Box(139, 166, 220, 183), .98),) if box == bounds else ()

        with patch("autoim.wecom.visual_contacts._avatars", return_value=()):
            rows = detect_contacts(pixels, bounds, Reader(), 1.75, VisualThresholds(), multi_feature=True)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.boundary_source, row.badge_kind, row.unread_count, row.name_reliable),
                         ("GRAY_DIVIDERS", "NORMAL_NUMBER", 2, False))
        self.assertTrue(row.geometry_reliable)
        self.assertEqual(unread_targets(replace(sample(), contacts=rows), CollectionScope())[0].scope_decision, "ELIGIBLE")

    def test_bell_uses_local_background_and_refuses_normal_badge_conflict(self):
        for unit, color in ((1.75, (245, 138, 57)), (1., (229, 229, 229))):
            with self.subTest(unit=unit):
                pixels = np.full((110, 430, 3), color, dtype=np.uint8)
                row = Box(0, 0, 430, 110)
                size = round(22 * unit / 1.75)
                icon = cv2.resize(BELL, (size, size), interpolation=cv2.INTER_NEAREST).astype(bool)
                pixels[12:12 + size, 330:330 + size][icon] = 40
                self.assertEqual(mute_evidence(pixels, row, unit, "SMALL_RED")[:2], (True, "BELL_TEMPLATE"))
                self.assertEqual(mute_evidence(pixels, row, unit, "NORMAL_NUMBER")[:2], (None, "BELL_NUMBER_CONFLICT"))

    def test_unavailable_avatar_has_independent_confirmation(self):
        before = sample(unread={1: (1, False)})
        after = sample(selected=1, tone=190)
        row = before.contacts[1]
        target = _Target(row, name_key(row.display_name), "", ())
        self.assertEqual(_core_selection_evidence(before, after, target, True)[0],
                         "CLICK_TITLE_ROW_BADGE_REFRESH_AVATAR_UNAVAILABLE")
        self.assertEqual(_core_selection_evidence(before, after, target, False)[0], "TRANSITION_EVIDENCE_MISSING")

    def test_conflicting_avatar_remains_blocked(self):
        before = with_avatar_texture(sample(unread={1: (1, False)}), 1)
        after = with_avatar_texture(sample(selected=1, tone=190), 1, different=True)
        row = before.contacts[1]
        target = _Target(row, name_key(row.display_name), "", ())
        self.assertEqual(_core_selection_evidence(before, after, target, True)[0], "AVATAR_CONFLICT")

    def test_unread_row_without_name_requires_selected_geometry_and_neighbor(self):
        before = sample(unread={1: (1, False)})
        row = replace(before.contacts[1], raw_name="", display_name="", name_reliable=False, geometry_reliable=True)
        before = replace(before, contacts=(before.contacts[0], row, *before.contacts[2:]))
        after = sample(selected=1, tone=190)
        selected = replace(after.contacts[1], geometry_reliable=True)
        after = replace(after, contacts=(after.contacts[0], selected, *after.contacts[2:]))
        target = _Target(row, "", "", ())
        self.assertEqual(_core_geometry_continuity(before, after, target, True)[0], "CLICK_GEOMETRY_TITLE_BADGE_REFRESH")
        self.assertEqual(_core_geometry_continuity(before, replace(after, contacts=(selected,)), target, True)[0],
                         "GEOMETRY_NEIGHBORS_CHANGED")

    def test_message_group_time_anchors_bare_clock_to_capture_date(self):
        self.assertEqual(parse_message_time("昨天　08：15", NOW).anchor, "2026-11-23T08:15+08:00")
        self.assertEqual(parse_message_time("08:15", NOW).anchor, "2026-11-24T08:15+08:00")
        self.assertEqual(parse_message_time("昨天　08：15", NOW).time_source, "GROUP_ANCHOR")
        self.assertIsNone(parse_message_time("15分钟前", NOW).anchor)
        self.assertIsNone(parse_message_time("今天 21:00", NOW).anchor)

    def test_short_external_label_requires_consistent_explicit_identity_evidence(self):
        original = sample(selected=1)
        selected = original.contacts[1]
        title = (TextSpan(selected.display_name, Box(0, 0, 70, 25), .99),
                 TextSpan("@示例商贸", Box(73, 3, 130, 23), .99))
        matching = (replace(selected, display_name=selected.display_name + "@示例商贸",
                            peer_type="EXTERNAL_WECOM", identity_labels=("示例商贸",)),)
        conflict = (replace(selected, display_name=selected.display_name + "@另一机构",
                            peer_type="EXTERNAL_WECOM", identity_labels=("另一机构",)),)
        self.assertTrue(compare_conversation(title, matching, VisualThresholds()).title_name_reliable)
        self.assertEqual(compare_conversation(title, matching, VisualThresholds(), multi_feature=True).match_status,
                         "VISUAL_MATCH")
        self.assertEqual(compare_conversation(title, conflict, VisualThresholds(), multi_feature=True).match_status,
                         "CONVERSATION_MISMATCH")


class IsolatedHistoryTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "isolated.sqlite3"
        self.store = VisualStore(self.path)
        self.template = analyze_read(sample(selected=1), "synthetic-task", "合成会话", 1, "", verified=True,
                                     badge_cleared=True, refreshed=True)

    def save(self, entries, *, time="今天 16:54", gaps=(), check=lambda: None, contact="合成会话"):
        messages = tuple(replace(self.template.messages[0], sequence=i, text=body, direction=direction, time=time,
                                 quality=quality, contact=contact)
                         for i, (body, direction, quality) in enumerate(entries, 1))
        return save_visible(self.store, ACCOUNT, replace(self.template, messages=messages, gaps=gaps),
                            "CONTACT", NOW, "synthetic-run", check)

    def rows(self):
        with self.store.transaction() as db:
            return [tuple(row) for row in db.execute(
                "SELECT m.body,m.direction,m.actual_time,t.time_source,t.group_sequence FROM messages m "
                "JOIN core_message_times t ON m.id=t.message_id WHERE source=? ORDER BY position", (SOURCE,))]

    def test_first_save_preserves_both_directions_group_order_and_unverified_account(self):
        result = self.save([("问候", "INBOUND", "RELIABLE"), ("确认", "OUTBOUND", "RELIABLE")])
        self.assertEqual((result.first, result.new), (2, 0))
        self.assertEqual(self.rows(), [("问候", "INBOUND", None, "GROUP_ANCHOR", 1),
                                       ("确认", "OUTBOUND", None, "GROUP_ANCHOR", 2)])
        with self.store.transaction() as db:
            self.assertEqual(tuple(db.execute("SELECT profile,identity_status FROM accounts").fetchone()),
                             (ACCOUNT.profile, "LOCAL_DEFAULT_UNVERIFIED"))

    def test_same_minute_identical_text_is_saved_a_second_time_after_overlap(self):
        first = ("开场", "INBOUND", "RELIABLE")
        repeated = ("重复", "INBOUND", "RELIABLE")
        self.save([first, repeated])
        self.store = VisualStore(self.path)
        result = self.save([first, repeated, repeated])
        self.assertEqual((result.existing, result.new, result.overlap), (2, 1, 2))
        self.assertEqual([r[0] for r in self.rows()], ["开场", "重复", "重复"])
        result = self.save([first, repeated, repeated])
        self.assertEqual((result.existing, result.new), (3, 0))

    def test_same_base_names_use_three_histories_and_never_deduplicate_across_suffixes(self):
        entries = [("共同文字", "INBOUND", "RELIABLE"), ("第二条", "OUTBOUND", "RELIABLE")]
        for title in ("合成会话", "合成会话@微信", "合成会话@演示机构"):
            self.assertEqual(self.save(entries, contact=title).first, 2)
        result = self.save([*entries, ("新增", "INBOUND", "RELIABLE")], contact="合成会话@微信")
        self.assertEqual((result.new, result.existing), (1, 2))
        with self.store.transaction() as db:
            self.assertEqual(dict(db.execute("SELECT c.display_name, COUNT(m.id) FROM conversations c JOIN messages m "
                                             "ON c.id=m.conversation_id GROUP BY c.id").fetchall()),
                             {"合成会话": 2, "合成会话@微信": 3, "合成会话@演示机构": 2})

    def test_legacy_bare_name_history_is_retained_and_never_linked_without_evidence(self):
        entries = [("原序列", "INBOUND", "RELIABLE")]
        self.save(entries)
        with self.store.transaction() as db:
            previous_id = db.execute("SELECT id FROM conversations").fetchone()[0]
            db.execute("UPDATE conversations SET evidence='VISUAL_MULTI_EVIDENCE' WHERE id=?", (previous_id,))
        result = self.save(entries)
        self.assertEqual((result.first, result.existing), (1, 0))
        retained = self.store.browse(ACCOUNT.profile)["conversations"]
        self.assertEqual(len(retained), 2)
        self.assertEqual(next(row for row in retained if row["id"] == previous_id)["last_status"], "旧版基础姓名 · 待复核")
        self.assertEqual(self.store.browse(ACCOUNT.profile, previous_id)["messages"][0]["body"], "原序列")

    def test_repeated_weak_single_message_cannot_prove_continuity(self):
        self.save([("相同", "INBOUND", "RELIABLE")], time="")
        result = self.save([("相同", "INBOUND", "RELIABLE"), ("下一条", "OUTBOUND", "RELIABLE")], time="")
        self.assertEqual((result.new, result.uncertain, result.gap), (0, 2, True))

    def test_two_ordered_messages_can_prove_continuity_without_clock(self):
        first = [("一", "INBOUND", "RELIABLE"), ("二", "OUTBOUND", "RELIABLE")]
        self.save(first, time="")
        result = self.save([*first, ("三", "INBOUND", "RELIABLE")], time="")
        self.assertEqual((result.existing, result.new), (2, 1))

    def test_unresolved_gap_blocks_later_time_only_append(self):
        self.save([("一", "INBOUND", "RELIABLE")])
        result = self.save([("一", "INBOUND", "RELIABLE"), ("缺口", "INBOUND", "CLIPPED")])
        self.assertTrue(result.gap)
        result = self.save([("后来", "INBOUND", "RELIABLE")], time="今天 16:56")
        self.assertEqual((result.new, result.uncertain), (0, 1))

    def test_unambiguous_newer_time_after_checkpoint_can_append(self):
        self.save([("过去", "INBOUND", "RELIABLE")])
        result = self.save([("后来", "OUTBOUND", "RELIABLE")], time="今天 16:56")
        self.assertEqual((result.new, result.anchor_status), (1, "AFTER_TIME_ANCHOR"))

    def test_stop_before_commit_rolls_back_all_of_the_batch(self):
        self.save([("过去", "INBOUND", "RELIABLE")])
        checks = 0

        def stop():
            nonlocal checks
            checks += 1
            if checks == 4:
                raise RuntimeError("stopped")

        with self.assertRaisesRegex(RuntimeError, "stopped"):
            self.save([("后来", "INBOUND", "RELIABLE"), ("又一条", "OUTBOUND", "RELIABLE")],
                      time="今天 16:56", check=stop)
        self.assertEqual(len(self.rows()), 1)

    def test_configuration_loads_without_email_verification(self):
        path = self.path.parent / "core-default-account.json"
        self.assertIsNone(CoreAccount.load(path))
        path.write_text('{"name":"Synthetic","email":"USERNAME@EXAMPLE.TEST"}', encoding="utf-8")
        self.assertEqual(CoreAccount.load(path), CoreAccount("Synthetic", "username@example.test"))

    def test_independent_ocr_is_excluded_from_message_transactions(self):
        independent = replace(self.template.messages[0], unread="INDEPENDENT_OCR")
        with self.assertRaisesRegex(ValueError, "会话归属"):
            save_visible(self.store, ACCOUNT, replace(self.template, messages=(independent,)),
                         "CONTACT", NOW, "synthetic-run", lambda: None)
        self.assertFalse(self.path.exists())
