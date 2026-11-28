"""Physical displacement and ordered object stitching for one verified C2 viewport."""

from dataclasses import asdict, dataclass, replace
import base64

import cv2
import numpy as np

from uuid import uuid4
from autoim.wecom.visual_history import edge_bubbles, same_history_viewport
from autoim.wecom.collection_core import CoreAnalysis, CoreMessage, _read_message
from autoim.wecom.collection_pending import _decode_message
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_messages import Container, content_anchors, direction_evidence
from autoim.wecom.visual_layout import VisualThresholds


def fingerprint(pixels, area):
    gray = cv2.cvtColor(area.crop(pixels), cv2.COLOR_BGR2GRAY)
    # Preserve vertical pixel coordinates for registration; omit the scrollbar.
    return cv2.resize(
        gray[:, : max(1, gray.shape[1] - 10)], (192, gray.shape[0]), interpolation=cv2.INTER_AREA
    )


def displacement(before, after, direction):
    """Return the movement of the viewport origin, in source pixels."""
    if before.shape != after.shape or before.size == 0:
        return None
    difference = cv2.absdiff(before, after)
    # A scrollbar or tooltip can appear without moving the messages. Require
    # stationary textured evidence at separated heights, beyond the overlay.
    fractions = np.array_split(difference, 5)
    fixed = sum(
        np.mean(part > 8) < 0.0015 and source.std() > 8
        for part, source in zip(fractions, np.array_split(before, 5))
    )
    if np.mean(difference > 8) < 0.001 or (np.mean(difference > 8) < 0.006 and fixed >= 3):
        return 0
    height = len(before)
    n = max(12, height // 20)
    offsets = []
    for start in np.linspace(0, height - n, 24, dtype=int):
        block = after[start : start + n]
        if block.std() < 5:
            continue
        scores = cv2.matchTemplate(before, block, cv2.TM_CCOEFF_NORMED).ravel()
        best = int(scores.argmax())
        if scores[best] < 0.92:
            continue
        other = scores.copy()
        other[max(0, best - 4) : best + 5] = -1
        if other.max() < scores[best] - 0.035:
            offsets.append((int(best - start), int(start)))
    motions = [(d, start) for d, start in offsets if d and (d < 0) == (direction == "UP")]
    registered = set()
    for offset in {d for d, _ in motions}:
        group = [(value, start) for value, start in motions if abs(value - offset) <= 2]
        if group[-1][1] - group[0][1] < n * 0.8:
            continue
        delta = int(np.median([value for value, _ in group]))
        if abs(delta) > height * 0.87:
            continue
        left, right = (
            (before[delta:], after[: height - delta])
            if delta > 0
            else (before[: height + delta], after[-delta:])
        )
        edges = np.abs(cv2.Laplacian(left, cv2.CV_32F)) > 12
        if edges.sum() >= 40 and np.mean(cv2.absdiff(left, right)[edges] > 30) <= 0.16:
            registered.add(delta)
    if registered and max(registered) - min(registered) <= 2:
        return int(np.median(tuple(registered)))
    return None


def page_displacement(before, after, direction):
    if not same_history_viewport(before.area, after.area):
        return None
    top = max(before.area.top, after.area.top)
    bottom = min(before.area.bottom, after.area.bottom)
    return displacement(
        before.image[top - before.area.top : bottom - before.area.top],
        after.image[top - after.area.top : bottom - after.area.top],
        direction,
    )


@dataclass
class Page:
    image: np.ndarray
    area: Box
    items: tuple
    gaps: tuple
    at: str
    top: bool
    bottom: bool
    n1: bool
    n2: int | None
    loading: bool

    @classmethod
    def read(cls, raw, analysis, task):
        area = raw.layout.get("C2")
        thumb = raw.history_scrollbar
        unit = raw.frame.visual_unit
        messages = {item.sequence: item for item in analysis.messages}
        containers = tuple(
            Container(m.box, False, "HISTORY") for m in raw.messages if m.direction != "UNKNOWN"
        )
        thresholds = VisualThresholds()
        anchors = content_anchors(containers, area, raw.frame.pixels.shape[1], {}, thresholds)
        for m in raw.messages:
            if not m.viewport_limit:
                continue
            item = _read_message(m, task.id, raw.conversation.title)
            side = direction_evidence(m.box, area, anchors, thresholds)[0]
            kind = m.kind
            if kind == "UNKNOWN" and m.ocr_spans and all(s.score >= 0.8 for s in m.ocr_spans):
                kind = "TEXT"
            messages[m.sequence] = replace(item, direction=side, kind=kind)
        ocr = [
            entry.span
            for entry in raw.message_ocr
            if entry.kind in ("MESSAGE", "UNASSIGNED", "TOP_VIEWPORT_CLIPPED", "BOTTOM_VIEWPORT_CLIPPED")
        ]
        fragments = []
        for box in edge_bubbles(raw.frame.pixels, area, ocr, unit):
            if any(m.box == box and m.kind == "IMAGE" for m in raw.messages):
                continue
            words = sorted(
                (s for s in ocr if box.contains(s.box.center)), key=lambda s: (s.box.top, s.box.left)
            )
            if not words or any(
                m.kind in ("IMAGE", "FILE", "LINK_CARD", "CHAT_HISTORY_CARD") and m.box.contains(box.center)
                for m in raw.messages
            ):
                continue
            side = direction_evidence(box, area, raw.anchors, thresholds)[0]
            fragments.append(box)
            messages = {k: m for k, m in messages.items() if not box.contains(m.box.center)}
            key = max(messages, default=0) + 1
            messages[key] = CoreMessage(
                uuid4().hex,
                task.id,
                raw.conversation.title,
                key,
                side,
                "TEXT",
                "\\n".join(s.text for s in words),
                "",
                "",
                min(s.score for s in words),
                box,
                "CLIPPED",
                "UNKNOWN",
                "CONTAINER_CLIPPED",
                "PARTIALLY_VISIBLE",
                ocr_boxes=tuple(tuple(asdict(s.box).values()) for s in words),
            )
        controls = {
            entry.kind: entry.span.box for entry in raw.message_ocr if entry.kind in ("C2_N1", "C2_N2")
        }
        loading = any(
            entry.span.text.strip() in ("加载中", "正在加载", "加载中…") for entry in raw.message_ocr
        )
        gaps = tuple(
            gap
            for gap in analysis.gaps
            if gap["reason"] != "CONTAINER_CLIPPED"
            and not (gap.get("box") and any(box.contains(Box(*gap["box"]).center) for box in fragments))
        )
        messages = {k: replace(m, observed_at=raw.frame.captured_at.isoformat()) for k, m in messages.items()}
        return cls(
            fingerprint(raw.frame.pixels, area),
            area,
            tuple(sorted(messages.values(), key=lambda m: m.box.top)),
            gaps,
            raw.frame.captured_at.isoformat(),
            bool(thumb and thumb.top - area.top <= 4 * unit),
            bool(analysis.latest != "UNKNOWN" or thumb and area.bottom - thumb.bottom <= 4 * unit),
            "C2_N1" in controls,
            controls["C2_N2"].bottom if "C2_N2" in controls else None,
            loading,
        )

    def encode(self):
        ok, png = cv2.imencode(".png", self.image)
        if not ok:
            raise ValueError("PAGE_RASTER_ENCODING_FAILED")
        return dict(
            raster=base64.b64encode(png).decode("ascii"),
            area=asdict(self.area),
            items=[asdict(i) for i in self.items],
            gaps=self.gaps,
            at=self.at,
            top=self.top,
            bottom=self.bottom,
            n1=self.n1,
            n2=self.n2,
            loading=self.loading,
        )

    @classmethod
    def decode(cls, data):
        pixels = cv2.imdecode(np.frombuffer(base64.b64decode(data["raster"]), np.uint8), cv2.IMREAD_GRAYSCALE)
        return cls(
            pixels,
            Box(**data["area"]),
            tuple(_decode_message(dict(i)) for i in data["items"]),
            tuple(data["gaps"]),
            data["at"],
            data["top"],
            data["bottom"],
            data["n1"],
            data["n2"],
            data["loading"],
        )


def _combine(upper, lower):
    a, b = upper.text.splitlines(), lower.text.splitlines()
    overlaps = [length for length in range(1, min(len(a), len(b)) + 1) if a[-length:] == b[:length]]
    if len(overlaps) > 1 and len(a) == len(upper.ocr_boxes) and len(b) == len(lower.ocr_boxes):
        overlaps = [
            length
            for length in overlaps
            if all(
                abs(left[1] - right[1]) <= 4 and abs(left[0] - right[0]) <= 5
                for left, right in zip(upper.ocr_boxes[-length:], lower.ocr_boxes[:length])
            )
        ]
    if len(overlaps) != 1:
        return None
    return "\n".join(a + b[overlaps[0] :])


def resume_segment(records, page):
    """Recover only a contiguous measured segment with one unambiguous current position."""
    if page.n1:
        return None
    segments = {}
    for record in records:
        if record.get("segment") and record.get("connected") and record.get("page"):
            segments.setdefault(record["segment"], []).append(record)
    for rows in reversed(segments.values()):
        if not any(r["page"]["bottom"] or r.get("bottom_verified") for r in rows):
            continue
        matches = set()
        shifted = set()
        decoded = []
        for row in rows:
            prior = Page.decode(row["page"])
            if not same_history_viewport(prior.area, page.area):
                break
            decoded.append((prior, row["origin"]))
            up = page_displacement(prior, page, "UP")
            if up == 0:
                matches.add(row["origin"])
            else:
                for movement in (up, page_displacement(prior, page, "DOWN")):
                    if movement is not None:
                        shifted.add(row["origin"] + movement)
        else:
            positions = matches | shifted
            if (
                positions
                and max(positions) - min(positions) <= 2
                and max(positions) <= max(row["origin"] for row in rows)
            ):
                return (
                    next(iter(matches)) if len(matches) == 1 else round(float(np.median(tuple(positions))))
                ), decoded
    return None


class Sequence:
    def __init__(self):
        self.objects = []
        self.gaps = []
        self.repeated = 0
        self.stitched = 0
        self.area = None

    def add(self, page, origin):
        self.area = page.area
        self.gaps.extend(page.gaps)
        for item in page.items:
            box = item.box.offset(0, origin)
            clipped = item.quality == "CLIPPED"
            top = clipped and item.box.top <= page.area.top + 5
            bottom = clipped and item.box.bottom >= page.area.bottom - 5
            near = [
                i
                for i, (other, start, end) in enumerate(self.objects)
                if abs(other.box.left - box.left) <= 5
                and abs(other.box.right - box.right) <= 5
                and min(other.box.bottom, box.bottom) - max(other.box.top, box.top) > 4
            ]
            current = replace(
                item,
                box=box,
                ocr_boxes=tuple((l, t + origin, r, b + origin) for l, t, r, b in item.ocr_boxes),
            )
            if not near:
                self.objects.append((current, top, bottom))
                continue
            if len(near) != 1:
                self.gaps.append(dict(reason="AMBIGUOUS_CROSS_PAGE_OBJECT", box=None))
                continue
            i = near[0]
            old, old_top, old_bottom = self.objects[i]
            direction = old.direction if item.direction == "UNKNOWN" else item.direction
            if (
                item.kind != old.kind
                or (item.direction != old.direction and "UNKNOWN" not in (old.direction, item.direction))
                or not (top or old_top)
                and abs(box.top - old.box.top) > 5
                or not (bottom or old_bottom)
                and abs(box.bottom - old.box.bottom) > 5
            ):
                self.gaps.append(dict(reason="CROSS_PAGE_STRUCTURE_CHANGED", box=None))
                continue
            if not (clipped or old_top or old_bottom):
                has_signatures = item.media_signature and old.media_signature
                if item.text != old.text or (has_signatures and item.media_signature != old.media_signature):
                    self.gaps.append(dict(reason="CROSS_PAGE_BODY_CHANGED", box=None))
                elif current.hover and not old.hover:
                    self.objects[i] = (replace(old, hover=current.hover), top, bottom)
                self.repeated += 1
                continue
            upper, lower = (old, current) if old.box.top <= box.top else (current, old)
            if old.box.top <= box.top + 3 and old.box.bottom >= box.bottom - 3:
                body = old.text if item.text in old.text else None
            elif box.top <= old.box.top + 3 and box.bottom >= old.box.bottom - 3:
                body = item.text if old.text in item.text else None
            else:
                body = _combine(upper, lower) if item.kind == "TEXT" else "" if item.kind == "IMAGE" else None
            if body is None:
                self.gaps.append(dict(reason="FRAGMENT_TEXT_OVERLAP_MISSING", box=None))
                continue
            top = top and old_top
            bottom = bottom and old_bottom
            merged = Box(box.left, min(old.box.top, box.top), box.right, max(old.box.bottom, box.bottom))
            reliable = not (top or bottom) and direction != "UNKNOWN" and item.kind in ("TEXT", "IMAGE")
            self.objects[i] = (
                replace(
                    upper,
                    text=body,
                    box=merged,
                    method="HISTORY_STITCHED",
                    direction=direction,
                    quality="RELIABLE" if reliable else "CLIPPED",
                    reason="" if reliable else "CONTAINER_CLIPPED",
                    time=upper.time or lower.time,
                    hover=upper.hover or lower.hover,
                    ocr_boxes=tuple(
                        sorted(set(upper.ocr_boxes + lower.ocr_boxes), key=lambda box: (box[1], box[0]))
                    ),
                ),
                top,
                bottom,
            )
            self.stitched += 1

    def result(self, latest):
        ordered = sorted(self.objects, key=lambda entry: entry[0].box.top)
        thresholds = VisualThresholds()
        lanes = (None, None)
        if self.area:
            reliable = [item for item, _, _ in ordered if item.quality == "RELIABLE"]
            received = tuple(
                Container(item.box, False, "HISTORY") for item in reliable if item.direction == "INBOUND"
            )
            sent = tuple(
                Container(item.box, False, "HISTORY") for item in reliable if item.direction == "OUTBOUND"
            )
            left, _ = content_anchors(received, self.area, self.area.right, {}, thresholds)
            _, right = content_anchors(sent, self.area, self.area.right, {}, thresholds)
            lanes = left, right
        messages = []
        for sequence, (item, cut_top, cut_bottom) in enumerate(ordered, 1):
            if (
                self.area
                and item.kind in ("TEXT", "IMAGE")
                and item.reason == "CONTAINER_CLIPPED"
                and item.method == "HISTORY_STITCHED"
                and not cut_top
                and not cut_bottom
                and item.direction == "UNKNOWN"
            ):
                direction = direction_evidence(item.box, self.area, lanes, thresholds)[0]
                if direction != "UNKNOWN":
                    item = replace(item, direction=direction, quality="RELIABLE", reason="")
            messages.append(replace(item, sequence=sequence))
        gaps = tuple(self.gaps) + tuple(
            dict(reason=item.reason or "OBJECT_UNVERIFIED", box=asdict(item.box).values())
            for item in messages
            if item.quality != "RELIABLE"
        )
        gaps = tuple(dict(gap, box=list(gap["box"]) if gap.get("box") is not None else None) for gap in gaps)
        return CoreAnalysis(
            tuple(messages),
            latest,
            "VISIBLE_MESSAGES_INCOMPLETE" if gaps else "VISIBLE_MESSAGES_COMPLETE",
            gaps,
        )
