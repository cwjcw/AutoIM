from concurrent.futures import CancelledError
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import sqlite3

import numpy as np

from autoim.version import PC_TITLE, PC_VERSION
from autoim.vision import WindowCapture
from autoim.windows_dpi import DpiStatus, initialize_dpi, physical_thread
from autoim.wecom.collection_core import CoreAnalysis, CoreMessage
from autoim.wecom.collection_hover import HoverRaster, recognize_hover
from autoim.wecom.collection_incremental import CoreAccount, preview_visible, save_visible
from autoim.wecom.collection_navigation import CollectionNavigation, _identity
from autoim.wecom.collection_pending import PendingBatch, PendingCollections
from autoim.wecom.collection_runtime import PC_VERSION as LOG_VERSION
from autoim.wecom.collection_service import CollectionService
from autoim.wecom.visual_capture import CaptureDiagnostics, CaptureError, VisualFrame, WindowState, _coverage, _render_complete_client
from autoim.wecom.visual_layout import Box, VisualThresholds, detect_layout
from autoim.wecom.visual_ocr import TextSpan
from autoim.wecom.visual_store import VisualStore
from autoim.wecom.visual_time import parse_hover_time
from autoim.wecom.window_coordinates import CoordinateError, DesktopGeometry, build_coordinates
from autoim.wecom.window_manager import WeComWindowInfo
from autoim.wecom.window_state import ProcessIdentity, WindowHealth, WindowSample
from test_wecom_visual_capture import fixture


STAMP = datetime(2026, 11, 26, 20, tzinfo=timezone(timedelta(hours=8)))
ACCOUNT = CoreAccount("Synthetic operator", "operator@example.test")


def geometry(size, origin=(-1600, -200), *, dpi_mode="PER_MONITOR_V2", monitors=1):
    x, y = origin
    w, h = size
    monitor = (x, y, x + w + 20, y + h + 20)
    return DesktopGeometry(origin, (x+w, y+h), (x-4, y-4, x+w+5, y+h+5),
                           "virtual-monitor", monitor, monitor, (x, y, x+w+20, y+h+20),
                           0, DpiStatus("PER_MONITOR", dpi_mode), "SYSTEM", monitors)


def mapping(size=(1540, 820), native=(2696, 1436), *, dpi=168, desktop=None):
    return build_coordinates(7, 11, "WeWorkWindow", size, native, dpi, desktop or geometry(size), paint_verified=True)


class CoordinateTests(TestCase):
    def test_reproduced_175_percent_dimensions_round_trip_original_pixels(self):
        model = mapping()
        self.assertEqual(model.method, "VERIFIED_DPI_RENDER")
        for source in ((0, 0), (1000, 800), (2695, 1435)):
            xy = model.image_to_screen(source, for_input=True)
            out = model.screen_to_image(xy)
            self.assertTrue(all(abs(a-b) <= 1 for a, b in zip(source, out)))
            self.assertEqual(xy, model.client_to_screen(model.image_to_client(source)))
        self.assertLess(model.image_to_screen((0, 0))[0], 0)

    def test_display_matrix_preserves_native_or_proven_fractional_coordinates(self):
        for image in ((1366, 768), (1920, 1080), (2560, 1440), (3840, 2160)):
            for dpi in (96, 120, 144, 168, 192):
                with self.subTest(image=image, dpi=dpi):
                    client = tuple(round(coordinate * 96 / dpi) for coordinate in image)
                    for size in (image, client):
                        model = mapping(size, image, dpi=dpi, desktop=geometry(size, (-1800, -1200)))
                        center = tuple(n // 2 for n in image)
                        self.assertTrue(all(abs(a-b) <= 1 for a, b in
                                            zip(center, model.screen_to_image(model.image_to_screen(center)))))
                        self.assertEqual(model.visual_unit, dpi/96)

    def test_ratio_is_unusable_without_a_verified_monitor_and_round_trip(self):
        good = geometry((1540, 820))
        for rejected, reason in (
            (replace(good, roundtrip_error=3), "ROUNDTRIP"),
            (replace(good, native_monitor_rect=(0, 0, 2560, 1440)), "MONITOR"),
            (replace(good, awareness=DpiStatus("SYSTEM", "SYSTEM")), "THREAD"),
            (replace(good, client_end=(0, 0)), "EXTENT"),
            (replace(good, virtual_desktop=(0, 0, 1920, 1080)), "DESKTOP"),
            (replace(good, frame_physical=(-50, -50, 5, 5)), "DWM"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(CoordinateError, reason):
                mapping(desktop=rejected)

    def test_unexplained_scale_and_missing_origin_fail_closed(self):
        for native in ((2600, 1436), (2310, 1230), (1450, 810)):
            with self.subTest(native=native), self.assertRaisesRegex(CoordinateError, "SCALE"):
                mapping(native=native)
        for painted, origin in ((False, (0, 0)), (True, (3, 0))):
            with self.assertRaisesRegex(CoordinateError, "PAINT_OR_ORIGIN"):
                build_coordinates(7, 11, "WeWorkWindow", (1540, 820), (2696, 1436), 168,
                                  geometry((1540, 820)), paint_verified=painted, image_origin=origin)

    def test_spanning_monitors_requires_safe_single_monitor_client_area(self):
        sample = geometry((1540, 820), monitors=2)
        sample = replace(sample, monitor_rect=(-1600, -200, -300, 640),
                         native_monitor_rect=(-1600, -200, -300, 640))
        with self.assertRaisesRegex(CoordinateError, "SPANS"):
            mapping(desktop=sample)

    def test_snapshot_changes_with_monitor_dpi_pid_and_remote_mode(self):
        original = mapping()
        variations = (replace(original, pid=9), replace(original, hwnd=12), replace(original, dpi=144),
                      replace(original, desktop=replace(original.desktop, monitor="another")),
                      replace(original, desktop=replace(original.desktop, virtual_desktop=(-1920, -200, 1920, 640))),
                      replace(original, desktop=replace(original.desktop, remote=True)),
                      mapping(desktop=geometry((1540, 820), (-1400, -100))))
        self.assertTrue(all(item.snapshot != original.snapshot for item in variations))

    def test_offscreen_mouse_and_nominal_boundary_refused(self):
        good = geometry((1540, 820))
        window = mapping(desktop=replace(good, monitor_rect=(-1598, -198, -80, 630),
                                         native_monitor_rect=(-1598, -198, -80, 630)))
        with self.assertRaisesRegex(CoordinateError, "MONITOR"):
            window.image_to_screen((0, 0), for_input=True)
        with self.assertRaisesRegex(CoordinateError, "OUTSIDE_IMAGE"):
            window.image_to_screen((2696, 1435))

    def test_frame_validation_uses_identity_disjoint_from_snapshot(self):
        model = mapping()
        shot = np.ones((1436, 2696, 3), np.uint8)
        rect = model.desktop.frame_physical
        info = WeComWindowInfo(7, 11, "企业微信", "WeWorkWindow", rect=rect)
        status = WindowSample(rect=rect, dpi=168, zoomed=False, client_size=model.client_size, desktop=model.desktop)
        health = WindowHealth("WINDOW_VALID", "", STAMP, window=info, first=status, last=status,
                              process=ProcessIdentity("WXWork.exe", "", 1))
        frame = VisualFrame(WindowCapture(7,11,"企业微信","WeWorkWindow",rect,2696,1436,shot), STAMP,
                            WindowState(False, False, 168, 1540, 820, None),
                            diagnostics=CaptureDiagnostics(window_health=health), coordinates=model)
        CollectionService._validate_read_frame(frame)
        self.assertEqual(CollectionService._lifetime(frame), (7, 11, health.identity))
        moved = replace(frame, coordinates=replace(model, desktop=replace(model.desktop, monitor="another")))
        self.assertNotEqual(_identity(frame), _identity(moved))
        with self.assertRaisesRegex(RuntimeError, "COORDINATE"):
            CollectionService._validate_read_frame(moved)
        self.assertIsNone(health.capture_blocker)

    def test_wide_black_and_clipped_origin_fail_without_resizing(self):
        pix = np.full((160, 260, 3), 245, np.uint8)
        pix[:, :32] = 0
        self.assertEqual(_coverage(pix)[0], "CAPTURE_INCOMPLETE")
        def render(_hwnd, width, height):
            raw = np.empty((height,width,3),np.uint8)
            raw[:] = (29,251,13)
            raw[3:71,5:122] = 200
            return raw
        with self.assertRaises(CaptureError) as exc:
            _render_complete_client(11, 120, 70, render=render)
        self.assertIsNotNone(exc.exception.image)

    def test_dpi_explicit_denial_and_query_result_are_preserved(self):
        status = DpiStatus("SYSTEM", "SYSTEM", "process_default_already_set", 5)
        with patch("autoim.windows_dpi.dpi_status", side_effect=[DpiStatus("SYSTEM","SYSTEM"), status]), patch(
                "autoim.windows_dpi._user", return_value=lambda _: False), patch(
                "autoim.windows_dpi.ctypes.get_last_error", return_value=5):
            self.assertEqual(initialize_dpi(), status)

    def test_scoped_thread_context_restores_previous_awareness_after_exception(self):
        setter = Mock(return_value=112)
        with patch("autoim.windows_dpi.thread_awareness", return_value="SYSTEM"), patch(
                "autoim.windows_dpi._user", return_value=setter):
            with self.assertRaisesRegex(ValueError, "probe failure"):
                with physical_thread():
                    raise ValueError("probe failure")
        self.assertEqual([call.args for call in setter.call_args_list], [(-4,), (112,)])

    def test_existing_per_monitor_context_is_not_overwritten(self):
        initial = DpiStatus("PER_MONITOR", "PER_MONITOR_V1")
        with patch("autoim.windows_dpi.dpi_status", return_value=initial), patch("autoim.windows_dpi._user") as api:
            self.assertEqual(initialize_dpi(), initial)
            api.assert_not_called()

    def test_independent_list_width_and_composer_height_rediscover_layout(self):
        base = fixture()[0].pixels
        columns = np.concatenate((base[:, :390], base[:, 305:390], base[:, 390:]), axis=1)
        shorter = np.concatenate((columns[:488], columns[554:]), axis=0)
        layout = detect_layout(shorter, {}, VisualThresholds())
        self.assertIsNotNone(layout.get("B2"))
        self.assertIsNotNone(layout.get("C2"))
        self.assertGreater(layout.get("C2").left, 430)
        self.assertLess(layout.get("C2").bottom, 530)


class TooltipTests(TestCase):
    def test_stop_aborts_before_cursor_or_screen_capture(self):
        navigator = CollectionNavigation(Mock())
        with patch("autoim.wecom.collection_navigation.move_cursor") as move, patch(
                "autoim.wecom.collection_navigation.capture_screen_region") as capture:
            with self.assertRaises(CancelledError):
                navigator.hover_message(None, None, Mock(side_effect=CancelledError))
            move.assert_not_called()
            capture.assert_not_called()

    def test_tooltip_far_from_target_is_not_attributed(self):
        source = np.zeros((180, 380, 3), dtype=np.uint8)
        dest = source.copy()
        box = Box(30, 5, 175, 24)
        box.crop(dest)[:] = 240
        raster = HoverRaster((0, 0, 380, 180), (140, 166), source, dest, 600)
        reader = Mock(return_value=(TextSpan("11/26 16:24:52", box, .98),))
        self.assertEqual(recognize_hover(raster, reader, STAMP)[1], "HOVER_NO_UNIQUE_TIMESTAMP")


    def test_read_valid_exact_time_after_group_minute_and_preserve_raw(self):
        stamp = parse_hover_time("11/26 16:24:52", STAMP, lower=STAMP.replace(hour=16, minute=20))
        self.assertEqual((stamp.precision, stamp.time_source, stamp.raw), ("SECOND","HOVER","11/26 16:24:52"))
        self.assertTrue(stamp.anchor.endswith("16:24:52+08:00"))

    def test_invalid_month_second_and_after_capture_rejected(self):
        for raw in ("99/26 16:24:52", "11/26 16:23:62", "11/26 22:00:04", "11/26 16:23", "prefix 11/26 16:24:52"):
            with self.subTest(raw=raw):
                value = parse_hover_time(raw, STAMP, lower=STAMP.replace(hour=0))
                self.assertTrue(value.issue)
                self.assertIsNone(value.anchor)

    def test_original_capture_cross_year_and_bound_conflict(self):
        at = datetime(2027, 1, 2, 20, tzinfo=STAMP.tzinfo)
        value = parse_hover_time("12/31 23:59:59", at)
        self.assertTrue(value.anchor.startswith("2026-12-31"))
        conflict = parse_hover_time("11/26 16:24:52", STAMP, upper=STAMP.replace(hour=16, minute=22))
        self.assertEqual(conflict.issue, "HOVER_TIME_GROUP_CONFLICT")

    def test_still_image_and_already_present_timestamp_are_not_attributed(self):
        a = np.full((180,380,3),235,np.uint8)
        box = Box(90,65,220,94)
        b = a.copy()
        box.crop(b)[:] = 25
        reader = Mock(return_value=(TextSpan("11/26 16:24:52",box,.97),))
        raster = HoverRaster((40,100,420,280),(190,175),a,a,600)
        self.assertEqual(recognize_hover(raster, reader, STAMP)[1], "HOVER_NO_FRESH_RASTER")
        reader.assert_not_called()
        self.assertEqual(recognize_hover(replace(raster,after=b), reader, STAMP)[1], "HOVER_PREVIOUS_TOOLTIP_PRESENT")

    def test_fresh_footprint_requires_ocr_proximity_and_does_not_reject_equal_text_in_another_capture(self):
        before = np.full((180,380,3),235,np.uint8)
        after = before.copy()
        box = Box(90,65,220,94)
        box.crop(after)[:] = 25
        raster = HoverRaster((40,100,420,280),(190,175),before,after,650)
        def ocr(source, _box):
            return (TextSpan("11/26 16:24:52",box,.97),) if source is after else ()
        for copy in (raster, replace(raster, region=(-450,-200,-70,-20),point=(-300,-125))):
            hover, reason, where, _ = recognize_hover(copy,ocr,STAMP)
            self.assertEqual((reason,where,hover.precision),("FRESH_LOCAL_TOOLTIP",box,"SECOND"))

    def test_title_and_logs_use_single_version(self):
        self.assertEqual(PC_TITLE, "AutoIM " + LOG_VERSION)
        self.assertEqual(LOG_VERSION, PC_VERSION)


class PersistentSecondsTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = VisualStore(Path(tmp.name) / "isolated.sqlite3")

    def message(self, sequence, body="Example", second=None, *, direction="INBOUND", group="16:20"):
        hover = parse_hover_time(f"11/26 16:23:{second:02d}", STAMP) if second is not None else None
        return CoreMessage(f"observation-{sequence}", "task", "Example conversation", sequence, direction, "TEXT",
                           body, group, "", .96, Box(10,sequence*40,150,sequence*40+32), "RELIABLE", "UNKNOWN",
                           "", "CANDIDATE", hover=hover)
    def analysis(self, messages):
        return CoreAnalysis(tuple(messages), "SCROLLBAR_BOTTOM", "VISIBLE_MESSAGES_ACCOUNTED_FOR", ())
    def save(self, *messages, at=STAMP):
        return save_visible(self.store,ACCOUNT,self.analysis(messages),"CONTACT",at,"run",lambda:None)
    def rows(self, sql):
        with closing(sqlite3.connect(self.store.path)) as db:
            return db.execute(sql).fetchall()

    def test_initial_precise_storage_retains_group_and_message_order(self):
        observed = (self.message(1,second=15), self.message(2,second=15), self.message(3,second=28,direction="OUTBOUND"))
        result = self.save(*observed)
        self.assertEqual(result.first,3)
        self.assertEqual(self.rows("SELECT time_text,raw_time,h.source FROM messages m JOIN core_hover_times h ON h.message_id=m.id ORDER BY position"),
                         [("16:20","11/26 16:23:15","HOVER")]*2 + [("16:20","11/26 16:23:28","HOVER")])
        rerun = self.save(*observed)
        self.assertEqual((rerun.existing,rerun.new),(3,0))

    def test_old_minutes_backfill_precise_without_replacing_message_id(self):
        a, b = self.message(1,"alpha"), self.message(2,"beta")
        self.save(a,b)
        ids = self.rows("SELECT id FROM messages ORDER BY position")
        first, second = self.message(1,"alpha",17), self.message(2,"beta",29)
        _, preliminary = preview_visible(self.store,ACCOUNT,self.analysis((first,second)),STAMP)
        self.assertEqual(preliminary,["EXISTING","EXISTING"])
        result = self.save(first,second,at=STAMP+timedelta(minutes=1))
        self.assertEqual((result.existing,result.new),(2,0))
        self.assertEqual(self.rows("SELECT id FROM messages ORDER BY position"),ids)
        self.assertEqual(self.rows("SELECT count(*) FROM core_hover_times"),[(2,)])

    def test_exact_suffix_resolves_duplicate_position_and_allows_following_new(self):
        self.save(self.message(1,second=12),self.message(2,second=23))
        observations = (self.message(1,second=12),self.message(2,second=23),self.message(3,second=48))
        result = self.save(*observations, at=STAMP+timedelta(minutes=1))
        self.assertEqual((result.existing,result.new),(2,1))

    def test_conflicting_hover_preserves_existing_group_and_remains_review(self):
        item = self.message(1,second=20)
        self.save(item)
        conflict = replace(item,hover=replace(item.hover,issue="HOVER_TIME_GROUP_CONFLICT"))
        result = self.save(conflict,at=STAMP+timedelta(minutes=1))
        self.assertEqual(result.uncertain,1)
        self.assertEqual(self.rows("SELECT count(*) FROM messages"),[(1,)])

    def test_old_pending_wire_format_replays_and_new_precise_survives_reopen(self):
        item = self.message(1,second=21)
        old = PendingBatch.from_observation(ACCOUNT,self.analysis((replace(item,hover=None),)),"CONTACT",
                                            STAMP,"verified","run","task")
        self.assertNotIn('"hover"', old.encode())
        self.assertEqual(PendingBatch.decode(old.encode()).encode(),old.encode())
        batch = PendingBatch.from_observation(ACCOUNT,self.analysis((item,)),"CONTACT",STAMP,"verified","run","task")
        path = self.store.path.with_name("pending.sqlite3")
        PendingCollections(path).stage(batch)
        recovery = PendingCollections(path).recover(self.store,ACCOUNT,lambda:None)
        self.assertEqual(recovery.results[0][1].first,1)
        self.assertEqual(self.rows("SELECT raw_time FROM core_hover_times"),[("11/26 16:23:21",)])
