"""Visual recognition limited to the signed-in user’s profile."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

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
class ProfileSurface:
    hwnd: int
    pixels: np.ndarray
    bounds: Box
    geometry: tuple

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
    known_windows: tuple[int, ...]
    stage: str = "OPENING_PROFILE_MENU"
    email: str = ""
    email_surface: tuple = ()
    email_at: datetime | None = None
    close_issued: bool = False
    failed: str = ""
    polls: int = 0


def _key(text):
    return "".join(unicodedata.normalize("NFKC", text).split()).strip(":").casefold()


def menu_action(surface, spans, name, dpi):
    reliable = [span for span in spans if span.score >= .88 and surface.bounds.contains(span.box.center)]
    labels = [span for span in reliable if _key(span.text) in _MENU_LABELS]
    names = [span for span in reliable if _key(span.text) == _key(name)]
    unit = dpi / 96
    if len({_key(span.text) for span in labels}) < 2 or len(names) != 1:
        return None
    label = names[0].box
    if label.bottom >= min(item.box.top for item in labels):
        return None
    # The portrait sits directly to the left of the local account name.
    for diameter in (32, 38, 44):
        d = round(diameter * unit)
        right = label.left - round(8 * unit)
        x, y = right - d, label.center[1] - d // 2
        control = Box(x, y, x + d, y + d)
        if not surface.bounds.contains((x, y)) or not surface.bounds.contains((control.right - 1, control.bottom - 1)):
            continue
        tile = control.crop(surface.pixels)
        inset = d // 4
        center = tile[inset:-inset, inset:-inset]
        if len(np.unique((center // 16).reshape(-1, 3), axis=0)) >= 14 and np.max(center.std(axis=(0, 1))) >= 9:
            return ProfileAction("MENU_AVATAR", surface, control)
    return None


def email_from_spans(surface, spans):
    reliable = [span for span in spans if span.score >= .88 and surface.bounds.contains(span.box.center)]
    anchors = [span for span in reliable if _key(span.text).startswith("邮箱")]
    if len(anchors) != 1:
        return ""
    label = anchors[0]
    inline = re.fullmatch(r"邮箱\s*[:：]?\s*(.+)", label.text.strip())
    if inline and inline[1].strip(":： "):
        return normalize_email(inline[1].strip(":： "))
    values = [span for span in reliable if span is not label and span.box.left >= label.box.right - 3
              and abs(span.box.center[1] - label.box.center[1]) <= max(4, label.box.height * .45)
              and span.box.left - label.box.right < surface.bounds.width * .68]
    return normalize_email(values[0].text) if len(values) == 1 else ""


def is_profile_card(spans, name, surface):
    labels = frozenset(("部门", "职位", "岗位", "工号", "手机号", "手机", "企业", "企业名称", "个人资料"))
    reliable = [span for span in spans if span.score >= .88 and surface.bounds.contains(span.box.center)]
    names = [span for span in reliable if _key(span.text) == _key(name)]
    anchors = [span for span in reliable if _key(span.text).startswith("邮箱")]
    return (len(names) == len(anchors) == 1 and names[0].box.bottom < anchors[0].box.top
            and any(_key(span.text) in labels for span in reliable))


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
                and 100 * unit <= h <= min(800 * unit, height * .93)):
            boxes.append(Box(x, y, x+w, y+h))
    return tuple(sorted(boxes, key=lambda box: box.width * box.height, reverse=True)[:4])
