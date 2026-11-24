"""GUI entry points for explicitly authorized WeCom collection and local history."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import replace

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from autoim import __version__
from autoim.vision_widgets import fit_dialog_to_screen
from autoim.wecom.collection_notifications import CollectionNotifier
from autoim.wecom.collection_service import CollectionService, ScanStatus
from autoim.wecom.visual_diagnostics import DiagnosticCopyControls
from autoim.wecom.visual_store import VisualStore
from autoim.wecom.window_state import window_diagnostic

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
        f"登录账号 {state.account_name or '—'} · {account_states[state.account_state]} · {account}"
        f" · 最近识别 {at(state.account_at)} · {state.account_reason or '—'}",
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

    def __init__(self, run_worker, make_service, parent=None):
        super().__init__(parent)
        self.run_worker = run_worker
        self.make_service = make_service
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
        self._notifications = deque(maxlen=20)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(QLabel("多会话自动采集"))
        account = QHBoxLayout()
        self.identify_button = QPushButton("识别当前登录账号")
        self.identify_button.clicked.connect(self.identify_account)
        self.confirm_button = QPushButton("确认账号绑定")
        self.confirm_button.clicked.connect(self.confirm_account)
        account.addWidget(self.identify_button)
        account.addWidget(self.confirm_button)
        account.addStretch(1)
        layout.addLayout(account)
        self.scroll = QCheckBox("可见会话真机通过后启用有限 B2 滚动")
        layout.addWidget(self.scroll)
        actions = QHBoxLayout()
        self.start_button = QPushButton("开始自动采集所有未读会话")
        self.stop_button = QPushButton("停止自动采集")
        self.save_button = QPushButton("保存当前可见消息")
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.save_button.clicked.connect(self.save_current)
        for button in (self.start_button, self.stop_button, self.save_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
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
        layout.addWidget(self.summary)
        self.copy_controls = DiagnosticCopyControls(self._report, self)
        layout.addWidget(self.copy_controls)
        self._render(self._status)
        self._controls()

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
        self.save_button.setEnabled(enabled)
        self.identify_button.setEnabled(enabled)
        self.confirm_button.setVisible(self._status.account_state == "CONFIRMATION_REQUIRED")
        self.confirm_button.setEnabled(self._available and not self._in_flight)
        self.scroll.setEnabled(enabled)
        self.stop_button.setEnabled(bool(self._service and self._service.active))

    def _refresh(self):
        self._controls()
        self.busyChanged.emit(self.busy)

    def _get_service(self):
        if self._service is None:
            self._service = self.make_service(None, self.store)
        return self._service

    def start(self):
        if self.busy or not self._available:
            return
        if QMessageBox.question(
                self, "开启多会话自动采集",
                "请仅用于获授权的测试账号。程序在您切回最大化企业微信且操作空闲时，"
                "会点击经过核对的联系人行，可能改变当前会话并清除未读。"
                "视觉观察会写入本机 SQLite。程序不会输入或发送消息，您可以随时停止。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        service = self._get_service()
        try:
            service.start(scroll=self.scroll.isChecked())
        except (OSError, RuntimeError) as exc:
            self._render(ScanStatus("PAUSED", str(exc)))
            return
        self._render(service.snapshot())
        self._stop_requested = False
        self._refresh()
        self._pulse.start()
        self._timer.start(2500)

    def stop(self):
        self._stop_requested = True
        self._timer.stop()
        self._pulse.stop()
        if self._service:
            self._service.stop()
        self._render(replace(self._status, status="STOPPED", reason="许可已撤销", waiting_for_window=False))
        self._refresh()

    def save_current(self):
        if self.busy or not self._available:
            return
        self._stop_requested = False
        self._submit(self._get_service().save_current)

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
                                               f"当前显示 {self._status.account_name}。请选择能确认归属的历史账号，"
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
        self._refresh()
        self._pulse.start()

        def run():
            state = action()
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
        result, jobs = data
        if jobs is not None:
            self._jobs = jobs
        elif result.account_state in ("CONFIRMATION_REQUIRED", "IDENTIFICATION_FAILED"):
            self._jobs = []
        self._notifier.enqueue(result.notice)
        if self._stop_requested:
            result = replace(result, status="STOPPED", reason="许可已撤销", waiting_for_window=False)
        self._render(result)
        self._refresh()
        if self._service and self._service.active:
            self._timer.start(PAUSE_RECHECK_MS if result.status == "PAUSED" else SCAN_DELAY_MS)
        else:
            self._pulse.stop()

    def _failed(self, _message):
        self._in_flight = False
        self.stop()
        self._render(replace(self._status, status="FAILED", reason="窗口、OCR 或 SQLite 操作失败，已撤销自动采集许可。"
                             "请在任务中查看已保存的证据。"))

    def _notification_reported(self, notices, outcome, at):
        for notice in notices:
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

    def _show_history(self, tab):
        def failed(_):
            self.summary.setText("本地数据库读取失败。")

        def load(profile):
            def show(data):
                dialog = CollectionHistory(self.run_worker, self.store, profile, data, tab, self)
                self._history_dialog = dialog
                dialog.finished.connect(self._forget_history)
                dialog.show()
            self.run_worker(self.store.browse, profile, on_result=show, on_error=failed)

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

        self.run_worker(self.store.accounts, on_result=choose, on_error=failed)

    def _forget_history(self):
        self._history_dialog = None

    def _report(self, full):
        state = self._status
        candidates = str(state.candidates) if state.b2_scan_completed_at else "—"
        lines = [f"AutoIM {__version__} · PC-V3.1 · WINDOW_VISUAL_OCR",
                 f"状态 {state.status} · 匹配 {state.match} · {state.reason}",
                 f"候选 {candidates} · 等待 {state.queued} · 完成 {state.completed} · 异常 {state.errors}",
                 f"已保存 {state.saved} · 新增 {state.added} · 重复观察 {state.repeated} · 未确认 {state.uncertain}"]
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
