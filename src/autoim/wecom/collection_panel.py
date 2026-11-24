"""GUI entry points for explicitly authorized WeCom collection and local history."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from datetime import datetime
from pathlib import Path
from time import monotonic
from dataclasses import replace

from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl, QSettings, QTimer, Qt, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from autoim import __version__
from autoim.vision_widgets import fit_dialog_to_screen
from autoim.wecom.collection_notifications import CollectionNotifier
from autoim.wecom.collection_hotkey import CollectionHotkey
from autoim.wecom.collection_runtime import safe_reason
from autoim.wecom.collection_service import CollectionScope, CollectionService, ScanStatus
from autoim.wecom.visual_diagnostics import DiagnosticCopyControls
from autoim.wecom.visual_store import VisualStore
from autoim.wecom.window_state import window_diagnostic

def collapsible(title, parent=None):
    section = QWidget(parent)
    layout = QVBoxLayout(section)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QPushButton(title)
    button.setCheckable(True)
    body = QWidget(section)
    body.hide()
    button.toggled.connect(body.setVisible)
    layout.addWidget(button)
    layout.addWidget(body)
    content = QVBoxLayout(body)
    content.setContentsMargins(0, 0, 0, 0)
    return section, button, content


SCAN_DELAY_MS = 2000
PAUSE_RECHECK_MS = 3000


def _collection_lines(state: ScanStatus) -> list[str]:
    def at(value):
        return value.iso() if value else "—"

    recent = state.recent
    timings = state.last_success.durations() if state.last_success else {}
    duration_labels = (("unread_to_open_ms", "未读→打开"), ("open_to_ocr_ms", "打开→OCR"),
                       ("ocr_to_commit_ms", "OCR→提交"), ("unread_to_commit_ms", "未读→提交"))
    elapsed = " · ".join(f"{label} {timings[key] / 1000:.2f} 秒" for key, label in duration_labels if key in timings)
    account_states = {"UNIDENTIFIED": "尚未识别", "IDENTIFYING": "识别中", "BOUND": "已绑定",
                      "IDENTIFICATION_FAILED": "识别失败", "CONFIRMATION_REQUIRED": "需要确认"}
    account = f"本地 ID {state.account_id}" if state.account_id is not None else "尚未绑定"
    return [
        f"登录账号 {state.account_name or '—'} · {account_states.get(state.account_state, state.account_state)} · {account}"
        f" · 邮箱 {state.account_email or '—'} · 最近识别 {at(state.account_at)} · {state.account_reason or '—'}",
        f"最近未读发现 {at(recent.unread_detected_at)}",
        f"导航尝试 {at(recent.navigation_started_at)} · 打开 {at(recent.conversation_opened_at)}"
        f" · {state.navigation_result or '—'}",
        f"OCR 完成 {at(recent.ocr_completed_at)} · {state.ocr_result or '—'}",
        f"SQLite 最近成功提交 {at(recent.sqlite_committed_at)} · {state.sqlite_result or '—'}",
        f"最近新增保存耗时 {elapsed or '—'} · 最近失败 {state.last_failure or '—'}",
    ] + _scan_lines(state)


def _scan_lines(state: ScanStatus) -> list[str]:
    def timestamp(value):
        return value.isoformat(timespec="milliseconds") if value else "—"

    return [
        f"B2 轮次 {state.scan_id or '—'} · {state.scan_disposition} · 截图 {timestamp(state.screenshot_succeeded_at)}"
        f" · OCR 完成 {timestamp(state.scan_ocr_completed_at)} · B2 开始 {timestamp(state.b2_scan_attempt_at)}"
        f" · B2 完成 {timestamp(state.b2_scan_completed_at)}",
        (f"联系人 {state.contact_count} · 原始未读标记 {state.unread_marker_count} · "
         f"可靠未读 {state.reliable_unread_count} · 不可靠 {state.unreliable_unread_count}"
         if state.b2_scan_completed_at else f"最近完成的 B2 轮次 {state.last_b2_summary or '—'}"),
        f"选中行 {state.selection_name or '—'} · 姓名 {'可靠' if state.selection_name_reliable else '未确认'}"
        f" · {state.selection_method} · {state.selection_confidence}"
        f" · {state.selection_evidence or '—'}",
        f"策略排除 {state.policy_skipped_unread} · 特殊入口 {state.unsupported_unread} · "
        f"类型待核 {state.unknown_type_unread} · 静音待核 {state.unknown_muted_unread} · "
        f"其他待核 {state.other_unreliable_unread} · 合格但暂不可导航 {state.navigation_blocked_unread}",
        f"策略及入口合计 {state.skipped_unread} · {'；'.join(state.skipped_reasons) or '—'}",
        f"输入复核 {state.input_diagnostic or '—'}",
        f"不可靠原因 {'；'.join(state.unreliable_reasons) or '—'} · 等待导航 {state.awaiting_navigation}"
        f" · 进入导航 {('通过预检' if state.navigation_allowed else '未通过或无任务') if state.navigation_allowed is not None else '待核对'}"
        f" · 导航阻断 "
        f"{state.navigation_block_reason or '—'} · 前台变化阶段 {state.foreground_changed_stage or '—'}",
    ]


def _window_lines(state: ScanStatus) -> list[str]:
    def timestamp(value):
        return value.strftime("%Y-%m-%d %H:%M:%S%z") if value else "—"

    binding = f"PID {state.bound_pid} · HWND 0x{state.bound_hwnd:X}" if state.bound_hwnd else "尚未绑定"
    health = state.window_health
    valid = ("是" if health.status in ("WINDOW_VALID", "WINDOW_NOT_FOREGROUND", "WINDOW_MINIMIZED")
             else "否" if health.status == "WINDOW_CLOSED" else "未知") if health else "未知"
    minimized = "未知" if not health or health.minimized is None else "是" if health.minimized else "否"
    return [
        f"当前绑定 {binding} · 窗口有效 {valid} · 确认最小化 {minimized}",
        f"等待恢复 {'是' if state.waiting_for_window else '否'} · 最近恢复巡检 {timestamp(state.resumed_at)}",
        f"最近窗口变化 {timestamp(state.window_change_at)} · {state.window_change or '—'}",
        f"最近重新绑定 {timestamp(state.rebind_at)} · {state.rebind_result or '—'}",
        f"当前暂停原因 {state.reason}" if state.status == "PAUSED" else "当前暂停原因 —",
        window_diagnostic(health) if health else "最近窗口检查 —",
    ]


def _table(columns):
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _rows(table, records):
    table.setRowCount(len(records))
    for i, record in enumerate(records):
        for j, value in enumerate(record):
            cell = QTableWidgetItem(str(value) if value is not None else "—")
            cell.setToolTip(cell.text())
            table.setItem(i, j, cell)


class CollectionHistory(QDialog):
    def __init__(self, run_worker, store: VisualStore, profile: str | int, data: dict, tab: int, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("AutoIM 本地采集记录")
        self.resize(1050, 630)
        self.run_worker, self.store, self.profile = run_worker, store, profile
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        history = QWidget()
        page = QVBoxLayout(history)
        choose = QHBoxLayout()
        self.conversation = QComboBox()
        for row in data["conversations"]:
            self.conversation.addItem(f"{row['display_name']} · {row['kind']} · {row['id'][:8]}", row["id"])
        self.previous = QPushButton("更早 200 条")
        self.latest = QPushButton("最新")
        self.previous.setEnabled(False)
        choose.addWidget(self.conversation, 1)
        choose.addWidget(self.previous)
        choose.addWidget(self.latest)
        page.addLayout(choose)
        self.history = _table(["顺序", "发送人", "方向", "类型", "正文或文件名", "大小", "可见时间",
                               "识别状态", "首次采集"])
        page.addWidget(self.history)
        self.note = QLabel("记录按本地可验证顺序显示。")
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        page.addWidget(self.note)
        self.tabs.addTab(history, "已保存聊天记录")
        self.jobs = _table(["会话", "任务", "导航", "未读证据", "可能已读", "持久化", "失败原因", "过程"])
        self.tabs.addTab(self.jobs, "自动巡检任务")
        self.errors = _table(["观察时间", "类型", "状态", "判据", "原因", "原始观察"])
        self.tabs.addTab(self.errors, "采集异常")
        self.tabs.setCurrentIndex(tab)
        self._first = None
        self._fill(data)
        self.latest.clicked.connect(lambda: self._load())
        self.previous.clicked.connect(lambda: self._load(self._first))
        self.conversation.currentIndexChanged.connect(lambda _: self._load())
        if self.conversation.count():
            self._load()

    def showEvent(self, event):
        super().showEvent(event)
        fit_dialog_to_screen(self)

    def _fill(self, data):
        records = data["messages"]
        _rows(self.history, [(row["position"], row["sender_name"], row["direction"], row["kind"],
                              row["filename"] or row["body"], row["size_text"], row["actual_time"] or row["time_text"],
                              row["status"], row["first_seen"]) for row in records])
        self._first = records[0]["position"] if records else None
        self.previous.setEnabled(bool(self._first and self._first > 1))
        _rows(self.jobs, [(row["display_name"], row["status"], row["navigation_status"], row["unread_evidence"],
                           "是" if row["read_risk"] else "否", row["persistence_result"],
                           row["failure_reason"], row["transitions"]) for row in data["jobs"]])
        errors = [(row["created_at"], "SCAN_JOB", row["status"], row["navigation_status"],
                   row["failure_reason"], row["display_name"])
                  for row in data["jobs"] if row["status"] in ("FAILED", "UNCERTAIN", "PAUSED")]
        errors.extend((row["observed_at"], row["kind"], row["status"], row["dedup_evidence"],
                       row["reason"], row["raw_ocr"]) for row in data["errors"])
        _rows(self.errors, errors)

    def _load(self, before=None):
        cid = self.conversation.currentData()
        if not cid:
            return
        self.conversation.setEnabled(False)
        self.latest.setEnabled(False)
        self.previous.setEnabled(False)
        self.run_worker(lambda: self.store.browse(self.profile, cid, before=before),
                        on_result=self._loaded, on_error=self._failed)

    def _loaded(self, data):
        self.conversation.setEnabled(True)
        self.latest.setEnabled(True)
        self._fill(data)
        self.note.setText(f"当前页 {len(data['messages'])} 条 · 本地账号共 {data['message_count']} 条")

    def _failed(self, _error):
        self.conversation.setEnabled(True)
        self.latest.setEnabled(True)
        self.note.setText("本地数据库读取失败。")


class VisualCollectionPanel(QWidget):
    busyChanged = Signal(bool)

    def __init__(self, run_worker, make_service, parent=None, *, settings=None):
        super().__init__(parent)
        self.run_worker = run_worker
        self.make_service = make_service
        self.settings = settings
        self._authorized = False
        self._asking_permission = False
        self._hotkey = None
        self._shortcut = "F10"
        self.runtime = None
        self._last_detection = ""
        self._stop_source = "control"
        self._pending_assist = None
        self._failure_key = None
        self._failure_seen = ""
        self._failure_count = 0
        self._failure_started = 0.
        self._exception_dialog = None
        self._failure_notice = None
        self.store = VisualStore()
        self._service: CollectionService | None = None
        self._status = ScanStatus()
        self._jobs = []
        self._in_flight = False
        self._stop_requested = False
        self._available = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)
        self._pulse = QTimer(self)
        self._pulse.setInterval(400)
        self._pulse.timeout.connect(self._refresh_progress)
        self._history_dialog = None
        self._notifier = CollectionNotifier(self)
        self._notifier.reported.connect(self._notification_reported)
        self._notifier.submitting.connect(self._notification_submitting)
        self._notifications = deque(maxlen=20)
        self._notice_runs = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        self.account_label = QLabel()
        self.account_label.setTextFormat(Qt.TextFormat.PlainText)
        self.account_label.setWordWrap(True)
        layout.addWidget(self.account_label)
        ranges = QHBoxLayout()
        ranges.addWidget(QLabel("运行模式"))
        self.mode_select = QComboBox()
        self.mode_select.addItem("测试模式", "test")
        self.mode_select.addItem("正式运行模式", "production")
        if settings is not None and settings.value("collection/mode", "test", type=str) == "production":
            self.mode_select.setCurrentIndex(1)
        self.mode_select.currentIndexChanged.connect(self._save_mode)
        ranges.addWidget(self.mode_select)
        ranges.addWidget(QLabel("采集范围"))
        self.scope_checks = {}
        for key, title, default in (("personal", "普通单聊", True), ("group", "普通群聊", True),
                                    ("muted", "静音会话", False), ("folded", "折叠的聊天（待实现）", False)):
            check = QCheckBox(title)
            check.setChecked(settings.value(f"collection/scope/{key}", default, type=bool)
                             if settings is not None and key != "folded" else default)
            if key == "folded":
                check.setEnabled(False)
            self.scope_checks[key] = check
            check.toggled.connect(self._save_scope)
            ranges.addWidget(check)
        ranges.addStretch(1)
        layout.addLayout(ranges)
        diagnostics, self.diagnostics_toggle, details = collapsible("运行诊断", self)
        tools, self.advanced_toggle, advanced = collapsible("高级工具", self)
        self.advanced_tools = tools
        self.advanced_layout = advanced
        account = QHBoxLayout()
        self.identify_button = QPushButton("重新核验登录账号")
        self.identify_button.clicked.connect(self.reverify_account)
        self.confirm_button = QPushButton("确认账号绑定")
        self.confirm_button.clicked.connect(self.confirm_account)
        account.addWidget(self.identify_button)
        account.addStretch(1)
        advanced.addLayout(account)
        self.scroll = QCheckBox("可见会话真机通过后启用有限 B2 滚动")
        advanced.addWidget(self.scroll)
        if settings is not None:
            self.scroll.hide()
        actions = QHBoxLayout()
        self.start_button = QPushButton("开始检测")
        self.stop_button = QPushButton("停止检测")
        self.save_button = QPushButton("保存当前可见消息")
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.save_button.clicked.connect(self.save_current)
        advanced.addWidget(self.save_button)
        self.shortcut_button = QPushButton("全局启停快捷键 · F10")
        self.shortcut_button.clicked.connect(self._change_shortcut)
        self.hotkey_status = QLabel()
        self.hotkey_status.setWordWrap(True)
        self.compact_status = QLabel()
        self.compact_status.setTextFormat(Qt.TextFormat.PlainText)
        self.compact_status.setWordWrap(True)
        for button in (self.start_button, self.stop_button, self.shortcut_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addWidget(self.confirm_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.hotkey_status)
        layout.addWidget(self.compact_status)
        self.assistance_button = QPushButton("处理核验异常")
        self.assistance_button.clicked.connect(self._show_exception)
        self.assistance_button.hide()
        layout.addWidget(self.assistance_button, 0, Qt.AlignmentFlag.AlignLeft)
        test_controls = QHBoxLayout()
        self.isolated_button = QPushButton("授权指定测试会话")
        self.isolated_button.clicked.connect(self._authorize_isolated)
        self.isolated_button.hide()
        self.test_history_button = QPushButton("查看隔离测试记录")
        self.test_history_button.clicked.connect(self._show_isolated_history)
        self.test_history_button.hide()
        self.test_notice_confirm = QPushButton("确认已看到 TEST 通知")
        self.test_notice_confirm.clicked.connect(self._confirm_test_notice)
        self.test_notice_confirm.hide()
        self.test_db_button = QPushButton("隔离库事务自检")
        self.test_db_button.clicked.connect(self._run_test_database_probe)
        self.test_db_button.hide()
        self._last_test_notice = None
        for button in (self.isolated_button, self.test_history_button, self.test_db_button, self.test_notice_confirm):
            test_controls.addWidget(button)
        test_controls.addStretch()
        layout.addLayout(test_controls)
        history = QHBoxLayout()
        for i, title in enumerate(("查看已保存聊天记录", "查看自动巡检任务", "查看采集异常")):
            button = QPushButton(title)
            button.clicked.connect(lambda _, tab=i: self._show_history(tab))
            history.addWidget(button)
        history.addStretch(1)
        layout.addLayout(history)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        details.addWidget(self.summary)
        self.copy_controls = DiagnosticCopyControls(self._report, self)
        details.addWidget(self.copy_controls)
        actions = QHBoxLayout()
        self.diagnostic_buttons = []
        for label, callback in (("日志目录", self._open_logs), ("查看运行日志", self._view_log),
                                ("流程测试报告", self._view_flow), ("导出诊断 ZIP", self._export_logs)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self.diagnostic_buttons.append(button)
        actions.addStretch(1)
        details.addLayout(actions)
        layout.addWidget(diagnostics)
        layout.addWidget(tools)
        self._render(self._status)
        self._controls()

    def enable_runtime(self, runtime):
        self.runtime = runtime
        runtime.emit(self.mode_select.currentData(), "", "APP", "APPLICATION_STARTED")
        for button in self.diagnostic_buttons:
            button.setEnabled(True)

    def _save_mode(self):
        if self.settings is not None:
            self.settings.setValue("collection/mode", self.mode_select.currentData())
        if self.runtime and self._service is not None and not self.busy:
            self._service.configure_runtime(self.runtime, self.mode_select.currentData())

    def _authorize_isolated(self):
        if not self._service or not self._service.active or self._in_flight:
            return
        choices = self._status.isolated_choices
        if not choices:
            return
        self._timer.stop()
        labels = {f"{item.target.row.display_name} · 可见第 {item.target.row.sequence} 行": item for item in choices}
        try:
            label, ok = QInputDialog.getItem(
                self, "授权隔离导航测试",
                "只操作你选定且拥有读取权限的测试会话。打开会话可能改变未读状态。"
                "记录进入独立测试库。请选择并确认本轮授权。", list(labels), 0, False)
            if ok and label in labels and self._service.active and not self._in_flight:
                self._render(self._service.authorize_isolated(labels[label]))
        finally:
            if self._service.active:
                self._timer.start(1000)

    def _show_isolated_history(self):
        service = self._get_service()
        self._show_history(0, store=service.test_store, isolated=True)

    def _run_test_database_probe(self):
        if self._service and self._service.active and not self._in_flight and self.mode_select.currentData() == "test":
            self._timer.stop()
            self._submit(self._service.test_database_probe)

    def _confirm_test_notice(self):
        if not self._last_test_notice or not self.runtime:
            return
        run, test_run = self._last_test_notice
        self._last_test_notice = None
        self.test_notice_confirm.hide()
        self.runtime.emit("test", run, "TEST_NOTIFY", "TEST_NOTIFICATION_SEEN", "MANUAL_ASSISTED",
                          details={"test_run": test_run, "evidence_scope": "ISOLATED_DATABASE_TEST"})

    def _profile_assist(self, action):
        if not self._service or not self._service.active:
            return
        if self._in_flight:
            self._pending_assist = action
            if self._failure_notice:
                self._failure_notice.appendPlainText("已收到选择，将在当前只读检查结束后处理。")
            return
        self._service.assist_identity(action)
        self._render(self._service.snapshot())
        if self._exception_dialog:
            self._exception_dialog.close()
        self._timer.start(0)

    def _handle_failure(self, result):
        if not self.runtime:
            return
        if not result.failure_at or result.failure_at == self._failure_seen:
            if (self._failure_key and result.status in {"COMPLETED", "RUNNING"} and not result.assist
                    and not result.navigation_block_reason and not result.unreliable_unread_count
                    and result.failure_code != "NAVIGATION_NO_PROGRESS"):
                self._notifier.announce("AutoIM 已恢复", "采集流程继续运行")
                if self._exception_dialog:
                    self._exception_dialog.close()
                self._failure_key = None
            return
        self._failure_seen = result.failure_at
        reason = safe_reason(result.last_failure)
        key = result.failure_stage, result.failure_code, reason
        if key == self._failure_key:
            self._failure_count += 1
            if self._failure_notice:
                self._failure_notice.setPlainText(self._failure_text())
            return
        self._failure_key = key
        self._failure_started = monotonic()
        self._failure_count = 1
        if self._exception_dialog:
            self._exception_dialog.close()
        if result.failure_level == "ERROR":
            self._show_exception()
        else:
            self._notifier.announce("AutoIM 暂停或跳过一项操作", reason[:220])

    def _failure_text(self):
        state = self._status
        elapsed = round(monotonic() - self._failure_started) if self._failure_started else 0
        detail = (f"时间 {state.failure_at or '—'}\n模式 {self.mode_select.currentText()}\n"
                  f"阶段 {state.failure_stage or '—'} · {state.failure_code or '—'}\n"
                  f"{safe_reason(state.last_failure or state.reason)}\n"
                  f"重复 {self._failure_count} 次 · 持续 {elapsed} 秒\n"
                  f"已执行 点击任务 {state.trace_job or '—'} · 已完成 {state.completed} · 已保存 {state.saved}\n"
                  f"运行状态 {state.status} · 停止来源 {state.stop_reason or '—'}\n"
                  "受影响的操作已暂停，可查看流程报告或导出诊断。")
        if state.assist == "CARD":
            detail += "\n可手动打开登录账号的个人资料卡，再选择「我已手动完成」。"
        elif state.assist == "CLOSE":
            detail += "\n请手动关闭资料卡及个人菜单，再选择「我已手动完成」。"
        return detail

    def _show_exception(self):
        if self._exception_dialog is not None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("AutoIM 运行异常")
        dialog.setWindowFlag(Qt.WindowType.Tool)
        dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(620, 320)
        layout = QVBoxLayout(dialog)
        notice = QPlainTextEdit(self._failure_text())
        notice.setReadOnly(True)
        layout.addWidget(notice)
        self._exception_dialog, self._failure_notice = dialog, notice

        def forget():
            self._exception_dialog = self._failure_notice = None
        dialog.finished.connect(forget)

        actions = QHBoxLayout()
        for name, callback in (("复制异常", lambda: QApplication.clipboard().setText(self._failure_text())),
                               ("详细诊断", self._view_flow), ("导出日志", self._export_logs),
                               ("关闭", dialog.close)):
            button = QPushButton(name)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        if self._status.assist:
            choices = QHBoxLayout()
            buttons = [("自动重试", lambda: self._profile_assist("retry")),
                       ("跳过模块", lambda: self._profile_assist("skip")),
                       ("停止全部测试", self.stop)]
            if self._status.assist in {"CARD", "CLOSE"}:
                buttons.insert(1, ("我已手动完成", lambda: self._profile_assist("manual")))
            for name, callback in buttons:
                button = QPushButton(name)
                button.clicked.connect(callback)
                choices.addWidget(button)
            layout.addLayout(choices)
        dialog.show()

    def _open_logs(self):
        if self.runtime:
            self.runtime.root.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.runtime.root.resolve())))

    def _show_text(self, title, value):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(750, 500)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dialog)
        output = QPlainTextEdit(value)
        output.setReadOnly(True)
        layout.addWidget(output)
        dialog.show()

    def _view_log(self):
        if self.runtime:
            def tail():
                from autoim.wecom.collection_runtime import _human
                return "\n".join(_human(event) for event in deque(self.runtime.events(), maxlen=500))
            self.run_worker(tail, on_result=lambda value: self._show_text("本次运行日志", value),
                            on_error=lambda _: self.hotkey_status.setText("无法读取运行日志"))

    def _view_flow(self):
        if self.runtime:
            self._show_text("完整流程测试报告", json.dumps(
                self.runtime.report(self._last_detection), ensure_ascii=False, indent=2))

    def _export_logs(self):
        if not self.runtime:
            return
        scopes = {"本次检测": "detection", "本次应用运行": "session", "今天": "today", "全部保留日志": "all"}
        choice, accepted = QInputDialog.getItem(self, "导出诊断 ZIP", "日志范围", list(scopes), 0, False)
        if not accepted:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "导出诊断 ZIP",
                                                 f"AutoIM-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip",
                                                 "诊断 ZIP (*.zip)")
        if not filename:
            return
        state, detection = self._status, self._last_detection
        snapshot = {"status": state.status, "account_state": state.account_state, "scan": state.scan_id,
                    "window_state": state.window_health.status if state.window_health else None,
                    "failure": safe_reason(state.last_failure), "failure_stage": state.failure_stage,
                    "failure_code": state.failure_code, "failure_time": state.failure_at,
                    "stop_reason": state.stop_reason, "contacts": state.contact_count,
                    "unread": state.unread_marker_count}
        self.run_worker(lambda: self.runtime.export(Path(filename), scopes[choice], detection, snapshot),
                        on_result=lambda _: self.hotkey_status.setText("诊断 ZIP 已导出"),
                        on_error=lambda _: self.hotkey_status.setText("导出失败，请选择新的可写文件路径"))

    def _save_scope(self):
        if self.settings is not None:
            for key in ("personal", "group", "muted"):
                self.settings.setValue(f"collection/scope/{key}", self.scope_checks[key].isChecked())

    def bind_hotkey(self, window):
        self._hotkey = CollectionHotkey(QApplication.instance(), int(window.winId()), self.toggle)
        shortcut = self.settings.value("collection/hotkey", "F10", type=str) if self.settings is not None else "F10"
        self._set_shortcut(shortcut)

    def close_hotkey(self):
        if self._hotkey is not None:
            self._hotkey.close()

    def _set_shortcut(self, text):
        if not self._hotkey:
            return
        try:
            self._hotkey.set_shortcut(text)
        except (OSError, ValueError) as exc:
            self.hotkey_status.setText(str(exc))
            return
        self._shortcut = text
        self.shortcut_button.setText(f"全局启停快捷键 · {text}")
        self.hotkey_status.clear()
        if self.settings is not None:
            self.settings.setValue("collection/hotkey", text)

    def _change_shortcut(self):
        text, accepted = QInputDialog.getText(self, "全局启停快捷键", "例如 F9 或 Ctrl+Alt+M", text=self._shortcut)
        if accepted:
            self._set_shortcut(text.strip())

    def toggle(self):
        if self._service and self._service.active:
            self._stop_source = "hotkey"
            self.stop()
        elif self._authorized or self.runtime:
            self.start()
        else:
            self.hotkey_status.setText("首次使用请在 AutoIM 中点击「开始检测」并确认授权。")

    @property
    def busy(self) -> bool:
        return self._in_flight or bool(self._service and self._service.active)

    def set_available(self, enabled: bool) -> None:
        self._available = enabled
        self._controls()

    def set_calibration(self, path) -> None:
        if self._service is not None:
            self._service.read.set_calibration(path)

    def _controls(self) -> None:
        enabled = self._available and not self.busy
        self.start_button.setEnabled(enabled)
        self.mode_select.setEnabled(enabled)
        for button in self.diagnostic_buttons:
            button.setEnabled(self.runtime is not None)
        self.assistance_button.setEnabled(not self._in_flight)
        self.isolated_button.setEnabled(not self._in_flight and bool(self._service and self._service.active))
        self.test_history_button.setVisible(bool(self.runtime and self.mode_select.currentData() == "test"))
        self.test_db_button.setVisible(self.test_history_button.isVisibleTo(self))
        self.test_db_button.setEnabled(not self._in_flight and bool(self._service and self._service.active))
        self.save_button.setEnabled(enabled)
        self.identify_button.setEnabled(enabled)
        self.confirm_button.setVisible(self._status.account_state == "CONFIRMATION_REQUIRED")
        self.confirm_button.setEnabled(self._available and not self._in_flight)
        self.scroll.setEnabled(enabled)
        for key, check in self.scope_checks.items():
            check.setEnabled(enabled and key != "folded")
        self.stop_button.setEnabled(bool(self._service and self._service.active))

    def _refresh(self):
        self._controls()
        self.busyChanged.emit(self.busy)

    def _get_service(self):
        if self._service is None:
            self._service = self.make_service(None, self.store)
            if self.runtime:
                self._service.configure_runtime(self.runtime, self.mode_select.currentData())
        return self._service

    def start(self):
        if self.busy or not self._available or self._asking_permission:
            return
        if not self._authorized:
            self._asking_permission = True
            try:
                result = QMessageBox.question(
                    self, "授予企业微信自动采集权限",
                    "本次检测会恢复、最大化或激活企业微信窗口，并在核验后移动鼠标打开会话，可能改变未读状态。"
                    f"采集结果会保存到本机 SQLite。本次运行中按 {self._shortcut} 可随时停止。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
            finally:
                self._asking_permission = False
            if result != QMessageBox.StandardButton.Yes:
                return
            self._authorized = True
        service = self._get_service()
        if self.settings is not None:
            service.scope = CollectionScope(**{key: self.scope_checks[key].isChecked()
                                               for key in ("personal", "group", "muted")})
        if self._hotkey and self._hotkey.active_id is not None:
            self.hotkey_status.clear()
        try:
            service.start(scroll=self.scroll.isChecked())
        except (OSError, RuntimeError) as exc:
            self._render(ScanStatus("PAUSED", str(exc)))
            return
        self._render(service.snapshot())
        if self.runtime:
            self._last_detection = service.detection_id
            self._failure_key = None
        self._stop_requested = False
        self._refresh()
        self._pulse.start()
        self._notifier.announce("自动采集已启动", "正在准备企业微信窗口并核验账号")
        self._timer.start(2500)

    def stop(self):
        self._pending_assist = None
        if self._exception_dialog:
            self._exception_dialog.close()
        self._stop_requested = True
        self._timer.stop()
        self._pulse.stop()
        if self._service:
            was_active = self._service.active
            if self.runtime:
                if was_active:
                    self.runtime.emit(self.mode_select.currentData(), self._last_detection, "STOP_CONTROL",
                                      "STOP_SCHEDULING", "AUTO_PASS")
                self._service.stop(source=self._stop_source)
                self._notifier.flush()
            else:
                self._service.stop()
            if was_active:
                self._notifier.announce("自动采集已停止", "鼠标控制权已交还")
        state = self._service.snapshot() if self.runtime and self._service else self._status
        self._stop_source = "control"
        self._render(replace(state, status="STOPPED", reason="许可已撤销", waiting_for_window=False, assist=""))
        self._refresh()

    def save_current(self):
        if self.busy or not self._available:
            return
        self._stop_requested = False
        self._submit(self._get_service().save_current)

    def reverify_account(self):
        if not self.busy and self._available:
            self._stop_requested = False
            self._submit(self._get_service().request_identity_check)

    def identify_account(self):
        if not self.busy and self._available:
            self._stop_requested = False
            self._submit(self._get_service().identify_account)

    def confirm_account(self):
        if self._in_flight or not self._available or self._status.account_state != "CONFIRMATION_REQUIRED":
            return
        self._timer.stop()
        choices = {f"{row.label} · 本地 ID {row.id}": row.id for row in self._status.account_choices}
        choices["创建独立的本地账号"] = None
        label, accepted = QInputDialog.getItem(self, "确认账号绑定",
                                               f"当前账号 {self._status.account_name} · {self._status.account_email or '邮箱未显示'}。请选择能确认归属的历史账号，"
                                               "也可创建独立的本地账号。",
                                               list(choices), 0, False)
        if accepted and label in choices:
            self._stop_requested = False
            self._submit(lambda: self._service.confirm_account(choices[label]))
        elif self._service.active:
            self._timer.start(PAUSE_RECHECK_MS)

    def _refresh_progress(self):
        if self._service and self._in_flight and not self._stop_requested:
            self._render(self._service.snapshot())

    def _tick(self):
        if self._service and self._service.active and not self._in_flight:
            self._submit(self._service.tick)

    def _submit(self, action):
        self._in_flight = True
        if self.runtime and self._service:
            self._service.begin_worker()
        self._refresh()
        self._pulse.start()

        def run():
            try:
                state = action()
            except Exception as exc:
                if self.runtime:
                    self._service._failure("RUN", "BACKGROUND_WORKER_EXCEPTION", type(exc).__name__,
                                           level="ERROR", exception=exc)
                raise
            profile = state.account_id or self._service.profile
            if profile is None or (state.waiting_for_window and state.b2_scan_completed_at is None):
                return state, None
            try:
                return state, self.store.browse(profile)["jobs"]
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                return state, None

        self.run_worker(run, on_result=self._finished, on_error=self._failed)

    def _finished(self, data):
        self._in_flight = False
        if self.runtime and self._service:
            self._service.end_worker()
        result, jobs = data
        if jobs is not None:
            self._jobs = jobs
        elif result.account_state in ("CONFIRMATION_REQUIRED", "IDENTIFICATION_FAILED"):
            self._jobs = []
        if self.runtime and result.notice:
            self._notice_runs[result.notice.id] = (self.mode_select.currentData(), self._last_detection)
        self._notifier.enqueue(result.notice)
        if self._stop_requested:
            result = replace(result, status="STOPPED", reason="许可已撤销", waiting_for_window=False)
        self._render(result)
        self._refresh()
        if not self._stop_requested:
            self._handle_failure(result)
        if self._service and self._service.active:
            if self._pending_assist and result.assist:
                action, self._pending_assist = self._pending_assist, None
                self._profile_assist(action)
                return
            self._pending_assist = None
            self._timer.start(PAUSE_RECHECK_MS if result.status == "PAUSED" else SCAN_DELAY_MS)
        else:
            self._pulse.stop()

    def _failed(self, _message):
        self._in_flight = False
        if self.runtime and self._service:
            self._service.end_worker()
        reason = "窗口、OCR 或 SQLite 操作失败，请查看已保存的任务证据"
        if self.runtime and self._service:
            if self._service.snapshot().failure_code != "BACKGROUND_WORKER_EXCEPTION":
                self._service._failure("RUN", "BACKGROUND_WORKER_FAILED", reason, level="ERROR")
            self._stop_source = "failure"
        self.stop()
        result = replace(self._status, status="FAILED", reason=reason)
        self._render(result)
        self._handle_failure(result)

    def _notification_submitting(self, notices):
        if not self.runtime:
            return
        for notice in notices:
            if notice.test_run:
                mode, run = self._notice_runs.get(notice.id, (self.mode_select.currentData(), self._last_detection))
                self.runtime.emit(mode, run, "TEST_NOTIFY", "TEST_NOTIFICATION_API_CALL", "PARTIAL",
                                  details={"test_run": notice.test_run, "evidence_scope": "ISOLATED_DATABASE_TEST"})

    def _notification_reported(self, notices, outcome, at):
        for notice in notices:
            if self.runtime:
                mode, run = self._notice_runs.pop(notice.id, (self.mode_select.currentData(), self._last_detection))
                if notice.test_run:
                    self.runtime.emit(mode, run, "TEST_NOTIFY", "TEST_WINDOWS_NOTIFICATION",
                                      "AUTO_PASS" if outcome == "已提交给系统通知" else "FAILED",
                                      details={"count": notice.count, "reason": outcome, "test_run": notice.test_run,
                                               "evidence_scope": "ISOLATED_DATABASE_TEST", "user_seen": False})
                    if outcome == "已提交给系统通知":
                        self._last_test_notice = run, notice.test_run
                        self.test_notice_confirm.show()
                else:
                    self.runtime.emit(mode, run, "NOTIFY", "WINDOWS_NOTIFICATION",
                                      "AUTO_PASS" if outcome == "已提交给系统通知" else "FAILED",
                                      task=notice.job_id or "", details={"count": notice.count, "reason": outcome})
            self._notifications.append(f"{at.iso()} · {notice.job_id or '手工保存'} · {notice.count} 条 · {outcome}")
        self._render(self._status)
        jobs = tuple(notice for notice in notices if notice.job_id)
        if jobs:
            def audit():
                for notice in jobs:
                    self.store.job_event(notice.job_id, "DESKTOP_NOTIFICATION", at,
                                         {"summary": outcome, "new": notice.count})
            self.run_worker(audit, on_result=lambda _: None, on_error=lambda _: None)

    def _render(self, result):
        self._status = result
        self.assistance_button.setVisible(bool(result.assist or result.failure_at))
        self.isolated_button.setVisible(bool(self.runtime and result.isolated_choices))
        self.account_label.setText(
            f"当前账号 {result.account_name or '—'} · 邮箱 {result.account_email or '—'} · "
            f"{result.account_reason or '等待识别'}")
        committed = result.last_collected_at
        mode = "自动采集模式" if self._service and self._service.active else "人工操作模式"
        self.compact_status.setText(
            f"{mode} · {result.status} · {result.reason}\n"
            f"发现未读 {result.unread_marker_count if result.b2_scan_completed_at else '—'} · "
            f"等待采集 {result.queued} · 已完成任务 {result.completed} · 采集异常 {result.errors}\n"
            f"已保存消息 {result.saved} · 新增消息 {result.added} · 最近成功采集 {committed.iso() if committed else '—'}")
        candidates = str(result.candidates) if result.b2_scan_completed_at else "—"
        self.summary.setText(
            f"{result.status} · {result.reason}\n"
            f"目标 {result.target or '—'} · 匹配 {result.match} · "
            f"未读候选 {candidates} · 等待 {result.queued} · "
            f"完成 {result.completed} · 失败/异常 {result.errors} · B2 页 {result.pages}\n"
            f"数据库已保存 {result.saved} · 新增 {result.added} · 重复观察 {result.repeated} · 未确认 {result.uncertain}\n"
            + "\n".join(_collection_lines(result))
            + f"\n桌面通知 {self._notifications[-1] if self._notifications else '—'}\n"
            + "\n".join(_window_lines(result)))

    def _show_history(self, tab, *, store=None, isolated=False):
        store = store or self.store
        def failed(_):
            self.summary.setText("本地数据库读取失败。")

        def load(profile):
            def show(data):
                dialog = CollectionHistory(self.run_worker, store, profile, data, tab, self)
                if isolated:
                    dialog.setWindowTitle("TEST · 隔离测试聊天记录")
                self._history_dialog = dialog
                dialog.finished.connect(self._forget_history)
                dialog.show()
            self.run_worker(store.browse, profile, on_result=show, on_error=failed)

        def choose(accounts):
            choices = {f"{row.label} · 本地 ID {row.id}": row.id for row in accounts}
            if not choices:
                self.summary.setText("当前没有已保存的本地账号。")
                return
            if len(accounts) == 1:
                load(accounts[0].id)
                return
            label, accepted = QInputDialog.getItem(self, "查看本地账号历史", "选择历史记录所属账号",
                                                   list(choices), 0, False)
            if accepted and label in choices:
                load(choices[label])

        self.run_worker(store.accounts, on_result=choose, on_error=failed)

    def _forget_history(self):
        self._history_dialog = None

    def _report(self, full):
        state = self._status
        candidates = str(state.candidates) if state.b2_scan_completed_at else "—"
        lines = [f"AutoIM {__version__} · PC-V3.4 · WINDOW_VISUAL_OCR",
                 f"状态 {state.status} · 匹配 {state.match} · {state.reason}",
                 f"候选 {candidates} · 等待 {state.queued} · 完成 {state.completed} · 异常 {state.errors}",
                 f"已保存 {state.saved} · 新增 {state.added} · 重复观察 {state.repeated} · 未确认 {state.uncertain}"]
        if self.runtime:
            lines.append(f"模式 {self.mode_select.currentText()} · 核验 {state.account_state} · 停止来源 {state.stop_reason or '—'}")
            lines.extend(_window_lines(state))
            lines.extend(_scan_lines(replace(state, selection_name="")))
            if state.failure_at:
                lines.append(self._failure_text())
            return "\n".join(safe_reason(line) for line in lines)
        lines.extend(_collection_lines(state))
        lines.extend(_window_lines(state))
        if state.trace_job:
            lines.append(f"最近任务 {state.trace_job} · {json.dumps(state.trace.detail(), ensure_ascii=False)}")
        lines.extend(f"桌面通知 {item}" for item in self._notifications)
        if full:
            lines.append(f"目标 {state.target or '—'} · {state.reason}")
            if self._service:
                for scan in self._service.recent_scans():
                    if scan.scan_id != state.scan_id:
                        lines.extend(_scan_lines(scan))
        for job in self._jobs[:20]:
            transitions = json.loads(job["transitions"])
            after = next((item["detail"] for item in reversed(transitions)
                          if item.get("detail") and "badge_after" in item["detail"]), {})
            unread = json.loads(job["unread_evidence"])
            lines.append(f"{job['status']} · {job['navigation_status']} · 可能已读 {bool(job['read_risk'])} · "
                         f"未读标记 {unread['hasUnreadIndicator']} → {after.get('badge_after', '未知')} · "
                         f"{job['capture_result']} · {job['persistence_result']}")
            event_names = {"DISCOVERED", "NAVIGATING", "CONVERSATION_OPENED", "OCR_COMPLETED", "SQLITE_COMMITTED",
                           "DESKTOP_NOTIFICATION", "WAITING_REVALIDATION"}
            events = [item for item in transitions if item["status"] in event_names]
            if events:
                lines.append(f"任务 {job['id']}")
                for item in events:
                    public = {key: value for key, value in (item.get("detail") or {}).items()
                              if key.endswith(("_at", "_ms")) or key in ("summary", "saved", "new", "repeated",
                                                                         "uncertain", "scan_id", "observations")}
                    lines.append(f"  {item['status']} · {item['at']} · {json.dumps(public, ensure_ascii=False)}")
            if full:
                lines.extend((f"{job['display_name']} · {job['unread_evidence']} · {job['failure_reason']}",
                              job["transitions"]))
        return "\n".join(lines)
