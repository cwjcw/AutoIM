"""Bounded tooltip recognition using a fresh, local physical-screen change."""

from dataclasses import dataclass
from time import monotonic

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_time import parse_hover_time


class HoverBudget(RuntimeError):
    pass


@dataclass(frozen=True)
class HoverRaster:
    region: tuple[int, int, int, int]
    point: tuple[int, int]
    before: np.ndarray
    after: np.ndarray
    move_ms: int


def hover_region(mapping, history, bubble, point):
    viewport = mapping.image_rect_to_screen((history.left, history.top, history.right, history.bottom))
    target = mapping.image_to_screen(point, for_input=True)
    portion = mapping.image_rect_to_screen((bubble.left, bubble.top, bubble.right, bubble.bottom))
    client = (*mapping.desktop.client_origin, *mapping.desktop.client_end)
    monitor = mapping.desktop.monitor_rect
    x, y = target
    proposed = (min(x, portion[0]) - 190, y - 118, max(x, portion[2]) + 190, y + 122)
    return (max(proposed[0], x - 325, viewport[0], client[0], monitor[0]) + 2,
            max(proposed[1], viewport[1], client[1], monitor[1]) + 2,
            min(proposed[2], x + 325, viewport[2], client[2], monitor[2]) - 2,
            min(proposed[3], viewport[3], client[3], monitor[3]) - 2)


def changed_pixels(raster):
    if raster.before.shape != raster.after.shape or not raster.after.size:
        return None
    diff = cv2.absdiff(raster.before, raster.after).max(axis=2)
    _, mask = cv2.threshold(diff, 18, 255, cv2.THRESH_BINARY)
    if np.count_nonzero(mask) < 24:
        return None
    points = cv2.findNonZero(mask)
    x, y, w, h = cv2.boundingRect(points)
    pad = 8
    return Box(max(0, x-pad), max(0, y-pad), min(mask.shape[1], x+w+pad), min(mask.shape[0], y+h+pad))


def recognize_hover(raster, read, observed, *, lower=None, upper=None, check=lambda: None):
    changed = changed_pixels(raster)
    if changed is None:
        return None, "HOVER_NO_FRESH_RASTER", None, 0
    started = monotonic()
    check()
    result = []
    for span in read(raster.after, changed):
        check()
        if not changed.contains(span.box.center) or span.score < .75:
            continue
        timestamp = parse_hover_time(span.text, observed, lower=lower, upper=upper)
        if timestamp.issue == "INVALID_HOVER_FORMAT":
            continue
        # Restrict evidence to the pixels around the hovered bubble. Require
        # changed glyph pixels and rule out a timestamp in the same prior crop.
        x, y = span.box.center
        relx, rely = (value - origin for value, origin in zip(raster.point, raster.region[:2]))
        if abs(x - relx) > 270 or abs(y - rely) > 96:
            continue
        before, after = span.box.crop(raster.before), span.box.crop(raster.after)
        if not before.size or np.mean(cv2.absdiff(before, after).max(axis=2) > 24) < .13:
            continue
        result.append((timestamp, span.box))
    elapsed = lambda: round((monotonic() - started) * 1000)
    if len(result) != 1:
        return None, "HOVER_NO_UNIQUE_TIMESTAMP", None, elapsed()
    timestamp, region = result[0]
    check()
    if any(parse_hover_time(span.text, observed).issue != "INVALID_HOVER_FORMAT"
           for span in read(raster.before, region)):
        return None, "HOVER_PREVIOUS_TOOLTIP_PRESENT", region, elapsed()
    return timestamp, timestamp.issue or "FRESH_LOCAL_TOOLTIP", region, elapsed()
