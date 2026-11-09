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
class DisplayTransform:
    """One explicit mapping between screenshot source pixels and Qt logical widget pixels."""

    source_width: int
    source_height: int
    widget_width: int
    widget_height: int
    displayed_width: float
    displayed_height: float
    offset_x: float
    offset_y: float
    device_pixel_ratio: float = 1.0

    @property
    def display_right(self) -> float:
        return self.offset_x + self.displayed_width

    @property
    def display_bottom(self) -> float:
        return self.offset_y + self.displayed_height


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
    device_pixel_ratio: float = 1.0


def create_display_transform(source_width: int, source_height: int,
                             widget_width: int, widget_height: int,
                             device_pixel_ratio: float = 1.0) -> DisplayTransform:
    if min(source_width, source_height, widget_width, widget_height) <= 0:
        raise ValueError("截图和承载控件尺寸必须大于零")
    if device_pixel_ratio <= 0:
        raise ValueError("devicePixelRatio 必须大于零")
    # QWidget mouse events and widget geometry are both in Qt logical pixels.
    # The image is painted into this explicit rectangle, so DPR is diagnostic
    # metadata only and never used as a guessed correction factor.
    scale = min(widget_width / source_width, widget_height / source_height)
    displayed_width = source_width * scale
    displayed_height = source_height * scale
    return DisplayTransform(
        source_width, source_height, widget_width, widget_height,
        displayed_width, displayed_height,
        (widget_width - displayed_width) / 2,
        (widget_height - displayed_height) / 2,
        device_pixel_ratio,
    )


def widget_point_to_normalized(x: float, y: float, transform: DisplayTransform) -> tuple[float, float]:
    if not (transform.offset_x <= x < transform.display_right and
            transform.offset_y <= y < transform.display_bottom):
        raise ValueError("鼠标点位于截图图片之外的留白区域")
    return ((x - transform.offset_x) / transform.displayed_width,
            (y - transform.offset_y) / transform.displayed_height)


def normalized_point_to_widget(x: float, y: float, transform: DisplayTransform) -> tuple[float, float]:
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError("normalized 点必须在 0..1 范围内")
    return (transform.offset_x + x * transform.displayed_width,
            transform.offset_y + y * transform.displayed_height)


def widget_rect_to_normalized(left: float, top: float, right: float, bottom: float,
                              transform: DisplayTransform) -> NormalizedRect:
    if not (left < right and top < bottom):
        raise ValueError("拖框矩形尺寸无效")
    x1, y1 = widget_point_to_normalized(left, top, transform)
    x2, y2 = widget_point_to_normalized(right, bottom, transform)
    return NormalizedRect(x1, y1, x2, y2)


def normalized_rect_to_widget(rect: NormalizedRect, transform: DisplayTransform) -> tuple[float, float, float, float]:
    left, top = normalized_point_to_widget(rect.left, rect.top, transform)
    right, bottom = normalized_point_to_widget(rect.right, rect.bottom, transform)
    return left, top, right, bottom


def normalized_rect_to_source(rect: NormalizedRect, source_width: int,
                              source_height: int) -> tuple[int, int, int, int]:
    return (round(rect.left * source_width), round(rect.top * source_height),
            round(rect.right * source_width), round(rect.bottom * source_height))


def relative_rect_from_pixels(left: int, top: int, right: int, bottom: int,
                              width: int, height: int) -> NormalizedRect:
    if width <= 0 or height <= 0 or not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("截图区域无效")
    return NormalizedRect(left / width, top / height, right / width, bottom / height)


def relative_rect_to_pixels(rect: NormalizedRect, width: int, height: int) -> tuple[int, int, int, int]:
    return normalized_rect_to_source(rect, width, height)


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
