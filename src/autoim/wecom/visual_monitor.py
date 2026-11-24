"""Bounded, read-only monitoring of the currently visible WeCom conversation."""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from time import monotonic, perf_counter
from typing import Callable

import cv2
import numpy as np

from autoim.calibration import CalibrationStore
from autoim.wecom.visual_capture import CaptureError, VisualFrame, capture_visible_window
from autoim.wecom.visual_layout import Box, VisualThresholds
from autoim.wecom.visual_messages import OcrDisposition, VisibleMessageObservation
from autoim.wecom.visual_observation import ConversationObservation, VisualObservation, observe_frame
from autoim.wecom.visual_ocr import LocalTextRecognizer, OcrUnavailable, TextSpan

_CANDIDATE_LIMIT = 100
_EVENT_LIMIT = 80
_OBSERVATION_LIMIT = 12
_TRUSTED_KINDS = {"TEXT", "IMAGE", "FILE"}
_WINDOW_ERRORS = {"WINDOW_UNAVAILABLE", "WINDOW_CLOSED", "WINDOW_MINIMIZED", "NOT_MAXIMIZED",
                  "WINDOW_CHANGED", "WINDOW_STATE_UNCERTAIN"}


def _key(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split()).casefold()


def _text_key(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def _thumbnail(pixels: np.ndarray, box: Box, size: tuple[int, int]) -> np.ndarray:
    return cv2.resize(box.crop(pixels), size, interpolation=cv2.INTER_AREA)


def _different(first: np.ndarray, second: np.ndarray, *, fraction: float = .0035,
               tolerance: int = 12, localized: bool = False) -> bool:
    if first.shape != second.shape:
        return True
    changed = np.max(cv2.absdiff(first, second), axis=2) >= tolerance
    if float(np.mean(changed)) >= fraction:
        return True
    if localized and np.count_nonzero(changed) >= 18:
        density = cv2.boxFilter(changed.astype(np.uint8), cv2.CV_16U, (7, 7), normalize=False)
        return bool(density.max() >= 14)
    return False


def _window_key(frame: VisualFrame) -> tuple:
    capture = frame.capture
    return capture.pid, capture.hwnd, capture.rect, capture.width, capture.height, frame.state.dpi


@dataclass(frozen=True, slots=True)
class FrameProbe:
    history: np.ndarray
    header: np.ndarray | None = None
    list_lane: np.ndarray | None = None

    def differs(self, other: FrameProbe) -> bool:
        return (_different(self.history, other.history, localized=True)
                or _optional_change(self.header, other.header, .007)
                or _optional_change(self.list_lane, other.list_lane, .009))


def _optional_change(first: np.ndarray | None, second: np.ndarray | None, fraction: float) -> bool:
    if first is None or second is None:
        return (first is None) != (second is None)
    return _different(first, second, fraction=fraction)


@dataclass(frozen=True, slots=True)
class ViewRegions:
    header: Box
    list_rows: Box
    history: Box


def probe_frame(frame: VisualFrame, regions: ViewRegions | None) -> FrameProbe:
    if regions is None:
        window = Box(0, 0, frame.capture.width, frame.capture.height)
        return FrameProbe(_thumbnail(frame.pixels, window, (224, 132)))
    content = _history_content_box(regions.history, frame.state.dpi)
    header = Box(regions.header.left, regions.header.top,
                 regions.header.left + max(1, round(regions.header.width * .69)), regions.header.bottom)
    lane = Box(regions.list_rows.left, regions.list_rows.top,
               regions.list_rows.left + max(1, round(regions.list_rows.width * .20)), regions.list_rows.bottom)
    return FrameProbe(_thumbnail(frame.pixels, content, (280, 168)),
                      _thumbnail(frame.pixels, header, (144, 28)), _thumbnail(frame.pixels, lane, (32, 180)))


def _history_content_box(history: Box, dpi: int) -> Box:
    # The composer is outside C2. Ignore only the narrow scrollbar gutter.
    gutter = min(round(history.width * .035), max(4, round(dpi / 96 * 15)))
    return Box(history.left, history.top, history.right - gutter, history.bottom)


def _tail_pixels_changed(before: FrameProbe, after: FrameProbe, box: Box, content: Box) -> bool:
    if before.history.shape != after.history.shape:
        return False
    height, width = after.history.shape[:2]
    tile = Box(max(0, (box.left - content.left) * width // content.width),
               max(0, (box.top - content.top) * height // content.height),
               min(width, (box.right - content.left) * width // content.width),
               min(height, (box.bottom - content.top) * height // content.height))
    return min(tile.width, tile.height) >= 3 and _different(tile.crop(before.history), tile.crop(after.history),
                                                           fraction=.02, localized=True)


@dataclass(frozen=True, slots=True)
class MonitoredMessage:
    message: VisibleMessageObservation
    media_pixels: np.ndarray | None = None


@dataclass(frozen=True, slots=True)
class MessageGap:
    box: Box | None
    direction: str
    kind: str
    reason: str
    spans: tuple[TextSpan, ...] = ()
    possible_message: bool = True


def _trusted(item: VisibleMessageObservation) -> bool:
    return (item.status == "CANDIDATE" and item.kind in _TRUSTED_KINDS and item.direction in ("INBOUND", "OUTBOUND")
            and (item.kind == "IMAGE" or bool(item.text.strip())))


def _message_gaps(observation: VisualObservation) -> tuple[MessageGap, ...]:
    unassigned: dict[Box, list[OcrDisposition]] = {}
    for entry in observation.message_ocr:
        if not entry.possible_message:
            continue
        index = entry.nearest_container
        candidate = observation.candidates[index - 1] if index and index <= len(observation.candidates) else None
        box = (candidate.container.box if candidate and candidate.container.box.contains(entry.span.box.center)
               else entry.span.box)
        unassigned.setdefault(box, []).append(entry)

    gaps = []
    for item in observation.messages:
        entries = unassigned.pop(item.box, ())
        if _trusted(item) and not entries:
            continue
        reason = {
            "PARTIALLY_VISIBLE": "消息框被视口截断",
            "OCR_UNCERTAIN": "消息框内 OCR 缺失或置信度不足",
        }.get(item.status, "消息类型或方向证据不足")
        if item.file and item.file.failure_reasons:
            reason = "；".join(item.file.failure_reasons)
        elif item.kind in ("IMAGE_PLACEHOLDER", "COMPLEX_CANDIDATE"):
            reason = next((note for note in item.evidence if note.startswith(("疑似", "仅识别到"))), reason)
        notes = ([reason] if not _trusted(item) else []) + [entry.reason for entry in entries]
        spans = tuple(dict.fromkeys((*item.ocr_spans, *(entry.span for entry in entries))))
        gaps.append(MessageGap(item.box, item.direction, item.kind, "；".join(dict.fromkeys(notes)), spans))
    for box, entries in unassigned.items():
        gaps.append(MessageGap(box, "UNKNOWN", "UNASSIGNED_OCR",
                               "；".join(dict.fromkeys(entry.reason for entry in entries)),
                               tuple(entry.span for entry in entries)))
    localized = {gap.box for gap in gaps}
    for candidate in observation.candidates:
        container = candidate.container
        if (candidate.parsed_sequence is None and not container.needs_text
                and candidate.direction in ("INBOUND", "OUTBOUND") and container.box not in localized):
            gaps.append(MessageGap(container.box, candidate.direction, "UNCLASSIFIED_CONTAINER", candidate.reason))
    for step in observation.ocr_areas:
        if step.region.startswith("C2") and step.status == "FAILED":
            gaps.append(MessageGap(None, "UNKNOWN", "C2_OCR_FAILED", step.reason or "C2 区域 OCR 失败",
                                   possible_message=False))
    if not gaps and observation.message_completeness == "VISIBLE_MESSAGES_INCOMPLETE":
        gaps.append(MessageGap(None, "UNKNOWN", "UNLOCALIZED_GAP", "当前观察报告缺失，但无法定位具体消息",
                               possible_message=False))
    return tuple(gaps)


@dataclass(frozen=True, slots=True)
class MonitorObservation:
    at: datetime
    title: str
    selected_name: str
    match_status: str
    identity_unique: bool
    identity_hint: tuple[str, ...]
    contact_pixels: np.ndarray | None
    regions: ViewRegions | None
    window_key: tuple
    messages: tuple[MonitoredMessage, ...]
    completeness: str
    at_bottom: bool
    blocker: str = ""
    selected_row_top: int | None = None
    scroll_thumb: Box | None = None
    conversation: ConversationObservation = ConversationObservation()
    page: str = "UNKNOWN"
    selected_row_status: str = "MISSING"
    messages_reliable: bool = False
    blocker_code: str = ""
    gaps: tuple[MessageGap, ...] = ()
    context_ocr: tuple[OcrDisposition, ...] = ()
    unassigned_ocr: tuple[OcrDisposition, ...] = ()
    recognition_ms: float = 0

    @property
    def file_messages(self) -> tuple[VisibleMessageObservation, ...]:
        return tuple(item.message for item in self.messages if item.message.file is not None)

    @property
    def reliable_messages(self) -> tuple[MonitoredMessage, ...]:
        affected = {gap.box for gap in self.gaps}
        return tuple(item for item in self.messages if _trusted(item.message) and item.message.box not in affected)

    @property
    def baseline_type(self) -> str:
        if self.messages_reliable:
            return "RELIABLE"
        return "PARTIAL" if self.reliable_messages else "NONE"


def summarize(observation: VisualObservation) -> MonitorObservation:
    frame, conv = observation.frame, observation.conversation
    header, rows, history = (observation.layout.get(name) for name in ("C1", "B2", "C2"))
    regions = ViewRegions(header, rows, history) if header and rows and history else None
    selected = [row for row in observation.contacts if row.is_selected is True]
    row = selected[0] if len(selected) == 1 else observation.verified_target if not selected else None
    duplicates = sum(bool(contact.display_name and _key(contact.display_name) == _key(conv.selected_name))
                     for contact in observation.contacts)
    hint = tuple(_key(item) for item in conv.visible_identity if _key(item))
    contact_pixels = None
    if row and rows:
        # Strip the avatar's upper/right border where unread badges appear.
        # If its contour is unavailable, use the stable inner icon lane.
        avatar = row.avatar or Box(row.box.left + round(row.box.width * .025), row.box.top,
                                   row.box.left + round(row.box.width * .175), row.box.bottom)
        inner = Box(avatar.left + round(avatar.width * .14), avatar.top + round(avatar.height * .32),
                    avatar.right - round(avatar.width * .24), avatar.bottom - round(avatar.height * .12))
        if min(inner.width, inner.height) >= 4:
            contact_pixels = _thumbnail(frame.pixels, inner, (20, 20))
            if float(np.max(np.std(contact_pixels, axis=(0, 1)))) < 9:
                contact_pixels = None
    accounted_for = observation.message_completeness == "VISIBLE_MESSAGES_ACCOUNTED_FOR"
    gaps = _message_gaps(observation)
    comparable = bool(observation.messages) and not gaps and all(map(_trusted, observation.messages))
    row_status = row.status if row else "AMBIGUOUS" if selected else "MISSING"
    code, blocker = "", ""
    if regions is None:
        missing = "、".join(name for name, box in (("C1", header), ("B2", rows), ("C2", history)) if box is None)
        code, blocker = "LAYOUT_UNCERTAIN", f"消息页面缺少可用的 {missing} 区域"
    elif row is None:
        code = "SELECTED_ROW_UNCERTAIN"
        blocker = "B2 存在多个选中行候选" if selected else "B2 未识别到选中行"
    elif row_status == "PARTIALLY_VISIBLE":
        code, blocker = "SELECTED_ROW_UNCERTAIN", "B2 选中行仅部分可见"
    elif not conv.selected_name_reliable:
        code = "SELECTED_NAME_UNCERTAIN"
        blocker = "B2 选中姓名证据不足" + (f"（{row.name_issue}）" if row.name_issue else "")
    elif not conv.title_name_reliable:
        code, blocker = "TITLE_UNCERTAIN", f"C1 标题姓名无法核对（{conv.comparison_evidence}）"
    elif conv.match_status == "CONVERSATION_MISMATCH":
        code, blocker = "NAME_MISMATCH", "B2 选中姓名与 C1 标题姓名不同"
    elif conv.match_status != "VISUAL_MATCH":
        code, blocker = "CONVERSATION_UNCERTAIN", conv.comparison_evidence
    elif observation.page != "MESSAGES_CANDIDATE":
        code, blocker = "PAGE_UNCERTAIN", "无法确认当前为消息页面"
    elif duplicates > 1:
        code, blocker = "AMBIGUOUS_HOMONYMS", "B2 可见多个同名联系人，无法确认会话连续性"
    elif not accounted_for:
        code, blocker = "MESSAGES_UNRELIABLE", "C2 可见消息归属或 OCR 证据不完整"
    elif not comparable:
        code, blocker = "MESSAGES_UNRELIABLE", "C2 缺少可靠、可比较的可见消息序列"
    messages = tuple(
        MonitoredMessage(item, _thumbnail(frame.pixels, item.box, (20, 14)) if item.kind == "IMAGE" else None)
        for item in observation.messages)
    thumb = observation.history_scrollbar
    at_bottom = bool(history and thumb and history.bottom - thumb.bottom <= max(4, history.height * .045))
    return MonitorObservation(frame.captured_at, conv.title, conv.selected_name, conv.match_status,
                              conv.identity_unique, hint, contact_pixels, regions, _window_key(frame),
                              messages, observation.message_completeness, at_bottom, blocker,
                              selected_row_top=row.box.top if row else None, scroll_thumb=thumb,
                              conversation=conv, page=observation.page, selected_row_status=row_status,
                              messages_reliable=accounted_for and comparable, blocker_code=code, gaps=gaps,
                              context_ocr=tuple(entry for entry in observation.message_ocr if entry.kind in ("TIME", "SYSTEM")),
                              unassigned_ocr=tuple(entry for entry in observation.message_ocr if entry.kind == "UNASSIGNED"),
                              recognition_ms=observation.recognition_ms)


def _same_conversation(before: MonitorObservation, after: MonitorObservation) -> bool:
    prev, current = before.conversation, after.conversation
    return (before.window_key == after.window_key and before.regions == after.regions
            and before.match_status == after.match_status == "VISUAL_MATCH"
            and prev.conversation_type == current.conversation_type
            and _key(before.title) == _key(after.title) and _key(before.selected_name) == _key(after.selected_name)
            and before.identity_hint == after.identity_hint
            and prev.selected_peer_type == current.selected_peer_type
            and (prev.selected_peer_type != "EXTERNAL_WECOM" or
                 tuple(map(_key, prev.selected_identity_labels)) == tuple(map(_key, current.selected_identity_labels)))
            and (before.contact_pixels is not None or before.selected_row_top == after.selected_row_top)
            and not _optional_change(before.contact_pixels, after.contact_pixels, .18))


def _message_match(first: MonitoredMessage, second: MonitoredMessage, *, fuzzy: bool = False) -> bool:
    a, b = first.message, second.message
    if a.direction != b.direction or a.kind != b.kind or a.sender_name != b.sender_name:
        return False
    if a.kind == "IMAGE":
        return (first.media_pixels is not None and second.media_pixels is not None
                and not _different(first.media_pixels, second.media_pixels, fraction=.10, tolerance=19))
    if a.kind == "FILE" and (a.file is not None or b.file is not None):
        return (a.file is not None and b.file is not None and a.file.file_name == b.file.file_name
                and _key(a.file.file_size_text or "") == _key(b.file.file_size_text or ""))
    left, right = _text_key(a.text), _text_key(b.text)
    if left == right:
        return True
    return (fuzzy and a.kind == "TEXT" and min(len(left), len(right)) >= 12
            and SequenceMatcher(a=left, b=right, autojunk=False).ratio() >= .92)


def _motion(before: tuple[MonitoredMessage, ...], after: tuple[MonitoredMessage, ...],
            region: Box) -> float | None:
    shifts = []
    for old, new in zip(before, after):
        a, b = old.message.box, new.message.box
        side_shift = a.left - b.left if new.message.direction == "INBOUND" else a.right - b.right
        if abs(side_shift) > region.width * .045 or abs(a.height - b.height) > max(12, a.height * .28):
            return None
        shifts.append(b.top - a.top)
    median = float(np.median(shifts))
    return median if max(abs(shift - median) for shift in shifts) <= max(9, region.height * .035) else None


@dataclass(frozen=True, slots=True)
class SequenceChange:
    status: str
    reason: str
    appended: tuple[MonitoredMessage, ...] = ()
    preserve_baseline: bool = False


def _slots(observation: MonitorObservation) -> list[tuple[Box, MonitoredMessage | None]]:
    reliable = {item.message.box: item for item in observation.reliable_messages}
    boxes = {item.message.box for item in observation.messages}
    boxes.update(gap.box for gap in observation.gaps if gap.box is not None)
    return [(box, reliable.get(box)) for box in sorted(boxes, key=lambda item: (item.top, item.left))]


def _compare_partial(before: MonitorObservation, after: MonitorObservation,
                     region: Box, *, cursor_in_history: bool) -> SequenceChange:
    uncertain = "UNCONFIRMED_VISUAL_CHANGE"
    old, new = _slots(before), _slots(after)
    if len(new) < len(old):
        return SequenceChange("RESYNC_REQUIRED", "部分观察的可见位置消失，可能滚动或重新布局")
    if not old or not new:
        return SequenceChange(uncertain, "部分观察缺少可定位的旧消息或新消息")
    same_count = len(old) == len(new)
    if not same_count and any(item.scroll_thumb is not None and not item.at_bottom for item in (before, after)):
        return SequenceChange("RESYNC_REQUIRED", "可见滚动条离开底部，不能使用部分序列判断新增")
    # Match known containers in place. Unknown entries never supply a text anchor.
    old_refs, new_refs = [], []
    suffix_start = len(old)
    for index, ((_, previous), (_, current)) in enumerate(zip(old, new)):
        if previous is None or current is None:
            if not same_count:
                suffix_start = len(old)
                old_refs.clear()
                new_refs.clear()
            continue
        if not _message_match(previous, current, fuzzy=same_count):
            status = "RESYNC_REQUIRED" if cursor_in_history else uncertain
            return SequenceChange(status, "相同位置的可靠消息文字或类型不一致")
        if not old_refs:
            suffix_start = index
        old_refs.append(previous)
        new_refs.append(current)

    motion = _motion(tuple(old_refs), tuple(new_refs), region) if old_refs else None
    if motion is None:
        return SequenceChange(uncertain, "部分观察缺少可核对的尾部重叠位置")
    tolerance = max(7, region.height * .02)
    if motion > tolerance or (abs(motion) > tolerance and
                              (cursor_in_history or not (before.at_bottom and after.at_bottom))):
        return SequenceChange("RESYNC_REQUIRED", "部分观察出现整体移动，缺少连续底部证据")
    for (a, _), (b, _) in zip(old, new):
        if (abs(a.top + motion - b.top) > tolerance or abs(a.bottom + motion - b.bottom) > tolerance
                or abs(a.left - b.left) > region.width * .035 or abs(a.right - b.right) > region.width * .035):
            return SequenceChange(uncertain, "无法逐一定位旧消息或未识别位置，可能是 OCR 修复或重排")
    if same_count:
        return SequenceChange("NO_CHANGE", "旧消息位置保持一致，识别变化未形成尾部追加")
    if any(gap.box is None for gap in (*before.gaps, *after.gaps)):
        return SequenceChange(uncertain, "存在无法定位的 C2 缺失，不能核对新增区域")
    if len(old_refs) < 2 or all(_message_match(old_refs[0], item) for item in old_refs[1:]):
        return SequenceChange(uncertain, "旧序列尾部缺少两个可独立核对的可靠锚点")
    tail = tuple(item for _, item in new[len(old):] if item is not None)
    if len(tail) != len(new) - len(old):
        return SequenceChange(uncertain, "新增区域含未可靠识别的消息框或文字")
    if any(box.bottom > old[suffix_start][0].top + 4 for box, _ in old[:suffix_start]):
        return SequenceChange(uncertain, "旧消息缺失位置触及可靠尾部重叠区域")
    if any(box.bottom > new[suffix_start][0].top + 4 for box, _ in new[:suffix_start]):
        return SequenceChange(uncertain, "当前不确定位置触及可靠尾部重叠区域")

    complete_before = replace(before, messages=tuple(old_refs), gaps=(), messages_reliable=True)
    complete_after = replace(after, messages=(*new_refs, *tail), gaps=(), messages_reliable=True)
    delta = compare_sequences(complete_before, complete_after, cursor_in_history=cursor_in_history)
    if delta.status == "VISUAL_NEW_CANDIDATE":
        return replace(delta, reason="旧缺失位于尾部锚点之前，稳定位置和顺序支持独立尾部追加")
    return delta


def compare_sequences(before: MonitorObservation, after: MonitorObservation,
                      *, cursor_in_history: bool = False) -> SequenceChange:
    reset = "RESYNC_REQUIRED"
    if not _same_conversation(before, after) or after.regions is None:
        return SequenceChange(reset, "窗口、布局或会话视觉证据已变化")
    old, new = before.messages, after.messages
    region = after.regions.history
    if (before.scroll_thumb is not None and after.scroll_thumb is not None
            and abs(after.scroll_thumb.bottom - before.scroll_thumb.bottom) > max(5, region.height * .025)
            and not (before.at_bottom and after.at_bottom)):
        return SequenceChange(reset, "可见滚动条位置变化，视口连续性未知")
    if not before.messages_reliable or not after.messages_reliable:
        return _compare_partial(before, after, region, cursor_in_history=cursor_in_history)
    if not old or not new:
        return SequenceChange(reset, "缺少可比较的旧序列或新序列")
    strict = len(old) == len(new) and all(_message_match(a, b) for a, b in zip(old, new))
    if strict or (len(old) == len(new) and all(_message_match(a, b, fuzzy=True) for a, b in zip(old, new))):
        motion = _motion(old, new, region)
        if motion is None or abs(motion) > region.height * .22 or (
                cursor_in_history and abs(motion) > max(7, region.height * .02)):
            return SequenceChange(reset, "可见内容相同但视口整体移动或重新布局")
        return SequenceChange("NO_CHANGE", "序列保持一致" if strict else "轻微 OCR 波动，保留原基线",
                              preserve_baseline=not strict)

    # Accept exact suffix/prefix overlap in order, never a set of text values.
    # Shorter competing alignments are ambiguous when they also explain a shift.
    sizes = [size for size in range(1, min(len(old), len(new)) + 1)
             if all(_message_match(a, b) for a, b in zip(old[-size:], new[:size]))]
    if not sizes:
        return SequenceChange(reset, "可见序列没有可靠重叠，可能已滚动或重新布局")
    overlap = max(sizes)
    if overlap == len(new):
        return SequenceChange(reset, "底部没有追加证据，可能滚动或消息已离开视口")
    if ((before.scroll_thumb is not None and not before.at_bottom)
            or (after.scroll_thumb is not None and not after.at_bottom)):
        return SequenceChange(reset, "至少一次观察的滚动条明确离开底部，疑似进入历史")
    if overlap < min(2, len(old)) or (len(sizes) > 1 and overlap < len(old)):
        return SequenceChange(reset, "可见序列重叠太短或对应关系不唯一")
    motion = _motion(old[-overlap:], new[:overlap], region)
    if motion is None or motion > max(7, region.height * .02):
        return SequenceChange(reset, "保留消息位置重排或向下移动，疑似手动滚动")
    if cursor_in_history and abs(motion) > max(7, region.height * .02):
        return SequenceChange(reset, "历史区内有鼠标且视口移动，无法排除手工滚动")
    if before.at_bottom and not after.at_bottom and cursor_in_history:
        return SequenceChange(reset, "无法确认滚动条仍在底部")
    # If older messages left the screen, append and manual scrolling may
    # look identical. Require positive bottom-scroll evidence on both ends.
    if (overlap < len(old) or abs(motion) > max(7, region.height * .02)) and not (
            before.at_bottom and after.at_bottom):
        return SequenceChange(reset, "视口移动或旧消息离开，缺少两次底部锚定证据")
    tail = new[overlap:]
    if any(item.message.box.top < new[overlap + i - 1].message.box.bottom - 4 for i, item in enumerate(tail)):
        return SequenceChange(reset, "候选未形成独立的顺序底部追加")
    return SequenceChange("VISUAL_NEW_CANDIDATE", "两次稳定采样支持按顺序在可见序列尾部追加", tail)


@dataclass(frozen=True, slots=True)
class NewCandidate:
    first_seen: datetime
    title: str
    selected_name: str
    match_status: str
    identity_unique: bool
    message: VisibleMessageObservation
    evidence: str
    status: str = "VISUAL_NEW_CANDIDATE"
    integrity_risk: str = "仅见当前视口；消息历史完整性未知"

    @property
    def auto_reply_ready(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    at: datetime
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    status: str = "STOPPED"
    reason: str = ""
    sample_count: int = 0
    recognition_count: int = 0
    change_count: int = 0
    resync_count: int = 0
    baseline_count: int = 0
    candidate_count: int = 0
    latest_sample: datetime | None = None
    latest_ocr: datetime | None = None
    capture_ms: float = 0
    recognition_ms: float = 0
    latest_success: MonitorObservation | None = None
    latest_observation: MonitorObservation | None = None
    candidates: tuple[NewCandidate, ...] = ()
    events: tuple[MonitorEvent, ...] = ()
    stable_sample_count: int = 0
    baseline_type: str = "NONE"
    unconfirmed_count: int = 0
    latest_change: datetime | None = None
    observations: tuple[MonitorObservation, ...] = ()


class VisualMonitor:
    """One serial poll per caller. GUI scheduling is deliberately external."""

    def __init__(self, calibration_path: Path | None = None, thresholds_path: Path | None = None, *,
                 interval: float = 2.0, capture: Callable[[], VisualFrame] = capture_visible_window,
                 recognize: Callable[[VisualFrame], VisualObservation] | None = None,
                 clock: Callable[[], float] = monotonic) -> None:
        if not 1.0 <= interval <= 30.0:
            raise ValueError("采样周期必须在 1 到 30 秒之间")
        self.interval = interval
        self._capture = capture
        self._recognize = recognize
        self._clock = clock
        self._calibration_path = calibration_path
        self._thresholds_path = thresholds_path
        self._working = threading.Lock()
        self._state_lock = threading.Lock()
        self._stopped = threading.Event()
        self._stopped.set()
        self._state = MonitorSnapshot()
        self._reset()

    def _reset(self) -> None:
        self._basis: MonitorObservation | None = None
        self._regions: ViewRegions | None = None
        self._window: tuple | None = None
        self._last_probe: FrameProbe | None = None
        self._recognized_probe: FrameProbe | None = None
        self._pending: FrameProbe | None = None
        self._pending_at: datetime | None = None
        self._pending_hover = False
        self._first = True
        self._failures = 0
        self._next_sample = self._next_ocr = 0.0
        self._partial_retry_at = 0.0
        self._reader = None
        self._settings = None

    def start(self) -> bool:
        if not self._working.acquire(blocking=False):
            return False
        try:
            with self._state_lock:
                if not self._stopped.is_set():
                    return False
                self._reset()
                self._state = MonitorSnapshot("MONITORING", "正在验证窗口、会话和可见消息")
                self._stopped.clear()
            return True
        finally:
            self._working.release()

    def stop(self) -> None:
        with self._state_lock:
            self._stopped.set()
            self._state = replace(self._state, status="STOPPED", reason="监测已停止")
        if self._working.acquire(blocking=False):
            try:
                if self._stopped.is_set():
                    self._reset()
            finally:
                self._working.release()

    def snapshot(self) -> MonitorSnapshot:
        with self._state_lock:
            return self._state

    def clear_events(self) -> None:
        with self._state_lock:
            self._state = replace(self._state, events=(), candidates=(), candidate_count=0,
                                  unconfirmed_count=0, observations=())

    def next_delay(self) -> float:
        return max(self.interval, self._next_sample - self._clock())

    def _publish(self, status: str | None = None, reason: str = "", *,
                 record_observation: MonitorObservation | None = None, **changes) -> None:
        with self._state_lock:
            if self._stopped.is_set():
                return
            state = self._state
            if status is not None:
                changes.update(status=status, reason=reason)
                if status != state.status and status != "NO_CHANGE":
                    events = state.events + (MonitorEvent(datetime.now().astimezone(), status, reason),)
                    changes["events"] = events[-_EVENT_LIMIT:]
            if record_observation is not None:
                changes.update(latest_observation=record_observation,
                               observations=(*state.observations, record_observation)[-_OBSERVATION_LIMIT:])
            self._state = replace(state, **changes)

    def _invalidate(self, status: str, reason: str, *, delayed_retry: bool = False,
                    lost_frame: bool = False) -> None:
        self._basis = None
        self._first = False
        self._recognized_probe = self._pending = None
        self._pending_at = None
        self._pending_hover = False
        if delayed_retry:
            self._failures = min(self._failures + 1, 5)
            self._next_sample = self._clock() + max(self.interval, min(30.0, self.interval * 2 ** self._failures))
        state = self.snapshot()
        changes = {}
        if lost_frame:
            self._last_probe = None
            changes = {"latest_observation": None, "stable_sample_count": 0}
        self._publish(status, reason, baseline_count=0, baseline_type="NONE",
                      unconfirmed_count=state.unconfirmed_count + int(status == "UNCONFIRMED_VISUAL_CHANGE"),
                      resync_count=state.resync_count + int(status == "RESYNC_REQUIRED" or state.baseline_count > 0),
                      **changes)

    def _recognize_frame(self, frame: VisualFrame) -> VisualObservation:
        if self._recognize is not None:
            return self._recognize(frame)
        if self._settings is None:
            calibration = CalibrationStore(self._calibration_path).load() if self._calibration_path else {}
            self._settings = calibration, VisualThresholds.load(self._thresholds_path)
            cv2.setNumThreads(2)
        if self._reader is None:
            self._reader = LocalTextRecognizer()
        calibration, thresholds = self._settings
        return observe_frame(frame, calibration, self._reader, thresholds)

    def poll(self) -> MonitorSnapshot:
        if self._stopped.is_set() or not self._working.acquire(blocking=False):
            return self.snapshot()
        try:
            if not self._stopped.is_set() and self._clock() >= self._next_sample:
                self._poll_once()
        finally:
            if self._stopped.is_set():
                self._reset()
            self._working.release()
        return self.snapshot()

    def _poll_once(self) -> None:
        self._next_sample = self._clock() + self.interval
        state = self.snapshot()
        self._publish(sample_count=state.sample_count + 1)
        started = perf_counter()
        try:
            frame = self._capture()
        except CaptureError as exc:
            self._publish(capture_ms=(perf_counter() - started) * 1000)
            status = "RESYNC_REQUIRED" if exc.code == "WINDOW_CHANGED" else "PAUSED"
            reason = str(exc).splitlines()[0] if exc.code in _WINDOW_ERRORS else f"{exc.code} · 截图失败"
            self._invalidate(status, reason, delayed_retry=True, lost_frame=True)
            return
        except Exception:
            self._publish(capture_ms=(perf_counter() - started) * 1000)
            self._invalidate("PAUSED", "截图异常", delayed_retry=True, lost_frame=True)
            return
        if self._stopped.is_set():
            return
        self._publish(latest_sample=frame.captured_at, capture_ms=(perf_counter() - started) * 1000)
        health = frame.diagnostics.window_health
        window = (*_window_key(frame), health.identity if health else None)
        if self._window is not None and self._window != window:
            self._window = window
            self._regions = self._last_probe = None
            self._invalidate("RESYNC_REQUIRED", "窗口句柄、位置、尺寸或 DPI 已变化", lost_frame=True)
            return
        self._window = window
        probe = probe_frame(frame, self._regions)
        changed = self._last_probe is not None and probe.differs(self._last_probe)
        self._publish(change_count=state.change_count + int(changed),
                      latest_change=frame.captured_at if changed else state.latest_change,
                      stable_sample_count=1 if changed or self._last_probe is None else state.stable_sample_count + 1)
        self._last_probe = probe
        if self._recognized_probe is not None and self._basis is not None and not probe.differs(self._recognized_probe):
            self._pending = self._pending_at = None
            self._pending_hover = False
            if (self._basis.baseline_type == "PARTIAL" and not changed
                    and self._clock() >= max(self._partial_retry_at, self._next_ocr)):
                self._full_observation(frame, pixels_changed=False)
            else:
                self._publish("NO_CHANGE", "C2 有效区域和会话视觉守卫未见有效变化")
            return
        hovered = bool(frame.mouse and self._regions and self._regions.history.contains(frame.mouse))
        if not self._first and (self._pending is None or probe.differs(self._pending)):
            self._pending_at = frame.captured_at
            self._pending = probe
            self._pending_hover |= hovered
            if self._basis is not None:
                self._publish("MONITORING", "等待变化画面稳定")
            return
        self._pending_hover |= hovered
        if self._clock() < self._next_ocr or self._stopped.is_set():
            return
        self._full_observation(frame)

    def _full_observation(self, frame: VisualFrame, *, pixels_changed: bool = True) -> None:
        state = self.snapshot()
        self._publish(recognition_count=state.recognition_count + 1)
        started = perf_counter()
        try:
            observation = summarize(self._recognize_frame(frame))
        except (OcrUnavailable, OSError, ValueError):
            self._publish(latest_observation=None)
            self._invalidate("OBSERVATION_UNRELIABLE", "本地 OCR 或标定读取失败", delayed_retry=True)
            return
        except Exception:
            self._publish(latest_observation=None)
            self._invalidate("OBSERVATION_UNRELIABLE", "视觉识别异常", delayed_retry=True)
            return
        finally:
            self._publish(latest_ocr=datetime.now().astimezone(), recognition_ms=(perf_counter() - started) * 1000)
            self._next_ocr = self._clock() + 2.0
        if self._stopped.is_set():
            return
        self._first = False
        self._regions = observation.regions
        previous_probe = self._recognized_probe
        self._recognized_probe = self._last_probe = probe_frame(frame, self._regions)
        pending_at = self._pending_at
        hovered = self._pending_hover
        self._pending = self._pending_at = None
        self._pending_hover = False
        self._partial_retry_at = self._clock() + max(10.0, 5 * self.interval)
        self._publish(record_observation=observation)
        message_warning = observation.blocker_code == "MESSAGES_UNRELIABLE"
        if observation.blocker and not (message_warning and observation.baseline_type == "PARTIAL"):
            status = ("RESYNC_REQUIRED" if self._basis is not None and observation.blocker_code != "MESSAGES_UNRELIABLE"
                      else "OBSERVATION_UNRELIABLE")
            self._invalidate(status, observation.blocker, delayed_retry=True)
            return
        self._failures = 0
        self._publish(latest_success=observation)
        if self._basis is None:
            self._basis = observation
            if observation.baseline_type == "PARTIAL":
                self._publish("PARTIAL_BASELINE", "保留可读旧消息和缺失证据；尚未建立可靠基线",
                              baseline_count=len(observation.reliable_messages), baseline_type="PARTIAL")
            else:
                self._publish("BASELINE_CREATED", "记录当前已有消息；本次没有新增候选",
                              baseline_count=len(observation.messages), baseline_type="RELIABLE")
            return
        if not pixels_changed and _same_conversation(self._basis, observation):
            delta = SequenceChange("NO_CHANGE", "画面未发生有效变化，OCR 补读结果只用于更新基线")
        else:
            delta = compare_sequences(self._basis, observation, cursor_in_history=hovered)
        prior_type = self._basis.baseline_type
        if delta.appended:
            content = _history_content_box(observation.regions.history, frame.state.dpi)
            if previous_probe is None or not all(_tail_pixels_changed(
                    previous_probe, self._recognized_probe, item.message.box, content) for item in delta.appended):
                delta = SequenceChange("UNCONFIRMED_VISUAL_CHANGE", "追加区像素证据不足，可能只是旧消息补读")
        if delta.status in ("RESYNC_REQUIRED", "UNCONFIRMED_VISUAL_CHANGE"):
            self._invalidate(delta.status, delta.reason)
            return
        if not delta.preserve_baseline:
            self._basis = observation
        if delta.appended:
            integrity = ("尾部追加证据可靠，前段识别曾不完整"
                         if "PARTIAL" in (prior_type, observation.baseline_type)
                         else "仅见当前视口；消息历史完整性未知")
            candidates = tuple(NewCandidate(pending_at or observation.at, observation.title, observation.selected_name,
                                            observation.match_status, observation.identity_unique, item.message,
                                            delta.reason, integrity_risk=integrity) for item in delta.appended)
            with self._state_lock:
                if not self._stopped.is_set():
                    state = self._state
                    self._state = replace(state, candidate_count=state.candidate_count + len(candidates),
                                          candidates=(state.candidates + candidates)[-_CANDIDATE_LIMIT:])
        status, reason = delta.status, delta.reason
        basis_type = self._basis.baseline_type
        if status == "NO_CHANGE" and prior_type != basis_type:
            if basis_type == "RELIABLE":
                status, reason = "BASELINE_CREATED", "旧消息识别已补齐，未观察到尾部追加"
            else:
                status, reason = "PARTIAL_BASELINE", "当前观察存在识别缺失，保留已定位的旧消息"
        self._publish(status, reason, baseline_type=basis_type, baseline_count=len(self._basis.reliable_messages))
