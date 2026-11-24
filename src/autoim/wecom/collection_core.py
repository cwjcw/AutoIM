"""In-memory, explicitly scoped core collection evidence and message results."""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import blake2s
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import numpy as np

from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_monitor import _different, _history_content_box, _thumbnail
from autoim.wecom.visual_store import database_path

MAX_READ_POLLS = 4
READ_WINDOW_SECONDS = 40
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

    def diagnostic(self):
        return dict(observation_id=self.id, sequence=self.sequence, direction=self.direction,
                    kind=self.kind, character_count=len(self.text), confidence=self.confidence,
                    box=bounds(self.box), time_evidence=bool(self.time), sender_evidence=bool(self.sender),
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
                       item.time_evidence, item.sender_name, confidence, item.box, quality, "UNKNOWN", reason, item.status)


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


@dataclass(slots=True)
class CoreTask:
    target: object
    before: object
    created_at: datetime
    discovered_at: datetime
    before_sample: np.ndarray | None
    id: str = field(default_factory=lambda: uuid4().hex)
    clicked_at: datetime | None = None
    deadline: float = field(default_factory=lambda: perf_counter() + READ_WINDOW_SECONDS)
    polls: int = 0
    previous: np.ndarray | None = None
    previous_box: Box | None = None
    best: CoreAnalysis | None = None
    previous_read: tuple | None = None
    verified: bool = False
    changed: bool = False
    cursor_moved: bool = False
    applied_point: tuple[int, int] | None = None


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
