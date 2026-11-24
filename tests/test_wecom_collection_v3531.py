"""Full visible names and private evidence without real customer data."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4
import json
import zipfile

import cv2
import numpy as np

from autoim.wecom.collection_evidence import EvidenceExportError, contact_scan, message_evidence, title_evidence
from autoim.wecom.collection_navigation import row_target
from autoim.wecom.collection_runtime import CollectionRuntime
from autoim.wecom.collection_service import CollectionScope, unread_targets
from autoim.wecom.visual_b2 import B2Badge
from autoim.wecom.visual_contacts import parse_visual_name
from autoim.wecom.visual_layout import Box, VisualThresholds
from autoim.wecom.visual_observation import compare_conversation, observe_frame
from autoim.wecom.visual_store import name_key
from test_wecom_collection_v35 import sample
from test_wecom_visual_capture import FixtureReader, fixture, span


class CompleteNameTests(TestCase):
    def test_names_keep_parentheses_companies_and_special_group_characters(self):
        for raw, expected, peer in (
            ("同事 ＠ 微信", "同事@微信", "PERSONAL_WECHAT"),
            ("同事@示例分部有限公司", "同事@示例分部有限公司", "EXTERNAL_WECOM"),
            ("同事@示例简称", "同事@示例简称", "EXTERNAL_WECOM"),
            ("同事", "同事", "UNKNOWN"),
            ("测试员（Demo）", "测试员(Demo)", "UNKNOWN"),
            ("测试员(Demo)", "测试员(Demo)", "UNKNOWN"),
            ("产品群聊@测试&发布", "产品群聊@测试&发布", "UNKNOWN"),
        ):
            with self.subTest(raw=raw):
                parsed = parse_visual_name(raw)
                self.assertEqual((parsed.display_name, parsed.peer_type, parsed.issue), (expected, peer, ""))

    def test_split_b2_name_and_label_exclude_right_time_and_message_preview(self):
        frame, reader = fixture()
        original = tuple(item for item in reader.spans if item.text not in ("测试乙", "＠微信")
                         and not (157 <= item.box.left < 390 and 175 <= item.box.top < 201))
        name = span("样本员", 157, 160, 60)
        label = span("＠示例商贸", 222, 162, 92)
        preview = span("@其他人 预览", 157, 184, 102)
        activity = span("09:03", 310, 160, 63)
        title = (span("样本员", 418, 23, 62), span("@示例商贸", 486, 25, 108))
        raw = observe_frame(frame, {}, FixtureReader((*original, name, label, preview, activity, *title), ("7", "12")))
        selected = next(row for row in raw.contacts if row.is_selected is True)
        self.assertEqual((selected.display_name, selected.base_name, selected.activity_text),
                         ("样本员@示例商贸", "样本员", "09:03"))
        self.assertIn("@其他人", selected.preview)
        self.assertEqual(selected.name_spans, (name, label))
        self.assertEqual((raw.conversation.title, raw.conversation.match_status), ("样本员@示例商贸", "VISUAL_MATCH"))

    def test_unread_distinct_suffixes_do_not_collide(self):
        raw = sample(selected=2, unread={0: (1, False), 1: (1, False)})
        rows = tuple(replace(row, display_name=label, raw_name=label, base_name="样本员",
                             geometry_reliable=True, kind="CONTACT", peer_type="PERSONAL_WECHAT")
                     for row, label in zip(raw.contacts, ("样本员@微信", "样本员@示例商贸", "样本员")))
        targets = unread_targets(replace(raw, contacts=rows), CollectionScope(), allow_duplicate=True)
        self.assertEqual([target.name for target in targets], ["样本员@微信", "样本员@示例商贸"])
        self.assertEqual([target.scope_decision for target in targets], ["ELIGIBLE", "ELIGIBLE"])

    def test_real_duplicate_allows_only_independently_located_row(self):
        raw = sample(unread={1: (1, False)})
        a, b, c = raw.contacts
        target = replace(b, geometry_reliable=True)
        other = replace(c, display_name=b.display_name, raw_name=b.raw_name)
        raw = replace(raw, contacts=(a, target, other))
        result = unread_targets(raw, CollectionScope(), allow_duplicate=True)[0]
        self.assertTrue(result.duplicate_name)
        self.assertEqual(result.scope_decision, "ELIGIBLE")
        self.assertTrue(target.box.contains(row_target(raw, target, allow_duplicate=True)))
        overlapping = replace(other, box=target.box)
        blocked = unread_targets(replace(raw, contacts=(a, target, overlapping)), CollectionScope(), allow_duplicate=True)[0]
        self.assertEqual(blocked.scope_decision, "WAITING_REVALIDATION")

    def test_different_reliable_label_is_not_matched_and_truncated_title_is_unknown(self):
        selected = replace(sample(selected=1).contacts[1], display_name="样本员@微信", peer_type="PERSONAL_WECHAT",
                           identity_labels=("微信",))
        for title, outcome in (("样本员", "CONVERSATION_MISMATCH"), ("样本员@示例商贸", "CONVERSATION_MISMATCH"),
                               ("样本员@示例…", "UNKNOWN"), ("样本员@", "UNKNOWN")):
            result = compare_conversation((span(title, 418, 23, 180),), (selected,), VisualThresholds())
            self.assertEqual(result.match_status, outcome)
            self.assertNotEqual(result.title, selected.display_name)


class DiagnosticFileTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.runtime = CollectionRuntime(self.root / "logs", revision="test")
        self.addCleanup(self.runtime.close)
        self.detection, self.scan, self.task = (uuid4().hex for _ in range(3))
        self.private = "仅合成私有名称"
        self.body = "仅合成正文<script>never_run</script>"
        raw = sample(unread={1: (1, False)})
        targets = ("仅合成私有名称", "仅合成私有名称@微信", "仅合成私有名称@合成企业")
        rows = tuple(replace(row, raw_name=label, display_name=label, base_name=self.private,
                             name_spans=(span(label.strip(), row.box.left+78, row.box.top+5, 84, .987),))
                     for row, label in zip(raw.contacts, targets))
        unmatched = B2Badge(Box(92, 610, 109, 626), "SMALL_RED")
        self.raw = replace(raw, contacts=rows, b2_badges=(unmatched,))
        self.stats, self.overlays = contact_scan(self.raw, unread_targets(self.raw), self.scan, "")

    def emit(self, code, task="", **details):
        return self.runtime.emit("test", self.detection, "CORE_EVIDENCE", code, task=task, details=details)

    def keep_b2(self):
        self.emit("TEST_STARTED")
        event = self.emit("B2_SCANNED", scan=self.scan)
        return self.runtime.keep_visual(self.detection, "", self.scan, "b2", self.raw, self.stats,
                                        [("b2", self.raw.layout.get("B2"), self.overlays)], source_event=event)

    def export(self, name, full=False):
        path = self.root / name
        self.runtime.export(path, "detection", self.detection, {}, full_visual=full)
        return zipfile.ZipFile(path)

    def record_capture_chain(self):
        from autoim.wecom.collection_core import analyze_read
        self.keep_b2()
        title, markers = title_evidence(self.raw)
        self.runtime.keep_visual(self.detection, self.task, self.scan, "before", self.raw, title,
                                 [("c1", self.raw.layout.get("C1"), markers)])
        event = self.emit("CONTACT_CLICKED", self.task)
        self.runtime.keep_visual(self.detection, self.task, self.scan, "after", self.raw, title,
                                 [("c1", self.raw.layout.get("C1"), markers)], source_event=event)
        evidence = analyze_read(self.raw, self.task, self.private, 1, "", verified=True, badge_cleared=True, refreshed=True)
        messages = tuple(replace(item, text=self.body) for item in evidence.messages)
        data, boxes = message_evidence(self.raw, messages)
        event = self.emit("MESSAGE_OCR_COMPLETED", self.task)
        self.runtime.keep_visual(self.detection, self.task, self.scan, "c2", self.raw, data,
                                 [("c2", self.raw.layout.get("C2"), boxes)], source_event=event)
        self.emit("STOP_PERMISSION_REVOKED")
        self.emit("WORKER_DRAINED")

    def test_redacted_and_full_export_after_stop(self):
        self.record_capture_chain()
        with self.export("ordinary.zip") as archive:
            payloads = {file: archive.read(file) for file in archive.namelist() if not file.endswith(".png")}
            redacted = b"\n".join(payloads.values()).decode()
            self.assertNotIn(self.private, redacted)
            self.assertNotIn("never_run", redacted)
            scans = json.loads(payloads["contacts.json"])
            rows = scans[0]["data"]["rows"]
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1]["same_base_distinct_name_rows"], [1, 3])
            self.assertIn("@微信", rows[1]["full_display_name"])
            self.assertIn("@企业", rows[2]["full_display_name"])
            self.assertEqual(rows[1]["name_ocr"][0]["score"], .987)
            self.assertEqual(len(scans[0]["data"]["unassigned_badges"]), 1)
            pictures = [file for file in archive.namelist() if file.endswith(".png")]
            self.assertTrue(pictures)
            self.assertTrue(all(file.endswith("-masked.png") for file in pictures))
            self.assertFalse(any(file.endswith((".sqlite3", ".env")) for file in archive.namelist()))
            self.assertNotIn("http", payloads["recognition-report.html"].decode())
        with self.export("visual.zip", full=True) as archive:
            manifest = json.loads(archive.read("evidence-manifest.json"))
            evidence = manifest["evidence"]
            b2, *_, c2 = evidence
            self.assertEqual(c2["task"], self.task)
            self.assertEqual(c2["scan"], self.scan)
            self.assertTrue(c2["contains_chat"])
            self.assertIn(self.body, [record["data"]["messages"][0]["text"]
                      for record in json.loads(archive.read("message-observations.json")) if record["kind"] == "c2"])
            item = b2["regions"][0]
            image = archive.read(item["included"]["original"])
            self.assertEqual(sha256(image).hexdigest(), item["sha256"]["original"])
            decoded = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertTrue(np.array_equal(decoded, self.raw.layout.get("B2").crop(self.raw.frame.pixels)))
            self.assertNotIn("<script>never_run", archive.read("recognition-report.html").decode())

    def test_absent_image_is_reported_with_failed_stage(self):
        self.emit("TEST_STARTED")
        self.emit("STAGE_TIMEOUT", self.task)
        with self.export("missing.zip") as archive:
            manifest = json.loads(archive.read("evidence-manifest.json"))
            self.assertTrue(manifest["evidence"])
            self.assertTrue(all(row["status"] == "DIAGNOSTIC_EVIDENCE_MISSING" for row in manifest["evidence"]))
            self.assertIn(b"DIAGNOSTIC_EVIDENCE_MISSING", archive.read("recognition-report.html"))

    def test_invalid_checksum_does_not_leave_a_completed_zip(self):
        identifier = self.keep_b2()
        self.runtime.sync()
        path = self.runtime.evidence.root / self.detection / f"{identifier}-b2-original.png"
        path.write_bytes(b"damaged")
        with self.assertRaisesRegex(EvidenceExportError, "摘要"):
            self.export("corrupt.zip", full=True)
        self.assertFalse((self.root / "corrupt.zip").exists())
