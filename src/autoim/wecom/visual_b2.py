"""Independent geometry and icon evidence from the visible contact list."""

import re
import unicodedata
from dataclasses import dataclass

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box, contours

# Binary silhouette of the 22×22 glyph in the supplied 175% bell sample.
# Pixels outside the glyph and all screenshots are excluded.
_BELL_ROWS = (
    "1100000111111110000000", "1110001111111111000000", "0111011111111111100000",
    "0011111111111111100000", "0001111111111111110000", "0000111111111111110000",
    "0000011111111111110000", "0000001111111111110000", "0000000111111111110000",
    "0000000011111111110000", "0000100001111111110000", "0000110000111111110000",
    "0000111000011111110000", "0000111100001111110000", "0000111110000111110000",
    "0000111111000011110000", "0000111111100001110000", "0000111111110000111000",
    "0001111111111000011100", "0001111111111100001110", "0000000000000000000111",
    "0000000000000000000011",
)
BELL = np.array([[int(value) for value in row] for row in _BELL_ROWS], dtype=np.uint8)
ACTIVITY = re.compile(r"刚刚|昨天|\d{1,4}分钟前|(?:[01]?\d|2[0-3]):[0-5]\d|"
                      r"(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])|星期[一二三四五六日天]")


def activity_text(value):
    value = "".join(unicodedata.normalize("NFKC", value).split())
    return value if ACTIVITY.fullmatch(value) else ""


@dataclass(frozen=True, slots=True)
class B2Badge:
    box: Box
    kind: str
    edge: bool = False
    reason: str = ""


def unread_badges(pixels, region, unit, red_delta, avatars=(), *, rejected=None):
    tile = region.crop(pixels).astype(np.int16)
    b, g, r = tile.transpose(2, 0, 1)
    mask = ((r >= 203) & (r - np.maximum(b, g) >= max(60, red_delta))
            & (b >= 32) & (g >= 35) & (np.abs(b - g) <= 32))
    badge_x = np.median([avatar.right for avatar in avatars]) if avatars else None
    x_margin = max(6 * unit, np.median([avatar.width for avatar in avatars]) * .13) if avatars else 0
    results = []
    for item in contours(mask):
        if (not 5 * unit <= item.height <= 24 * unit or not .70 <= item.width / item.height <= 2.6
                or not .49 <= float(item.crop(mask).mean())
                or item.left == 0 or item.right == region.width):
            continue
        area = item.offset(region.left, region.top)
        if (not region.width * .07 <= item.center[0] <= region.width * .30
                or badge_x is not None and abs(area.center[0] - badge_x) > x_margin):
            if rejected is not None and len(rejected) < 32:
                rejected.append(B2Badge(area, "NON_BADGE", reason="紧凑红区位于徽标列之外"))
            continue
        margin = max(1, item.height // 5)
        close = area.crop(pixels)[margin:-margin, margin:-margin]
        white = close.size and float(np.mean(np.min(close, axis=2) >= 220)) >= .035
        kind = "NORMAL_NUMBER" if item.height >= 15 * unit and white else (
            "SMALL_RED" if item.height <= 13.5 * unit else "UNKNOWN_RED")
        edge = area.top - region.top <= 2 * unit or region.bottom - area.bottom <= 2 * unit
        results.append(B2Badge(area, kind, edge))
    return tuple(sorted(results, key=lambda item: item.box.top))


def divider_rows(pixels, region, unit):
    area = region.crop(pixels)
    gray = cv2.cvtColor(area, cv2.COLOR_BGR2GRAY).astype(np.int16)
    # Detect a thin dark line against its own neighbors on both sides.
    offset = max(2, round(2 * unit))
    if len(gray) <= offset * 2 or gray.shape[1] < 10:
        return ()
    a, b = round(gray.shape[1] * .08), round(gray.shape[1] * .96)
    lane = gray[:, a:b:2]
    core = lane[offset:-offset]
    first, second = lane[:-2 * offset] - core, lane[2 * offset:] - core
    neutral = np.max(area[offset:-offset, a:b:2], axis=2).astype(np.int16) - np.min(
        area[offset:-offset, a:b:2], axis=2)
    hit = np.mean((first >= 5) & (first <= 38) & (second >= 5) & (second <= 38) & (neutral <= 42), axis=1)
    cols = np.flatnonzero(hit >= .52)
    clusters = np.split(cols, np.flatnonzero(np.diff(cols) > 1) + 1)
    return tuple(region.top + offset + int(np.median(part)) for part in clusters
                 if 0 < len(part) <= max(2, round(3 * unit)))


def mute_region(row, unit):
    return Box(row.left + round(row.width * .65), row.top + max(1, round(2 * unit)),
               row.right - max(3, round(5 * unit)), row.top + round(row.height * .62))


def mute_evidence(pixels, row, unit, badge_kind):
    area = mute_region(row, unit)
    tile = area.crop(pixels)
    if tile.size == 0 or min(tile.shape[:2]) < 15 * unit:
        return None, "MUTE_REGION_INCOMPLETE", None
    g = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
    # Compare against local background to support blue and gray rows.
    bg = cv2.medianBlur(g, max(9, round(25 * unit) | 1))
    mask = (bg.astype(np.int16) - g >= 25).astype(np.uint8)
    best = 0.
    for scale in (.80, .90, 1., 1.12, 1.24):
        size = max(8, round(22 / 1.75 * unit * scale))
        if min(mask.shape) < size:
            continue
        icon = cv2.resize(BELL, (size, size), interpolation=cv2.INTER_NEAREST)
        best = max(best, float(cv2.matchTemplate(mask, icon, cv2.TM_CCOEFF_NORMED).max()))
    if best >= .76:
        return (None, "BELL_NUMBER_CONFLICT", best) if badge_kind == "NORMAL_NUMBER" else (True, "BELL_TEMPLATE", best)
    if badge_kind == "NORMAL_NUMBER":
        return False, "SAMPLE_NORMAL_BADGE_AND_NO_BELL", best
    return None, "MUTE_ICON_UNCONFIRMED", best


def typical_pitch(centers, lines, highlight, unit):
    spacings = [int(d) for lane in (centers, lines) for d in np.diff(sorted(lane))
                if 33 * unit <= d <= 93 * unit]
    if highlight:
        spacings.append(highlight.height)
    if not spacings:
        return 62 * unit
    median = float(np.median(spacings))
    close = [item for item in spacings if abs(item - median) <= median * .25]
    return float(np.median(close)) if close else median


def structural_span(center, region, pitch, lines, highlight):
    if highlight and highlight.top <= center < highlight.bottom and abs(highlight.height - pitch) <= .24 * pitch:
        return highlight.top, highlight.bottom, "BLUE_BAND"
    above = max((y for y in lines if y <= center), default=region.top)
    below = min((y for y in lines if y > center), default=region.bottom)
    if .76 * pitch <= below - above <= 1.24 * pitch:
        if above in lines and below in lines:
            return above, below, "GRAY_DIVIDERS"
        if abs(center - (above + below) / 2) <= .15 * pitch and (above in lines or below in lines):
            return above, below, "DIVIDER_AND_VIEWPORT"
    return None
