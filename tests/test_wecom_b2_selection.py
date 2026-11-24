"""B2 selection with anonymized raster fixtures and scripted OCR text."""

from dataclasses import replace
import unittest

import cv2
import numpy as np

from autoim.wecom.collection_navigation import row_target
from autoim.wecom.collection_service import unread_targets
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_observation import observe_frame
from test_wecom_visual_capture import FixtureReader, fixture


class B2SelectionTests(unittest.TestCase):
    def read_sample(self, fill, *, dpi=96, same_name=False, hover=None, title="测试乙"):
        frame, reader = fixture(thumbs=False, title=title, same_name=same_name, hover=hover)
        pixels = frame.pixels.copy()
        # Use the neutral and blue BGR values recorded in the 175% device sample.
        strip = pixels[64:, 86:390]
        strip[np.all(strip == (222, 223, 224), axis=2)] = (247, 243, 240)
        strip[np.all(strip == (241, 212, 162), axis=2)] = fill
        scale = dpi / 96
        width, height = round(frame.state.width * scale), round(frame.state.height * scale)
        if dpi != 96:
            pixels = cv2.resize(pixels, (width, height), interpolation=cv2.INTER_NEAREST)
            spans = [replace(item, box=Box(*(round(value * scale) for value in
                         (item.box.left, item.box.top, item.box.right, item.box.bottom)))) for item in reader.spans]
            reader = FixtureReader(spans, ("7", "12"))
        frame = replace(frame, state=replace(frame.state, dpi=dpi, width=width, height=height),
                        capture=replace(frame.capture, width=width, height=height, image=pixels))
        return observe_frame(frame, {}, reader)

    def test_foreground_blue_uses_independent_visual_evidence(self):
        raw = self.read_sample((247, 142, 70))
        self.assertEqual(raw.conversation.selection_method, "BLUE_HIGHLIGHT_BAND")
        self.assertEqual(raw.conversation.selection_confidence, "HIGH")
        self.assertEqual([row.is_selected for row in raw.contacts], [False, True, False])

    def test_pale_background_row_requires_unique_c1_support_at_175_percent(self):
        raw = self.read_sample((241, 237, 233), dpi=168)
        self.assertEqual(raw.conversation.selection_method, "SUBTLE_FILL_AND_C1")
        self.assertEqual(raw.conversation.selection_confidence, "CORROBORATED")
        self.assertEqual(raw.conversation.match_status, "VISUAL_MATCH")
        self.assertEqual([row.is_selected for row in raw.contacts], [False, True, False])
        self.assertTrue(all(row.box.height >= 114 for row in raw.contacts))
        self.assertIn("色差 7", raw.conversation.selection_evidence)

    def test_unique_title_without_independent_background_stays_unknown(self):
        raw = self.read_sample((247, 243, 240))
        self.assertTrue(raw.conversation.title_name_reliable)
        self.assertIn("缺少独立行底证据", raw.conversation.selection_evidence)
        self.assertTrue(all(row.is_selected is None for row in raw.contacts))
        self.assertTrue(all(not target.ambiguous for target in unread_targets(raw)))
        unread = next(row for row in raw.contacts if row.has_unread_indicator)
        self.assertTrue(raw.layout.get("B2").contains(row_target(raw, unread)))

    def test_same_name_blocks_pale_selection(self):
        raw = self.read_sample((241, 237, 233), same_name=True)
        self.assertIn("无法唯一核对", raw.conversation.selection_evidence)
        self.assertTrue(all(row.is_selected is None for row in raw.contacts))

    def test_unmatched_title_and_hover_do_not_confirm_pale_row(self):
        for options in ({"title": "不属于可见列表"}, {"hover": (110, 170)}):
            with self.subTest(options=options):
                raw = self.read_sample((241, 237, 233), **options)
                self.assertTrue(all(row.is_selected is None for row in raw.contacts))
