from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile
import tempfile
import unittest

import cv2
import numpy as np

from autoim.wecom.collection_core import CoreAnalysis, CoreMessage
from autoim.wecom.collection_evidence import write_evidence
from autoim.wecom.collection_history import Page, Sequence, displacement, page_displacement, resume_segment
from autoim.wecom.collection_incremental import CoreAccount, save_visible
from autoim.wecom.collection_pending import PendingCollections
from autoim.wecom.collection_tasks import CaptureTasks
from autoim.wecom.collection_test import TestClock
from autoim.wecom.visual_files import FileCardObservation
from autoim.wecom.visual_history import history_controls, edge_bubbles, report_images
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_ocr import TextSpan
from autoim.wecom.visual_store import VisualStore
from autoim.wecom.visual_time import parse_hover_time

AREA = Box(0, 0, 400, 200)
STAMP = datetime(2025, 8, 29, 15, 3, 17, tzinfo=timezone.utc)
ACCOUNT = CoreAccount("Synthetic", "history@example.test")


def msg(text, y, end, *, clipped=False, index=1):
    return CoreMessage(
        str(index),
        "task",
        "Synthetic chat",
        index,
        "INBOUND",
        "TEXT",
        text,
        "今天 10:00",
        "",
        0.98,
        Box(10, y, 90, end),
        "CLIPPED" if clipped else "RELIABLE",
        "UNKNOWN",
        "",
        "CANDIDATE",
    )


def page(items=(), image=None, **kwargs):
    blank = np.zeros((200, 192), np.uint8)
    return Page(
        image if image is not None else blank,
        AREA,
        items,
        (),
        STAMP.isoformat(),
        kwargs.get("top", False),
        kwargs.get("bottom", False),
        False,
        None,
        False,
    )


class HistoryDiagnosticTests(unittest.TestCase):
    def test_pagination_is_a_recognized_export_stage(self):
        event = {
            "id": "event",
            "time": STAMP.isoformat(),
            "code": "VISUAL_EVIDENCE",
            "result": "AUTO_PASS",
            "stage": "CORE",
            "task": "task",
            "detection": "run",
            "details": {"evidence_id": "photo", "kind": "history-page"},
        }
        store = SimpleNamespace(read=lambda *_: (None, None))
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            report = {'version': 'test', 'git': 'test', 'detection': 'run'}
            self.assertEqual(write_evidence(archive, store, [event], report, full=False), 0)
        with ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(b"history-page", archive.read("recognition-report.html"))


class HistoryBudgetTests(unittest.TestCase):
    def test_history_limits_agree_between_live_snapshot_and_event(self):
        events = []
        clock = TestClock(
            lambda code, status, fields: events.append((code, fields)),
            now=lambda: 0,
            seconds=600,
            history=True,
        )
        clock.begin_target()
        clock.begin(6, "History")
        self.assertEqual(clock.snapshot()["duration"], 145)
        self.assertEqual(clock.snapshot()["remaining"], 145)
        self.assertEqual(events[-1][1]["stage_limit_seconds"], 145)
        self.assertEqual(dict(events)["TEST_DEADLINE_SET"]["target_seconds"], 180)


class PageSequenceTests(unittest.TestCase):
    def setUp(self):
        world = np.random.default_rng(9).integers(0, 255, (350, 192), dtype=np.uint8)
        self.before, self.after = world[:200], world[150:]

    def test_measured_downward_25_percent_overlap(self):
        self.assertEqual(displacement(self.before, self.after, "DOWN"), 150)

    def test_measured_upward_25_percent_overlap(self):
        self.assertEqual(displacement(self.after, self.before, "UP"), -150)

    def test_reject_wrong_direction(self):
        self.assertIsNone(displacement(self.after, self.before, "DOWN"))

    def test_physical_overlap_survives_small_seam_jitter(self):
        before = page(image=self.before)
        after = replace(page(image=self.after[3:-1]), area=Box(0, 3, 400, 199))
        self.assertEqual(page_displacement(before, after, "DOWN"), 150)
        self.assertIsNone(page_displacement(before, replace(after, area=Box(0, 3, 398, 199)), "DOWN"))
        self.assertIsNone(page_displacement(before, replace(after, area=Box(0, 3, 400, 191)), "DOWN"))

    def test_stationary_view(self):
        self.assertEqual(displacement(self.after, self.after.copy(), "UP"), 0)

    def test_insufficient_overlap(self):
        self.assertIsNone(displacement(self.before, np.flipud(self.after), "UP"))

    def test_periodic_page_ambiguous(self):
        a = np.tile(self.before[:20], (10, 1))
        self.assertIsNone(displacement(a, np.roll(a, 5, axis=0), "DOWN"))

    def test_eleven_separate_same_minute_objects(self):
        seq = Sequence()
        items = tuple(
            msg(x, 10 + i * 14, 21 + i * 14, index=i) for i, x in enumerate("不不你好你我不不嗯好好")
        )
        seq.add(page(items), 0)
        seq.add(page(items), 0)
        self.assertEqual([m.text for m in seq.result("BOTTOM").messages], list("不不你好你我不不嗯好好"))
        self.assertEqual(seq.repeated, 11)

    def test_repeated_words_cross_page(self):
        seq = Sequence()
        seq.add(page((msg("same", 75, 95), msg("same", 125, 145))), 0)
        seq.add(page((msg("same", 25, 45), msg("same", 75, 95))), 100)
        self.assertEqual(len(seq.result("BOTTOM").messages), 3)

    def test_long_bubble_three_pages(self):
        seq = Sequence()
        seq.add(page((msg("a\nb\nc", 70, 200, clipped=True),)), 0)
        seq.add(page((msg("c\nd\ne", 0, 200, clipped=True),)), 150)
        seq.add(page((msg("e\nf", 0, 80, clipped=True),)), 300)
        result = seq.result("BOTTOM")
        self.assertFalse(result.gaps)
        self.assertEqual([m.text for m in result.messages], ["a\nb\nc\nd\ne\nf"])
        self.assertEqual(seq.stitched, 2)

    def test_discontinuous_long_message_stays_partial(self):
        seq = Sequence()
        seq.add(page((msg("a\nb", 50, 200, clipped=True),)), 0)
        seq.add(page((msg("z\nw", 0, 90, clipped=True),)), 150)
        self.assertTrue(seq.result("BOTTOM").gaps)

    def test_repetitive_long_message_without_registered_lines_is_partial(self):
        seq = Sequence()
        seq.add(page((msg("intro\nsame\nsame", 50, 200, clipped=True),)), 0)
        seq.add(page((msg("same\nsame\nend", 0, 110, clipped=True),)), 150)
        self.assertTrue(seq.result("BOTTOM").gaps)

    def test_long_message_uses_registered_lines_to_resolve_repeated_overlap(self):
        seq = Sequence()
        upper = replace(
            msg("intro\nsame\nsame", 50, 200, clipped=True),
            ocr_boxes=((12, 90, 80, 100), (12, 160, 80, 172), (12, 184, 80, 195)),
        )
        lower = replace(
            msg("same\nsame\nend", 0, 110, clipped=True),
            ocr_boxes=((12, 10, 80, 22), (12, 34, 80, 45), (12, 82, 80, 95)),
        )
        seq.add(page((upper,)), 0)
        seq.add(page((lower,)), 150)
        result = seq.result("BOTTOM")
        self.assertFalse(result.gaps)
        self.assertEqual([item.text for item in result.messages], ["intro\nsame\nsame\nend"])

    def test_distinct_directions_do_not_merge(self):
        seq = Sequence()
        seq.add(page((msg("a", 60, 120),)), 0)
        seq.add(page((replace(msg("a", 60, 120), direction="OUTBOUND"),)), 0)
        self.assertTrue(seq.result("BOTTOM").gaps)

    def test_restore_private_page_and_timestamp(self):
        old = page((msg("one", 20, 45),), self.before, bottom=True)
        restored = Page.decode(old.encode())
        self.assertEqual(restored.items, old.items)
        self.assertEqual(restored.at, old.at)
        self.assertTrue(np.array_equal(restored.image, old.image))

    def test_page_file_evidence_can_be_decoded_repeatedly(self):
        item = replace(
            msg("sample.pdf", 20, 50),
            kind="FILE",
            file=FileCardObservation(
                "sample.pdf",
                "pdf",
                "30 KB",
                Box(10, 20, 90, 50),
                ("sample.pdf",),
                (),
                (),
                0.98,
                "COMPLETE",
                (),
            ),
        )
        evidence = page((item,), self.before).encode()
        self.assertEqual(Page.decode(evidence).items, (item,))
        self.assertEqual(Page.decode(evidence).items, (item,))

    def test_resume_uses_verified_shift_for_new_viewport_position(self):
        old = page(image=self.after, bottom=True)
        row = dict(segment="capture", origin=0, page=old.encode(), connected=True)
        self.assertEqual(resume_segment([row], page(image=self.before))[0], -150)

    def test_resume_only_at_unique_saved_position(self):
        old = page(image=self.before, bottom=True)
        row = dict(segment="capture", origin=-80, page=old.encode(), connected=True)
        self.assertEqual(resume_segment([row], old)[0], -80)
        self.assertIsNone(resume_segment([row, dict(row, origin=20)], old))
        self.assertIsNone(resume_segment([dict(row, connected=False)], old))


class NativeControlTests(unittest.TestCase):
    def button(self, n=3, offset=0, arrows=True):
        p = np.full((200, 400, 3), 235, np.uint8)
        x = 100 + offset
        cv2.rectangle(p, (x, 45), (x + 130, 86), (255, 255, 255), -1)
        blue = (230, 135, 35)
        for y in (55, 63) if arrows else ():
            cv2.polylines(p, [np.array([(x + 10, y + 5), (x + 15, y), (x + 20, y + 5)])], False, blue, 2)
        cv2.putText(p, str(n), (x + 32, 71), cv2.FONT_HERSHEY_SIMPLEX, 0.6, blue, 1)
        s = TextSpan(str(n) + "条新消息", Box(x + 7, 53, x + 108, 75), 0.99)
        return history_controls(p, AREA, [s], 1)

    def test_three_and_fifteen_and_hundred(self):
        for n in (3, 15, 100):
            with self.subTest(n=n):
                self.assertEqual([v[0] for v in self.button(n).values()], ["C2_N1"])

    def test_whole_history_region_searched(self):
        for x in (-85, 80):
            self.assertTrue(self.button(24, x))

    def test_chat_with_same_text(self):
        self.assertFalse(self.button(arrows=False))

    def test_divider_with_both_rules(self):
        p = np.full((200, 400, 3), 245, np.uint8)
        s = TextSpan("以下为新消息", Box(155, 86, 245, 108), 0.99)
        cv2.line(p, (45, 96), (147, 96), (155, 155, 155), 1)
        self.assertFalse(history_controls(p, AREA, [s], 1))
        cv2.line(p, (251, 96), (350, 96), (155, 155, 155), 1)
        self.assertEqual(history_controls(p, AREA, [s], 1)[s][0], "C2_N2")

    def test_report_image_contains_table_words(self):
        p = np.full((200, 400, 3), 240, np.uint8)
        cv2.rectangle(p, (15, 22), (180, 160), (84, 43, 13), -1)
        spans = [
            TextSpan("cell", Box(x, y, x + 14, y + 9), 0.99) for x in (24, 64, 105) for y in (45, 86, 126)
        ]
        self.assertEqual(report_images(p, AREA, spans, 1), [Box(15, 22, 181, 161)])

    def test_two_sided_clipped_text_fill(self):
        p = np.full((200, 400, 3), 245, np.uint8)
        p[:, 16:170] = (222, 220, 220)
        s = TextSpan("synthetic", Box(35, 76, 91, 92), 0.98)
        self.assertEqual(edge_bubbles(p, AREA, [s], 1), [Box(16, 0, 170, 200)])


class TimeAndJournalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.queue = PendingCollections(self.root / "pending.db")
        self.jobs = CaptureTasks(self.queue, self.root / "target.db", ACCOUNT)

    def test_tooltip_missing_date_separator_unique(self):
        self.assertEqual(parse_hover_time("8/290:09:58", STAMP).anchor, "2025-08-29T00:09:58+00:00")

    def test_tooltip_ambiguous_digit_split(self):
        self.assertTrue(parse_hover_time("8/111:10:20", STAMP).issue)

    def test_navigation_intent_survives_restart(self):
        key = self.jobs.discover("run", "Synthetic chat", {"count": 3})
        self.jobs.update(key, "NAVIGATION_PENDING", before="shot")
        restarted = CaptureTasks(self.queue, self.root / "target.db", ACCOUNT)
        self.assertEqual(restarted.outstanding()[0]["id"], key)

    def test_stop_preserves_checkpoint_and_atomic_commit_state(self):
        tasks = self.jobs
        active = tasks.discover("run", "Synthetic chat", {})
        committed = tasks.discover("run", "Other chat", {})
        tasks.page(active, STAMP.isoformat(), {"page": 1})
        tasks.update(active, "CAPTURING")
        tasks.update(committed, "COMMIT_PENDING", batch="already-staged")
        tasks.pause({active, committed})
        states = {item["id"]: item["state"] for item in tasks.outstanding()}
        self.assertEqual(states, {active: "RETRY_PENDING", committed: "COMMIT_PENDING"})
        self.assertEqual(tasks.pages(active), [{"page": 1}])

    def test_a2_unknown_target_is_preserved(self):
        key = self.jobs.discover("run", "", {"count": 3}, kind="A2")
        self.jobs.update(key, "REVIEW_REQUIRED", reason="UNVERIFIED_DIRECT_OPEN")
        self.assertEqual(self.jobs.outstanding()[0]["kind"], "A2")

    def test_account_and_database_isolation(self):
        self.jobs.discover("run", "Synthetic chat", {})
        self.assertFalse(CaptureTasks(self.queue, self.root / "other.db", ACCOUNT).outstanding())
        self.assertFalse(
            CaptureTasks(
                self.queue, self.root / "target.db", CoreAccount("other", "x@example.test")
            ).outstanding()
        )

    def test_new_event_same_contact_separate_id(self):
        a, b = [self.jobs.discover("r", "S", {}) for _ in range(2)]
        self.assertNotEqual(a, b)
        self.jobs.update(a, "COMPLETED")
        self.assertEqual([j["id"] for j in self.jobs.outstanding()], [b])

    def test_page_order_survives_new_queue_instance(self):
        key = self.jobs.discover("r", "s", {})
        self.jobs.page(key, STAMP.isoformat(), {"offset": 0})
        self.jobs.page(key, STAMP.isoformat(), {"offset": -90})
        restarted = CaptureTasks(PendingCollections(self.queue.path), self.root / "target.db", ACCOUNT)
        self.assertEqual(restarted.pages(key), [{"offset": 0}, {"offset": -90}])

    def test_image_fingerprint_supports_matching_without_body_text(self):
        store = VisualStore(self.root / "target.db")
        picture = replace(msg("", 0, 19), kind="IMAGE", media_signature="a" * 24)
        caption = msg("following text", 30, 43, index=2)
        analysis = CoreAnalysis((picture, caption), "BOTTOM", "VISIBLE", ())
        first = save_visible(store, ACCOUNT, analysis, "CONTACT", STAMP, "run", lambda: None)
        again = save_visible(store, ACCOUNT, analysis, "CONTACT", STAMP, "other", lambda: None)
        self.assertEqual(first.first, 2)
        self.assertEqual(again.existing, 2)
        self.assertEqual(again.new, 0)

    def test_complete_history_can_prepend_without_replacing_ids(self):
        store = VisualStore(self.root / "target.db")

        def save(words, complete=False):
            messages = tuple(msg(w, i * 20, i * 20 + 15, index=i) for i, w in enumerate(words))
            analysis = CoreAnalysis(
                messages, "BOTTOM", "ACCESSIBLE_HISTORY_CONTIGUOUS" if complete else "VISIBLE", ()
            )
            return save_visible(store, ACCOUNT, analysis, "CONTACT", STAMP, "run", lambda: None)

        save(("two", "three"))
        with closing(sqlite3.connect(store.path)) as db:
            old = dict(db.execute("select body,id from messages"))
        save(("one", "two", "three", "four"), True)
        again = save(("one", "two", "three", "four"), True)
        self.assertEqual(again.new, 0)
        with closing(sqlite3.connect(store.path)) as db:
            current = dict(db.execute("select body,id from messages order by position"))
        self.assertEqual(list(current), ["one", "two", "three", "four"])
        self.assertEqual({k: current[k] for k in old}, old)
