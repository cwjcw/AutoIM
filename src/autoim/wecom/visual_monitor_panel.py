"""Qt controls for the serial visual monitor."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from autoim.vision_widgets import fit_dialog_to_screen
from autoim.wecom.visual_diagnostics import DiagnosticCopyControls, build_visual_report, file_details
from autoim.wecom.visual_monitor import MonitorObservation, MonitorSnapshot, NewCandidate, VisualMonitor


class VisualMonitorPanel(QWidget):
    busyChanged = Signal(bool)

    def __init__(self, run_worker, make_monitor, parent=None) -> None:
        super().__init__(parent)
        self._run_worker = run_worker
        self._make_monitor = make_monitor
        self._monitor: VisualMonitor | None = None
        self._active = False
        self._in_flight = False
        self._available = True
        self._shown_candidates: tuple[NewCandidate, ...] = ()
        self._shown_observation: MonitorObservation | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(QLabel("当前会话监测"))
        actions = QHBoxLayout()
        self.interval = QDoubleSpinBox()
        self.interval.setDecimals(1)
        self.interval.setRange(1.0, 30.0)
        self.interval.setSingleStep(.5)
        self.interval.setValue(2.0)
        self.interval.setSuffix(" 秒")
        self.start_button = QPushButton("开始监测当前会话")
        self.stop_button = QPushButton("停止监测")
        self.clear_button = QPushButton("清空诊断事件")
        self.events_button = QPushButton("查看诊断事件")
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.clear_button.clicked.connect(self.clear)
        self.events_button.clicked.connect(self._show_events)
        actions.addWidget(QLabel("采样周期"))
        for widget in (self.interval, self.start_button, self.stop_button, self.clear_button, self.events_button):
            actions.addWidget(widget)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.copy_controls = DiagnosticCopyControls(lambda full: build_visual_report(state=self._last_state, full=full), self)
        layout.addWidget(self.copy_controls)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        tabs = QTabWidget()
        tabs.setMaximumHeight(226)
        self.observed_table = QTableWidget(0, 7)
        self.observed_table.setHorizontalHeaderLabels(["原图位置", "方向", "类型", "正文、文件名或 OCR",
                                                      "识别状态", "失败原因或证据", "文件大小"])
        self.observed_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.observed_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.observed_table.setColumnWidth(3, 220)
        tabs.addTab(self.observed_table, "当前已观察到的消息")
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["首次观察", "方向", "正文或文件名", "类型",
                                             "识别证据", "状态", "完整性风险", "文件大小"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setColumnWidth(2, 260)
        tabs.addTab(self.table, "新增消息候选")
        layout.addWidget(tabs)
        self._render(MonitorSnapshot())
        self._controls()

    @property
    def busy(self) -> bool:
        return self._active or self._in_flight

    def set_available(self, available: bool) -> None:
        self._available = available
        self._controls()

    def _controls(self) -> None:
        ready = self._available and not self.busy
        self.start_button.setEnabled(ready)
        self.interval.setEnabled(ready)
        self.stop_button.setEnabled(self._active)
        self.clear_button.setEnabled(self._monitor is not None)
        self.events_button.setEnabled(self._monitor is not None)

    def start(self) -> None:
        if self.busy or not self._available:
            return
        self._monitor = self._make_monitor(self.interval.value())
        if not self._monitor.start():
            return
        self._active = True
        self._render(self._monitor.snapshot())
        self._controls()
        self.busyChanged.emit(True)
        self._tick()

    def stop(self) -> None:
        self._active = False
        self._timer.stop()
        if self._monitor is not None:
            self._monitor.stop()
            self._render(self._monitor.snapshot())
        self._controls()
        self.busyChanged.emit(self.busy)

    def clear(self) -> None:
        if self._monitor is not None:
            self._monitor.clear_events()
            self._render(self._monitor.snapshot())

    def _tick(self) -> None:
        if not self._active or self._in_flight or self._monitor is None:
            return
        self._in_flight = True
        self._run_worker(self._monitor.poll, on_result=self._finished, on_error=self._failed)

    def _finished(self, _result: MonitorSnapshot) -> None:
        self._in_flight = False
        if self._monitor is None:
            return
        self._render(self._monitor.snapshot())
        self._controls()
        self.busyChanged.emit(self.busy)
        if self._active:
            self._timer.start(round(self._monitor.next_delay() * 1000))

    def _failed(self, _error: str) -> None:
        self._in_flight = False
        self.stop()
        self.summary.setText("STOPPED · 后台任务异常，请重新启动监测。")

    def _render(self, state: MonitorSnapshot) -> None:
        self._last_state = state
        observation = state.latest_observation
        candidate = state.candidates[-1] if state.candidates else None
        last_sample = state.latest_sample.strftime("%H:%M:%S") if state.latest_sample else "—"
        last_ocr = state.latest_ocr.strftime("%H:%M:%S") if state.latest_ocr else "—"
        last_change = state.latest_change.strftime("%H:%M:%S") if state.latest_change else "—"
        lines = [f"{state.status} · {state.reason}"]
        if observation:
            conv = observation.conversation
            selected_labels = " / ".join(conv.selected_identity_labels) or "无"
            title_labels = " / ".join(conv.title_identity_labels) or "无"
            lines.extend((
                f"消息页面 {observation.page} · B2 选中行 {observation.selected_row_status}",
                f"B2 原始名称 {conv.selected_raw_name or '—'} · 显示姓名 {conv.selected_name or '—'} · "
                f"身份标签 {selected_labels}（{conv.selected_peer_type}）",
                f"C1 原始标题 {conv.raw_title_line or '—'} · 标题姓名 {conv.title or '—'} · "
                f"身份标签 {title_labels}（{conv.title_peer_type}）",
                f"视觉匹配 {observation.match_status} · 可靠可见消息 "
                f"{'已取得' if observation.messages_reliable else '未取得'}（{observation.completeness}）",
            ))
            reliable = len(observation.reliable_messages)
            unresolved = sum(gap.box is not None for gap in observation.gaps)
            lines.append(f"可见消息候选 {reliable + unresolved} · 可靠识别 {reliable} · 不可靠 {unresolved}")
            if observation.file_messages:
                lines.append(f"文件卡片 {len(observation.file_messages)} · 字段成功解析 "
                             f"{sum(item.file.parse_status == 'COMPLETE' for item in observation.file_messages)}")
            regional_gaps = [gap.reason for gap in observation.gaps if gap.box is None]
            if regional_gaps:
                lines.append("无法定位的 C2 缺失 · " + "／".join(regional_gaps))
        else:
            lines.append("消息页面与 B2 选中行待核对 · 可靠可见消息未取得")
        lines.extend((
            "唯一客户身份未验证 · 当前只核对可见会话的消息连续性",
            f"最近采集 {last_sample} · 最近 OCR {last_ocr} · 最近图像变化 {last_change} · "
            f"采样 {state.sample_count} · 完整识别 {state.recognition_count} · 图像变化 {state.change_count} · "
            f"连续稳定采样 {state.stable_sample_count} 次",
            f"基线 {state.baseline_count} 条 · 新增候选 {state.candidate_count} 条 · 重同步 {state.resync_count} 次 · "
            f"未确认变化 {state.unconfirmed_count} 次 · "
            f"单次采集 {state.capture_ms:.0f} ms · 识别 {state.recognition_ms:.0f} ms",
        ))
        if state.baseline_type == "PARTIAL":
            lines.append("部分基线 · 可靠基线尚未建立。缺失位置详见当前观察。")
        elif state.baseline_type == "RELIABLE":
            lines.append("可靠基线")
        if not state.baseline_count and state.reason and state.status != "STOPPED":
            lines.append(f"基线未建立 · {state.reason}")
        if candidate:
            lines.append(f"最近候选 {candidate.message.direction} · {candidate.message.text[:65] or candidate.message.kind}")
        if not self._active and self._in_flight:
            lines.append("等待当前后台调用退出。")
        self.summary.setText("\n".join(lines))
        self._render_observed(observation)
        if self._shown_candidates is state.candidates:
            return
        self._shown_candidates = state.candidates
        visible = tuple(reversed(state.candidates))
        self.table.setRowCount(len(visible))
        for i, item in enumerate(visible):
            message = item.message
            evidence = f"{item.title} · {item.match_status} · {item.evidence} · " + "／".join(message.evidence)
            if message.ocr_spans:
                evidence += f" · OCR 最低 {min(span.score for span in message.ocr_spans):.0%}"
            if message.file:
                evidence += "\n" + file_details(message)
            values = (item.first_seen.strftime("%H:%M:%S"), message.direction, message.text,
                      message.kind, evidence, "尾部新增候选 · " + item.status, item.integrity_risk,
                      message.file.file_size_text or "—" if message.file else "—")
            for j, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(i, j, cell)

    def _render_observed(self, observation: MonitorObservation | None) -> None:
        if self._shown_observation is observation:
            return
        self._shown_observation = observation
        rows = []
        if observation:
            for entry in observation.reliable_messages:
                item = entry.message
                text = item.text or "图片卡片"
                detail = "／".join(item.evidence) + ("\n" + file_details(item) if item.file else "")
                rows.append((item.box, item.direction, item.kind, text, "可靠识别", detail,
                             item.file.file_size_text or "—" if item.file else "—"))
            files = {item.box: item for item in observation.file_messages}
            for gap in observation.gaps:
                text = "／".join(f"{span.text}（{span.score:.0%}）" for span in gap.spans)
                reason = gap.reason + (" · 可能为正文，视位置影响消息连续性" if gap.possible_message else "")
                file_message = files.get(gap.box)
                size = "—"
                if file_message:
                    reason += "\n" + file_details(file_message)
                    text = file_message.text or "文件名未确认"
                    size = file_message.file.file_size_text or "—"
                rows.append((gap.box, gap.direction, gap.kind, text, "不可靠", reason, size))
            for context in observation.context_ocr:
                rows.append((context.span.box, "—", context.kind, context.span.text, "上下文", context.reason, "—"))
        rows.sort(key=lambda row: (row[0] is None, row[0].top if row[0] else 0, row[0].left if row[0] else 0))
        self.observed_table.setRowCount(len(rows))
        for i, (box, *details) in enumerate(rows):
            coords = f"({box.left}, {box.top})–({box.right}, {box.bottom})" if box else "未定位"
            for j, value in enumerate((coords, *details)):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.observed_table.setItem(i, j, cell)

    def _show_events(self) -> None:
        if self._monitor is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("当前会话监测诊断事件")
        dialog.resize(700, 400)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        state = self._monitor.snapshot()
        lines = [f"{event.at:%H:%M:%S} · {event.status} · {event.reason}" for event in state.events]
        lines.extend(("", "最近 OCR 观察"))
        for observed in state.observations:
            lines.append(f"{observed.at:%H:%M:%S} · {observed.completeness} · 可靠 {len(observed.reliable_messages)} · "
                         f"不确定证据 {len(observed.gaps)} · {observed.blocker or '可比较'}")
        text.setPlainText("\n".join(lines))
        layout.addWidget(text)
        fit_dialog_to_screen(dialog)
        dialog.exec()
