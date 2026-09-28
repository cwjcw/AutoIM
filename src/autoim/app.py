from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from autoim import __version__
from autoim.automation.clipboard import ClipboardSnapshot
from autoim.clipboard_diagnostics import run_manual_copy_test, update_manual_copy_report
from autoim.diagnostics import run_multi_backend_diagnostics
from autoim.wecom.driver import WeComDriver
from autoim.wecom.window_manager import WeComWindowInfo
from autoim.workers import ClientInfo, FunctionWorker, detect_wecom, scan_uia_tree

ROOT = Path(__file__).resolve().parents[2]


class LogBus(QObject):
    message = Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, bus: LogBus) -> None:
        super().__init__()
        self.bus = bus
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.bus.message.emit(self.format(record))


def setup_logging(bus: LogBus) -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    file_handler = logging.FileHandler(log_dir / "autoim.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
    root_logger.addHandler(file_handler)
    root_logger.addHandler(QtLogHandler(bus))
    logging.info("AutoIM %s 启动", __version__)


def label(text: str, style: str = "") -> QLabel:
    widget = QLabel(text)
    if style:
        widget.setStyleSheet(style)
    return widget


def card(title: str, value: str, note: str = "") -> tuple[QFrame, QLabel, QLabel]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.addWidget(label(title, "color:#8290a5;font-size:13px;font-weight:600"))
    value_label = label(value, "font-size:24px;font-weight:700;color:#172235")
    layout.addWidget(value_label)
    note_label = label(note, "color:#8290a5;font-size:12px")
    layout.addWidget(note_label)
    return frame, value_label, note_label


class MainWindow(QMainWindow):
    def __init__(self, log_bus: LogBus) -> None:
        super().__init__()
        self.log_bus = log_bus
        self.thread_pool = QThreadPool.globalInstance()
        self._workers: set[FunctionWorker] = set()
        self.client: ClientInfo | None = None
        self.setWindowTitle("AutoIM")
        self.resize(1120, 760)
        self.setMinimumSize(900, 620)
        self._build_ui()
        self.log_bus.message.connect(self.append_log)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 18, 18)
        side.setSpacing(8)
        side.addWidget(label("AutoIM", "font-size:24px;font-weight:800;color:#f4f7fb;padding:4px 8px"))
        side.addWidget(label("桌面自动化工作台", "font-size:12px;color:#90a0b8;padding:0 8px 18px"))
        self.nav_buttons: list[QPushButton] = []
        self.stack = QStackedWidget()
        pages = [self._home_page(), self._messages_page(), self._wecom_page(), self._simple_page("AI 设置", "本阶段不包含 AI 或 LLM 能力。"), self._simple_page("系统设置", "当前版本使用默认路径与日志设置。"), self._logs_page()]
        nav_names = ["首页", "消息", "企业微信", "AI 设置", "系统设置", "日志"]
        for index, name in enumerate(nav_names):
            button = QPushButton(name)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self.show_page(i))
            self.nav_buttons.append(button)
            side.addWidget(button)
            self.stack.addWidget(pages[index])
        side.addStretch(1)
        side.addWidget(label(f"Windows · v{__version__}", "font-size:11px;color:#8290a5;padding:8px"))
        root.addWidget(sidebar)
        content = QFrame()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(34, 28, 34, 28)
        content_layout.addWidget(self.stack)
        root.addWidget(content, 1)
        self.show_page(0)

    def _page_shell(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        layout.addWidget(label(title, "font-size:27px;font-weight:750;color:#172235"))
        layout.addWidget(label(subtitle, "font-size:13px;color:#7c899c"))
        return page, layout

    def _home_page(self) -> QWidget:
        page, layout = self._page_shell("首页", "企业微信连接与本地 Agent 运行概览")
        cards = QHBoxLayout()
        c, self.home_connection, self.home_connection_note = card("企业微信连接", "未检测", "等待检测")
        cards.addWidget(c)
        c, self.home_agent, _ = card("Agent 运行状态", "未启动", "当前阶段仅提供桌面检测能力")
        cards.addWidget(c)
        c, self.home_messages, _ = card("消息数量", "0", "当前阶段未读取消息")
        cards.addWidget(c)
        layout.addLayout(cards)
        panel = QFrame(); panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel); panel_layout.setContentsMargins(22, 20, 22, 20)
        panel_layout.addWidget(label("快速开始", "font-size:16px;font-weight:700;color:#172235"))
        panel_layout.addWidget(label("前往「企业微信」页面检测客户端进程、主窗口和 UI Automation 可访问性。", "color:#66758a"))
        self.home_detect_button = QPushButton("检测企业微信")
        self.home_detect_button.setObjectName("primaryButton")
        self.home_detect_button.clicked.connect(self.start_detection)
        panel_layout.addWidget(self.home_detect_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(panel)
        layout.addStretch(1)
        return page

    def _messages_page(self) -> QWidget:
        page, layout = self._page_shell("消息", "消息读取功能不在当前阶段范围内")
        panel = QFrame(); panel.setObjectName("panel")
        box = QVBoxLayout(panel); box.setContentsMargins(22, 22, 22, 22)
        box.addWidget(label("消息数量：0", "font-size:18px;font-weight:700;color:#172235"))
        box.addWidget(label("当前版本不会读取消息、监听未读状态或自动回复。", "color:#66758a"))
        layout.addWidget(panel); layout.addStretch(1)
        return page

    def _wecom_page(self) -> QWidget:
        page, layout = self._page_shell("企业微信", "检测 Windows 客户端，并验证 UI Automation 能力")
        panel = QFrame(); panel.setObjectName("panel")
        box = QVBoxLayout(panel); box.setContentsMargins(22, 20, 22, 20); box.setSpacing(14)
        self.connection_status = label("状态：尚未检测", "font-size:16px;font-weight:700;color:#172235")
        box.addWidget(self.connection_status)
        self.info_labels: dict[str, QLabel] = {}
        for key, title in [("pid", "进程 PID"), ("path", "程序路径"), ("title", "主窗口标题"), ("hwnd", "窗口句柄"), ("class_name", "ClassName"), ("uia", "UI Automation")]:
            row = QHBoxLayout(); row.addWidget(label(title, "color:#8290a5;min-width:130px"))
            value = label("—", "color:#27364b"); value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(value, 1); box.addLayout(row); self.info_labels[key] = value
        self.detect_button = QPushButton("检测企业微信")
        self.detect_button.setObjectName("primaryButton")
        self.detect_button.clicked.connect(self.start_detection)
        box.addWidget(self.detect_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(panel)

        scan_panel = QFrame(); scan_panel.setObjectName("panel")
        scan = QVBoxLayout(scan_panel); scan.setContentsMargins(22, 20, 22, 20); scan.setSpacing(12)
        scan.addWidget(label("UI Automation 扫描", "font-size:16px;font-weight:700;color:#172235"))
        controls = QHBoxLayout(); controls.addWidget(label("最大扫描深度"))
        self.depth = QSpinBox(); self.depth.setRange(0, 30); self.depth.setValue(6); self.depth.setSuffix(" 层")
        controls.addWidget(self.depth); controls.addStretch(1)
        self.scan_button = QPushButton("开始扫描")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.clicked.connect(self.start_scan)
        controls.addWidget(self.scan_button); scan.addLayout(controls)
        self.scan_progress = QProgressBar(); self.scan_progress.setTextVisible(False); self.scan_progress.setMaximumHeight(6); self.scan_progress.hide()
        scan.addWidget(self.scan_progress)
        self.scan_output = QPlainTextEdit(); self.scan_output.setReadOnly(True); self.scan_output.setPlaceholderText("扫描结果将在此显示，并写入 outputs/wecom-uia-tree.txt")
        self.scan_output.setMinimumHeight(190)
        scan.addWidget(self.scan_output)
        layout.addWidget(scan_panel, 1)

        diagnostics_panel = QFrame(); diagnostics_panel.setObjectName("panel")
        diag = QVBoxLayout(diagnostics_panel); diag.setContentsMargins(22, 20, 22, 20); diag.setSpacing(12)
        diag.addWidget(label("UI Automation 诊断", "font-size:16px;font-weight:700;color:#172235"))
        diag.addWidget(label("对同一企业微信主窗口依次运行 uiautomation、pywinauto UIA / Win32 和 UIA Raw View。", "color:#66758a"))
        params = QHBoxLayout()
        params.addWidget(label("最大深度")); self.diag_depth = QSpinBox(); self.diag_depth.setRange(0, 50); self.diag_depth.setValue(10); self.diag_depth.setSuffix(" 层"); params.addWidget(self.diag_depth)
        params.addWidget(label("最大控件数")); self.diag_limit = QSpinBox(); self.diag_limit.setRange(1, 50000); self.diag_limit.setValue(5000); params.addWidget(self.diag_limit)
        params.addWidget(label("单 backend 超时")); self.diag_timeout = QSpinBox(); self.diag_timeout.setRange(1, 120); self.diag_timeout.setValue(30); self.diag_timeout.setSuffix(" 秒"); params.addWidget(self.diag_timeout)
        params.addStretch(1)
        self.diag_button = QPushButton("开始多后端诊断"); self.diag_button.setObjectName("primaryButton"); self.diag_button.clicked.connect(self.start_multi_backend_diagnostics); params.addWidget(self.diag_button)
        diag.addLayout(params)
        self.diag_progress = QProgressBar(); self.diag_progress.setTextVisible(False); self.diag_progress.setMaximumHeight(6); self.diag_progress.hide(); diag.addWidget(self.diag_progress)
        self.diag_output = QPlainTextEdit(); self.diag_output.setReadOnly(True); self.diag_output.setPlaceholderText("诊断汇总将显示于此，并保存到 outputs/uia-diagnostics/summary.txt"); self.diag_output.setMinimumHeight(260); diag.addWidget(self.diag_output)
        layout.addWidget(diagnostics_panel)

        clipboard_panel = QFrame(); clipboard_panel.setObjectName("panel")
        clip = QVBoxLayout(clipboard_panel); clip.setContentsMargins(22, 20, 22, 20); clip.setSpacing(12)
        clip.addWidget(label("剪贴板诊断", "font-size:16px;font-weight:700;color:#172235"))
        clip.addWidget(label("先在企业微信手动打开聊天并点击文字消息或聊天区域，再启动测试。测试会清空并替换当前剪贴板；不会自动点击联系人或消息。", "color:#66758a;word-wrap: true"))
        self.clip_contact_name = QLineEdit(); self.clip_contact_name.setPlaceholderText("当前联系人名称（可选，用于文本包含检查）")
        clip.addWidget(self.clip_contact_name)
        buttons = QHBoxLayout()
        self.clip_activate_button = QPushButton("激活企业微信")
        self.clip_read_button = QPushButton("读取当前剪贴板")
        self.clip_ctrl_c_button = QPushButton("发送 Ctrl+C")
        self.clip_ctrl_a_c_button = QPushButton("发送 Ctrl+A + Ctrl+C")
        self.clip_clear_button = QPushButton("清空剪贴板")
        self.clip_test_button = QPushButton("开始手工复制测试")
        for button in (self.clip_activate_button, self.clip_read_button, self.clip_ctrl_c_button,
                       self.clip_ctrl_a_c_button, self.clip_clear_button, self.clip_test_button):
            button.setObjectName("primaryButton" if button is self.clip_test_button else "")
            buttons.addWidget(button)
        clip.addLayout(buttons)
        self.clip_activate_button.clicked.connect(self.clipboard_activate)
        self.clip_read_button.clicked.connect(self.clipboard_read)
        self.clip_ctrl_c_button.clicked.connect(lambda: self.clipboard_send_shortcut("ctrl_c"))
        self.clip_ctrl_a_c_button.clicked.connect(lambda: self.clipboard_send_shortcut("ctrl_a_c"))
        self.clip_clear_button.clicked.connect(self.clipboard_clear)
        self.clip_test_button.clicked.connect(self.start_manual_copy_test)

        extra_buttons = QHBoxLayout()
        self.clip_ctrl_v_button = QPushButton("发送 Ctrl+V")
        self.clip_escape_button = QPushButton("发送 Esc")
        extra_buttons.addWidget(self.clip_ctrl_v_button); extra_buttons.addWidget(self.clip_escape_button); extra_buttons.addStretch(1)
        clip.addLayout(extra_buttons)
        self.clip_ctrl_v_button.clicked.connect(lambda: self.clipboard_send_shortcut("ctrl_v"))
        self.clip_escape_button.clicked.connect(lambda: self.clipboard_send_shortcut("escape"))

        self.clip_status = label("操作状态：等待操作", "font-weight:600;color:#27364b")
        self.clip_last_length = 0
        self.clip_last_type = "未读取"
        self.clip_metadata = label("文本长度：0    内容类型：空    操作时间：—", "color:#8290a5")
        clip.addWidget(self.clip_status); clip.addWidget(self.clip_metadata)
        self.clip_progress = QProgressBar(); self.clip_progress.setTextVisible(False); self.clip_progress.setMaximumHeight(6); self.clip_progress.hide(); clip.addWidget(self.clip_progress)
        self.clip_output = QPlainTextEdit(); self.clip_output.setReadOnly(True); self.clip_output.setPlaceholderText("当前剪贴板文本和手工复制结果显示在这里"); self.clip_output.setMinimumHeight(180); clip.addWidget(self.clip_output)

        review = QHBoxLayout()
        review.addWidget(label("复制文本判断（核对文本后选择）"))
        self.clip_message_classification = QComboBox(); self.clip_message_classification.addItems(["待人工核对", "是", "否"]); review.addWidget(label("包含聊天消息")); review.addWidget(self.clip_message_classification)
        self.clip_unrelated_classification = QComboBox(); self.clip_unrelated_classification.addItems(["待人工核对", "是", "否"]); review.addWidget(label("包含界面无关文本")); review.addWidget(self.clip_unrelated_classification)
        self.clip_save_button = QPushButton("保存核对结果"); self.clip_save_button.setEnabled(False); review.addWidget(self.clip_save_button)
        clip.addLayout(review)
        self.clip_save_button.clicked.connect(self.save_clipboard_report)
        layout.addWidget(clipboard_panel)

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame); scroll.setWidget(page)
        return scroll

    def _simple_page(self, title: str, text: str) -> QWidget:
        page, layout = self._page_shell(title, text)
        layout.addStretch(1)
        return page

    def _logs_page(self) -> QWidget:
        page, layout = self._page_shell("日志", "应用日志同时保存到 logs/autoim.log")
        self.log_output = QPlainTextEdit(); self.log_output.setReadOnly(True); self.log_output.setMaximumBlockCount(3000)
        layout.addWidget(self.log_output, 1)
        return page

    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index)

    def append_log(self, message: str) -> None:
        if hasattr(self, "log_output"):
            self.log_output.appendPlainText(message)

    def _run_worker(self, fn, *args, on_result, on_error=None) -> None:
        worker = FunctionWorker(fn, *args)
        self._workers.add(worker)
        worker.signals.result.connect(on_result)
        worker.signals.error.connect(on_error or self.show_error)
        worker.signals.finished.connect(self._worker_finished)
        self.thread_pool.start(worker)

    def _worker_finished(self, worker: FunctionWorker) -> None:
        self._workers.discard(worker)

    def start_detection(self) -> None:
        self.detect_button.setEnabled(False)
        self.home_detect_button.setEnabled(False)
        self.connection_status.setText("状态：正在检测…")
        logging.info("开始检测企业微信进程和窗口")
        self._run_worker(detect_wecom, on_result=self.detection_finished)

    def detection_finished(self, info: ClientInfo) -> None:
        self.client = info
        self.detect_button.setEnabled(True); self.home_detect_button.setEnabled(True)
        status = "已连接" if info.connected else "未运行"
        self.connection_status.setText(f"状态：{status}" + (f"（{info.detail}）" if info.detail else ""))
        self.info_labels["pid"].setText(str(info.pid or "—"))
        self.info_labels["path"].setText(info.path or "—")
        self.info_labels["title"].setText(info.title or "—")
        self.info_labels["hwnd"].setText(f"0x{info.hwnd:X}" if info.hwnd else "—")
        self.info_labels["class_name"].setText(info.class_name or "—")
        self.info_labels["uia"].setText("可访问" if info.uia_accessible else "不可访问 / 未检测")
        self.home_connection.setText(status)
        self.home_connection_note.setText(info.title or info.detail or "企业微信客户端")
        logging.info("企业微信检测完成：%s", status)

    def start_scan(self) -> None:
        if not self.client or not self.client.connected or not self.client.hwnd:
            QMessageBox.information(self, "需要先检测", "请先检测到正在运行且有主窗口的企业微信客户端。")
            return
        self.scan_button.setEnabled(False)
        self.scan_progress.setRange(0, 0); self.scan_progress.show()
        depth = self.depth.value()
        path = ROOT / "outputs" / "wecom-uia-tree.txt"
        logging.info("开始 UIA 扫描，最大深度 %d", depth)
        self._run_worker(scan_uia_tree, self.client.hwnd, depth, path, on_result=self.scan_finished)

    def start_multi_backend_diagnostics(self) -> None:
        if not self.client or not self.client.connected or not self.client.hwnd:
            QMessageBox.information(self, "需要先检测", "请先检测到正在运行且有主窗口的企业微信客户端。")
            return
        self.diag_button.setEnabled(False)
        self.diag_progress.setRange(0, 0); self.diag_progress.show()
        client = {
            "pid": self.client.pid,
            "hwnd": self.client.hwnd,
            "title": self.client.title,
            "class_name": self.client.class_name,
        }
        depth, limit, timeout = self.diag_depth.value(), self.diag_limit.value(), self.diag_timeout.value()
        output_dir = ROOT / "outputs" / "uia-diagnostics"
        self.diag_output.setPlainText("正在依次执行 4 种 backend；每种 backend 都有独立超时保护…")
        logging.info("开始 UIA 多后端诊断：depth=%d, controls=%d, timeout=%ds", depth, limit, timeout)
        self._run_worker(run_multi_backend_diagnostics, client, depth, limit, timeout, output_dir,
                         on_result=self.multi_backend_finished)

    def multi_backend_finished(self, result: dict) -> None:
        self.diag_button.setEnabled(True); self.diag_progress.hide()
        self.diag_output.setPlainText(result["summary"])
        logging.info("多后端诊断完成：%s", result["output_dir"])

    def _wecom_available(self) -> bool:
        if not self.client or not self.client.connected or not self.client.hwnd:
            QMessageBox.information(self, "需要先检测", "请先检测到正在运行的企业微信客户端。")
            return False
        return True

    def _sync_window_info(self, info: WeComWindowInfo) -> None:
        """Reflect a newly resolved PID/HWND in the GUI after each guarded action."""
        if self.client is None:
            self.client = ClientInfo(True)
        self.client.connected = True
        self.client.pid = info.pid
        self.client.hwnd = info.hwnd
        self.client.title = info.title
        self.client.class_name = info.class_name
        self.client.path = info.process_path or self.client.path
        self.info_labels["pid"].setText(str(info.pid))
        self.info_labels["path"].setText(info.process_path or "—")
        self.info_labels["title"].setText(info.title or "—")
        self.info_labels["hwnd"].setText(f"0x{info.hwnd:X}")
        self.info_labels["class_name"].setText(info.class_name or "—")
        self.connection_status.setText("状态：已连接（窗口已重新验证）")
        self.home_connection.setText("已连接")
        self.home_connection_note.setText(info.title or "企业微信客户端")

    @staticmethod
    def _driver_activate() -> WeComWindowInfo:
        return WeComDriver().activate()

    @staticmethod
    def _driver_read_clipboard() -> dict:
        info, snapshot = WeComDriver().read_clipboard()
        return {"info": info, "snapshot": snapshot}

    @staticmethod
    def _driver_clear_clipboard() -> dict:
        info, snapshot = WeComDriver().clear_clipboard()
        return {"info": info, "snapshot": snapshot}

    @staticmethod
    def _driver_shortcut(shortcut: str) -> WeComWindowInfo:
        return WeComDriver().send_shortcut(shortcut)

    def _set_clipboard_busy(self, operation: str) -> None:
        for button in (self.clip_activate_button, self.clip_read_button, self.clip_ctrl_c_button,
                       self.clip_ctrl_a_c_button, self.clip_clear_button, self.clip_test_button,
                       self.clip_ctrl_v_button, self.clip_escape_button):
            button.setEnabled(False)
        self.clip_progress.setRange(0, 0); self.clip_progress.show()
        self.clip_status.setText(f"操作状态：{operation}…")

    def _clipboard_action_done(self) -> None:
        for button in (self.clip_activate_button, self.clip_read_button, self.clip_ctrl_c_button,
                       self.clip_ctrl_a_c_button, self.clip_clear_button, self.clip_test_button,
                       self.clip_ctrl_v_button, self.clip_escape_button):
            button.setEnabled(True)
        self.clip_progress.hide()

    def _clipboard_snapshot_display(self, snapshot: ClipboardSnapshot, action: str, success: bool = True) -> None:
        self.clip_last_length = len(snapshot.text or "")
        self.clip_last_type = snapshot.content_type
        self.clip_metadata.setText(
            f"文本长度：{self.clip_last_length}    内容类型：{self.clip_last_type}    "
            f"操作时间：{datetime.now().astimezone().isoformat(timespec='seconds')}"
        )
        self.clip_output.setPlainText(snapshot.text or "<当前剪贴板没有 Unicode 文本>")
        self.clip_status.setText(f"操作状态：{action} {'成功' if success else '失败'}")
        logging.info("剪贴板操作%s：类型=%s，字符数=%d", "成功" if success else "失败", snapshot.content_type, len(snapshot.text or ""))

    def clipboard_activate(self) -> None:
        if not self._wecom_available(): return
        self._set_clipboard_busy("激活企业微信")
        self._run_worker(self._driver_activate, on_result=lambda info: self._clipboard_action_success(info, "已激活企业微信"))

    def clipboard_read(self) -> None:
        if not self._wecom_available(): return
        self._set_clipboard_busy("读取剪贴板")
        self._run_worker(self._driver_read_clipboard, on_result=lambda result: self._clipboard_guarded_snapshot_done(result, "读取剪贴板"))

    def clipboard_clear(self) -> None:
        if not self._wecom_available(): return
        self._set_clipboard_busy("清空剪贴板")
        self._run_worker(self._driver_clear_clipboard, on_result=lambda result: self._clipboard_guarded_snapshot_done(result, "清空剪贴板"))

    def _clipboard_guarded_snapshot_done(self, result: dict, action: str) -> None:
        self._sync_window_info(result["info"])
        self._clipboard_snapshot_done(result["snapshot"], action)

    def _clipboard_snapshot_done(self, snapshot: ClipboardSnapshot, action: str) -> None:
        self._clipboard_action_done()
        self._clipboard_snapshot_display(snapshot, action)

    def _clipboard_action_success(self, info: WeComWindowInfo, message: str) -> None:
        self._sync_window_info(info)
        self._clipboard_simple_done(message)

    def clipboard_send_shortcut(self, shortcut: str) -> None:
        if not self._wecom_available(): return
        labels = {"ctrl_c": "Ctrl+C", "ctrl_a_c": "Ctrl+A + Ctrl+C",
                  "ctrl_v": "Ctrl+V", "escape": "Esc"}
        self._set_clipboard_busy(f"激活企业微信并发送 {labels[shortcut]}")
        self._run_worker(self._driver_shortcut, shortcut,
                         on_result=lambda info: self._clipboard_action_success(info, f"已发送 {labels[shortcut]}"))

    def _clipboard_simple_done(self, message: str) -> None:
        self._clipboard_action_done()
        self.clip_status.setText(f"操作状态：{message}")
        self.clip_metadata.setText(
            f"文本长度：{self.clip_last_length}    内容类型：{self.clip_last_type}    "
            f"操作时间：{datetime.now().astimezone().isoformat(timespec='seconds')}"
        )
        logging.info(message)

    def start_manual_copy_test(self) -> None:
        if not self._wecom_available(): return
        self._set_clipboard_busy("开始手工复制测试")
        self.clip_save_button.setEnabled(False)
        self.clip_message_classification.setCurrentIndex(0)
        self.clip_unrelated_classification.setCurrentIndex(0)
        contact_name = self.clip_contact_name.text().strip()
        output_path = ROOT / "outputs" / "clipboard-diagnostics.txt"
        summary_path = ROOT / "outputs" / "clipboard-diagnostics-summary.txt"
        self.clip_output.setPlainText("将依次执行 Ctrl+C 与 Ctrl+A + Ctrl+C，请稍候…")
        self._run_worker(
            run_manual_copy_test, contact_name, output_path, summary_path,
            on_result=self.manual_copy_test_finished,
        )

    def manual_copy_test_finished(self, result: dict) -> None:
        self._clipboard_action_done()
        if result["metadata"].get("hwnd"):
            self._sync_window_info(WeComWindowInfo(
                pid=int(result["metadata"]["pid"]),
                hwnd=int(result["metadata"]["hwnd"]),
                title=result["metadata"]["window_title"],
                class_name=result["metadata"]["class_name"],
                process_path=result["metadata"].get("process_path", ""),
            ))
        self.clip_test_result = result
        self.clip_output.setPlainText(result["transcript"])
        last_attempt = result["results"][-1]
        self.clip_metadata.setText(
            f"文本长度：{last_attempt['text_length']}    内容类型：{last_attempt['content_type']}    "
            f"操作时间：{last_attempt['operation_time']}"
        )
        self.clip_last_length = last_attempt["text_length"]
        self.clip_last_type = last_attempt["content_type"]
        self.clip_status.setText("操作状态：两项手工复制测试完成；请核对文本并选择内容判断后保存")
        self.clip_save_button.setEnabled(True)
        logging.info("剪贴板手工测试完成，汇总文件：%s", result["summary_path"])

    def save_clipboard_report(self) -> None:
        if not hasattr(self, "clip_test_result"):
            return
        result = self.clip_test_result
        contact_name = self.clip_contact_name.text().strip()
        message_state = self.clip_message_classification.currentText()
        unrelated_state = self.clip_unrelated_classification.currentText()
        self.clip_save_button.setEnabled(False)
        self._run_worker(update_manual_copy_report, result, contact_name, message_state, unrelated_state,
                         on_result=self.clipboard_report_saved)

    def clipboard_report_saved(self, rendered: dict) -> None:
        self.clip_test_result.update(rendered)
        self.clip_output.setPlainText(rendered["transcript"])
        self.clip_save_button.setEnabled(True)
        self.clip_status.setText("操作状态：已保存剪贴板诊断与汇总报告")
        logging.info("剪贴板核对结果已保存")

    def scan_finished(self, result: dict) -> None:
        self.scan_button.setEnabled(True); self.scan_progress.hide()
        self.scan_output.setPlainText(result["text"])
        logging.info("扫描结果：%d 个控件，%s", result["count"], result["path"])

    def show_error(self, message: str) -> None:
        self.detect_button.setEnabled(True); self.home_detect_button.setEnabled(True); self.scan_button.setEnabled(True)
        if hasattr(self, "diag_button"):
            self.diag_button.setEnabled(True)
            self.diag_progress.hide()
        if hasattr(self, "clip_progress"):
            self._clipboard_action_done()
        self.scan_progress.hide()
        logging.error("操作失败：%s", message)
        QMessageBox.critical(self, "操作失败", message)


STYLESHEET = """
QMainWindow, QWidget#content { background:#f4f6fa; }
QFrame#sidebar { background:#172235; }
QFrame#content { background:#f4f6fa; }
QFrame#card, QFrame#panel { background:white; border:1px solid #e7ebf1; border-radius:12px; }
QPushButton#navButton { color:#aeb9c9; text-align:left; border:0; border-radius:8px; padding:11px 12px; font-size:14px; }
QPushButton#navButton:hover { background:#25354c; color:#fff; }
QPushButton#navButton:checked { background:#2d5fe8; color:white; font-weight:700; }
QPushButton#primaryButton { background:#2d5fe8; color:white; border:0; border-radius:7px; padding:9px 16px; font-weight:600; }
QPushButton#primaryButton:hover { background:#234fc7; }
QPushButton#primaryButton:disabled { background:#9eb2ed; }
QPlainTextEdit { background:#fbfcfe; border:1px solid #e7ebf1; border-radius:8px; padding:8px; color:#27364b; font-family:Consolas,monospace; }
QSpinBox { padding:6px; border:1px solid #dce2eb; border-radius:6px; background:white; }
QProgressBar { border:0; background:#e8edf6; border-radius:3px; }
QProgressBar::chunk { background:#2d5fe8; border-radius:3px; }
"""


def main() -> int:
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    app = QApplication(sys.argv)
    app.setApplicationName("AutoIM")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLESHEET)
    bus = LogBus()
    setup_logging(bus)
    window = MainWindow(bus)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
