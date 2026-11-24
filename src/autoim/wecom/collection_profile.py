"""Visual recognition limited to the signed-in user’s profile."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box

if TYPE_CHECKING:
    from autoim.wecom.visual_observation import VisualObservation


_EMAIL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_{|}~-]+)*"
                    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
_MENU_LABELS = frozenset(("收藏", "消息管理器", "设置", "我的收藏", "个人设置"))


def normalize_email(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    if len(value) > 254 or not _EMAIL.fullmatch(value) or len(value.split("@", 1)[0]) > 64:
        return ""
    return value.casefold()


@dataclass(frozen=True, slots=True)
class PopupEvidence:
    hwnd: int
    pid: int
    thread: int
    class_name: str
    owner: int
    parent: int
    root: int
    root_owner: int
    chain: tuple[int, ...]
    geometry: tuple
    valid_chain: bool = True

    @property
    def identity(self):
        return (self.hwnd, self.pid, self.thread, self.class_name, self.owner,
                self.parent, self.root, self.root_owner, self.chain)


@dataclass(slots=True)
class ProfileWindows:
    lifetime: tuple
    baseline: frozenset[int]
    id: str = field(default_factory=lambda: uuid4().hex)
    menu_click: str = ""
    card_click: str = ""
    menu: PopupEvidence | None = None
    card: PopupEvidence | None = None
    menu_embedded: Box | None = None
    card_embedded: Box | None = None
    before_card: frozenset[int] = frozenset()
    seen: dict[int, tuple] = field(default_factory=dict)
    retired: set[int] = field(default_factory=set)
    visible: set[int] = field(default_factory=set)
    valid: bool = True

    def relation(self, popup: PopupEvidence, *, detached: bool):
        if (not self.valid or not popup.valid_chain or popup.pid != self.lifetime[0]
                or popup.hwnd in self.baseline or popup.hwnd in self.retired):
            return None
        previous = self.seen.get(popup.hwnd)
        if previous and previous != popup.identity:
            self.retired.add(popup.hwnd)
            return None
        if self.menu and popup.identity == self.menu.identity:
            return "VERIFIED_MENU"
        if self.card and popup.identity == self.card.identity:
            return "VERIFIED_CARD"
        root = self.lifetime[1]
        if self.card_click:
            if popup.hwnd in self.before_card:
                return None
            if self.menu and self.menu.hwnd in popup.chain and self.menu.hwnd not in self.retired:
                return "VERIFIED_MENU_DESCENDANT"
            if popup.root_owner == root and root in popup.chain:
                return "MAIN_DESCENDANT"
            return None
        if not self.menu_click or popup.class_name == "UserInfoWindow":
            return None
        if popup.root_owner == root and root in popup.chain:
            return "MAIN_DESCENDANT"
        return "DETACHED_MENU_CANDIDATE" if detached else None

    def observe(self, candidates, present=None):
        latest = set(candidates if present is None else present)
        self.retired.update(self.visible - latest)
        self.visible = latest
        for hwnd, popup in candidates.items():
            self.seen.setdefault(hwnd, popup.identity)


def owner_chain(hwnd, owner_of, identify, pid, *, limit=8):
    """Verify each hop before permitting ancestry to grant read-only admission."""
    seen = {hwnd}
    chain = []
    current = hwnd
    try:
        for _ in range(limit):
            current = owner_of(current)
            if not current:
                return tuple(chain), True
            if current in seen or identify(current) != pid:
                return tuple(chain), False
            seen.add(current)
            chain.append(current)
    except OSError:
        return tuple(chain), False
    return tuple(chain), False


@dataclass(frozen=True, slots=True)
class ProfileSurface:
    hwnd: int
    pixels: np.ndarray
    bounds: Box
    geometry: tuple
    source: str = ""
    window_class: str = ""

    @property
    def embedded(self):
        return self.geometry == ()


@dataclass(frozen=True, slots=True)
class ProfileAction:
    stage: str
    surface: ProfileSurface
    control: Box

    @property
    def point(self):
        return self.control.center



@dataclass(slots=True)
class ProfileCheck:
    reference: VisualObservation
    known_windows: tuple[int, ...] | ProfileWindows
    stage: str = "OPENING_PROFILE_MENU"
    email: str = ""
    email_surface: tuple = ()
    email_at: datetime | None = None
    close_issued: bool = False
    failed: str = ""
    polls: int = 0
    retries: int = 0
    manual_card: bool = False
    manual_close: bool = False
    awaiting: str = ""
    email_retries: int = 0
    embedded_layers: list[Box] = field(default_factory=list)


def _key(text):
    return "".join(unicodedata.normalize("NFKC", text).split()).strip(":").casefold()


def menu_action(surface, spans, name, dpi, *, avatar=None, evidence=None):
    """Use menu geometry and OCR together, with an A1-matched image fallback."""
    def report(**facts):
        if evidence is not None:
            evidence.update(facts)

    reliable = [span for span in spans if span.score >= .80 and surface.bounds.contains(span.box.center)]
    labels = [span for span in reliable if _key(span.text) in _MENU_LABELS]
    names = [span for span in reliable if _key(span.text) == _key(name)]
    unit = dpi / 96
    label_count = len({_key(span.text) for span in labels})
    report(ocr_spans=len(spans), matched_labels=label_count, matched_names=len(names))
    if not labels or len(names) != 1:
        report(rejection="MENU_TEXT_OR_NAME_AMBIGUOUS")
        return None
    label = names[0].box
    if label.bottom >= min(item.box.top for item in labels):
        report(rejection="MENU_NAME_NOT_ABOVE_ITEMS")
        return None
    if label.center[1] > surface.bounds.top + .52 * surface.bounds.height:
        report(rejection="MENU_NAME_OUTSIDE_HEADER")
        return None

    def avatar_thumbnail(tile):
        if tile is None or min(tile.shape[:2]) < 6:
            return None
        h, w = tile.shape[:2]
        inset_y, inset_x = h // 5, w // 5
        return cv2.resize(tile[inset_y:h-inset_y, inset_x:w-inset_x], (20, 20), interpolation=cv2.INTER_AREA)

    reference = avatar_thumbnail(avatar)
    candidates = []
    # Scale against both menu header geometry and DPI. Keep points left of the name.
    for diameter in sorted({round(value * unit) for value in (25, 32, 36, 38, 44, 52, 60)} |
                           {round(surface.bounds.width * fraction) for fraction in (.14, .20, .25)}):
        if not 16 * unit <= diameter <= 68 * unit:
            continue
        for gap in (4, 6, 8, 14, 20):
            right = label.left - round(gap * unit)
            x, y = right - diameter, label.center[1] - diameter // 2
            control = Box(x, y, x + diameter, y + diameter)
            if (not surface.bounds.contains((x, y)) or
                    not surface.bounds.contains((control.right - 1, control.bottom - 1))):
                continue
            tile = avatar_thumbnail(control.crop(surface.pixels))
            if (tile is None or np.max(tile.std(axis=(0, 1))) < 9 or
                    len(np.unique((tile // 16).reshape(-1, 3), axis=0)) < 14):
                continue
            distance = float(np.abs(tile.astype(np.float32) - reference).mean()) if reference is not None else 255.
            candidates.append((distance, abs(diameter - 38 * unit) + abs(gap - 8) * unit, control))
    candidates.sort(key=lambda item: item[:2])
    report(texture_candidates=len(candidates), best_avatar_distance=round(candidates[0][0], 1) if candidates else None)
    if not candidates:
        report(rejection="NO_TEXTURED_AVATAR_IN_HEADER")
        return None
    strong = [item for item in candidates if item[0] <= 24.]
    if strong:
        best = strong[0][2]
        if any(not best.contains(item[2].center) for item in strong[1:]):
            report(rejection="DISJOINT_AVATAR_MATCHES")
            return None
        report(method="A1_AVATAR_AND_MENU")
    elif (label_count >= 2 and names[0].score >= .88
          and all(item.score >= .88 for item in labels)
          and label.left - surface.bounds.left <= surface.bounds.width * .70):
        # Preserve V3.2's two-label structural route, with a bounded avatar cluster.
        best = min(candidates, key=lambda item: item[1])[2]
        report(method="MENU_HEADER_STRUCTURE")
    else:
        report(rejection="INSUFFICIENT_INDEPENDENT_AVATAR_EVIDENCE")
        return None
    report(control=(best.left, best.top, best.right, best.bottom))
    return ProfileAction("MENU_AVATAR", surface, best)


def unique_menu(actions, physical, dpi):
    """Merge only overlapping representations of one physically located avatar."""
    found = []
    for action in actions:
        position = physical(action)
        def same_target(previous, prior):
            if position is None or prior is None or (abs(position[0] - prior[0]) > 8 * dpi / 96
                                                     or abs(position[1] - prior[1]) > 8 * dpi / 96):
                return False
            if previous.surface.hwnd != action.surface.hwnd and not (previous.surface.embedded or action.surface.embedded):
                return False
            first = cv2.resize(previous.control.crop(previous.surface.pixels), (16, 16))
            second = cv2.resize(action.control.crop(action.surface.pixels), (16, 16))
            return float(cv2.absdiff(first, second).mean()) < 28

        duplicate = next((index for index, (previous, prior) in enumerate(found) if same_target(previous, prior)), None)
        if duplicate is None:
            found.append((action, position))
        elif not action.surface.embedded:
            found[duplicate] = (action, position)
    return [action for action, _ in found]


def email_from_spans(surface, spans, *, evidence=None):
    def report(**facts):
        if evidence is not None:
            evidence.update(facts)
    reliable = [span for span in spans if span.score >= .88 and surface.bounds.contains(span.box.center)]
    anchors = [span for span in reliable if _key(span.text).startswith("邮箱")]
    report(email_labels=len(anchors), value_fragments=0)
    if len(anchors) != 1:
        report(email_rejection="EMAIL_LABEL_COUNT")
        return ""
    label = anchors[0]
    inline = re.fullmatch(r"邮箱\s*[:：]?\s*(.*)", label.text.strip())
    prefix = inline[1].strip(":： ") if inline else ""
    row = sorted((span for span in reliable if span is not label and span.box.left >= label.box.right - 3
                  and abs(span.box.center[1] - label.box.center[1]) <= max(4, label.box.height * .45)
                  and span.box.left - label.box.right < surface.bounds.width * .68),
                 key=lambda span: span.box.left)
    report(value_fragments=len(row) + bool(prefix), label_y=label.box.center[1],
           fragment_boxes=[(item.box.left, item.box.top, item.box.right, item.box.bottom) for item in row])
    if len(row) > 6 or any(not re.fullmatch(r"[\x21-\x7e]+", unicodedata.normalize("NFKC", item.text).strip())
                            for item in row):
        report(email_rejection="UNRELATED_OR_TOO_MANY_FRAGMENTS")
        return ""
    if prefix and row and normalize_email(prefix):
        report(email_rejection="MULTIPLE_INLINE_OR_ROW_VALUES")
        return ""
    if any(not -2 <= right.box.left - left.box.right <= max(6, label.box.height * .8)
           for left, right in zip(row, row[1:])):
        report(email_rejection="FRAGMENTS_NOT_ADJACENT")
        return ""
    value = normalize_email(prefix + "".join(item.text.strip() for item in row))
    report(email_rejection="" if value else "INVALID_EMAIL_FORMAT", email_valid=bool(value))
    return value


def is_profile_card(spans, name, surface, *, evidence=None):
    labels = frozenset(("部门", "职位", "岗位", "工号", "手机号", "手机", "企业", "企业名称", "个人资料"))
    reliable = [span for span in spans if span.score >= .88 and surface.bounds.contains(span.box.center)]
    names = [span for span in reliable if _key(span.text) == _key(name)]
    anchors = [span for span in reliable if _key(span.text).startswith("邮箱")]
    label_count = sum(_key(span.text) in labels for span in reliable)
    ordered = len(names) == len(anchors) == 1 and names[0].box.bottom < anchors[0].box.top
    secondary = bool(label_count)
    if ordered and surface.window_class == "UserInfoWindow":
        # On this native class a separate, well-formed labeled row supports the
        # name match even when other labels were not recognized.
        secondary = secondary or bool(email_from_spans(surface, spans))
    accepted = ordered and secondary
    if evidence is not None:
        evidence.update(name_matches=len(names), email_labels=len(anchors), other_field_labels=label_count,
                        name_above_email=bool(ordered), accepted=bool(accepted),
                        card_rejection="" if accepted else "CARD_NAME_OR_FIELD_STRUCTURE")
    return accepted


def unique_cards(scanned, physical):
    """Prefer native captures only when physical extents overlap substantially."""
    distinct = []
    for surface, spans in scanned:
        x1, y1, x2, y2 = physical(surface)
        duplicate = None
        for index, (old, _) in enumerate(distinct):
            if not (old.hwnd == surface.hwnd or old.embedded != surface.embedded):
                continue
            a1, b1, a2, b2 = physical(old)
            overlap = max(0, min(x2, a2) - max(x1, a1)) * max(0, min(y2, b2) - max(y1, b1))
            size = min((a2-a1)*(b2-b1), (x2-x1)*(y2-y1))
            if size and overlap / size >= .75:
                duplicate = index
                break
        if duplicate is None:
            distinct.append((surface, spans))
        elif not surface.embedded:
            distinct[duplicate] = (surface, spans)
    return distinct


def card_close(surface, dpi):
    """Find an X with two opposing, crossing strokes in the card's top right."""
    bounds = surface.bounds
    unit = dpi / 96
    area = Box(max(bounds.left, bounds.right - round(57 * unit)), bounds.top,
               bounds.right, min(bounds.bottom, bounds.top + round(43 * unit)))
    if min(area.width, area.height) < 10:
        return None
    gray = cv2.cvtColor(area.crop(surface.pixels), cv2.COLOR_BGR2GRAY)
    background = float(np.median(gray))
    mask = (np.abs(gray.astype(np.float32) - background) >= 45).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    matches = []
    for x, y, w, h, _ in stats[1:n]:
        if not (7 * unit <= min(w, h) and max(w, h) <= 21 * unit and .76 <= w / h <= 1.31):
            continue
        icon = mask[y:y+h, x:x+w]
        d = cv2.resize(icon.astype(np.float32), (13, 13), interpolation=cv2.INTER_AREA)
        forward = np.eye(13, dtype=bool)
        reverse = np.fliplr(forward)
        far = np.abs(np.arange(13)[:, None] - np.arange(13)[None, :]) > 2
        if d[forward].mean() >= .55 and d[reverse].mean() >= .55 and d[far & np.fliplr(far)].mean() <= .20:
            matches.append(Box(area.left+x, area.top+y, area.left+x+w, area.top+y+h))
    return ProfileAction("CARD_CLOSE", surface, matches[0]) if len(matches) == 1 else None


def changed_regions(before, after, dpi):
    """Find bounded overlay candidates within the captured client."""
    if before.shape != after.shape:
        return ()
    diff = (np.max(cv2.absdiff(before, after), axis=2) >= 20).astype(np.uint8)
    unit = dpi / 96
    size = max(3, round(7 * unit))
    merged = cv2.morphologyEx(diff, cv2.MORPH_CLOSE, np.ones((size, size), dtype=np.uint8))
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    height, width = diff.shape
    boxes = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if (120 * unit <= w <= min(650 * unit, width * .87)
                and 60 * unit <= h <= min(800 * unit, height * .93)):
            boxes.append(Box(x, y, x+w, y+h))
    return tuple(sorted(boxes, key=lambda box: box.width * box.height, reverse=True)[:4])
