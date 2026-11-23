"""Recognize visible attachment metadata within an independently detected card."""

from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_ocr import TextSpan

_EXTENSION = re.compile(r"\.([a-z0-9]{1,12})$", re.I)
_COMMON_EXTENSIONS = frozenset(
    "pdf md markdown doc docx xls xlsx ppt pptx txt zip rar 7z csv wps ofd exe mp3 mp4 json png jpg jpeg webp".split())
_SIZE_PATTERN = r"\d+(?:[.,]\d+)?[ \t\u00a0]*(?:[KMGT](?:i?B)?|B)"
_SIZE = re.compile(_SIZE_PATTERN, re.I)
_INLINE_SIZE = re.compile(rf"(?P<name>.+\.[a-z0-9]{{1,12}})[ \t\u00a0]+(?P<size>{_SIZE_PATTERN})", re.I)
_ICON_TEXT = re.compile(r"(?:[a-z]{1,5}[ \t]*)?[↓⇩↧]|[a-z]{1,5}", re.I)


@dataclass(frozen=True, slots=True)
class FileCardObservation:
    file_name: str | None
    file_extension: str | None
    file_size_text: str | None
    card_bounds: Box
    name_rows: tuple[str, ...]
    name_ocr_blocks: tuple[TextSpan, ...]
    icon_evidence: tuple[TextSpan, ...]
    confidence: float | None
    parse_status: str
    failure_reasons: tuple[str, ...]
    content_read: bool = False


def quiet_card_bounds(pixels: np.ndarray, region: Box, unit: float) -> tuple[Box, ...]:
    """Recover large closed, low-contrast outlines before assigning any text."""
    gray = cv2.cvtColor(region.crop(pixels), cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 3, 8)
    outlines, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for outline in outlines:
        x, y, width, height = cv2.boundingRect(outline)
        if (not 115 * unit <= width <= region.width * .70
                or not 39 * unit <= height <= 180 * unit
                or not 1.20 <= width / height <= 8
                or cv2.contourArea(outline) < width * height * .78):
            continue
        boxes.append(Box(x, y, x + width, y + height).offset(region.left, region.top))
    return tuple(boxes)


def _rows(spans: tuple[TextSpan, ...]) -> list[list[TextSpan]]:
    rows: list[list[TextSpan]] = []
    for span in sorted(spans, key=lambda entry: (entry.box.center[1], entry.box.left)):
        first = rows[-1][0] if rows else None
        if first and abs(span.box.center[1] - first.box.center[1]) <= .42 * max(first.box.height, span.box.height):
            rows[-1].append(span)
        else:
            rows.append([span])
    return [sorted(row, key=lambda entry: entry.box.left) for row in rows]


def _row_text(row: list[TextSpan], unit: float) -> str:
    parts = [row[0].text]
    for left, right in zip(row, row[1:]):
        gap = right.box.left - left.box.right
        if gap > max(2 * unit, min(left.box.height, right.box.height) * .28):
            if not left.text[-1:].isspace() and not right.text[:1].isspace():
                parts.append(" ")
        parts.append(right.text)
    return "".join(parts)


def parse_file_card(box: Box, spans: tuple[TextSpan, ...], unit: float, score_floor: float, *,
                    clipped: bool = False) -> FileCardObservation | None:
    if box.height < (22 if clipped else 39) * unit or box.width < 78 * unit or not spans:
        return None
    sizes = [span for span in spans if _SIZE.fullmatch(span.text.strip())]
    named = [span for span in spans if _EXTENSION.search(span.text.strip()) or _INLINE_SIZE.fullmatch(span.text.strip())]
    column = min(sizes or named, key=lambda span: span.box.left, default=None)
    icons = tuple(span for span in spans if column and _ICON_TEXT.fullmatch(span.text.strip())
                  and abs(span.box.left - column.box.left) >= box.width * .24
                  and (span.box.center[0] >= box.left + box.width * .72
                       or span.box.center[0] <= box.left + box.width * .20))

    rows = _rows(tuple(span for span in spans if span not in icons and span not in sizes))
    names = tuple(_row_text(row, unit) for row in rows)
    blocks = tuple(span for row in rows for span in row)
    inline = _INLINE_SIZE.fullmatch(names[0].strip()) if len(names) == 1 and not sizes else None
    if inline:
        names = (inline["name"],)
    joined = "".join(names).strip()
    extension = _EXTENSION.search(joined)
    file_signal = (extension and (extension[1].casefold() in _COMMON_EXTENSIONS or sizes or inline or icons))
    if not (file_signal or (sizes and icons)):
        return None

    reasons = []
    if clipped:
        reasons.append("文件卡片触及视口边缘，名称可能被截断")
    if not blocks or not extension:
        reasons.append("缺少可核对的完整文件名或扩展名")
    if re.search(r"…|\.{3,}|�", joined):
        reasons.append("文件名包含省略或缺字标记")
    if any(span.score < score_floor for span in blocks):
        reasons.append("文件名 OCR 置信度不足")
    for first, second in zip(rows, rows[1:]):
        height = max(first[0].box.height, second[0].box.height)
        if (abs(first[0].box.left - second[0].box.left) > max(6 * unit, height * .55)
                or second[0].box.top - first[0].box.bottom > max(16 * unit, height * 1.1)):
            reasons.append("文件名各行未形成同列且连续的布局")
            break
    if any(_EXTENSION.search(name.strip()) for name in names[:-1]):
        reasons.append("卡片内存在多个带扩展名的名称，无法确定归属")
    if any(right.box.left - left.box.right > max(14 * unit, left.box.height)
           for row in rows for left, right in zip(row, row[1:])):
        reasons.append("同一行存在相距过远的 OCR 块，名称归属不明")
    if sizes and rows and (
            abs(sizes[0].box.left - rows[0][0].box.left) > max(8 * unit, sizes[0].box.height * .65)
            or sizes[0].box.center[1] <= rows[-1][0].box.center[1]
            or sizes[0].box.top - rows[-1][0].box.bottom > max(24 * unit, sizes[0].box.height * 1.55)):
        reasons.append("文件大小与名称不在同列连续布局中")
    name = joined if not reasons else None

    size = inline["size"] if inline else sizes[0].text.strip() if len(sizes) == 1 else None
    if not size or any(span.score < score_floor for span in sizes):
        reasons.append("疑似文件卡片，文件名或大小证据不足")
        size = None
    confidence = min((span.score for span in (*blocks, *sizes)), default=None)
    return FileCardObservation(name, extension[1] if name and extension else None, size, box, names, blocks,
                               icons, confidence, "INCOMPLETE" if reasons else "COMPLETE", tuple(reasons))
