"""Non-activating desktop feedback for committed new messages."""

from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon

from autoim.wecom.collection_events import EventTime, SaveNotice


class CollectionNotifier(QObject):
    reported = Signal(object, str, object)
    submitting = Signal(object)

    def __init__(self, parent=None, *, tray_factory=QSystemTrayIcon):
        super().__init__(parent)
        self._tray_factory = tray_factory
        self._tray = None
        self._recent = deque(maxlen=128)
        self._pending = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.flush)

    def enqueue(self, notice: SaveNotice | None):
        if notice is None or not notice.id or notice.count <= 0 or notice.id in self._recent:
            return
        self._recent.append(notice.id)
        self._pending.append(notice)
        if not self._timer.isActive():
            self._timer.start(2200)

    def _system_tray(self):
        if not self._tray_factory.isSystemTrayAvailable() or not self._tray_factory.supportsMessages():
            return None
        if self._tray is None:
            app = QApplication.instance()
            icon = app.windowIcon()
            if icon.isNull():
                icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self._tray = self._tray_factory(icon, self)
            self._tray.setToolTip("AutoIM")
            self._tray.show()
        return self._tray

    def announce(self, title, text):
        try:
            tray = self._system_tray()
            if tray:
                tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information)
        except Exception:
            pass

    def flush(self):
        self._timer.stop()
        notices, self._pending = self._pending, []
        if not notices:
            return
        groups = (tuple(notice for notice in notices if not notice.test_run),
                  tuple(notice for notice in notices if notice.test_run))
        for group in groups:
            if group:
                self._deliver(group)

    def _deliver(self, notices):
        count = sum(notice.count for notice in notices)
        test = bool(notices[0].test_run)
        result = "系统桌面通知不可用"
        try:
            tray = self._system_tray()
            if tray:
                latest = notices[-1]
                durations = latest.trace.durations()
                total = durations.get("unread_to_commit_ms")
                title = latest.title[:60] if len(notices) == 1 else f"{len(notices)} 批采集"
                duration = f" · 耗时 {total / 1000:.1f} 秒" if total is not None and len(notices) == 1 else ""
                self.submitting.emit(tuple(notices))
                tray.showMessage("TEST · AutoIM 隔离测试" if test else "AutoIM 采集成功", f"{title or '当前会话'} · 新增 {count} 条消息 · 已保存{duration}",
                                       QSystemTrayIcon.MessageIcon.Information)
                result = "已提交给系统通知"
        except Exception:
            result = "系统通知调用失败"
        self.reported.emit(tuple(notices), result, EventTime.now())
