"""Observe only contact rows inside the B2 region."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box, VisualThresholds, contours, near_background
from autoim.wecom.visual_ocr import TextRecognizer, TextSpan, text_lines


_PERSONAL_LABELS = {"微信", "微信联系人"}
_ORGANIZATION_LABEL = re.compile(
    r"(?:公司|集团|企业|制品厂|工厂|部门|中心|事业部|研究院|事务所|工作室|Corporation|Company|Inc\.?|Ltd\.?)$", re.I)


@dataclass(frozen=True, slots=True)
class VisualName:
    display_name: str
    labels: tuple[str, ...] = ()
    peer_type: str = "UNKNOWN"
    issue: str = ""


def parse_visual_name(raw: str, *, labels_from_title: tuple[str, ...] = ()) -> VisualName:
    """Separate only recognized PC identity labels from reliable name text."""
    name, *inline = raw.replace("＠", "@").split("@")
    labels = tuple(tag.strip() for tag in inline) + labels_from_title
    if not labels:
        return VisualName(raw.strip())
    tags = tuple(unicodedata.normalize("NFKC", tag) for tag in labels)
    personal = tuple(tag for tag in tags if "".join(tag.split()) in _PERSONAL_LABELS)
    if not name.strip() or not all(tags):
        return VisualName(raw, labels, issue="名称或身份标签不完整")
    if personal and len(personal) != len(tags):
        return VisualName(raw, labels, issue="个人微信与其他身份标签冲突")
    if personal:
        return VisualName(name.strip(), labels, "PERSONAL_WECHAT")
    if (len(tags) == 1 and _ORGANIZATION_LABEL.search(tags[0]) and any(char.isalpha() for char in tags[0])
            and not re.search(r"[|…]|\.\.\.", tags[0])):
        return VisualName(name.strip(), labels, "EXTERNAL_WECOM")
    return VisualName(raw, labels, issue="无法可靠分离身份标签")


ACTIVITY_TIME = re.compile(r"(?:[0-9]+\s*分钟前|(?:[01]?[0-9]|2[0-3]):[0-5][0-9]|"
                           r"(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12][0-9]|3[01])|昨天|星期[一二三四五六日天])")


@dataclass(frozen=True, slots=True)
class ContactObservation:
    sequence: int
    box: Box
    avatar: Box | None
    display_name: str
    preview: str
    has_unread_indicator: bool
    unread_count: int | None
    unread_count_reliable: bool
    badge: Box | None
    is_muted: bool | None
    activity_text: str
    activity_time_reliable: bool
    is_selected: bool | None
    status: str
    evidence: tuple[str, ...]
    name_reliable: bool = False
    kind: str = "CONTACT"
    raw_name: str = ""
    identity_labels: tuple[str, ...] = ()
    peer_type: str = "UNKNOWN"
    name_issue: str = ""


@dataclass(frozen=True, slots=True)
class ContactTextDiagnostic:
    span: TextSpan
    reason: str


@dataclass(frozen=True, slots=True)
class GlobalUnreadObservation:
    entry: Box | None = None
    badge: Box | None = None
    global_unread_visible: bool | None = None
    global_unread_count: int | None = None
    global_unread_count_reliable: bool = False
    evidence: str = "缺少 A 区或消息入口证据"


def _avatars(pixels: np.ndarray, region: Box, unit: float) -> list[Box]:
    tile = region.crop(pixels)
    lane_end = round(region.width * .29)
    lane = tile[:, :lane_end].astype(np.int16)
    # Sample the left padding per scan line so a selected row's fill is
    # background, while the entire avatar remains a foreground candidate.
    padding = max(2, round(5 * unit))
    reference = np.median(lane[:, :padding], axis=1, keepdims=True)
    mask = (np.max(np.abs(lane - reference), axis=2) >= 19).astype(np.uint8)
    kernel = max(3, round(3 * unit))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((kernel, kernel), np.uint8))
    size_min, size_max = max(10, round(19 * unit)), round(67 * unit)
    avatars = []
    for box in contours(mask):
        if not (size_min <= box.width <= size_max and box.right < lane_end):
            continue
        if box.top <= 1 or box.bottom >= region.height - 1:
            if .35 * size_min <= box.height <= size_max and box.crop(mask).mean() >= .60:
                avatars.append(box.offset(region.left, region.top))
            continue
        if not (size_min <= box.height <= size_max and .67 <= box.width / box.height <= 1.5):
            continue
        lower = mask[box.center[1]:box.bottom, box.left:box.right]
        columns = np.flatnonzero(lower.mean(axis=0) >= .60)
        if len(columns) < size_min:
            continue
        left, right = box.left + int(columns[0]), box.left + int(columns[-1]) + 1
        # The lower silhouette estimates the avatar independently of a badge
        # that can enlarge the connected component at its upper right.
        avatar = Box(left, max(box.top, box.bottom - (right - left)), right, box.bottom)
        avatars.append(avatar.offset(region.left, region.top))
    return sorted(avatars, key=lambda box: box.top)


def _badge(pixels: np.ndarray, avatar: Box, row: Box, unit: float,
           thresholds: VisualThresholds) -> Box | None:
    if avatar.width <= 0:
        return None
    roi = Box(max(row.left, avatar.right - round(avatar.width * .63)),
              max(row.top, avatar.top - round(avatar.height * .36)),
              min(row.right, avatar.right + round(avatar.width * .55)),
              min(row.bottom, avatar.top + round(avatar.height * .48)))
    tile = roi.crop(pixels).astype(np.int16)
    blue, green, red = tile.transpose(2, 0, 1)
    mask = (red >= 143) & (red - np.maximum(blue, green) >= thresholds.badge_red_delta)
    hits = []
    for local in contours(mask):
        part = local.crop(mask)
        bounds = local.offset(roi.left, roi.top)
        if (max(4, round(4 * unit)) <= local.height <= avatar.height * .53
                and .72 <= local.width / local.height <= 2.8 and np.mean(part) >= .45
                and abs(bounds.center[0] - avatar.right) <= avatar.width * .32
                and abs(bounds.center[1] - avatar.top) <= avatar.height * .28
                and (bounds.right > avatar.right or bounds.top < avatar.top)
                and local.left > 0 and local.top > 0
                and local.right < roi.width and local.bottom < roi.height):
            hits.append(bounds)
    return hits[0] if len(hits) == 1 else None


def _badge_number(pixels: np.ndarray, box: Box, reader: TextRecognizer,
                  thresholds: VisualThresholds) -> tuple[int | None, bool]:
    # A solid dot offers no white ink to read. Ignore the outside ring.
    ink = Box(box.left + max(1, box.width // 5), box.top + max(1, box.height // 5),
              box.right - max(1, box.width // 5), box.bottom - max(1, box.height // 5))
    if ink.width < 1 or ink.height < 1 or not np.any(np.min(ink.crop(pixels), axis=2) >= 216):
        return None, False
    spans = reader.read(pixels, box, single_line=True)
    if len(spans) == 1 and spans[0].score >= max(.82, thresholds.text_score):
        text = spans[0].text.strip()
        if re.fullmatch(r"[1-9][0-9]{0,3}", text):
            return int(text), True
    return None, False


def detect_global_unread(pixels: np.ndarray, area: Box, reader: TextRecognizer,
                         thresholds: VisualThresholds) -> GlobalUnreadObservation:
    spans = reader.read(pixels, area)
    labels = [span for span in spans if re.fullmatch(r"消息(?:\s*[1-9][0-9]{0,3})?", span.text.strip())
              and span.score >= thresholds.text_score]
    if len(labels) != 1:
        return GlobalUnreadObservation(evidence=f"A 区“消息”入口 OCR 候选 {len(labels)} 个，无法唯一定位")
    label = labels[0]
    entry = label.box
    merged = label.text.strip() != "消息"
    start = entry.left + entry.width // 2 if merged else entry.right
    end = min(area.right, entry.right + entry.height * 10)
    if end <= start:
        return GlobalUnreadObservation(entry, evidence="消息入口右侧超出 A 区")
    roi = Box(start, max(area.top, entry.top - entry.height // 2),
              end, min(area.bottom, entry.bottom + entry.height // 2))
    tile = roi.crop(pixels).astype(np.int16)
    blue, green, red = tile.transpose(2, 0, 1)
    mask = (red >= 143) & (red - np.maximum(blue, green) >= thresholds.badge_red_delta)
    hits = [box.offset(roi.left, roi.top) for box in contours(mask)
            if entry.height * .35 <= box.height <= entry.height * 1.65
            and .7 <= box.width / box.height <= 3.5 and box.bottom < roi.height
            and box.top > 0 and box.crop(mask).mean() >= .44]
    if len(hits) != 1:
        return GlobalUnreadObservation(entry, global_unread_visible=False if not hits else None,
                                       evidence=f"消息入口右侧红色徽标候选 {len(hits)} 个")
    badge = hits[0]
    value, reliable = _badge_number(pixels, badge, reader, thresholds)
    if not reliable and merged and re.fullmatch(r"消息\s*[1-9][0-9]{0,3}", label.text.strip()):
        value = int(re.search(r"[0-9]+$", label.text.strip()).group())
        reliable = label.score >= max(.82, thresholds.text_score)
    return GlobalUnreadObservation(entry, badge, True, value if reliable else None, reliable,
                                   "A 区消息入口文字及右侧红色徽标 · 非静音会话范围")


def _muted(pixels: np.ndarray, row: Box, unit: float) -> bool | None:
    half = row.center[1]
    # Exclude the side gutter and the lower activity time line.
    area = Box(row.left + round(row.width * .75), row.top + max(1, round(3 * unit)),
               row.right - max(3, round(8 * unit)), half)
    if min(area.width, area.height) < 3:
        return None
    mask = (~near_background(area.crop(pixels), 36)).astype(np.uint8)
    boxes = contours(mask)
    if not boxes:
        return False
    for box in boxes:
        if not (6 * unit <= box.width <= 30 * unit and 6 * unit <= box.height <= 26 * unit):
            continue
        lines = cv2.HoughLinesP(box.crop(mask).copy(), 1, np.pi / 180, max(4, round(5 * unit)),
                                minLineLength=max(4, round(6 * unit)), maxLineGap=max(1, round(2 * unit)))
        if lines is not None:
            for x1, y1, x2, y2 in lines[:, 0]:
                if (abs(y2 - y1) >= box.height * .42 and abs(x2 - x1) >= box.width * .42
                        and .45 <= abs(y2 - y1) / abs(x2 - x1) <= 2.2):
                    return True
    return None


def _row_selection(pixels: np.ndarray, rows: list[Box]) -> list[bool | None]:
    fills = []
    coverages = []
    for row in rows:
        inset = max(5, round(row.width * .06))
        strip = pixels[row.top + 2:row.bottom - 2:2, row.left + inset:row.right - inset:2]
        fill = np.median(strip, axis=(0, 1))
        fills.append(fill)
        coverages.append(float(np.mean(np.max(np.abs(strip - fill), axis=2) < 12)))
    blue = [i for i, (b, g, r) in enumerate(fills)
            if b - r >= 13 and b - g >= 4 and coverages[i] >= .60]
    if len(blue) == 1:
        return [i == blue[0] for i in range(len(rows))]
    if not blue and len(rows) >= 3:
        baseline = np.median(fills, axis=0)
        distinct = [i for i, color in enumerate(fills)
                    if np.max(np.abs(color - baseline)) >= 14 and coverages[i] >= .70]
        if len(distinct) == 1:
            return [i == distinct[0] for i in range(len(rows))]
    return [None] * len(rows)


def _highlight_row(pixels: np.ndarray, region: Box, unit: float) -> Box | None:
    inset = max(2, round(region.width * .12))
    lane = pixels[region.top:region.bottom, region.left + inset:region.right - inset:2].astype(np.int16)
    blue, green, red = lane.transpose(2, 0, 1)
    wide_fill = np.mean((blue - red >= 13) & (blue - green >= 4), axis=1) >= .60
    edges = np.flatnonzero(np.diff(np.r_[False, wide_fill, False]))
    bands = [(top, bottom) for top, bottom in zip(edges[::2], edges[1::2])
             if 29 * unit <= bottom - top <= 102 * unit]
    if len(bands) != 1:
        return None
    top, bottom = bands[0]
    return Box(region.left, region.top + int(top), region.right, region.top + int(bottom))


def _title_spans(spans: tuple[TextSpan, ...], region: Box,
                 thresholds: VisualThresholds) -> tuple[TextSpan, ...]:
    return tuple(item for item in spans
                 if region.left + region.width * .13 <= item.box.left <= region.left + region.width * .58
                 and item.score >= thresholds.text_score
                 and not ACTIVITY_TIME.fullmatch(item.text.strip())
                 and any(char.isalpha() for char in item.text))


def _text_row_seeds(positioned: list[tuple[int, Box | None, str]], title_spans: tuple[TextSpan, ...],
                    pitch: float, region: Box) -> list[tuple[int, Box | None, str]]:
    # Learn the title baseline from rows with independent geometry. Compare
    # missing slots to that baseline so previews cannot seed a second row.
    anchored = []
    for y, _, _ in positioned:
        plausible = [span for span in title_spans if y - .40 * pitch <= span.box.center[1] <= y - .04 * pitch]
        if plausible:
            anchored.append((y, min(plausible, key=lambda item: abs(item.box.center[1] - y + .18 * pitch))))
    added = []
    if anchored:
        x = float(np.median([item.box.left for _, item in anchored]))
        offset = float(np.median([y - item.box.center[1] for y, item in anchored]))
        for span in title_spans:
            sx, sy = span.box.left, span.box.center[1]
            if abs(sx - x) > region.width * .09:
                continue
            expected = min((other.box.center[1] + round((sy - other.box.center[1]) / pitch) * pitch
                            for _, other in anchored), key=lambda value: abs(value - sy))
            center = round(sy + offset)
            if (abs(expected - sy) <= .20 * pitch and region.top - pitch / 2 <= center < region.bottom + pitch / 2
                    and all(abs(center - y) >= .60 * pitch for y, _, _ in positioned + added)):
                added.append((center, None, "名称 OCR、名称列与相邻联系人行节距"))
    # With no reliable avatar/selection anchors, require repeated title
    # positions at row spacing. A single disconnected text line is diagnostic only.
    elif not positioned and len(title_spans) >= 2:
        for span in title_spans:
            y = span.box.center[1]
            corroborates = any(abs(other.box.left - span.box.left) <= region.width * .06
                               and .78 * pitch <= abs(other.box.center[1] - y) <= 1.22 * pitch
                               for other in title_spans)
            if corroborates and all(abs(y + pitch * .20 - row_y) >= pitch * .60 for row_y, _, _ in added):
                added.append((round(y + pitch * .20), None, "重复 OCR 名称列与行节距候选"))
    return added


def _contact_kind(name: str) -> str:
    if re.search(r"机器人|机器人服务|bot\b", name, re.I):
        return "BOT_CANDIDATE"
    if re.search(r"系统[通知消息]+|官方[通知消息]+|企业微信.*团队$", name):
        return "SYSTEM_CANDIDATE"
    if re.search(r"群聊|群[（(]|[（(]\d+\s*人?[）)]$", name):
        return "GROUP_CANDIDATE"
    return "CONTACT"


def detect_contacts(pixels: np.ndarray, region: Box, reader: TextRecognizer,
                    unit: float, thresholds: VisualThresholds,
                    diagnostics: list[ContactTextDiagnostic] | None = None) -> tuple[ContactObservation, ...]:
    avatars = _avatars(pixels, region, unit)
    highlight = _highlight_row(pixels, region, unit)
    positioned: list[tuple[int, Box | None, str]] = []
    for avatar in avatars:
        center = avatar.center[1]
        if avatar.top <= region.top + 1:
            center = max(region.top, avatar.bottom - avatar.width // 2)
        elif avatar.bottom >= region.bottom - 1:
            center = min(region.bottom - 1, avatar.top + avatar.width // 2)
        positioned.append((center, avatar, "B2 头像轮廓与相邻行几何"))
    if highlight and not any(highlight.top <= y < highlight.bottom for y, _, _ in positioned):
        positioned.append((highlight.center[1], None, "独立行高亮与相邻行几何；头像未定位"))
    positioned.sort(key=lambda item: item[0])
    # Row height comes from neighboring avatars, capped to keep sparse or
    # partly visible rows from taking ownership of a neighboring line.
    centers = [y for y, _, _ in positioned]
    spacing = np.diff(centers)
    normal_spacing = [int(value) for value in spacing if 33 * unit <= value <= 93 * unit]
    pitch = float(np.median(normal_spacing)) if normal_spacing else 62 * unit
    spans = reader.read(pixels, region)
    positioned.extend(_text_row_seeds(positioned, _title_spans(spans, region, thresholds), pitch, region))
    positioned.sort(key=lambda item: item[0])
    centers = [y for y, _, _ in positioned]
    rows = []
    for i, (y, _, _) in enumerate(positioned):
        top = max(region.top, round(y - pitch / 2))
        bottom = min(region.bottom, round(y + pitch / 2))
        if i:
            top = max(top, (y + centers[i - 1] + 1) // 2)
        if i + 1 < len(centers):
            bottom = min(bottom, (y + centers[i + 1] + 1) // 2)
        rows.append(Box(region.left, top, region.right, bottom))
    selected = _row_selection(pixels, rows)
    if highlight:
        matched = [i for i, y in enumerate(centers) if highlight.top <= y < highlight.bottom]
        if len(matched) == 1:
            selected = [i == matched[0] for i in range(len(rows))]
    results = []
    used = set()
    for i, (row, (y, avatar, source)) in enumerate(zip(rows, positioned)):
        text_left = (min(avatar.right + max(2, round(3 * unit)), row.left + round(row.width * .26))
                     if avatar else row.left + round(row.width * .13))
        right_column = row.left + round(row.width * .63)
        name: list[TextSpan] = []
        preview: list[TextSpan] = []
        activities: list[TextSpan] = []
        for span in spans:
            _, sy = span.box.center
            if not row.top <= sy < row.bottom or span.box.left < text_left:
                continue
            if sy >= y and span.box.left >= right_column:
                activities.append(span)
            elif not (sy < y and span.box.left >= row.left + round(row.width * .8)):
                (name if sy < y else preview).append(span)
        activity = activities[0] if len(activities) == 1 else None
        badge = _badge(pixels, avatar, row, unit, thresholds) if avatar else None
        count, reliable = _badge_number(pixels, badge, reader, thresholds) if badge else (None, False)
        muted = _muted(pixels, row, unit)
        good_names = tuple(item for item in name if item.score >= thresholds.text_score)
        name_text = " ".join(text_lines(good_names))
        raw_name = " ".join(text_lines(tuple(name)))
        parsed_name = parse_visual_name(name_text)
        used.update(good_names)
        used.update(preview)
        if activity:
            used.add(activity)
        tolerance = max(2, round(2 * unit))
        partial = (y - pitch / 2 < region.top - tolerance or y + pitch / 2 > region.bottom + tolerance
                   or bool(avatar and (avatar.top <= region.top + 1 or avatar.bottom >= region.bottom - 1))
                   or any(item.box.top <= region.top or item.box.bottom >= region.bottom for item in good_names))
        reliable_name = (bool(name_text) and len(good_names) == len(name) and not parsed_name.issue
                         and "…" not in name_text and "..." not in name_text
                         and all(item.box.top > region.top and item.box.bottom < region.bottom
                                 and item.box.right < region.right - tolerance for item in good_names))
        status = "PARTIALLY_VISIBLE" if partial else "CANDIDATE" if reliable_name else "NAME_UNCERTAIN"
        evidence = [source]
        if partial:
            evidence.append("联系人行触及 B2 视口，字段可能截断")
        if not reliable_name:
            evidence.append(parsed_name.issue or "名称不完整或缺少可靠 OCR")
        if not preview:
            evidence.append("未识别预览字段")
        if badge:
            evidence.append("头像右上紧凑红色徽标候选")
        if muted:
            evidence.append("右上图标轮廓含斜线")
        if selected[i]:
            evidence.append("与其他行可区分的背景高亮")
        results.append(ContactObservation(i + 1, row, avatar, parsed_name.display_name, "\n".join(text_lines(tuple(preview))),
                                          bool(badge), count, reliable, badge, muted,
                                          activity.text if activity else "",
                                          bool(activity and activity.score >= thresholds.text_score
                                               and ACTIVITY_TIME.fullmatch(activity.text.strip())), selected[i],
                                          status, tuple(evidence), reliable_name, _contact_kind(parsed_name.display_name),
                                          raw_name=raw_name, identity_labels=parsed_name.labels,
                                          peer_type=parsed_name.peer_type if reliable_name else "UNKNOWN",
                                          name_issue=parsed_name.issue))
    if diagnostics is not None:
        for span in spans:
            if span not in used:
                reason = ("低置信文字未用于确认名称" if span.score < thresholds.text_score else
                          "文字不在可靠联系人行的名称、预览或时间位置")
                diagnostics.append(ContactTextDiagnostic(span, reason))
    return tuple(results)
