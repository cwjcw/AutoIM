"""In-memory, explicitly scoped core collection evidence and message results."""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import blake2s
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_monitor import _different, _history_content_box, _thumbnail
from autoim.wecom.visual_store import database_path
from autoim.wecom.visual_time import MessageTime

MAX_READ_POLLS = 4
MAX_DISPLAYED_MESSAGES = 500
MAX_CACHED_OBSERVATIONS = 2500


def failure_crop_directory() -> Path:
    return database_path().parent / "private-test-crops"


def bounds(box):
    return (box.left, box.top, box.right, box.bottom) if box else None


def normalized_text(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).strip()


def history_sample(raw) -> np.ndarray | None:
    area = raw.layout.get("C2")
    if not area or area.width < 8 or area.height < 8:
        return None
    content = _history_content_box(area, raw.frame.state.dpi)
    return _thumbnail(raw.frame.pixels, content, (280, 168))


def history_changed(first, second):
    return first is not None and second is not None and _different(first, second, localized=True)


def avatar_distance(before, previous, after, current):
    """Compare interior avatar pixels without relying on an identical crop hash."""
    if not previous.avatar:
        return None

    def tile(frame, box):
        # The upper-right unread badge can change the detected avatar bounds.
        interior = Box(box.left + box.width // 5, box.top + box.height * 2 // 5,
                       box.right - box.width // 4, box.bottom - box.height // 8)
        crop = interior.crop(frame.pixels)
        if min(crop.shape[:2]) < 8:
            return None
        sample = cv2.resize(crop, (28, 28), interpolation=cv2.INTER_AREA)
        return sample.astype(np.float32) if np.max(sample.std(axis=(0, 1))) >= 9 else None

    first = tile(before.frame, previous.avatar)
    if first is None:
        return None
    interior = first[4:-4, 4:-4]
    boxes = [current.avatar] if current.avatar else []
    # A selected row may lose its avatar contour. Translate the before-click
    # crop within the newly identified row, only at the same viewport scale.
    if (previous.box.width == current.box.width and before.frame.state.dpi == after.frame.state.dpi
            and abs(current.box.height - previous.box.height) <= previous.box.height * .12):
        projected = previous.avatar.offset(current.box.left - previous.box.left,
                                           current.box.center[1] - previous.box.center[1])
        if (current.box.contains((projected.left, projected.top))
                and current.box.contains((projected.right - 1, projected.bottom - 1)) and projected not in boxes):
            boxes.append(projected)
    distances = []
    for box in boxes:
        second = tile(after.frame, box)
        if second is not None:
            # Allow a few pixels of registration error while comparing RGB texture.
            scores = cv2.matchTemplate(second, interior, cv2.TM_SQDIFF)
            distances.append(float(np.sqrt(max(0., scores.min()) / interior.size)))
    return min(distances, default=None)


@dataclass(frozen=True, slots=True)
class CoreMessage:
    id: str
    task: str
    contact: str
    sequence: int
    direction: str
    kind: str
    text: str
    time: str
    sender: str
    confidence: float | None
    box: Box
    quality: str
    unread: str
    reason: str
    parser_status: str
    clock_source: str = "GROUP_ANCHOR"
    clock: MessageTime = MessageTime()
    clock_sequence: int = 0
    collection_status: str = "NOT_COMPARED"
    save_status: str = "NOT_SAVED"
    file: object | None = None
    method: str = "BUBBLE_FIRST"
    ocr_boxes: tuple = ()

    def diagnostic(self):
        return dict(observation_id=self.id, sequence=self.sequence, direction=self.direction,
                    kind=self.kind, character_count=len(self.text), confidence=self.confidence,
                    box=bounds(self.box), time_evidence=bool(self.time), sender_evidence=bool(self.sender),
                    ocr_boxes=self.ocr_boxes, method=self.method,
                    recovered=self.method in ("OCR_REVERSE", "MERGED_EVIDENCE"),
                    clipped=self.quality == "CLIPPED", quality=self.quality, parser_status=self.parser_status,
                    unread=self.unread,
                    reason=self.reason)


@dataclass(frozen=True, slots=True)
class CoreAnalysis:
    messages: tuple[CoreMessage, ...]
    latest: str
    completeness: str
    gaps: tuple[dict, ...]


def _read_message(item, task, contact):
    confidence = min((span.score for span in item.ocr_spans), default=None)
    text = item.text
    if item.file:
        text = " · ".join(filter(None, (item.file.file_name, item.file.file_size_text))) or text
    quality, reason = "RELIABLE", ""
    if item.status == "PARTIALLY_VISIBLE":
        quality, reason = "CLIPPED", "CONTAINER_CLIPPED"
    elif item.kind == "UNKNOWN":
        quality, reason = "UNKNOWN_TYPE", "MESSAGE_TYPE_UNVERIFIED"
    elif item.status == "OCR_UNCERTAIN":
        quality, reason = "LOW_CONFIDENCE", "BODY_OCR_UNCERTAIN"
    elif item.status != "CANDIDATE" or item.direction == "UNKNOWN":
        quality, reason = "PARTIAL", "CONTAINER_OR_DIRECTION_UNVERIFIED"
    elif item.kind == "TEXT" and (not text or confidence is None or confidence < .70):
        quality, reason = "LOW_CONFIDENCE", "BODY_OCR_BELOW_CORE_THRESHOLD"
    elif item.file and item.file.parse_status != "COMPLETE":
        quality, reason = "PARTIAL", "FILE_METADATA_INCOMPLETE"
    return CoreMessage(uuid4().hex[:12], task, contact, item.sequence, item.direction, item.kind, text,
                       item.time_evidence, item.sender_name, confidence, item.box, quality, "UNKNOWN", reason, item.status,
                       clock_source=item.time_source, file=item.file, method=item.method,
                       ocr_boxes=tuple(bounds(span.box) for span in item.ocr_spans))


def analyze_read(raw, task, contact, count, preview, *, verified, badge_cleared, refreshed):
    """Report each bubble independently, then assess its unread range."""
    messages = tuple(_read_message(item, task, contact) for item in raw.messages)
    # C2 OCR text classified as a centered SYSTEM row by the existing parser.
    separators = [entry.span.box for entry in raw.message_ocr if entry.kind == "SYSTEM"
                  and entry.span.score >= .85 and normalized_text(entry.span.text) == "以下是新消息"]
    jump_hint = any(entry.kind in ("SYSTEM", "UNASSIGNED") and entry.span.score >= .85
                    and normalized_text(entry.span.text) in ("跳转到最新消息", "回到最新消息", "查看新消息")
                    for entry in raw.message_ocr)
    gaps = tuple({"reason": "UNASSIGNED_MESSAGE_OCR", "box": bounds(entry.span.box),
                  "confidence": round(entry.span.score, 3), "character_count": len(entry.span.text)}
                 for entry in raw.message_ocr if entry.possible_message)
    gaps += tuple({"reason": "C2_OCR_" + area.status, "box": bounds(area.box)}
                  for area in raw.ocr_areas if area.region.startswith("C2") and area.status in ("FAILED", "SKIPPED"))
    gaps += tuple({"reason": item.reason, "box": bounds(item.box), "sequence": item.sequence}
                  for item in messages if item.quality != "RELIABLE")
    c2, thumb = raw.layout.get("C2"), raw.history_scrollbar
    bottom = bool(c2 and thumb and c2.bottom - thumb.bottom <= max(4, c2.height * .045))
    normalized_preview = normalized_text(preview)
    exact_preview = bool(messages and normalized_preview and not normalized_preview.endswith(("...", "…"))
                         and messages[-1].quality == "RELIABLE" and messages[-1].kind == "TEXT"
                         and messages[-1].direction == "INBOUND"
                         and normalized_preview == normalized_text(messages[-1].text))
    latest = "UNKNOWN"
    if verified and not jump_hint:
        if bottom:
            latest = "SCROLLBAR_BOTTOM"
        elif not thumb and exact_preview and badge_cleared and refreshed:
            latest = "PREVIEW_AND_REFRESH"
    if not verified:
        return CoreAnalysis(tuple(replace(item, unread="ATTRIBUTION_UNCERTAIN") for item in messages),
                            latest, raw.message_completeness, gaps)
    separator = separators[0] if len(separators) == 1 else None
    after = [item for item in messages if separator and item.box.top >= separator.bottom]
    gap_after = bool(separator and any(gap["box"] and gap["box"][3] > separator.bottom for gap in gaps))
    range_ordered = all(a.box.bottom <= b.box.top for a, b in zip(after, after[1:]))
    confirmed_range = bool(latest != "UNKNOWN" and badge_cleared and after and not gap_after and range_ordered
                           and count is not None and len(after) == count and
                           all(item.direction == "INBOUND" and item.quality == "RELIABLE" for item in after))
    preview_is_unique = sum(item.kind == "TEXT" and normalized_text(item.text) == normalized_preview
                            for item in messages) == 1
    final_has_gap = bool(messages and any(not gap["box"] or gap["box"][3] >= messages[-1].box.bottom
                                          for gap in gaps))
    confirmed_single = (count == 1 and latest != "UNKNOWN" and exact_preview and preview_is_unique
                        and badge_cleared and refreshed and not final_has_gap and not separators)
    checked = []
    for item in messages:
        state = "UNKNOWN"
        if separator and item.box.bottom <= separator.top:
            state = "HISTORY_VISIBLE"
        elif (confirmed_range and item in after) or (confirmed_single and item is messages[-1]):
            state = "CONFIRMED_UNREAD"
        elif item.direction == "INBOUND" and (item in after or (exact_preview and item is messages[-1])):
            state = "UNREAD_CANDIDATE"
        elif latest != "UNKNOWN" and item is messages[-1]:
            state = "LATEST_VISIBLE"
        checked.append(replace(item, unread=state))
    return CoreAnalysis(tuple(checked), latest, raw.message_completeness, gaps)


@dataclass(frozen=True, slots=True)
class CoreView:
    phase: str = "PREPARING_WINDOW"
    contact: str = ""
    before_count: int | None = None
    clicked: bool = False
    title_match: bool = False
    latest: str = "UNKNOWN"
    ocr_complete: bool = False
    completeness: str = ""
    risk: str = ""
    discovered: int = 0
    waiting: int = 0
    clicks: int = 0
    verified: int = 0
    failures: int = 0
    partial: int = 0
    repeated: int = 0
    messages: tuple[CoreMessage, ...] = ()
    read_unfinished: int = 0
    first_saved: int = 0
    new_saved: int = 0
    existing: int = 0
    review_messages: int = 0
    save_failures: int = 0
    independent_messages: int = 0


@dataclass(slots=True)
class CoreTask:
    target: object
    before: object
    created_at: datetime
    discovered_at: datetime
    before_sample: np.ndarray | None
    id: str = field(default_factory=lambda: uuid4().hex)
    clicked_at: datetime | None = None
    polls: int = 0
    previous: np.ndarray | None = None
    previous_box: Box | None = None
    best: CoreAnalysis | None = None
    verified: bool = False
    changed: bool = False
    cursor_moved: bool = False
    applied_point: tuple[int, int] | None = None
    match_evidence: str = ""
    match_distance: float | None = None
    read_at: datetime | None = None
    conversation_kind: str = "UNKNOWN"
    scan_id: str = ""
    before_evidence: str = ""
    c2_evidence: str = ""
    body_reads: int = 0


@dataclass(slots=True)
class CoreRun:
    failure_crops: bool
    login: object | None = None
    lifetime: tuple | None = None
    invalidated: bool = False
    seen: set = field(default_factory=set)
    signatures: dict = field(default_factory=dict)
    discovered_at: dict = field(default_factory=dict)
    handled: set = field(default_factory=set)
    precheck_failures: Counter = field(default_factory=Counter)
    cleared: set = field(default_factory=set)
    results_seen: dict = field(default_factory=dict)
    displayed: set = field(default_factory=set)
    task: CoreTask | None = None
    navigation_task: CoreTask | None = None
    clock: object | None = None
    reader: object | None = None
    presentation: str = ""
    independent_reason: str = ""
    independent_done: bool = False
    finish_after_independent: bool = False
    selection_waits: int = 0
    mute_trials: Counter = field(default_factory=Counter)
    mute_recent: dict = field(default_factory=dict)
    muted_crops: set = field(default_factory=set)
    evidence_raw: object | None = None
    evidence_frame: object | None = None
    storage_ready: bool = False
    storage_blocked: str = ""
    view: CoreView = CoreView()

    def accept_messages(self, task, analysis):
        occurrences = Counter()
        added = []
        repeated = self.view.repeated
        for item in reversed(analysis.messages):
            signature = blake2s(repr((item.kind, item.direction, normalized_text(item.text),
                                      item.time, item.sender)).encode()).digest()
            occurrences[signature] += 1
            key = (task.target.key, signature, occurrences[signature])
            if item.unread == "CONFIRMED_UNREAD":
                self.results_seen[key] = None
                key = (*key, task.id)
            elif not task.verified:
                key = (*key, task.id, "UNVERIFIED")
            if key in self.results_seen:
                repeated += 1
            else:
                self.results_seen[key] = None
                added.append(item)
        while len(self.results_seen) > MAX_CACHED_OBSERVATIONS:
            self.results_seen.pop(next(iter(self.results_seen)))
        self.view = replace(self.view, repeated=repeated,
                            messages=(*self.view.messages, *reversed(added))[-MAX_DISPLAYED_MESSAGES:])

    def clear_results(self):
        self.view = replace(self.view, messages=())
