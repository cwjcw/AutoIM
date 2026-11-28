"""Recognize native C2 controls and screenshot-backed media boundaries in image coordinates."""

import re

import cv2
import numpy as np

from autoim.wecom.visual_layout import Box


def same_history_viewport(before, after):
    # Allow measured raster jitter at the title and composer seams. Keep
    # the physical column and register only the common C2 pixels.
    return bool(
        before
        and after
        and abs(before.top - after.top) <= 4
        and abs(before.bottom - after.bottom) <= 2
        and (before.left, before.right) == (after.left, after.right)
    )


def _near(span, area, unit):
    box = span.box
    pad = round(38 * unit)
    return Box(
        max(area.left, box.left - pad),
        max(area.top, box.top - pad),
        min(area.right, box.right + pad),
        min(area.bottom, box.bottom + pad),
    )


def history_controls(pixels, area, spans, unit):
    found = {}
    for span in spans:
        label = "".join(span.text.split())
        if span.score < 0.82 or not area.contains(span.box.center):
            continue
        box = span.box
        if label == "以下为新消息" and abs(box.center[0] - area.center[0]) < area.width * 0.10:
            # Require gray rules on both sides at the same height.
            local = area.crop(pixels)
            gray = cv2.cvtColor(local, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 8, 25)
            y = box.center[1] - area.top
            strip = edges[max(0, y - round(5 * unit)) : y + round(5 * unit) + 1]
            width = max(10, round(24 * unit))
            lines = cv2.morphologyEx(strip, cv2.MORPH_OPEN, np.ones((1, width), np.uint8))
            left, right = box.left - area.left, box.right - area.left
            if (
                left > width
                and right + width < area.width
                and any(
                    line[max(0, left - 4 * width) : left].any() and line[right : right + 4 * width].any()
                    for line in lines
                )
            ):
                found[span] = ("C2_N2", box)
            continue
        if not re.fullmatch(r"[1-9]\d{0,3}条新消息", label):
            continue
        outer = _near(span, area, unit)
        tile = outer.crop(pixels)
        blue, green, red = tile.astype(np.int16).transpose(2, 0, 1)
        ink = ((blue - red > 48) & (blue > 145) & (green > red + 16)).astype(np.uint8)
        contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        arrows = sorted(
            (x, y, w, h)
            for c in contours
            for x, y, w, h in [cv2.boundingRect(c)]
            if 4 * unit <= w <= 16 * unit
            and 2 * unit <= h <= 9 * unit
            and x + outer.left < box.left + box.width * 0.23
            and w >= h * 1.35
        )
        paired = any(
            abs(x - X) <= 2 * unit and abs(w - W) <= 2 * unit and h * 0.65 <= Y - y <= h * 1.6
            for x, y, w, h in arrows
            for X, Y, W, H in arrows
        )
        if not paired:
            continue
        contours, _ = cv2.findContours(
            (tile.min(axis=2) > 249).astype("uint8"), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            frame = Box(x + outer.left, y + outer.top, x + w + outer.left, y + h + outer.top)
            if (
                x
                and y
                and x + w < outer.width
                and y + h < outer.height
                and 1.8 <= w / h <= 8
                and frame.contains(box.center)
                and box.right <= frame.right
                and box.bottom <= frame.bottom
                and cv2.contourArea(c) > 0.82 * w * h
            ):
                found[span] = ("C2_N1", frame)
                break
    return found


def edge_bubbles(pixels, area, spans, unit):
    """Continuous, contrasting side edges of a text fill cut by the viewport."""
    tile = area.crop(pixels)
    sample = np.concatenate((tile[2:5, ::4].reshape(-1, 3), tile[-5:-2, ::4].reshape(-1, 3)))
    colors, counts = np.unique(sample, axis=0, return_counts=True)
    boxes = []
    for index in np.argsort(counts)[-4:]:
        color = colors[index].astype(float)
        mask = cv2.inRange(tile, tuple(color - 4), tuple(color + 4))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if (
                not (y < 4 * unit or y + h > area.height - 4 * unit)
                or h < 45 * unit
                or not area.width * 0.12 < w < area.width * 0.9
                or x < 3
                or x + w + 3 >= area.width
                or cv2.contourArea(contour) < 0.88 * w * h
            ):
                continue
            for side in (tile[y : y + h, x - 3], tile[y : y + h, x + w + 2]):
                if np.mean(np.max(np.abs(side - color), axis=1) > 7) < 0.75:
                    break
            else:
                box = Box(x + area.left, y + area.top, x + w + area.left, y + h + area.top)
                inside = [s for s in spans if box.contains(s.box.center)]
                if inside and all(s.score >= 0.78 for s in inside) and box not in boxes:
                    boxes.append(box)
    return boxes


def report_images(pixels, area, spans, unit):
    # Dense coloured rectangular panels frequently contain dozens of tiny OCR
    # fragments. Internal table cells must share an actual outer component.
    tile = area.crop(pixels)
    hsv = cv2.cvtColor(tile, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 1] > 50) & (hsv[:, :, 2] < 220)).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    images = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < 90 * unit or h < 65 * unit or cv2.contourArea(c) < w * h * 0.86 or w > area.width * 0.9:
            continue
        box = Box(area.left + x, area.top + y, area.left + x + w, area.top + y + h)
        words = [s for s in spans if box.contains(s.box.center)]
        if len(words) >= 6 and len({round(s.box.center[0] / (20 * unit)) for s in words}) >= 3:
            images.append(box)
    return images
