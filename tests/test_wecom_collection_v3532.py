"""C2 regression fixtures with no production screenshots or names."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
import json
import zipfile

import cv2
import numpy as np

from autoim.wecom.collection_core import analyze_read
from autoim.wecom.collection_evidence import PRIVATE_RASTERS, c2_evidence_regions
from autoim.wecom.collection_incremental import CoreAccount, DefaultAccountConflict, attach_times, save_visible
from autoim.wecom.collection_runtime import CollectionRuntime
from autoim.wecom.visual_layout import Box, VisualThresholds
from autoim.wecom.visual_messages import analyze_messages, find_containers
from autoim.wecom.visual_ocr import TextSpan
from autoim.wecom.visual_store import VisualStore
from autoim.wecom.visual_time import parse_message_time
from test_wecom_collection_v35 import sample


CAPTURE = datetime.fromisoformat("2026-11-25T14:08:43+08:00")
ACCOUNT = CoreAccount("合成账号", "example@example.test")
REGION = Box(80, 80, 880, 600)
THRESHOLDS = VisualThresholds()


def glyph(text, box, angle=None):
    return TextSpan(text, Box(*box), .99, angle)


def bubble(image, box, color):
    x, y, right, bottom = box
    radius = 8
    cv2.rectangle(image, (x + radius, y), (right - radius, bottom), color, -1)
    cv2.rectangle(image, (x, y + radius), (right, bottom - radius), color, -1)
    for px, py in ((x+radius, y+radius), (right-radius, y+radius),
                   (x+radius, bottom-radius), (right-radius, bottom-radius)):
        cv2.circle(image, (px, py), radius, color, -1)


class SurfaceTests(TestCase):
    def image(self):
        return np.full((640, 920, 3), 250, np.uint8)

    def detect(self, frame, spans, existing=None):
        if existing is None:
            existing = find_containers(frame, REGION, 1.75, THRESHOLDS)
        return analyze_messages(frame, REGION, existing, spans, 1.75, {}, THRESHOLDS, capture_masks=True)

    def test_thin_diagonal_foreground_touching_panel_and_bubble_is_recovered(self):
        frame = self.image()
        bubble(frame, (117, 230, 266, 296), (230, 230, 230))
        cv2.line(frame, (80, 140), (127, 241), (228, 228, 228), 2)
        cv2.line(frame, (80, 140), (80, 400), (228, 228, 228), 2)
        cv2.line(frame, (80, 140), (870, 140), (228, 228, 228), 2)
        span = glyph("短消息", (136, 248, 232, 278))
        result = self.detect(frame, (span,), existing=())
        self.assertEqual(len(result.messages), 1)
        recovered = result.messages[0]
        self.assertEqual((recovered.text, recovered.direction, recovered.method),
                         ("短消息", "INBOUND", "MERGED_EVIDENCE"))
        self.assertEqual(result.masks[0].shape, (REGION.height, REGION.width))
        self.assertGreater(np.count_nonzero(result.masks[0] != result.masks[1]), 0)

    def test_low_contrast_bubble_is_found_by_local_reverse_fill(self):
        frame = self.image()
        bubble(frame, (116, 248, 278, 320), (242, 242, 242))
        span = glyph("低反差正文", (134, 267, 251, 300))
        result = self.detect(frame, (span,), existing=())
        self.assertEqual([(m.text, m.method) for m in result.messages], [("低反差正文", "OCR_REVERSE")])

    def test_pair_of_identical_messages_and_blue_multiline_keep_separate_order(self):
        frame = self.image()
        for bounds in ((117, 190, 266, 256), (117, 266, 266, 332)):
            bubble(frame, bounds, (230, 230, 230))
        bubble(frame, (617, 360, 842, 462), (255, 226, 192))
        spans = (glyph("同文", (133, 209, 182, 236)), glyph("同文", (133, 286, 182, 315)),
                 glyph("第一行", (645, 379, 756, 405)), glyph("第二行", (644, 414, 766, 441)))
        result = self.detect(frame, spans)
        self.assertEqual([(m.text, m.direction) for m in result.messages],
                         [("同文", "INBOUND"), ("同文", "INBOUND"), ("第一行\n第二行", "OUTBOUND")])
        self.assertEqual(len(result.containers), 3)

    def test_pale_slant_on_background_is_watermark_without_affecting_center_time(self):
        frame = self.image()
        span = glyph("局部斜线", (140, 240, 270, 290), 22)
        for x in range(140, 260, 12):
            cv2.line(frame, (x, 251), (x+7, 275), (228, 228, 228), 2)
        time = glyph("星期— 11:59", (429, 130, 530, 148))
        result = self.detect(frame, (span, time), existing=())
        self.assertEqual([item.kind for item in result.dispositions], ["WATERMARK", "TIME"])
        self.assertFalse(any(item.possible_message for item in result.dispositions))

    def test_emoji_only_and_text_with_emoji_do_not_hide_other_messages(self):
        frame = self.image()
        for bounds in ((116, 190, 278, 256), (116, 300, 278, 371)):
            bubble(frame, bounds, (230, 230, 230))
        result = self.detect(frame, (glyph("👍", (134, 206, 175, 235)), glyph("收到👍", (134, 322, 231, 349))))
        self.assertEqual([(message.kind, message.status) for message in result.messages],
                         [("EMOJI", "CANDIDATE"), ("TEXT", "CANDIDATE")])


class LocalTimeTests(TestCase):
    def test_today_labels_and_correction_are_anchored_to_original_capture(self):
        for label, suffix in (("13:59", "13:59"), (" 1 4：01 ", "14:01")):
            stamp = parse_message_time(label, CAPTURE)
            self.assertEqual(stamp.anchor, f"2026-11-25T{suffix}+08:00")
            self.assertEqual((stamp.raw, stamp.normalized, stamp.precision), (label, suffix, "MINUTE"))

    def test_calendar_rules_and_weekday_ocr(self):
        for raw, expected in (("星期— 11:59", "2026-11-23T11:59+08:00"),
                              ("星期-11:59", "2026-11-23T11:59+08:00"),
                              ("星期日 09:30", "2026-11-22T09:30+08:00"),
                              ("昨天 16:30", "2026-11-24T16:30+08:00"),
                              ("10/6 08:30", "2026-10-06T08:30+08:00"),
                              ("1/2 11:50", "2026-01-02T11:50+08:00"),
                              ("12/25 09:00", "2025-12-25T09:00+08:00")):
            with self.subTest(raw=raw):
                result = parse_message_time(raw, CAPTURE)
                self.assertEqual(result.anchor, expected)
        self.assertIn("WEEKDAY_ONE_HORIZONTAL_STROKE", parse_message_time("星期— 11:59", CAPTURE).correction)
        self.assertTrue(parse_message_time("12/25 09:00", CAPTURE).year_inferred)

    def test_date_precision_and_calendar_rollovers(self):
        only_date = parse_message_time("10/06", CAPTURE)
        self.assertEqual((only_date.precision, only_date.local_date, only_date.anchor), ("DATE", "2026-10-06", None))
        for capture, yesterday in (("2024-03-01T08:11+08:00", "2024-02-29T23:59+08:00"),
                                   ("2027-01-01T01:05+08:00", "2026-12-31T23:59+08:00")):
            self.assertEqual(parse_message_time("昨天 23:59", datetime.fromisoformat(capture)).anchor, yesterday)
        leap = parse_message_time("2/29 09:00", datetime.fromisoformat("2024-03-10T10:00+08:00"))
        self.assertEqual(leap.anchor, "2024-02-29T09:00+08:00")
        old = parse_message_time("2/29 09:00", datetime.fromisoformat("2025-03-10T10:00+08:00"))
        self.assertIsNone(old.anchor)

    def test_invalid_and_future_times_stay_unconfirmed(self):
        for raw, reason in (("25:15", "INVALID_DATE_OR_CLOCK"), ("12:61", "INVALID_DATE_OR_CLOCK"),
                            ("23:30", "AFTER_CAPTURE"), ("星期三 11:59", "WEEKDAY_BOUNDARY_UNVERIFIED"),
                            ("星期二 11:59", "WEEKDAY_BOUNDARY_UNVERIFIED"), ("2027-01-01 14:52", "AFTER_CAPTURE")):
            with self.subTest(raw=raw):
                result = parse_message_time(raw, CAPTURE)
                self.assertIsNone(result.anchor)
                self.assertEqual(result.issue, reason)

    def test_group_inheritance_uses_only_own_label_and_keeps_unknown_top(self):
        base = analyze_read(sample(selected=1), "synthetic", "合成会话", None, "", verified=True,
                            badge_cleared=True, refreshed=True).messages[0]
        messages = tuple(replace(base, time=raw) for raw in ("", "昨天 19:22", "13:59", "13:59", "14:01"))
        observations = attach_times(messages, CAPTURE)
        self.assertEqual([m.clock_sequence for m in observations], [0, 1, 1, 2, 1])
        self.assertIsNone(observations[0].clock.anchor)
        self.assertEqual(observations[2].clock.anchor, "2026-11-25T13:59+08:00")


class ConfigurationTests(TestCase):
    def test_missing_and_encoding_and_json_and_invalid_fields_are_distinct(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            self.assertEqual(CoreAccount.inspect(path).reason, "DEFAULT_ACCOUNT_FILE_MISSING")
            for data, expected in ((b"\xff", "ENCODING_INVALID"), (b"{", "JSON_INVALID"),
                                   (b'{"name":[],"email":"x"}', "NAME_INVALID"),
                                   (b'{"name":"synthetic","email":45}', "EMAIL_INVALID")):
                path.write_bytes(data)
                self.assertEqual(CoreAccount.inspect(path).reason, "DEFAULT_ACCOUNT_" + expected)

    def test_bom_json_and_utf16_are_loaded_on_repeated_startup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            raw = json.dumps({"name": ACCOUNT.name, "email": ACCOUNT.email.upper()})
            for encoding in ("utf-8", "utf-8-sig", "utf-16"):
                path.write_text(raw, encoding=encoding)
                status = CoreAccount.inspect(path)
                self.assertEqual((status.path, status.reason, status.account), (path, "DEFAULT_ACCOUNT_LOADED", ACCOUNT))
                self.assertEqual(CoreAccount.load(path), ACCOUNT)

    def test_io_error_has_own_reason(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError):
            self.assertEqual(CoreAccount.inspect(Path("inaccessible")).reason, "DEFAULT_ACCOUNT_READ_FAILED")

    def test_default_path_uses_runtime_config(self):
        from autoim.runtime_paths import RuntimePaths, project_root
        with TemporaryDirectory() as directory:
            paths = RuntimePaths(project_root(), Path(directory).resolve() / "runtime")
            path = paths.account
            with patch("autoim.wecom.collection_incremental.runtime_paths", return_value=paths):
                self.assertEqual(CoreAccount.inspect().path, path)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"name": ACCOUNT.name, "email": ACCOUNT.email}), encoding="utf-8-sig")
                self.assertEqual(CoreAccount.inspect().account, ACCOUNT)


class PersistenceTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "isolated.sqlite3"
        self.store = VisualStore(self.path)
        self.observation = replace(analyze_read(sample(selected=1), "synth", "合成会话", None, "", verified=True,
                                               badge_cleared=True, refreshed=True), latest="SCROLLBAR_BOTTOM")
        self.message = replace(self.observation.messages[0], time="14:01")

    def save(self, count=1, gaps=()):
        messages = tuple(replace(self.message, sequence=i+1) for i in range(count))
        return save_visible(self.store, ACCOUNT, replace(self.observation, messages=messages, gaps=gaps), "CONTACT",
                            CAPTURE, uuid4().hex, lambda: None)

    def test_same_anchored_minute_same_text_appends_only_extra_occurrences(self):
        self.assertEqual(self.save().first, 1)
        self.store = VisualStore(self.path)
        addition = self.save(3)
        self.assertEqual((addition.existing, addition.new, addition.anchor_status), (1, 2, "COUNTED_EQUAL_TAIL"))
        self.assertEqual(self.save(3).new, 0)
        with self.store.transaction() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT group_sequence FROM core_message_times ORDER BY group_sequence")],
                             [1, 2, 3])

    def test_legacy_misdated_clock_uses_first_seen_for_matching_without_overwrite(self):
        self.save()
        with self.store.transaction() as db:
            db.execute("UPDATE core_message_times SET anchor='2026-11-24T14:01+08:00',normalized_text=NULL")
        self.assertEqual((self.save().existing, self.save().new), (1, 0))
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT anchor FROM core_message_times").fetchone()[0], "2026-11-24T14:01+08:00")

    def test_gap_evidence_is_retained_and_does_not_advance_checkpoint(self):
        gap = {"reason": "UNASSIGNED_MESSAGE_OCR", "box": [460, 400, 620, 430], "confidence": .92}
        self.assertTrue(self.save(gaps=(gap,)).gap)
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT last_anchor FROM core_conversation_tail").fetchone()[0])
            raw = json.loads(db.execute("SELECT raw_ocr FROM message_observations WHERE kind='C2_COVERAGE_GAP'").fetchone()[0])
        self.assertEqual(raw["box"], gap["box"])
        self.assertEqual(raw["before"]["time"], "2026-11-25T14:01+08:00")
        self.assertTrue(raw["blocks_time_only_append"])

    def test_account_namespace_conflict_rolls_back_history(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO accounts(profile,identity_status,first_seen) VALUES(?, 'MANUAL', ?)",
                       (ACCOUNT.profile, CAPTURE.isoformat()))
        with self.assertRaisesRegex(DefaultAccountConflict, "DEFAULT_ACCOUNT_DATABASE_CONFLICT"):
            self.save()
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM conversations").fetchone()[0], 0)


class MaskDiagnosticTests(TestCase):
    def test_masks_are_only_in_full_export_and_reference_original_coordinates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = CollectionRuntime(root / "logs", revision="test")
            try:
                raw = sample()
                c2 = raw.layout.get("C2")
                raw = replace(raw, c2_masks=tuple(np.full((c2.height, c2.width), value, np.uint8) for value in (0, 255)))
                run = uuid4().hex
                runtime.keep_visual(run, "synthetic", "synthetic", "c2", raw, {}, c2_evidence_regions(raw, []))
                for full in (False, True):
                    path = root / f"{full}.zip"
                    runtime.export(path, "detection", run, {}, full_visual=full)
                    with zipfile.ZipFile(path) as archive:
                        evidence = json.loads(archive.read("evidence-manifest.json"))["evidence"][0]
                        images = {item["kind"]: item for item in evidence["regions"]}
                        for mask in PRIVATE_RASTERS:
                            self.assertEqual(bool(images[mask]["included"]), full)
                            self.assertEqual(images[mask]["box"], [c2.left, c2.top, c2.right, c2.bottom])
                            if not full:
                                self.assertTrue(images[mask]["withheld"])
            finally:
                runtime.close()
