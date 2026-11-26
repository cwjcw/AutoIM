from dataclasses import replace
from unittest import TestCase

import cv2
import numpy as np

from autoim.wecom.collection_hover import HoverRaster, hover_region, recognize_hover
from autoim.wecom.collection_service import CollectionScope, unread_targets
from autoim.wecom.visual_b2 import unread_badges
from autoim.wecom.visual_contacts import detect_contacts
from autoim.wecom.visual_layout import Box, VisualThresholds, _search_toolbar, detect_layout
from autoim.wecom.visual_messages import analyze_messages, find_containers
from autoim.wecom.visual_ocr import TextSpan
from test_wecom_collection_v35 import sample
from test_wecom_collection_v3535 import STAMP, geometry, mapping


THRESHOLDS = VisualThresholds()


class ToolbarTests(TestCase):
    def test_control_and_adjacent_header_anchor_b2_at_multiple_scales(self):
        for size, unit in (((1366, 768), 1), ((1536, 864), 1.25), ((1920, 1080), 1.5),
                           ((2696, 1436), 1.75), ((3840, 2160), 2)):
            width, height = size
            b_left, c_left = round(width * .12), round(width * .38)
            header, false_seam = round(65 * unit), round(110 * unit)
            with self.subTest(size=size, scale=unit):
                pixels = np.full((height, width, 3), 242, dtype=np.uint8)
                pixels[:, :b_left] = 200
                pixels[:, c_left:] = 255
                pixels[:header, c_left:] = 226
                pixels[round(height*.81):, c_left:] = 229
                pixels[false_seam:false_seam+round(6*unit), b_left:c_left] = 224
                x, y = b_left + round(17*unit), round(15*unit)
                w, h, radius = c_left - x - round(46*unit), round(35*unit), round(7*unit)
                cv2.rectangle(pixels, (x+radius, y), (x+w-radius, y+h), (223,223,223), -1)
                cv2.rectangle(pixels, (x, y+radius), (x+w, y+h-radius), (223,223,223), -1)
                for cx in (x+radius, x+w-radius):
                    for cy in (y+radius, y+h-radius):
                        cv2.circle(pixels, (cx, cy), radius, (223,223,223), -1)
                icon = (x + round(13*unit), y + h//2)
                cv2.circle(pixels, icon, round(4*unit), (124,124,124), max(1, round(unit)))
                cv2.line(pixels, (icon[0]+round(3*unit), icon[1]+round(3*unit)),
                         (icon[0]+round(6*unit), icon[1]+round(6*unit)), (124,124,124), max(1, round(unit)))
                plus = (x+w+round(19*unit), y+h//2)
                for dx, dy in ((round(5*unit), 0), (0, round(5*unit))):
                    cv2.line(pixels, (plus[0]-dx, plus[1]-dy), (plus[0]+dx, plus[1]+dy), (125,125,125), 2)
                layout = detect_layout(pixels, {}, THRESHOLDS, unit)
                self.assertIsNotNone(layout.toolbar.search)
                self.assertIsNotNone(layout.toolbar.icon)
                self.assertIsNotNone(layout.toolbar.add)
                self.assertLessEqual(abs(layout.region("B2").top - header), 3)
                self.assertFalse(layout.b2_boundary_uncertain(unit))

    def test_horizontal_seam_alone_does_not_confirm_toolbar(self):
        pixels = np.full((400, 360, 3), 242, dtype=np.uint8)
        pixels[110:115] = 215
        toolbar = _search_toolbar(pixels, Box(0, 0, 360, 400), 64, 1.)
        self.assertIsNone(toolbar.search)
        self.assertIsNone(toolbar.bottom)


class B2CoverageTests(TestCase):
    def setUp(self):
        self.bounds = Box(0, 0, 320, 195)
        self.pixels = np.full((195, 320, 3), 244, dtype=np.uint8)
        self.names = (TextSpan("合成联系人", Box(96, 16, 172, 30), .99),
                      TextSpan("合成预览", Box(96, 41, 150, 52), .99),
                      TextSpan("12:34", Box(265, 16, 306, 29), .99))
        self.pixels[70:71] = 228
        self.pixels[17:57, 20:60] = (95, 137, 192)

    def read(self, pixels, box, *, single_line=False):
        return (TextSpan("2", box, .99),) if single_line else tuple(
            span for span in self.names if box.contains(span.box.center))

    def contacts(self):
        return detect_contacts(self.pixels, self.bounds, self, 1., THRESHOLDS, multi_feature=True)

    def test_independent_first_badge_matches_contact_when_single_row(self):
        cv2.circle(self.pixels, (60, 14), 9, (78, 90, 252), -1)
        self.pixels[10:18, 58:62] = 255
        rows = self.contacts()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].unread_count, rows[0].is_muted), (2, False))
        self.assertTrue(rows[0].name_reliable)
        self.assertEqual(unread_targets(replace(sample(), contacts=rows), CollectionScope(),
                                        allow_duplicate=True)[0].scope_decision, "ELIGIBLE")

    def test_avatar_art_cannot_trigger_navigation(self):
        cv2.circle(self.pixels, (42, 33), 7, (78, 90, 252), -1)
        self.assertEqual(len(unread_badges(self.pixels, self.bounds, 1., 74)), 1)
        self.assertFalse(self.contacts()[0].has_unread_indicator)

    def test_badge_touching_viewport_is_preserved_without_activation(self):
        cv2.circle(self.pixels, (60, 3), 7, (78, 90, 252), -1)
        self.assertTrue(unread_badges(self.pixels, self.bounds, 1., 74)[0].edge)
        self.assertFalse(self.contacts()[0].has_unread_indicator)


class C2CoverageTests(TestCase):
    def test_local_gray_fill_beside_tinted_history_preserves_same_text(self):
        region = Box(0, 0, 900, 560)
        pixels = np.full((560, 900, 3), (250,247,245), dtype=np.uint8)
        pixels[4:329, 129:877] = (255,231,201)
        first = TextSpan("合成历史", Box(180, 7, 273, 19), .91)
        last = TextSpan("长段续行", Box(181, 275, 265, 297), .99)
        spans = [first, last]
        for y, text in ((372, "好"), (435, "收到"), (494, "收到")):
            pixels[y:y+37, 21:83] = 235
            pixels[y+13:y+24, 32:40] = 42
            spans.append(TextSpan(text, Box(30, y+9, 66, y+28), .99))
        analyzed = analyze_messages(pixels, region, find_containers(pixels, region, 1., THRESHOLDS),
                                    tuple(spans), 1., {}, THRESHOLDS)
        self.assertEqual([item.text for item in analyzed.messages], ["好", "收到", "收到"])
        self.assertTrue(all(item.direction == "INBOUND" for item in analyzed.messages))
        self.assertEqual([item.kind for item in analyzed.dispositions[:2]], ["TOP_VIEWPORT_CLIPPED"]*2)
        self.assertTrue(all(not item.possible_message for item in analyzed.dispositions[:2]))


class HoverCoverageTests(TestCase):
    def test_low_contrast_timestamp_far_from_pointer_and_below_long_bubble(self):
        before = np.full((320, 950, 3), 250, dtype=np.uint8)
        after = before.copy()
        span = TextSpan("11/26 19:01:02", Box(671, 228, 819, 246), .97)
        after[230:243:2, 680:813:3] = 239
        evidence = HoverRaster((0, 0, 950, 320), (220, 90), before, after, 1260,
                               (50, 65, 780, 186), 1.5, "INBOUND", 1000)
        read = lambda pixels, area: (span,) if pixels is after and area.contains(span.box.center) else ()
        stamp, reason, box, _ = recognize_hover(evidence, read, STAMP)
        self.assertEqual((reason, box, stamp.precision), ("FRESH_LOW_CONTRAST_TOOLTIP", span.box, "SECOND"))

    def test_crop_includes_wide_bubble_right_side_and_extends_with_scale(self):
        model = mapping((1600, 900), (1600, 900), dpi=96, desktop=geometry((1600, 900)))
        history = Box(300, 90, 1550, 790)
        bubble = Box(370, 260, 1200, 340)
        point = (420, 270)
        one = hover_region(model, history, bubble, point, 1.)
        larger = hover_region(model, history, bubble, point, 1.75)
        screen_bubble = model.image_rect_to_screen((370, 260, 1200, 340))
        self.assertGreater(one[2], screen_bubble[2])
        self.assertGreater(larger[3], one[3])
