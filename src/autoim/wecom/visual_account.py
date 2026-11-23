"""Read login evidence from the top of the WeCom function rail."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_ocr import OcrUnavailable, TextRecognizer


@dataclass(frozen=True, slots=True)
class LoginEvidence:
    name: str = ""
    avatar: str = ""
    box: Box | None = None
    signature: str = ""
    reason: str = ""

    @property
    def key(self) -> str:
        return " ".join(unicodedata.normalize("NFKC", self.name).split()).casefold()

    def unchanged(self, pixels: np.ndarray) -> bool:
        return bool(self.box and self.signature == _signature(self.box.crop(pixels)))


def _signature(pixels: np.ndarray) -> str:
    return hashlib.sha256(pixels.tobytes()).hexdigest()


_MENU_TEXT = frozenset(("智能助理", "消息", "频道", "邮件", "文档", "日程", "待办", "会议",
                        "通讯录", "工作台", "企业微信", "设置", "智能机器人"))


def read_login(pixels: np.ndarray, rail: Box | None, reader: TextRecognizer, dpi: int = 96) -> LoginEvidence:
    if rail is None or rail.width < 32:
        return LoginEvidence(reason="未定位登录账号所在的 A 区")
    scale = max(.65, min(dpi / 96, 3))
    # Menu position follows UI scale, including when the rail is widened for text labels.
    top = Box(rail.left, rail.top, rail.right, min(rail.bottom, rail.top + round(68 * scale)))
    try:
        spans = reader.read(pixels, top)
    except OcrUnavailable:
        return LoginEvidence(reason="A1 账号 OCR 失败")
    names = [span for span in spans if span.score >= .88 and 2 <= len(span.text.strip()) <= 40
             and any(char.isalpha() for char in span.text) and span.text.strip() not in _MENU_TEXT
             and span.box.top >= top.top + 16 * scale and span.box.bottom <= top.bottom
             and top.contains(span.box.center)]
    if len(names) != 1:
        return LoginEvidence(reason="A1 未取得一个置信度足够且无歧义的登录姓名")
    span = names[0]
    if rail.width >= 120 * scale and span.box.left - rail.left >= 44 * scale:
        # Expanded rail: the profile picture is immediately left of the account name.
        cy = span.box.center[1]
        avatar_box = Box(rail.left + round(14 * scale), max(top.top, cy - round(18 * scale)),
                         span.box.left - round(7 * scale), min(top.bottom, cy + round(18 * scale)))
    else:
        avatar_box = Box(top.left + round(.09 * rail.width), top.top + round(4 * scale),
                         top.right - round(.09 * rail.width), span.box.top - round(2 * scale))
    avatar = ""
    if avatar_box.height >= 10:
        tile = cv2.resize(avatar_box.crop(pixels), (24, 12), interpolation=cv2.INTER_AREA)
        # A flat rail background is not an avatar. Retain only the fingerprint.
        colors = tile // 16
        if float(np.max(tile.std(axis=(0, 1)))) >= 9 and len(np.unique(colors.reshape(-1, 3), axis=0)) >= 14:
            avatar = _signature(colors)
    return LoginEvidence(span.text.strip(), avatar, top, _signature(top.crop(pixels)))
