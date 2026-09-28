from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizedRect:
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        if not (0 <= self.left < self.right <= 1 and 0 <= self.top < self.bottom <= 1):
            raise ValueError("区域必须是窗口内有效的 0..1 相对坐标")

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom


@dataclass(frozen=True, slots=True)
class WindowCapture:
    pid: int
    hwnd: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]
    width: int
    height: int
    image: object


def relative_rect_from_pixels(left: int, top: int, right: int, bottom: int,
                              width: int, height: int) -> NormalizedRect:
    if width <= 0 or height <= 0 or not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("截图区域无效")
    return NormalizedRect(left / width, top / height, right / width, bottom / height)


def relative_rect_to_pixels(rect: NormalizedRect, width: int, height: int) -> tuple[int, int, int, int]:
    return (round(rect.left * width), round(rect.top * height),
            round(rect.right * width), round(rect.bottom * height))


def image_point_to_relative(x: int, y: int, width: int, height: int) -> tuple[float, float]:
    if width <= 0 or height <= 0 or not (0 <= x < width and 0 <= y < height):
        raise ValueError("选取点位于截图范围之外")
    return x / width, y / height


def image_point_to_screen(x: int, y: int, width: int, height: int,
                          window_rect: tuple[int, int, int, int]) -> tuple[int, int]:
    rx, ry = image_point_to_relative(x, y, width, height)
    left, top, right, bottom = window_rect
    return round(left + rx * (right - left)), round(top + ry * (bottom - top))


def validate_click_target(x: int, y: int, width: int, height: int,
                          window_rect: tuple[int, int, int, int], chat_area: NormalizedRect) -> tuple[int, int]:
    rx, ry = image_point_to_relative(x, y, width, height)
    if not chat_area.contains(rx, ry):
        raise ValueError("选取点不在已标定聊天消息区域内")
    point = image_point_to_screen(x, y, width, height, window_rect)
    left, top, right, bottom = window_rect
    if not (left <= point[0] < right and top <= point[1] < bottom):
        raise ValueError("选取点位于企业微信窗口之外")
    return point
