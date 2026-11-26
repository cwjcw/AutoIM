"""A non-activating, input-transparent view of the current test phase."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from autoim.wecom.collection_runtime import PC_VERSION
from autoim.wecom.collection_test import PHASES


class CollectionBubble(QWidget):
    def __init__(self):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("AutoIM 测试阶段")
        self.setObjectName("collectionBubble")
        self.setStyleSheet(
            "QWidget#collectionBubble { background: #112133; border: 1px solid #416388; }"
            "QLabel { color: #ebf5ff; }"
            "QProgressBar { background: #29415b; border: 0px; max-height: 5px; }"
            "QProgressBar::chunk { background: #59b8ff; }"
        )
        layout = QVBoxLayout(self)
        self.setFixedWidth(360)
        self.text = QLabel()
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setWordWrap(True)
        layout.addWidget(self.text)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        layout.addWidget(self.bar)
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self.hide)
        self._finalized = False

    def update_test(self, progress, core, shortcut, *, waiting_for=None, target_screen=None, unread_markers=0):
        final = progress["finished"]
        if final and self._finalized:
            return
        if not final:
            self._finalized = False
            self._hide.stop()
        number = progress["number"]
        name = PHASES[number][0] if number else "准备"
        reason = progress["reason"] or core.risk or waiting_for or ""
        counters = (f"未读徽标 {unread_markers} · 可处理已发现 {core.discovered}\n"
                    f"点击 {core.clicks} · 会话确认 {core.verified} · "
                    f"已读取 {len(core.messages)} · 失败 {core.failures + len(core.blockers)}")
        if final:
            self._finalized = True
            outcomes = tuple(progress["outcomes"].values())
            summary = (f"总耗时 {progress['total']:.1f} 秒\n"
                       f"通过 {outcomes.count('AUTO_PASS')} · 部分 {outcomes.count('PARTIAL')} · "
                       f"失败/超时 {outcomes.count('FAILED') + outcomes.count('TIMEOUT')} · "
                       f"跳过/依赖 {outcomes.count('SKIPPED') + outcomes.count('BLOCKED_BY_DEPENDENCY')}")
            issue = progress["last_issue"]
            detail = f"\n最近异常 {issue}" if issue and issue != reason else ""
            body = f"AutoIM {PC_VERSION} · 本轮测试结束\n{summary}\n{counters}\n{reason}{detail}\n详细结果已保留在 AutoIM"
            self._hide.start(9000)
        else:
            label = "阶段已超时，正在收尾" if progress["awaiting_timeout"] else progress["operation"] or "进入下一阶段"
            title = f"阶段 {number or 1}/7 · {name}"
            previous = progress["previous"]
            outcomes = {"AUTO_PASS": "通过", "PARTIAL": "部分通过", "FAILED": "失败", "TIMEOUT": "超时",
                        "SKIPPED": "跳过", "BLOCKED_BY_DEPENDENCY": "依赖阻断"}
            transition = f"上一阶段 {previous} · {outcomes[progress['outcomes'][previous]]}\n" if previous else ""
            body = (f"AutoIM {PC_VERSION} · 测试中\n{title}\n{label}\n"
                    f"{'联系人 ' + core.contact + ' · ' if core.contact else ''}"
                    f"已耗时 {progress['elapsed']:.1f} 秒 · 剩余 {progress['remaining']:.1f} 秒\n"
                    f"{counters}\n{transition}{reason + chr(10) if reason else ''}{shortcut} 停止检测")
        self.text.setText(body)
        ratio = progress["elapsed"] / progress["duration"] if progress["duration"] else 0
        self.bar.setValue(1000 if final or progress["awaiting_timeout"] else min(1000, round(ratio * 1000)))
        self.adjustSize()
        screens = QGuiApplication.screens()
        if screens:
            # Keep the overlay off the target monitor when another one is available.
            display = (next((item for item in screens if item != target_screen), target_screen)
                       if target_screen else QGuiApplication.primaryScreen())
            area = display.availableGeometry()
            self.move(max(area.left(), area.right() - self.width() - 14),
                      max(area.top(), area.bottom() - self.height() - 14))
        if not self.isVisible():
            self.show()
