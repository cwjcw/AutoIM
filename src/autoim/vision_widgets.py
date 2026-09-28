from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QLabel, QVBoxLayout

from autoim.vision import NormalizedRect


class _ImageView(QGraphicsView):
    def __init__(self, pixmap: QPixmap, mode: str, allowed: NormalizedRect | None = None):
        super().__init__()
        self.mode = mode
        self.allowed = allowed
        self.start: QPointF | None = None
        self.selected_rect: tuple[int, int, int, int] | None = None
        self.selected_point: tuple[int, int] | None = None
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.pixmap_item = QGraphicsPixmapItem(pixmap)
        self.scene.addItem(self.pixmap_item)
        self.width_px, self.height_px = pixmap.width(), pixmap.height()
        self.marker = QGraphicsRectItem()
        self.marker.setPen(QPen(QColor("#ef4444"), 3))
        self.marker.setBrush(QBrush(QColor(239, 68, 68, 35)))
        self.scene.addItem(self.marker)
        self.allowed_marker = QGraphicsRectItem()
        self.allowed_marker.setPen(QPen(QColor("#22c55e"), 3, Qt.PenStyle.DashLine))
        self.allowed_marker.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.scene.addItem(self.allowed_marker)
        if allowed:
            self.allowed_marker.setRect(allowed.left*self.width_px, allowed.top*self.height_px,
                                        (allowed.right-allowed.left)*self.width_px,
                                        (allowed.bottom-allowed.top)*self.height_px)
        self.setSceneRect(0, 0, self.width_px, self.height_px)
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.setMinimumSize(640, 400)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _image_pos(self, event) -> QPointF:
        point = self.mapToScene(event.position().toPoint())
        return QPointF(min(max(point.x(), 0), self.width_px-1), min(max(point.y(), 0), self.height_px-1))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = self._image_pos(event)
            self.marker.setRect(self.start.x(), self.start.y(), 0, 0)
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.start is not None:
            end = self._image_pos(event)
            self.marker.setRect(min(self.start.x(), end.x()), min(self.start.y(), end.y()),
                                abs(self.start.x()-end.x()), abs(self.start.y()-end.y()))
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.start is not None:
            end = self._image_pos(event)
            if self.mode == "point":
                self.selected_point = (round(end.x()), round(end.y()))
                self.marker.setRect(end.x()-12, end.y()-12, 24, 24)
            else:
                left, right = sorted((round(self.start.x()), round(end.x())))
                top, bottom = sorted((round(self.start.y()), round(end.y())))
                if right > left and bottom > top:
                    self.selected_rect = (left, top, right, bottom)
                    self.marker.setRect(left, top, right-left, bottom-top)
            self.start = None
            event.accept(); return
        super().mouseReleaseEvent(event)


class ScreenshotSelectionDialog(QDialog):
    def __init__(self, pixmap: QPixmap, *, mode: str, title: str,
                 allowed: NormalizedRect | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 720)
        self.view = _ImageView(pixmap, mode, allowed)
        layout = QVBoxLayout(self)
        instruction = "在截图中拖动鼠标框选区域，再点击确定。" if mode == "region" else "在截图中单击一个位置，再点击确定。绿色虚线是已标定聊天区域。" if allowed else "在截图中单击一个位置，再点击确定。"
        layout.addWidget(QLabel(instruction))
        layout.addWidget(self.view)
        layout.addWidget(QDialogButtonBoxProxy(self))

    @property
    def selected_rect(self):
        return self.view.selected_rect

    @property
    def selected_point(self):
        return self.view.selected_point


class QDialogButtonBoxProxy:
    """Tiny button row wrapper kept separate to avoid mutating screenshot scene."""
    def __new__(cls, parent):
        from PySide6.QtWidgets import QDialogButtonBox
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(parent.accept)
        buttons.rejected.connect(parent.reject)
        return buttons
