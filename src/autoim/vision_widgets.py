from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from autoim.vision import (
    DisplayTransform, NormalizedRect, create_display_transform,
    normalized_point_to_widget, normalized_rect_to_source,
    normalized_rect_to_widget, widget_point_to_normalized,
    widget_rect_to_normalized,
)


class ScreenshotCanvas(QWidget):
    """Draw into one explicit KeepAspectRatio rectangle and map input through it."""

    selectionChanged = Signal()

    def __init__(self, pixmap: QPixmap, mode: str, allowed: NormalizedRect | None = None,
                 parent=None):
        super().__init__(parent)
        self.source_dpr = float(pixmap.devicePixelRatioF())
        self.source_width = int(pixmap.width())
        self.source_height = int(pixmap.height())
        # Normalize DPR metadata because all later drawing uses explicit target
        # widget coordinates while retaining the original source pixel dimensions.
        self.pixmap = pixmap.copy()
        self.pixmap.setDevicePixelRatio(1.0)
        self.mode = mode
        self.allowed = allowed
        self._drag_start: tuple[float, float] | None = None
        self._drag_end: tuple[float, float] | None = None
        self.selected_point: tuple[float, float] | None = None
        self.selected_widget_rect: tuple[float, float, float, float] | None = None
        self.selected_normalized_rect: NormalizedRect | None = None
        self.selection_error = ""
        self.transform = self._make_transform()
        self.setMinimumSize(640, 400)
        self.setMouseTracking(True)

    def _make_transform(self) -> DisplayTransform:
        return create_display_transform(
            self.source_width, self.source_height,
            max(1, self.width()), max(1, self.height()), self.source_dpr,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.transform = self._make_transform()
        self.update()
        self.selectionChanged.emit()

    def paintEvent(self, event):
        self.transform = self._make_transform()
        t = self.transform
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202833"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(QRectF(t.offset_x, t.offset_y, t.displayed_width, t.displayed_height),
                           self.pixmap, QRectF(0, 0, self.source_width, self.source_height))
        if self.allowed is not None:
            l, top, r, b = normalized_rect_to_widget(self.allowed, t)
            painter.setPen(QPen(QColor("#22c55e"), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(l, top, r-l, b-top))
        if self.selected_normalized_rect is not None:
            l, top, r, b = normalized_rect_to_widget(self.selected_normalized_rect, t)
            painter.setPen(QPen(QColor("#ef4444"), 2))
            painter.setBrush(QColor(239, 68, 68, 35))
            painter.drawRect(QRectF(l, top, r-l, b-top))
        elif self._drag_start is not None and self._drag_end is not None:
            x1, y1 = self._drag_start
            x2, y2 = self._drag_end
            painter.setPen(QPen(QColor("#ef4444"), 2))
            painter.setBrush(QColor(239, 68, 68, 35))
            painter.drawRect(QRectF(min(x1, x2), min(y1, y2), abs(x2-x1), abs(y2-y1)))
        elif self.selected_point is not None:
            x, y = normalized_point_to_widget(*self.selected_point, t)
            painter.setPen(QPen(QColor("#ef4444"), 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(x-10, y-10, 20, 20))
        painter.end()

    @property
    def selected_source_rect(self) -> tuple[int, int, int, int] | None:
        if self.selected_normalized_rect is None:
            return None
        return normalized_rect_to_source(self.selected_normalized_rect, self.source_width, self.source_height)

    @property
    def reverse_mapped_widget_rect(self) -> tuple[float, float, float, float] | None:
        if self.selected_normalized_rect is None:
            return None
        return normalized_rect_to_widget(self.selected_normalized_rect, self.transform)

    @property
    def round_trip_error_px(self) -> float | None:
        if self.selected_widget_rect is None or self.reverse_mapped_widget_rect is None:
            return None
        return max(abs(a-b) for a, b in zip(self.selected_widget_rect, self.reverse_mapped_widget_rect))

    @property
    def selection_valid(self) -> bool:
        return self.selected_normalized_rect is not None and self.round_trip_error_px is not None and self.round_trip_error_px <= 2.0

    def diagnostics(self) -> str:
        t = self.transform
        return "\n".join((
            f"Source screenshot: {self.source_width}x{self.source_height}",
            f"QPixmap original: {self.source_width}x{self.source_height}",
            f"Widget: {self.width()}x{self.height()}",
            f"Displayed pixmap: {t.displayed_width:.2f}x{t.displayed_height:.2f}",
            f"Pixmap offset: {t.offset_x:.2f},{t.offset_y:.2f}",
            f"devicePixelRatio: {self.source_dpr:g}",
            f"Dragged widget rect: {self.selected_widget_rect}",
            f"Converted source rect: {self.selected_source_rect}",
            f"Normalized rect: {self.selected_normalized_rect}",
            f"Reverse-mapped widget rect: {self.reverse_mapped_widget_rect}",
            f"Round-trip max error: {self.round_trip_error_px if self.round_trip_error_px is not None else '—'} px",
        ))

    def _event_normalized(self, event) -> tuple[float, float]:
        self.transform = self._make_transform()
        pos = event.position()
        return widget_point_to_normalized(pos.x(), pos.y(), self.transform)

    def mousePressEvent(self, event):
        if self.mode == "preview":
            return super().mousePressEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        try:
            point = self._event_normalized(event)
        except ValueError as exc:
            self.selection_error = str(exc)
            self._drag_start = self._drag_end = None
            self.selected_point = None
            self.selected_normalized_rect = None
            self.update()
            self.selectionChanged.emit()
            event.accept()
            return
        self.selection_error = ""
        self.selected_normalized_rect = None
        if self.mode == "point":
            self.selected_point = point
            self.selected_widget_rect = None
        else:
            self._drag_start = (event.position().x(), event.position().y())
            self._drag_end = self._drag_start
        self.update()
        self.selectionChanged.emit()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_start is not None:
            self._drag_end = (event.position().x(), event.position().y())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.mode == "preview":
            return super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        if self.mode == "region" and self._drag_start is not None:
            self._drag_end = (event.position().x(), event.position().y())
            x1, y1 = self._drag_start
            x2, y2 = self._drag_end
            self._drag_start = self._drag_end = None
            self.selected_widget_rect = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            try:
                self.selected_normalized_rect = widget_rect_to_normalized(*self.selected_widget_rect, self._make_transform())
                if not self.selection_valid:
                    self.selected_normalized_rect = None
                    self.selection_error = "标定往返偏差超过 2 px，拒绝该标定。"
            except ValueError as exc:
                self.selected_normalized_rect = None
                self.selection_error = str(exc)
        self.transform = self._make_transform()
        self.update()
        self.selectionChanged.emit()
        event.accept()


class ScreenshotSelectionDialog(QDialog):
    def __init__(self, pixmap: QPixmap, *, mode: str, title: str,
                 allowed: NormalizedRect | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 720)
        self.canvas = ScreenshotCanvas(pixmap, mode, allowed)
        self.view = self.canvas
        layout = QVBoxLayout(self)
        instruction = ("在截图中拖动鼠标框选区域，再点击确定。" if mode == "region" else
                       "在截图中单击一个位置，再点击确定。绿色虚线是已标定聊天区域。" if mode == "point" and allowed else
                       "在截图中单击一个位置，再点击确定。" if mode == "point" else
                       "截图中的绿色线框为保存后从配置文件重新读取的窗口相对区域。")
        layout.addWidget(QLabel(instruction))
        layout.addWidget(self.canvas, 1)
        self.diagnostic_label = QLabel("")
        self.diagnostic_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.diagnostic_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.button_box = buttons
        self.canvas.selectionChanged.connect(self._refresh_diagnostics)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _refresh_diagnostics(self):
        self.diagnostic_label.setText(self.canvas.diagnostics() + ("\n选择错误：" + self.canvas.selection_error if self.canvas.selection_error else ""))
        ok = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        if self.canvas.mode == "region":
            ok.setEnabled(self.canvas.selection_valid)
        elif self.canvas.mode == "point":
            ok.setEnabled(self.canvas.selected_point is not None)
        else:
            ok.setEnabled(True)

    def _accept_if_valid(self):
        self._refresh_diagnostics()
        if self.canvas.mode == "region" and not self.canvas.selection_valid:
            return
        if self.canvas.mode == "point" and self.canvas.selected_point is None:
            return
        self.accept()

    @property
    def selected_rect(self):
        return self.canvas.selected_source_rect

    @property
    def selected_normalized_rect(self):
        return self.canvas.selected_normalized_rect

    @property
    def selected_widget_rect(self):
        return self.canvas.selected_widget_rect

    @property
    def reverse_mapped_widget_rect(self):
        return self.canvas.reverse_mapped_widget_rect

    @property
    def selection_valid(self):
        return self.canvas.selection_valid

    @property
    def round_trip_error_px(self):
        return self.canvas.round_trip_error_px

    @property
    def diagnostics(self):
        return self.canvas.diagnostics()

    @property
    def selected_point(self):
        if self.canvas.selected_point is None:
            return None
        x, y = self.canvas.selected_point
        return round(x * self.canvas.source_width), round(y * self.canvas.source_height)
