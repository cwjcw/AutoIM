"""Screenshot geometry and structural evidence for the WeCom message page."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import cv2
import numpy as np

from autoim.vision import NormalizedRect, normalized_rect_to_source, relative_rect_from_pixels


@dataclass(frozen=True, slots=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, point: tuple[int, int]) -> bool:
        x, y = point
        return self.left <= x < self.right and self.top <= y < self.bottom

    def crop(self, pixels: np.ndarray) -> np.ndarray:
        return pixels[self.top:self.bottom, self.left:self.right]

    def offset(self, x: int, y: int) -> Box:
        return Box(self.left + x, self.top + y, self.right + x, self.bottom + y)

    def relative(self, width: int, height: int) -> NormalizedRect:
        return relative_rect_from_pixels(self.left, self.top, self.right, self.bottom, width, height)


@dataclass(frozen=True, slots=True)
class VisualThresholds:
    seam_delta: int = 9
    seam_coverage: float = .56
    foreground_delta: int = 13
    badge_red_delta: int = 74
    text_score: float = .65
    anchor_tolerance: float = .026
    anchor_search_limit: float = .25
    photo_texture: float = .22
    photo_color_fraction: float = .15

    @classmethod
    def load(cls, path: Path | None) -> VisualThresholds:
        if path is None or not path.is_file():
            return cls()
        limits = {"seam_delta": (1, 80), "seam_coverage": (.2, 1),
                  "foreground_delta": (2, 80), "badge_red_delta": (20, 200),
                  "text_score": (.25, 1), "anchor_tolerance": (.003, .10),
                  "anchor_search_limit": (.07, .35), "photo_texture": (.01, .99),
                  "photo_color_fraction": (.01, .99)}
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError
            for name, value in values.items():
                low, high = limits[name]
                if (type(value) not in (int, float) or not low <= value <= high
                        or (name.endswith("_delta") and type(value) is not int)):
                    raise ValueError
        except (OSError, ValueError, KeyError):
            raise ValueError("PC 视觉阈值配置无效，请检查 wecom-pc-visual-thresholds.json。") from None
        return cls(**values)


@dataclass(frozen=True, slots=True)
class UiRegion:
    name: str
    box: Box
    normalized: NormalizedRect
    evidence: str


@dataclass(frozen=True, slots=True)
class SearchToolbar:
    area: Box
    search: Box | None = None
    add: Box | None = None
    icon: Box | None = None
    bottom: int | None = None
    reason: str = ""
    rejected: tuple[Box, ...] = ()


@dataclass(frozen=True, slots=True)
class LayoutObservation:
    regions: tuple[UiRegion, ...]
    status: str
    reason: str = ""
    failures: tuple[tuple[str, str], ...] = ()
    toolbar: SearchToolbar | None = None
    b2_edge_review: str = ""

    def region(self, name: str) -> Box:
        return next(region.box for region in self.regions if region.name == name)

    def get(self, name: str) -> Box | None:
        return next((region.box for region in self.regions if region.name == name), None)

    def b2_boundary_uncertain(self, unit: float) -> bool:
        b2, title = self.get("B2"), self.get("C1")
        if b2 is None:
            return True
        note = next(part.evidence for part in self.regions if part.name == "B2")
        if "人工" in note or note.startswith("首行") or self.toolbar and self.toolbar.bottom == b2.top:
            return False
        return bool(self.toolbar and self.toolbar.search or title and b2.top - title.bottom > 20 * unit)


LEAF_REGIONS = {"A": "A 功能区", "B1": "B1 搜索工具栏", "B2": "B2 联系人列表",
                "C1": "C1 会话标题", "C2": "C2 历史消息", "C3": "C3 输入区"}
REGION_NAMES = ("A", "B", "B1", "B2", "C", "C1", "C2", "C3")


def _find_seam(pixels: np.ndarray, start: float, end: float, vertical: bool,
               thresholds: VisualThresholds) -> tuple[int | None, str]:
    if not vertical:
        pixels = pixels.transpose(1, 0, 2)
    height, width = pixels.shape[:2]
    delta = max(1, width // 450)
    first, last = max(delta, round(width * start)), min(width - delta, round(width * end))
    if first >= last:
        return None, "搜索区为空"
    # Continuous edges are measured across the perpendicular axis. Isolated
    # avatars, text and hover-only scroll thumbs have limited coverage.
    scan = pixels[height // 12:height - height // 12:max(1, height // 200)].astype(np.int16)
    diffs = np.max(np.abs(scan[:, first + delta:last + delta]
                         - scan[:, first - delta:last - delta]), axis=2)
    scores = np.mean(diffs >= thresholds.seam_delta, axis=0)
    peak = int(np.argmax(scores))
    strongest = float(scores[peak])
    if strongest < thresholds.seam_coverage:
        return None, f"持续边缘覆盖 {strongest:.0%}，要求 {thresholds.seam_coverage:.0%}"
    contenders = np.flatnonzero(scores >= max(thresholds.seam_coverage, strongest * .97))
    cluster = max(delta * 4, width // 90)
    if np.any(np.abs(contenders - peak) > cluster):
        alternatives = sorted({first + peak, first + int(contenders[0]), first + int(contenders[-1])})
        return None, f"多个相近分界候选 {alternatives}，最高覆盖 {strongest:.0%}"
    # A persistent one-pixel border gives several equally good offsets.
    return first + int(np.median(contenders)), f"持续边缘覆盖 {strongest:.0%}"


def _search_toolbar(pixels: np.ndarray, column: Box, title: int | None, unit: float) -> SearchToolbar:
    # Search the upper B column before proposing B1. A contact divider can
    # run across B but cannot supply a rounded horizontal control or glyph.
    area = Box(column.left, column.top, column.right, min(column.bottom, column.top + round(140 * unit)))
    tile = area.crop(pixels)
    if tile.size == 0:
        return SearchToolbar(area, reason="搜索区域为空")
    edge = max(2, round(5 * unit))
    background = np.median(tile[:, edge:edge*2], axis=1).astype(np.int16)
    delta = tile.astype(np.int16) - background[:, None, :]
    shade = np.max(np.abs(delta), axis=2)
    neutral = (shade >= 4) & (shade <= 52) & (np.ptp(delta, axis=2) <= 16)
    close = max(2, round(3 * unit))
    filled = cv2.morphologyEx(neutral.astype(np.uint8), cv2.MORPH_CLOSE,
                              np.ones((close, close), np.uint8))
    candidates = []
    rejected = []
    for box in contours(filled):
        if not (box.left < area.width * .24 and box.right < area.width - edge
                and box.top >= round(4 * unit) and box.bottom < area.height
                and 19 * unit <= box.height <= 52 * unit
                and box.width >= area.width * .44 and box.width >= box.height * 3.1):
            continue
        shape = box.crop(filled)
        h, w = shape.shape
        corner = max(2, round(3 * unit))
        if shape.mean() < .78 or (shape[:corner, :corner].mean() >= .91
                                  and shape[:corner, -corner:].mean() >= .91):
            rejected.append(box.offset(area.left, area.top))
            continue
        gray = cv2.cvtColor(box.crop(tile), cv2.COLOR_BGR2GRAY)
        fill = float(np.median(gray[:, w//2:]))
        ink = (gray.astype(np.int16) <= fill - 24)
        icons = [item.offset(box.left + area.left, box.top + area.top) for item in contours(ink[:, :round(29 * unit)])
                 if 4 * unit <= item.width <= 16 * unit and 4 * unit <= item.height <= 17 * unit
                 and item.left >= round(4 * unit) and .20 * h <= item.center[1] <= .80 * h]
        search = box.offset(area.left, area.top)
        if len(icons) != 1:
            rejected.append(search)
            continue
        candidates.append((search, icons[0]))
    if len(candidates) != 1:
        return SearchToolbar(area, reason="搜索框形状与放大镜无唯一候选",
                             rejected=tuple(rejected + [box for box, _ in candidates]))
    search, icon = candidates[0]
    strip = Box(search.right, search.top, area.right - edge, search.bottom)
    gray = cv2.cvtColor(strip.crop(pixels), cv2.COLOR_BGR2GRAY)
    add = None
    if gray.size:
        ink = gray.astype(np.int16) <= np.median(gray) - 27
        for mark in contours(ink):
            if (5 * unit <= mark.height <= 18 * unit and .7 <= mark.width / mark.height <= 1.45
                    and abs(mark.center[1] + strip.top - search.center[1]) <= .25 * search.height):
                cross = mark.crop(ink)
                mid_y, mid_x = mark.height // 2, mark.width // 2
                if (cross[max(0, mid_y-1):mid_y+2].any(axis=0).mean() >= .75
                        and cross[:, max(0, mid_x-1):mid_x+2].any(axis=1).mean() >= .75):
                    add = mark.offset(strip.left, strip.top)
                    break
    low, high = search.bottom + round(3 * unit), search.bottom + round(.95 * search.height)
    if title is not None and low <= title <= high:
        return SearchToolbar(area, search, add, icon, title, "搜索框、放大镜与相邻 C1 工具栏下沿", tuple(rejected))
    # A separator is corroboration only inside the space immediately after
    # the control, never a substitute for the search control itself.
    band_bottom = min(high + 1, area.bottom)
    if add and low + 2 < band_bottom:
        band = Box(area.left, low, area.right, band_bottom)
        seam, _ = _find_seam(band.crop(pixels), .04, .96, False, VisualThresholds(seam_delta=5))
        if seam is not None:
            return SearchToolbar(area, search, add, icon, low + seam,
                                 "搜索框、放大镜、加号及紧邻工具栏下沿", tuple(rejected))
    return SearchToolbar(area, search, add, icon, reason="搜索控件已识别，工具栏下沿缺少交叉证据",
                         rejected=tuple(rejected))


def _pinned_header(pixels, title, unit, thresholds):
    height, width = pixels.shape[:2]
    reach = min(height, title + round(89 * unit))
    first = title + round(13 * unit)
    if first >= reach or width < 50 * unit:
        return title
    edge, _ = _find_seam(pixels, first / height, reach / height, False, thresholds)
    if edge is None:
        return title
    ring = max(2, round(6 * unit))
    if edge - ring * 2 < title or edge + ring * 2 > height:
        return title
    before = np.median(pixels[edge-ring*2:edge-ring, width//8:7*width//8:4], axis=0).astype(np.int16)
    after = np.median(pixels[edge+ring:edge+ring*2, width//8:7*width//8:4], axis=0).astype(np.int16)
    if (before.shape == after.shape and before.size and float(np.mean(
            (before[:, 0] - before[:, 2]) - (after[:, 0] - after[:, 2]) >= 9)) >= .68):
        return edge + max(1, round(2 * unit))
    return title


def _auto_regions(pixels: np.ndarray, thresholds: VisualThresholds,
                  known: Mapping[str, Box], unit: float):
    height, width = pixels.shape[:2]
    regions: dict[str, Box] = {}
    evidence: dict[str, str] = {}
    failures: dict[str, str] = {}
    toolbar = None
    a, a_note = _find_seam(pixels, .025, .24, True, thresholds)
    if "A" in known:
        a, a_note = known["A"].right, "A 人工边界"
    elif a is not None:
        regions["A"] = Box(0, 0, a, height)
        evidence["A"] = a_note
    else:
        failures["A"] = f"A/B 垂直分界不明确 · {a_note}"
    b, b_note = _find_seam(pixels, max(.16, (a / width + .08) if a else .24), .56, True, thresholds)
    c_manual = next((known[name] for name in ("C1", "C3", "C2") if name in known), None)
    if c_manual:
        b, b_note = c_manual.left, "沿用会话区人工横向边界"
    title = None
    title_note = "C 主列未定位"
    if b is not None and (a is None or b > a):
        title, title_note = _find_seam(pixels[:, b:], .025, .19, False, thresholds)
    if "C1" in known:
        title, title_note = known["C1"].bottom, "沿用 C1 人工下边界"
    if a is not None and b is not None and b - a >= width * .07:
        regions["B"] = Box(a, 0, b, height)
        evidence["B"] = f"{a_note} / {b_note}"
        toolbar = _search_toolbar(pixels, regions["B"], title, unit)
        y, note = _find_seam(regions["B"].crop(pixels), .025, .19, False, thresholds)
        if "B1" in known:
            y, note = known["B1"].bottom, "沿用 B1 人工下边界"
        elif "B2" in known:
            y, note = known["B2"].top, "沿用 B2 人工上边界"
        elif toolbar.bottom is not None:
            y, note = toolbar.bottom, toolbar.reason
        elif y is None and title is not None:
            expected = title / height
            y, adjacent = _find_seam(regions["B"].crop(pixels), max(.025, expected - .018),
                                      min(.19, expected + .018), False, thresholds)
            note = (f"B 与 C1 相邻工具栏分界相近 · {adjacent}" if y is not None
                    else f"{note}；相邻工具栏验证 {adjacent}")
        if y is not None:
            regions.update(B1=Box(a, 0, b, y), B2=Box(a, y, b, height))
            evidence.update(B1=note, B2=note)
        else:
            failures.update(B1=f"B 工具栏下边界 · {note}", B2=f"B2 缺少安全上边界 · {note}")
    else:
        failures["B"] = f"联系人列缺少横向分界 · {a_note} / {b_note}"
    if b is None or (a is not None and b <= a):
        failures["C"] = f"B/C 垂直分界不明确 · {b_note}"
        return regions, evidence, failures, toolbar
    # An expanded side panel has its own persistent vertical boundary and
    # does not continue the composer seam across that boundary.
    d, d_note = _find_seam(pixels, max(.65, b / width + .24), .92, True, thresholds)
    if d is not None:
        composer_left, _ = _find_seam(pixels[:, b:d], .59, .95, False, thresholds)
        if composer_left is not None:
            y = composer_left
            band = max(2, height // 180)
            right_delta = np.abs(pixels[y-band, d:].astype(np.int16) - pixels[y+band, d:])
            if float(np.mean(np.max(right_delta, axis=1) >= thresholds.seam_delta)) < .30:
                regions["D"] = Box(d, 0, width, height)
                evidence["D"] = f"独立纵向边界；输入区下沿未延伸至右栏 · {d_note}"
    right = regions["D"].left if "D" in regions else width
    if title is None and "D" in regions:
        title, title_note = _find_seam(pixels[:, b:right], .025, .19, False, thresholds)
    regions["C"] = Box(b, 0, right, height)
    evidence["C"] = b_note
    c = regions["C"].crop(pixels)
    history_top = title
    if title is None and toolbar and toolbar.bottom is not None:
        expected = toolbar.bottom
        margin = max(3, round(6 * unit))
        local, confirmation = _find_seam(c, max(.025, (expected - margin) / height),
                                         min(.19, (expected + margin) / height), False, thresholds)
        if local is not None:
            title = local
            title_note = f"以已确认 B1 下沿复核 C1 边缘 · {confirmation}"
            history_top = _pinned_header(c, title, unit, thresholds)
            if history_top != title:
                title_note += " · 横跨会话的有色提示条独立排除"
    top, top_note = _find_seam(c, .59, .95, False, thresholds)
    if "C3" in known:
        top, top_note = known["C3"].top, "沿用 C3 人工上边界"
    if title is not None:
        regions["C1"] = Box(b, 0, right, title)
        evidence["C1"] = title_note
    else:
        failures["C1"] = f"标题区下边界 · {title_note}"
    if top is not None:
        regions["C3"] = Box(b, top, right, height)
        evidence["C3"] = top_note
    else:
        failures["C3"] = f"输入区上边界 · {top_note}"
    if history_top is not None and top is not None and history_top < top:
        regions["C2"] = Box(b, history_top, right, top)
        evidence["C2"] = f"{title_note} / {top_note}"
    else:
        failures["C2"] = "历史区缺少可隔离标题和草稿的上下边界"
    return regions, evidence, failures, toolbar


def _calibrated_regions(calibration: Mapping[str, NormalizedRect], width: int,
                        height: int) -> dict[str, Box]:
    leaves = {name: Box(*normalized_rect_to_source(calibration[name], width, height))
              for name in LEAF_REGIONS}
    a, b1, b2, c1, c2, c3 = (leaves[name] for name in LEAF_REGIONS)
    # Require a partition. Allow a small gap because drag boxes are drawn by hand.
    x_tol, y_tol = width * .035, height * .045
    if (any(box.width < width * .02 or box.height < height * .02 for box in leaves.values())
            or not (0 <= min(b1.left, b2.left) - a.right <= x_tol)
            or not (0 <= min(c1.left, c2.left, c3.left) - max(b1.right, b2.right) <= x_tol)
            or abs(b1.left - b2.left) > x_tol or abs(b1.right - b2.right) > x_tol
            or max(abs(c1.left - c2.left), abs(c2.left - c3.left),
                   abs(c1.right - c2.right), abs(c2.right - c3.right)) > x_tol
            or not (b1.top < b1.bottom <= b2.top < b2.bottom)
            or not (c1.top < c1.bottom <= c2.top < c2.bottom <= c3.top < c3.bottom)
            or max(b2.top - b1.bottom, c2.top - c1.bottom, c3.top - c2.bottom) > y_tol):
        return {}
    leaves["B"] = Box(min(b1.left, b2.left), b1.top, max(b1.right, b2.right), b2.bottom)
    leaves["C"] = Box(min(c1.left, c2.left, c3.left), c1.top, max(c1.right, c2.right, c3.right), c3.bottom)
    return leaves


def detect_layout(pixels: np.ndarray, calibration: Mapping[str, NormalizedRect],
                  thresholds: VisualThresholds, unit: float | None = None) -> LayoutObservation:
    height, width = pixels.shape[:2]
    manual = {}
    rejected = {}
    for name in LEAF_REGIONS.keys() & calibration.keys():
        box = Box(*normalized_rect_to_source(calibration[name], width, height))
        if box.width < width * .02 or box.height < height * .02:
            rejected[name] = "人工区域尺寸过小"
        elif name == "C2" and box.bottom > height * .96:
            rejected[name] = "人工 C2 延伸至底缘，可能包含输入区"
        else:
            manual[name] = box
    # Small drag overlaps are clipped in the later region. Larger overlaps
    # are conflicting evidence, so neither crop is used for recognition.
    for group in (("B1", "B2"), ("C1", "C2"), ("C2", "C3"), ("C1", "C3")):
        earlier, later = group
        if earlier not in manual or later not in manual:
            continue
        top, bottom = manual[earlier], manual[later]
        overlap = top.bottom - bottom.top
        if overlap > 0:
            if overlap <= height * .016 and bottom.bottom > top.bottom:
                manual[later] = Box(bottom.left, top.bottom, bottom.right, bottom.bottom)
            else:
                for name in group:
                    manual.pop(name, None)
                    rejected[name] = f"人工 {earlier} 与 {later} 重叠，需重新标定"
    b_left = min((manual[name].left for name in ("B1", "B2") if name in manual), default=width)
    b_right = max((manual[name].right for name in ("B1", "B2") if name in manual), default=0)
    c_left = min((manual[name].left for name in ("C1", "C2", "C3") if name in manual), default=width)
    for name in tuple(manual):
        if ((name == "A" and manual[name].right > min(b_left, c_left))
                or (name.startswith("B") and b_right > c_left)):
            manual.pop(name)
            rejected[name] = "人工区域与相邻主列重叠"
    regions, evidence, failures, toolbar = _auto_regions(pixels, thresholds, manual, unit or max(.65, height / 820))
    for name in rejected:
        regions.pop(name, None)
    regions.update(manual)
    evidence.update({name: "同一截图坐标系的人工相对标定" for name in manual})
    if all(name in manual for name in LEAF_REGIONS):
        normalized = {name: box.relative(width, height) for name, box in manual.items()}
        validated = _calibrated_regions(normalized, width, height)
        if validated:
            regions.update(validated)
            evidence.update(B="人工子区域合并", C="人工子区域合并")
        else:
            rejected["B"] = rejected["C"] = "人工区域未形成一致的主列"
            regions.pop("B", None)
            regions.pop("C", None)
    # Do not run history OCR across a manually placed header or composer.
    if any(name in regions for name in ("C1", "C3")) and "C2" in regions:
        history = regions["C2"]
        if (("C1" in regions and history.top < regions["C1"].bottom)
                or ("C3" in regions and history.bottom > regions["C3"].top)):
            regions.pop("C2")
            rejected["C2"] = "历史区与标题或草稿区域重叠"
    if "D" in regions:
        right = regions["D"].left
        for name in ("C", "C1", "C2", "C3"):
            if name in regions and regions[name].right > right:
                box = regions[name]
                if right > box.left:
                    regions[name] = Box(box.left, box.top, right, box.bottom)
                else:
                    regions.pop(name)
                    rejected[name] = "会话区域与群看板重叠"
    observations = tuple(UiRegion(name, regions[name], regions[name].relative(width, height),
                                  evidence.get(name, "分界几何")) for name in (*REGION_NAMES, "D") if name in regions)
    missing = tuple((name, rejected.get(name, failures.get(name, "缺少可验证的父区域边界")))
                    for name in REGION_NAMES if name not in regions)
    return LayoutObservation(observations, "LAYOUT_UNCERTAIN" if missing else "CANDIDATE",
                             "未确认区域可单独标定。" if missing else "", missing, toolbar)


def contours(mask: np.ndarray) -> list[Box]:
    outlines, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return [Box(x, y, x + w, y + h) for x, y, w, h in map(cv2.boundingRect, outlines)]


def near_background(tile: np.ndarray, delta: int) -> np.ndarray:
    step = max(1, min(tile.shape[:2]) // 220)
    reference = np.median(tile[::step, ::step], axis=(0, 1))
    lower = tuple(float(value) for value in np.floor(reference - delta) + 1)
    upper = tuple(float(value) for value in np.ceil(reference + delta) - 1)
    return cv2.inRange(tile, lower, upper) != 0


def detect_scroll_thumb(pixels: np.ndarray, region: Box, unit: float) -> Box | None:
    edge = min(max(4, round(12 * unit)), region.width)
    area = Box(region.right - edge, region.top, region.right, region.bottom)
    mask = ~near_background(area.crop(pixels), 18)
    hits = [box for box in contours(mask) if 1 <= box.width <= round(9 * unit)
            and max(round(14 * unit), box.width * 4) <= box.height < region.height * .85]
    return hits[0].offset(area.left, area.top) if len(hits) == 1 else None
