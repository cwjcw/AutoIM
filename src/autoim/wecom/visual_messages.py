"""Find C2 containers before assigning text and left/right evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Mapping

import cv2
import numpy as np

from autoim.vision import NormalizedRect
from autoim.wecom.visual_files import FileCardObservation, parse_file_card, quiet_card_bounds
from autoim.wecom.visual_layout import Box, VisualThresholds, near_background
from autoim.wecom.visual_ocr import TextSpan, text_lines
from autoim.wecom.visual_time import is_time_label
_SYSTEM = re.compile(r"撤回了[一1]条消息|加入了群聊|退出了群聊|移出了群聊|以下是新消息|开启了会话内容存档|你们已成为好友")
_IMAGE_PLACEHOLDER = re.compile(r"\[图片\]|【图片】|图片加载中[.…]*|图片未加载|图片已过期")
_COMPLEX_CARD = re.compile(r"引用|聊天记录|小程序|转发")


@dataclass(frozen=True, slots=True)
class VisibleMessageObservation:
    sequence: int
    box: Box
    direction: str
    kind: str
    text: str
    time_evidence: str
    left_anchor_distance: float | None
    right_anchor_distance: float | None
    status: str
    evidence: tuple[str, ...]
    ocr_spans: tuple[TextSpan, ...]
    file: FileCardObservation | None = None
    sender_name: str = ""
    time_source: str = "GROUP_ANCHOR"
    method: str = "BUBBLE_FIRST"
    viewport_limit: str = ""


@dataclass(frozen=True, slots=True)
class Container:
    box: Box
    clipped: bool
    source: str = "C2 前景轮廓"
    needs_text: bool = False
    quiet_outline: bool = False
    method: str = "BUBBLE_FIRST"


@dataclass(frozen=True, slots=True)
class OcrDisposition:
    span: TextSpan
    kind: str
    container_number: int | None
    nearest_container: int | None
    reason: str
    possible_message: bool = False


@dataclass(frozen=True, slots=True)
class MessageAnalysis:
    messages: tuple[VisibleMessageObservation, ...]
    dispositions: tuple[OcrDisposition, ...]
    anchors: tuple[int | None, int | None]
    containers: tuple[Container, ...] = ()
    masks: tuple[np.ndarray, ...] = ()
    candidate_evidence: tuple[dict, ...] = ()

    @property
    def context(self) -> tuple[TextSpan, ...]:
        return tuple(item.span for item in self.dispositions if item.kind in ("TIME", "SYSTEM", "UNASSIGNED"))


def find_containers(pixels: np.ndarray, region: Box, unit: float,
                    thresholds: VisualThresholds) -> tuple[Container, ...]:
    crop = region.crop(pixels)
    mask = (~near_background(crop, thresholds.foreground_delta)).astype(np.uint8)
    boxes, oversized = _container_contours(mask, region, unit)
    if oversized:
        # A second background tone can occupy an entire lane and hide the
        # inner outlines under RETR_EXTERNAL. Normalize along columns and
        # rows while keeping the same contour and size requirements.
        columns = np.median(crop[::4], axis=0).astype(np.int16)
        residue = crop.astype(np.int16) - columns
        rows = np.median(residue[:, ::5], axis=1).astype(np.int16)
        residue -= rows[:, None, :]
        mask = (np.max(np.abs(residue), axis=2) >= thresholds.foreground_delta).astype(np.uint8)
        localized, _ = _container_contours(mask, region, unit)
        for candidate in localized:
            if not any(_overlaps(candidate.box, item.box) for item in boxes):
                boxes.append(candidate)
    # Light blue fills can be close in value to a light page background, or
    # hidden inside a larger gray foreground contour. Isolate their chroma.
    blue, green, red = crop.astype(np.int16).transpose(2, 0, 1)
    blue_bias = blue - red
    ambient = np.quantile(blue_bias[::5], .25, axis=0)
    chroma = ((blue_bias >= np.maximum(7, ambient + 5)) & (green - red >= 2)
              & (blue - green >= 1) & (blue >= 120))
    local, _ = _container_contours(chroma.astype(np.uint8), region, unit, blue_pass=True)
    for candidate in local:
        duplicates = [index for index, item in enumerate(boxes) if _overlaps(candidate.box, item.box)]
        if not duplicates:
            boxes.append(candidate)
        elif len(duplicates) == 1:
            index = duplicates[0]
            original = boxes[index]
            # Keep the overall outline if it is reinforced by the fill.
            # Prefer the more specific fill when the foreground contour is
            # substantially larger (e.g. shadow or adjacent UI).
            if original.box.width * original.box.height > 1.55 * candidate.box.width * candidate.box.height:
                boxes[index] = candidate
    tolerance = max(2, round(2 * unit))
    for box in quiet_card_bounds(pixels, region, unit):
        edge_tolerance = max(round(3 * unit), min(box.width, box.height) * .055)
        if any(max(abs(box.left - entry.box.left), abs(box.top - entry.box.top),
                   abs(box.right - entry.box.right), abs(box.bottom - entry.box.bottom)) <= edge_tolerance
               for entry in boxes):
            continue
        clipped = box.top <= region.top + tolerance or box.bottom >= region.bottom - tolerance
        boxes.append(Container(box, clipped, "C2 浅色卡片闭合边界", needs_text=True, quiet_outline=True))
    return tuple(sorted(boxes, key=lambda container: (container.box.top, container.box.left)))


def _overlaps(first: Box, second: Box) -> bool:
    width = max(0, min(first.right, second.right) - max(first.left, second.left))
    height = max(0, min(first.bottom, second.bottom) - max(first.top, second.top))
    return width * height >= .65 * min(first.width * first.height, second.width * second.height)


def _container_contours(mask: np.ndarray, region: Box, unit: float, *,
                        blue_pass: bool = False) -> tuple[list[Container], bool]:
    # Close very small gaps in card contours. Larger gaps remain intact so
    # adjacent messages are not joined by nearby text or emoji.
    kernel = (2 * max(1, round(unit)) + 1) if blue_pass else max(2, round(2 * unit))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((kernel, kernel), np.uint8))
    outlines, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    oversized = False
    for outline in outlines:
        x, y, w, h = cv2.boundingRect(outline)
        oversized |= h >= region.height * .85 and w >= region.width * .12
        if not (max(15, (19 if blue_pass else 24) * unit) <= w < region.width * .98
                and max(10, 16 * unit) <= h < region.height * .85
                and cv2.contourArea(outline) >= w * h * .64):
            continue
        small_square = .70 <= w / h <= 1.35 and max(w, h) <= 53 * unit
        outer_square = small_square and (x < region.width * .03 or x + w > region.width * .97)
        if not blue_pass and outer_square:
            continue
        if blue_pass and (mask[y:y+h, x:x+w].mean() < .68
                          or h > max(160 * unit, region.height * .42)):
            continue
        tolerance = max(2, round(unit * 2))
        clipped = y <= tolerance or y + h >= region.height - tolerance
        boxes.append(Container(Box(x, y, x + w, y + h).offset(region.left, region.top), clipped,
                               "C2 蓝色填充轮廓" if blue_pass else "C2 前景轮廓",
                               blue_pass and small_square))
    return boxes, oversized


def _anchor(values: list[int], tolerance: float) -> int | None:
    if not values:
        return None
    bins = [[other for other in values if abs(other - value) <= tolerance] for value in values]
    best = max(bins, key=len)
    middle = int(np.median(best))
    if any(len(group) == len(best) and abs(np.median(group) - middle) > tolerance for group in bins):
        return None
    return middle


def content_anchors(containers: tuple[Container, ...], region: Box, frame_width: int,
                    calibration: Mapping[str, NormalizedRect],
                    thresholds: VisualThresholds) -> tuple[int | None, int | None]:
    candidates = [item.box for item in containers if not item.clipped]
    reach = region.width * thresholds.anchor_search_limit
    tolerance = region.width * thresholds.anchor_tolerance
    left = _anchor([box.left for box in candidates if box.left - region.left <= reach
                    and region.right - box.right > reach], tolerance)
    right = _anchor([box.right for box in candidates if region.right - box.right <= reach
                     and box.left - region.left > reach], tolerance)
    if "left_anchor" in calibration:
        value = round(calibration["left_anchor"].left * frame_width)
        left = value if region.left <= value <= region.left + reach else None
    if "right_anchor" in calibration:
        value = round(calibration["right_anchor"].right * frame_width)
        right = value if region.right - reach <= value <= region.right else None
    return left, right


def direction_evidence(box: Box, region: Box, anchors: tuple[int | None, int | None],
                       thresholds: VisualThresholds) -> tuple[str, float | None, float | None]:
    left, right = anchors
    left_distance = abs(box.left - left) / region.width if left is not None else None
    right_distance = abs(box.right - right) / region.width if right is not None else None
    left_hit = left_distance is not None and left_distance <= thresholds.anchor_tolerance
    right_hit = right_distance is not None and right_distance <= thresholds.anchor_tolerance
    reach = region.width * thresholds.anchor_search_limit
    # If the far edge also reaches its side and that side has no anchor,
    # there is insufficient evidence to exclude conflicting alignment.
    opposite_right = right is None and region.right - box.right <= reach
    opposite_left = left is None and box.left - region.left <= reach
    direction = "UNKNOWN"
    if left_hit and not right_hit and not opposite_right:
        direction = "INBOUND"
    if right_hit and not left_hit and not opposite_left:
        direction = "OUTBOUND"
    return direction, left_distance, right_distance


def _has_photo_texture(tile: np.ndarray, thresholds: VisualThresholds) -> bool:
    if min(tile.shape[:2]) < 8:
        return False
    hsv = cv2.cvtColor(tile, cv2.COLOR_BGR2HSV)
    saturated = float(np.mean(hsv[:, :, 1] > 55))
    gray = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
    texture = np.mean(np.abs(cv2.Laplacian(gray, cv2.CV_32F)) > 15)
    return saturated >= thresholds.photo_color_fraction and texture >= thresholds.photo_texture


def _has_quote_border(pixels: np.ndarray, box: Box, spans: tuple[TextSpan, ...], unit: float) -> bool:
    if len(spans) < 2 or box.height < 46 * unit or box.width < 78 * unit:
        return False
    tile = box.crop(pixels).astype(np.int16)
    inset = max(4, round(5 * unit))
    edge = min(round(box.width * .26), round(35 * unit))
    if edge <= inset:
        return False
    gray = tile.mean(axis=2)
    line = tile[:, inset:edge]
    neutral = np.ptp(line, axis=2) <= 15
    side = (gray[:, :edge-inset] + gray[:, 2*inset:edge+inset]) / 2
    mask = (neutral & (side - gray[:, inset:edge] >= 13)).astype(np.uint8)
    components, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask)
    for left, top, width, height, _ in stats[1:components]:
        if not (width <= 5 * unit and height >= max(18 * unit, box.height * .24)
                and top >= 2 * unit and top + height < box.height - 10 * unit):
            continue
        x, y0, y1 = box.left + inset + left, box.top + top, box.top + top + height
        if (any(s.box.left > x and y0 <= s.box.center[1] <= y1 for s in spans)
                and any(s.box.left >= x - inset and s.box.top >= y1 for s in spans)):
            return True
    return False


def _association_score(span: Box, box: Box, unit: float) -> float | None:
    if min(span.width, span.height, box.width, box.height) <= 0:
        return None
    dx = max(0, min(span.right, box.right) - max(span.left, box.left))
    dy = max(0, min(span.bottom, box.bottom) - max(span.top, box.top))
    horizontal, vertical = dx / span.width, dy / span.height
    margin = max(round(3 * unit), min(round(14 * unit), round(span.height * .45)))
    spill = max(0, box.left - span.left, span.right - box.right,
                box.top - span.top, span.bottom - box.bottom)
    if horizontal < .72 or vertical < .70 or spill > margin or not box.contains(span.center):
        return None
    return horizontal * vertical - .20 * (spill / max(1, margin))


def _distance(first: Box, second: Box) -> int:
    return (max(0, first.left - second.right, second.left - first.right)
            + max(0, first.top - second.bottom, second.top - first.bottom))


def _in_message_lane(box: Box, region: Box, thresholds: VisualThresholds) -> bool:
    reach = region.width * thresholds.anchor_search_limit
    return min(box.left - region.left, region.right - box.right) <= reach


def _inside(inner: Box, outer: Box) -> bool:
    return (outer.left <= inner.left and outer.right >= inner.right
            and outer.top <= inner.top and outer.bottom >= inner.bottom)


def _file_candidates(containers: tuple[Container, ...], spans: tuple[TextSpan, ...], unit: float,
                     score_floor: float) -> dict[Container, FileCardObservation]:
    files = {}
    for container in containers:
        owned = tuple(span for span in spans if _association_score(span.box, container.box, unit) is not None)
        card = parse_file_card(container.box, owned, unit, score_floor, clipped=container.clipped)
        if card is not None:
            files[container] = card
    return files


def _fill_between(span, box, pixels, background, unit):
    """Verify a bubble-colored band above and below the OCR body, outside the glyphs."""
    if not (box.left + 3 * unit <= span.left and span.right <= box.right - 3 * unit
            and box.top + 3 * unit <= span.top and span.bottom <= box.bottom - 3 * unit):
        return False
    margin = max(1, round(2 * unit))
    left, right = span.left, span.right
    top = pixels[box.top + margin:span.top, left:right]
    bottom = pixels[span.bottom:box.bottom - margin, left:right]
    if not top.size or not bottom.size:
        return False
    fill_a, fill_b = (np.median(area.reshape(-1, 3), axis=0) for area in (top, bottom))
    delta = background - (fill_a + fill_b) / 2
    gray = 5 <= np.min(delta) <= np.max(delta) <= 65 and np.ptp(delta) <= 20
    color = (fill_a[0] - fill_a[2]) - (background[0] - background[2]) >= 7 and delta[2] >= 6
    if not (gray or color) or np.max(np.abs(fill_a - fill_b)) > 6:
        return False
    return all(float(np.mean(np.max(np.abs(area - fill), axis=2) <= 6)) >= .62
               for area, fill in ((top, fill_a), (bottom, fill_b)))


def _local_fill_boxes(pixels, span, region, background, unit):
    side = round(52 * unit)
    vertical = round(28 * unit)
    area = Box(max(region.left, span.left - side), max(region.top, span.top - vertical),
               min(region.right, span.right + side), min(region.bottom, span.bottom + vertical))
    crop = area.crop(pixels)
    # Use the most common color next to the text; text ink cannot dominate
    # the sampled strips, and the page background never qualifies as fill.
    strip = max(2, round(7 * unit))
    palette = np.concatenate((pixels[max(area.top, span.top - strip):span.top, span.left:span.right],
                              pixels[span.bottom:min(area.bottom, span.bottom + strip), span.left:span.right]))
    if not palette.size:
        return ()
    color = np.median(palette.reshape(-1, 3), axis=0)
    if np.max(np.abs(color - background)) < 6:
        return ()
    mask = cv2.inRange(crop, tuple(color - 6), tuple(color + 6)) != 0
    candidates, _ = _container_contours(mask.astype(np.uint8), area, unit)
    margin = max(2, round(2 * unit))
    return tuple(item for item in candidates if not item.clipped and item.box.left >= area.left + margin
                 and item.box.right <= area.right - margin)


def _watermark_like(span, crop, background, repeats):
    if not ((span.angle is not None and 8 <= abs(span.angle) <= 55) or repeats >= 2):
        return False
    delta = background - crop.astype(np.float32)
    # Pale, sparse ink only. Dark text and a confirmed filled bubble never
    # enter this filter, regardless of the text or its repetition.
    return bool(crop.size and np.percentile(np.max(np.abs(delta), axis=2), 92) <= 43
                and .012 <= np.mean(np.max(np.abs(delta), axis=2) >= 7) <= .38)


def _recover_bubbles(pixels, region, containers, spans, contextual, unit, thresholds, capture_masks):
    crop = region.crop(pixels)
    background = np.median(crop[::5, ::5], axis=(0, 1))
    raw = (~near_background(crop, thresholds.foreground_delta)).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, round(4 * unit) | 1),) * 2)
    opened = cv2.morphologyEx(raw, cv2.MORPH_OPEN, kernel)
    candidates, _ = _container_contours(opened, region, unit)
    orphaned = [span for span in spans if span not in contextual and span.score >= max(.81, thresholds.text_score)
                and region.contains(span.box.center)
                and not any(not item.quiet_outline and _association_score(span.box, item.box, unit) is not None
                            for item in containers)]
    anchors = content_anchors(tuple(item for item in containers if not item.quiet_outline and
                                   any(_association_score(span.box, item.box, unit) is not None for span in spans
                                       if span not in contextual)), region, pixels.shape[1], {}, thresholds)
    recovered, watermarks = [], set()
    for span in orphaned[:48]:
        # Determine direction from the recovered rectangle, never from ink alone.
        # If no established lane exists, require a normal margin from the viewport.
        trials = candidates
        for attempt in range(2):
            matched = []
            for item in trials:
                box = item.box
                near_left = 9 * unit <= box.left - region.left <= 88 * unit
                near_right = 9 * unit <= region.right - box.right <= 88 * unit
                direction = direction_evidence(box, region, anchors, thresholds)[0]
                if (item.clipped or not _in_message_lane(box, region, thresholds)
                        or not (direction != "UNKNOWN" or near_left != near_right)
                        or _association_score(span.box, box, unit) is None
                        or not _fill_between(span.box, box, pixels, background, unit)):
                    continue
                if not any(_overlaps(box, old.box) for old in (*containers, *recovered)):
                    matched.append(item)
            if len(matched) == 1:
                recovered.append(replace(matched[0], source=("C2 形态去细笔画与正文反向复核" if not attempt
                                                            else "C2 正文旁局部连续填充"),
                                         method="MERGED_EVIDENCE" if not attempt else "OCR_REVERSE"))
                break
            if matched or attempt or not _in_message_lane(span.box, region, thresholds):
                break
            trials = _local_fill_boxes(pixels, span.box, region, background, unit)
        if (not any(_association_score(span.box, item.box, unit) is not None for item in recovered)
                and _watermark_like(span, span.box.crop(pixels), background,
                                    sum(other.text == span.text for other in orphaned))):
            watermarks.add(span)
    if not capture_masks:
        return (*containers, *recovered), watermarks, ()
    close = max(2, round(2 * unit))
    processed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
    return (*containers, *recovered), watermarks, (raw * 255, processed * 255)


def _emoji_only(value):
    return bool(value) and all(char.isspace() or ord(char) in (0x200D, 0xFE0F)
                               or 0x1F000 <= ord(char) <= 0x1FAFF or 0x2600 <= ord(char) <= 0x27BF
                               for char in value)


def _color_support(pixels, box, region):
    sample = box.crop(pixels)[::2, ::2].astype(np.int16)
    probe = Box(max(region.left, region.center[0] - 24), box.top,
                min(region.right, region.center[0] + 24), box.bottom)
    nearby = probe.crop(pixels)[::2, ::2].astype(np.int16)
    if not sample.size or not nearby.size:
        return 0., 0., 0.
    color = np.median(sample, axis=(0, 1))
    ambient = np.median(nearby, axis=(0, 1))
    contrast = float(np.max(np.abs(color - ambient)))
    fill = float(np.mean(np.max(np.abs(sample - color), axis=2) <= 5))
    foreground = float(np.mean(np.max(np.abs(sample - ambient), axis=2) >= 8))
    return contrast, fill, foreground


def _top_fragment(pixels, span, region, unit, thresholds):
    box = span.box
    if box.top > region.top + round(3 * unit) or not _in_message_lane(box, region, thresholds):
        return False
    at_edge = replace(box, top=region.top, bottom=min(region.bottom, box.bottom + round(unit)))
    contrast, fill, _ = _color_support(pixels, at_edge, region)
    # The viewport header can occupy several pixels before the data crop.
    # Require a coherent, distinct fill under the OCR fragment at the edge.
    return contrast >= 8 and fill >= .44


def analyze_messages(pixels: np.ndarray, region: Box, containers: tuple[Container, ...],
                     spans: tuple[TextSpan, ...], unit: float,
                     calibration: Mapping[str, NormalizedRect],
                     thresholds: VisualThresholds, *, page_verified: bool = True, capture_masks=False) -> MessageAnalysis:
    center = region.center[0]
    time_like = {span for span in spans if span.score >= thresholds.text_score
                 and abs(span.box.center[0] - center) <= region.width * .12
                 and is_time_label(span.text)}
    centered_system = {span for span in spans if span.score >= thresholds.text_score
                       and abs(span.box.center[0] - center) <= region.width * .12 and _SYSTEM.search(span.text)}
    watermarks, masks = set(), ()
    if page_verified and spans:
        containers, watermarks, masks = _recover_bubbles(pixels, region, containers, spans,
                                                        time_like | centered_system, unit, thresholds, capture_masks)
    attachment_spans = tuple(span for span in spans if span not in time_like)
    preliminary = _file_candidates(containers, attachment_spans, unit, thresholds.text_score) if page_verified else {}
    # Strong outer bounds own inner icon contours. Competing card-sized boxes
    # remain separate so the normal association check can flag ambiguous OCR.
    containers = tuple(sorted((item for item in containers if item in preliminary or not any(
        item.box.width * item.box.height <= card.box.width * card.box.height * .62
        and _inside(item.box, card.box) for card in preliminary)),
        key=lambda item: (item.box.top, item.box.left)))
    dispositions = []
    grouped: dict[int, list[int]] = {}
    times = []
    for span in spans:
        ranked = sorted(((score, index) for index, item in enumerate(containers, 1)
                         if not item.quiet_outline or item in preliminary
                         if (score := _association_score(span.box, item.box, unit)) is not None),
                        reverse=True)
        nearest = min(range(1, len(containers) + 1),
                      key=lambda i: (_distance(span.box, containers[i-1].box),
                                     abs(span.box.center[1] - containers[i-1].box.center[1])),
                      default=None)
        centered = abs(span.box.center[0] - center) <= region.width * .12
        side_supported = any(_in_message_lane(containers[i-1].box, region, thresholds)
                             and (containers[i-1] not in preliminary or _inside(span.box, containers[i-1].box))
                             for _, i in ranked)
        if span in time_like and not any(containers[i-1] not in preliminary
                                         and _in_message_lane(containers[i-1].box, region, thresholds)
                                         for _, i in ranked):
            times.append(span)
            dispositions.append(OcrDisposition(span, "TIME", None, nearest, "居中时间标签"))
            continue
        if centered and not side_supported and span.score >= thresholds.text_score:
            if _SYSTEM.search(span.text):
                dispositions.append(OcrDisposition(span, "SYSTEM", None, nearest, "居中系统提示"))
                continue
        if span in watermarks and not side_supported:
            dispositions.append(OcrDisposition(span, "WATERMARK", None, nearest, "浅色稀疏笔画且缺少气泡填充"))
            continue
        if not page_verified:
            reason = "消息页证据不足"
        elif not ranked:
            reason = ("最近候选框未达到空间包含、重叠或边距要求" if nearest else "C2 未找到消息框")
        elif len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .12:
            reason = "多个候选框重叠，无法唯一归属"
        else:
            index = ranked[0][1]
            grouped.setdefault(index, []).append(len(dispositions))
            dispositions.append(OcrDisposition(span, "UNASSIGNED", None, index,
                                               "对应容器未通过消息分类", True))
            continue
        if page_verified and _top_fragment(pixels, span, region, unit, thresholds):
            dispositions.append(OcrDisposition(span, "TOP_VIEWPORT_CLIPPED", None, nearest,
                                               "文字贴近上边界且具有连续的消息填充"))
        else:
            dispositions.append(OcrDisposition(span, "UNASSIGNED", None, nearest, reason, True))
    eligible = tuple(item for i, item in enumerate(containers, 1)
                     if (not item.needs_text or i in grouped) and _in_message_lane(item.box, region, thresholds)
                     and (not item.quiet_outline or item in preliminary))
    anchors = content_anchors(eligible, region, pixels.shape[1], calibration, thresholds)
    results = []
    diagnostics = []
    for index, item in enumerate(containers, 1):
        box = item.box
        detail = {"width": box.width, "height": box.height, "ocr_count": len(grouped.get(index, ())),
                  "classification": "INSUFFICIENT_EVIDENCE", "reason": "PAGE_OR_CONTAINER_EVIDENCE_INSUFFICIENT"}
        diagnostics.append(detail)
        if (not page_verified or (item.quiet_outline and item not in preliminary)
                or (item.needs_text and index not in grouped and item not in preliminary)
                or (not _in_message_lane(box, region, thresholds) and not item.clipped and item not in preliminary)):
            continue
        positions = grouped.get(index, ())
        matching = tuple(dispositions[i].span for i in positions)
        text = "\n".join(text_lines(matching))
        direction, left_dist, right_dist = direction_evidence(box, region, anchors, thresholds)
        evidence = [item.source, "按容器边缘与左右内容锚点对齐判定方向"]
        if "left_anchor" in calibration or "right_anchor" in calibration:
            evidence.append("包含人工标定锚点")
        else:
            evidence.append("从当前画面容器边缘推断候选锚点")
        kind = "UNKNOWN"
        weak = not matching or min(span.score for span in matching) < thresholds.text_score
        file = parse_file_card(box, matching, unit, thresholds.text_score, clipped=item.clipped)
        if file is None and item in preliminary:
            file = replace(preliminary[item], file_name=None, file_extension=None, file_size_text=None,
                           parse_status="INCOMPLETE", failure_reasons=("文件卡片 OCR 未能唯一归属",))
        photo_shape = box.height >= 48 * unit and box.width >= 54 * unit
        if file is not None:
            kind = "FILE" if file.parse_status == "COMPLETE" else "FILE_CANDIDATE"
            evidence.extend(file.failure_reasons or ("卡片内文件名和大小已读取，附件内容未读取",))
        elif photo_shape and _IMAGE_PLACEHOLDER.fullmatch(text):
            kind = "IMAGE_PLACEHOLDER"
            evidence.append("仅识别到图片占位，真实图片内容未取得")
        elif photo_shape and _has_photo_texture(box.crop(pixels), thresholds):
            kind = "IMAGE"
            evidence.append("带边界的彩色纹理区域，图片内容未读取")
        elif _COMPLEX_CARD.search(text) or _has_quote_border(pixels, box, matching, unit):
            kind = "COMPLEX_CANDIDATE"
            evidence.append("疑似引用、转发或其他复杂卡片，正文边界未确认")
        elif not weak and text:
            kind = "EMOJI" if _emoji_only(text) else "TEXT"
        contrast, fill, foreground = _color_support(pixels, box, region)
        detail.update(fill_contrast=round(contrast, 2), fill_fraction=round(fill, 3),
                      foreground_fraction=round(foreground, 3), direction=direction,
                      media_structure=kind if kind.startswith(("IMAGE", "FILE")) else "")
        if (kind == "UNKNOWN" and not matching and direction == "UNKNOWN" and not item.clipped
                and box.height >= box.width * 1.65 and contrast < 7 and foreground < .30):
            detail.update(classification="BACKGROUND_NOISE", reason="UNANCHORED_SPARSE_ELONGATED_CONTOUR")
            continue
        viewport_limit = ""
        if item.clipped:
            if box.bottom >= region.bottom - round(2 * unit):
                viewport_limit = "BOTTOM_VIEWPORT_CLIPPED"
            elif box.top <= region.top + round(2 * unit) and (
                    (contrast >= 8 and fill >= .44) or any(_top_fragment(pixels, span, region, unit, thresholds)
                                                          for span in matching)):
                viewport_limit = "TOP_VIEWPORT_CLIPPED"
            direction = "UNKNOWN"
            if file is None:
                kind = "UNKNOWN"
            status = "PARTIALLY_VISIBLE"
            evidence.append("容器接触视口边缘，文字可能截断")
        elif kind == "UNKNOWN" and weak:
            status = "OCR_UNCERTAIN"
        elif direction == "UNKNOWN" or kind not in ("TEXT", "IMAGE", "FILE", "EMOJI"):
            status = "UNCLASSIFIED"
        else:
            status = "CANDIDATE"
        detail.update(classification=viewport_limit or (kind if status == "CANDIDATE" else
                      "UNKNOWN_BUBBLE" if contrast >= 8 and fill >= .44 else "SUSPECTED_MESSAGE"),
                      reason=status)
        preceding = [span for span in times if span.box.bottom <= box.top]
        time = max(preceding, key=lambda span: span.box.bottom).text if preceding else ""
        if time:
            evidence.append("时间仅来自上方可见分隔标签")
        display_text = "" if kind == "IMAGE" else text
        if file is not None:
            display_text = file.file_name or "\n".join(file.name_rows)
        results.append(VisibleMessageObservation(len(results) + 1, box, direction, kind,
                                                 display_text, time, left_dist, right_dist,
                                                 status, tuple(evidence), matching, file=file, method=item.method,
                                                 viewport_limit=viewport_limit))
        for pos in positions:
            span = dispositions[pos].span
            if viewport_limit == "TOP_VIEWPORT_CLIPPED":
                dispositions[pos] = OcrDisposition(span, viewport_limit, index, index, "顶部气泡经视口裁切")
            elif file is not None:
                reason = ("已与文件卡片图标区关联" if span in file.icon_evidence else "已与文件卡片字段关联")
                dispositions[pos] = OcrDisposition(span, "MEDIA", index, index, reason)
            elif kind == "IMAGE":
                dispositions[pos] = OcrDisposition(span, "MEDIA", index, index, "已与媒体整体卡片关联")
            elif kind in ("TEXT", "EMOJI"):
                dispositions[pos] = OcrDisposition(span, "MESSAGE", index, index, "与唯一正文容器关联")
            else:
                reason = ("容器触及视口，正文可能截断" if item.clipped else
                          "OCR 置信度不足或容器类型未确认")
                dispositions[pos] = OcrDisposition(span, "UNASSIGNED", None, index, reason, True)
    return MessageAnalysis(tuple(results), tuple(dispositions), anchors, containers, masks, tuple(diagnostics))


def classify_messages(pixels: np.ndarray, region: Box, containers: tuple[Container, ...],
                      spans: tuple[TextSpan, ...], unit: float,
                      calibration: Mapping[str, NormalizedRect],
                      thresholds: VisualThresholds) -> tuple[tuple[VisibleMessageObservation, ...], tuple[TextSpan, ...]]:
    result = analyze_messages(pixels, region, containers, spans, unit, calibration, thresholds)
    return result.messages, result.context
