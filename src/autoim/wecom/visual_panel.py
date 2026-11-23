"""PC-V1 controls and presentation inside the existing WeCom page."""

from __future__ import annotations

from pathlib import Path

import cv2
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHeaderView, QHBoxLayout,
                               QLabel, QPlainTextEdit, QProgressBar, QPushButton, QTabWidget,
                               QTableWidget, QTableWidgetItem, QSizePolicy, QVBoxLayout)

from autoim.calibration import CalibrationStore
from autoim.vision_widgets import ScreenshotCanvas, ScreenshotSelectionDialog, fit_dialog_to_screen
from autoim.wecom.collection_panel import VisualCollectionPanel
from autoim.wecom.collection_service import CollectionReader, CollectionService
from autoim.wecom.visual_capture import VisualFrame, capture_visible_window
from autoim.wecom.visual_diagnostics import DiagnosticCopyControls, build_visual_report, file_details
from autoim.wecom.visual_layout import Box, LEAF_REGIONS, REGION_NAMES
from autoim.wecom.visual_monitor import VisualMonitor, summarize
from autoim.wecom.visual_monitor_panel import VisualMonitorPanel
from autoim.wecom.visual_observation import VisualObservation, read_visible_conversation

_CALIBRATION_LABELS = {**LEAF_REGIONS, "left_anchor": "左锚点 · 框选左侧气泡",
                       "right_anchor": "右锚点 · 框选右侧气泡"}


def _pixmap(frame: VisualFrame) -> QPixmap:
    pixels = cv2.cvtColor(frame.pixels, cv2.COLOR_BGR2RGB)
    image = QImage(pixels.data, frame.capture.width, frame.capture.height,
                   pixels.strides[0], QImage.Format.Format_RGB888)
    return QPixmap.fromImage(image.copy())


def _flag(value: bool | None) -> str:
    return "未知" if value is None else "是" if value else "否"


def _box(value: Box | None) -> str:
    return f"({value.left}, {value.top})–({value.right}, {value.bottom})" if value else "—"


def _table(headers: list[str], rows: list[list[str]]) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().setDefaultSectionSize(32)
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            table.setItem(i, j, item)
    return table


class VisualResultDialog(QDialog):
    def __init__(self, observation: VisualObservation, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("PC 企业微信视觉采集诊断")
        self.resize(1120, 740)
        layout = QVBoxLayout(self)
        frame = observation.frame
        window = frame.capture
        capture_status = (f"截图成功 · HWND 0x{window.hwnd:X} · DPI {frame.state.dpi} "
                          f"({window.device_pixel_ratio:.0%}) · 原图 {window.width}×{window.height} · "
                          f"采集 {frame.diagnostics.elapsed_ms:.0f} ms · 识别 {observation.recognition_ms:.0f} ms")
        capture_label = QLabel(capture_status)
        capture_label.setWordWrap(True)
        layout.addWidget(capture_label)
        conv = observation.conversation
        unread = observation.global_unread
        count = str(unread.global_unread_count) if unread.global_unread_count_reliable else "—"
        status = QLabel(f"{observation.status} · 页面 {observation.page} · {conv.completeness}\n"
                        f"标题 {conv.title or '未知'}　选中行 {conv.selected_name or '未知'}　"
                        f"{conv.match_status} · 唯一身份 {_flag(conv.identity_unique)}\n"
                        f"A 区全局未读 {_flag(unread.global_unread_visible)} · 数字 {count}（非静音会话范围）")
        status.setTextFormat(Qt.TextFormat.PlainText)
        status.setWordWrap(True)
        layout.addWidget(status)
        self.copy_controls = DiagnosticCopyControls(lambda full: build_visual_report(summarize(observation), full=full), self)
        layout.addWidget(self.copy_controls)
        self.tabs = tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.canvas = canvas = ScreenshotCanvas(_pixmap(frame), "preview")
        regions = [(region.name, region.normalized, "#22c55e") for region in observation.layout.regions]
        marks = [(str(message.sequence), message.box, "#fb923c") for message in observation.messages]
        marks += [("", row.box, "#60a5fa") for row in observation.contacts if row.is_selected]
        marks += [("", row.badge, "#f87171") for row in observation.contacts if row.badge]
        if unread.badge:
            marks.append(("", unread.badge, "#f87171"))
        marks += [("", box, "#c084fc") for box in (observation.list_scrollbar, observation.history_scrollbar,
                                                   observation.board_scrollbar) if box]
        canvas.overlays = tuple(regions + [(name, bounds.relative(window.width, window.height), color)
                                           for name, bounds, color in marks])
        base_overlays = canvas.overlays
        preview = QFrame()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(3, 3, 3, 3)
        preview_layout.addWidget(canvas, 1)
        self.preview_label = QLabel()
        self.preview_label.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_label.setWordWrap(True)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        preview_layout.addWidget(self.preview_label)
        incomplete_marks = [
            ("", item.span.box, "#facc15") for item in observation.message_ocr if item.kind == "UNASSIGNED"
        ] + [("", row.box, "#facc15") for row in observation.contacts if row.status != "CANDIDATE"]
        incomplete_marks += [("", entry.span.box, "#facc15") for entry in observation.contact_text_diagnostics]
        if incomplete_marks:
            show_incomplete = QCheckBox("显示未归属或不完整候选框")
            preview_layout.addWidget(show_incomplete)

            def toggle_incomplete(enabled: bool) -> None:
                extra = tuple((name, bounds.relative(window.width, window.height), color)
                              for name, bounds, color in incomplete_marks)
                canvas.overlays = base_overlays + extra if enabled else base_overlays
                canvas.update()

            show_incomplete.toggled.connect(toggle_incomplete)
        tabs.addTab(preview, "整图")
        canvas.viewportChanged.connect(self._refresh_preview)
        tabs.currentChanged.connect(self._refresh_preview)
        self._refresh_preview()
        regions_by_name = {region.name: region for region in observation.layout.regions}
        failures = dict(observation.layout.failures)
        area_rows = []
        for name in (*REGION_NAMES, *(("D",) if "D" in regions_by_name else ())):
            region = regions_by_name.get(name)
            if region:
                normalized = ", ".join(f"{getattr(region.normalized, key):.4f}"
                                       for key in ("left", "top", "right", "bottom"))
                source = ("人工标定" if "人工" in region.evidence
                          and "持续边缘" not in region.evidence else
                          "自动与人工边界" if "人工" in region.evidence else "自动识别")
                area_rows.append([name, "已定位", _box(region.box), normalized, source, region.evidence])
            else:
                area_rows.append([name, "未定位", "—", "—", "—", failures.get(name, "缺少证据")])
        tabs.addTab(_table(["区域", "状态", "原始像素", "相对坐标", "来源", "证据／失败原因"], area_rows), "区域")
        contact_page = QFrame()
        contact_layout = QVBoxLayout(contact_page)
        contacts = observation.contacts
        contact_summary = QLabel(
            f"可见候选行 {len(contacts)} · 可靠姓名 {sum(row.name_reliable for row in contacts)} · "
            f"不完整 {sum(row.status == 'PARTIALLY_VISIBLE' for row in contacts)} · "
            f"未归属文字 {len(observation.contact_text_diagnostics)} · "
            f"未读 {sum(row.has_unread_indicator for row in contacts)} · "
            f"静音 {sum(row.is_muted is True for row in contacts)}")
        contact_summary.setWordWrap(True)
        contact_layout.addWidget(contact_summary)
        contact_layout.addWidget(_table(["行", "显示名称", "类型候选", "消息预览", "未读", "未读数", "静音", "活动时间", "选中", "证据"], [
            [str(row.sequence), row.display_name, row.kind, row.preview, _flag(row.has_unread_indicator),
             str(row.unread_count) if row.unread_count_reliable else "—",
             _flag(row.is_muted), row.activity_text + ("（不确定）" if row.activity_text and not row.activity_time_reliable else ""),
             _flag(row.is_selected), f"{row.status} · {_box(row.box)} · " + " · ".join(row.evidence)]
            for row in contacts]))
        if observation.contact_text_diagnostics:
            contact_layout.addWidget(_table(["未归属 B2 文字", "原图坐标", "OCR", "过滤原因"], [
                [entry.span.text, _box(entry.span.box), f"{entry.span.score:.2%}", entry.reason]
                for entry in observation.contact_text_diagnostics]))
        tabs.addTab(contact_page, "联系人")
        messages = _table(["序号", "类型", "方向", "可见时间", "正文或文件名", "文件大小", "来源", "容器", "状态"], [
            [str(item.sequence), item.kind, item.direction, item.time_evidence, item.text[:160],
             item.file.file_size_text or "—" if item.file else "—", "C2", _box(item.box), item.status]
            for item in observation.messages])
        message_page = QFrame()
        message_layout = QVBoxLayout(message_page)
        items = observation.messages
        classified = sum(item.kind != "UNKNOWN" for item in items)
        message_layout.addWidget(QLabel(f"{observation.message_completeness} · 已解析 {classified} · C2 观察 {len(items)} · "
                                       f"可靠会话归属 {_flag(conv.attribution_reliable)}"))
        message_layout.addWidget(messages)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        message_layout.addWidget(detail)

        def show_message() -> None:
            row = messages.currentRow()
            if row < 0:
                return
            item = observation.messages[row]
            distances = " / ".join("未知" if value is None else f"{value:.2%}"
                                   for value in (item.left_anchor_distance, item.right_anchor_distance))
            detail.setPlainText(f"{item.kind} · {item.direction} · {item.status}\n"
                                f"左右锚点距离（占 C2 宽度） {distances}\n"
                                + "\n".join(item.evidence) + "\n\n" + (file_details(item) if item.file else item.text))

        messages.itemSelectionChanged.connect(show_message)
        if observation.messages:
            messages.selectRow(0)
        tabs.addTab(message_page, f"可见消息 ({len(observation.messages)})")
        candidate_page = QFrame()
        candidate_layout = QVBoxLayout(candidate_page)
        candidates = observation.candidates
        counts = " · ".join(f"{kind} {sum(item.kind == kind for item in items)}"
                           for kind in ("TEXT", "IMAGE", "FILE"))
        directions = " · ".join(f"{way} {sum(item.direction == way for item in candidates)}"
                               for way in ("INBOUND", "OUTBOUND", "UNKNOWN"))
        candidate_layout.addWidget(QLabel(f"消息框 {len(candidates)} · {counts}\n"
                                         f"{directions} · 锚点 X {observation.anchors}"))
        candidate_layout.addWidget(_table(["候选", "位置", "方向", "左距", "右距", "状态"], [
            [str(i), _box(item.container.box), item.direction,
             _distance(item.left_distance), _distance(item.right_distance),
             f"{item.container.source} · {item.reason}" + (" · 触及视口" if item.container.clipped else "")]
            for i, item in enumerate(candidates, 1)]))
        tabs.addTab(candidate_page, "消息框")
        ocr_page = QFrame()
        ocr_layout = QVBoxLayout(ocr_page)
        ocr_layout.addWidget(QLabel(f"OCR 初始化 {observation.ocr_status} · "
                                   f"裁剪 {len(observation.ocr_areas)} · "
                                   f"文字块 {sum(len(item.spans) for item in observation.ocr_areas)}"))
        ocr_rows = []
        for attempt in observation.ocr_areas:
            ocr_rows.append([attempt.region, _box(attempt.box), attempt.status,
                             str(len(attempt.spans)), "", "—", attempt.reason])
            ocr_rows.extend([attempt.region, _box(attempt.box), "文字块", "", span.text,
                             f"{span.score:.2%}", _box(span.box)] for span in attempt.spans)
        if observation.ocr_reason:
            ocr_rows.append(["初始化", "—", observation.ocr_status, "", "", "—", observation.ocr_reason])
        ocr_layout.addWidget(_table(["区域", "OCR 输入框", "状态", "块数", "识别文字", "置信分数", "位置／原因"],
                                   ocr_rows))
        tabs.addTab(ocr_page, "OCR")
        ledger_page = QFrame()
        ledger_layout = QVBoxLayout(ledger_page)
        ledger = observation.message_ocr
        ledger_summary = QLabel(
            f"C2 OCR {len(ledger)} · 消息框 {len(candidates)} · "
            f"已归属 {sum(item.kind in ('MESSAGE', 'MEDIA') for item in ledger)} · "
            f"时间 {sum(item.kind == 'TIME' for item in ledger)} · "
            f"系统 {sum(item.kind == 'SYSTEM' for item in ledger)} · "
            f"未归属 {sum(item.kind == 'UNASSIGNED' for item in ledger)} · 已解析 {classified}")
        ledger_summary.setWordWrap(True)
        ledger_layout.addWidget(ledger_summary)
        ledger_layout.addWidget(_table(["文字", "原图坐标", "OCR", "去向", "容器", "最近候选", "疑似正文", "证据／原因"], [
            [item.span.text, _box(item.span.box), f"{item.span.score:.2%}", item.kind,
             str(item.container_number or "—"), str(item.nearest_container or "—"),
             _flag(item.possible_message), item.reason] for item in ledger]))
        tabs.addTab(ledger_page, "C2 文字归属")
        composer = observation.composer
        details = [capture_status, f"物理窗口矩形 {window.rect} · 客户区 {frame.state.width}×{frame.state.height}",
                   f"采集时间 {frame.captured_at:%Y-%m-%d %H:%M:%S}", frame.diagnostics.coverage,
                   *observation.warnings, f"鼠标区域 {observation.hovered_region or '未确认'}",
                   f"B1 搜索、加号 {_box(observation.search_box)} / {_box(observation.add_button)}",
                   f"B2 滚动条 {_box(observation.list_scrollbar)} · C2 滚动条 {_box(observation.history_scrollbar)}",
                   f"C1 原始标题行 {conv.raw_title_line or '未知'}",
                   f"标题身份文字 {' / '.join(conv.visible_identity) or '未知'}",
                   f"B2 原始选中名称 {conv.selected_raw_name or '未知'} · 显示姓名 {conv.selected_name or '未知'} · "
                   f"身份标签 {' / '.join(conv.selected_identity_labels) or '无'}（{conv.selected_peer_type}）",
                   f"姓名比较证据 {conv.comparison_evidence} · "
                   f"C1 可靠 {_flag(conv.title_name_reliable)} · B2 可靠 {_flag(conv.selected_name_reliable)}",
                   f"A 区全局未读证据 {unread.evidence} · 入口 {_box(unread.entry)} · 徽标 {_box(unread.badge)}"]
        if composer:
            details += [f"C3 工具栏 {_box(composer.toolbar)} · 发送按钮候选 {_box(composer.send_button)}",
                        f"按钮外观 {composer.send_appearance} · 可见草稿 {_flag(composer.has_visible_draft)}"]
        if observation.context_text:
            details.append("C2 未归入消息容器的可见文字（诊断）")
            details.extend(f"{span.text} · {_box(span.box)} · OCR {span.score:.0%}"
                           for span in observation.context_text)
        report = QPlainTextEdit("\n".join(details))
        report.setReadOnly(True)
        tabs.addTab(report, "概况")

    def showEvent(self, event):
        super().showEvent(event)
        fit_dialog_to_screen(self)
        self._refresh_preview()

    def _refresh_preview(self, *_args) -> None:
        self.canvas.update()
        self.preview_label.setText(self.canvas.preview_geometry())


def _distance(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


class VisualCapturePanel(QFrame):
    def __init__(self, calibration_path: Path, run_worker, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.calibration = CalibrationStore(calibration_path)
        self.run_worker = run_worker
        self._manual_busy = False
        self._pending_region = "A"
        self.result_dialog: VisualResultDialog | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        title = QLabel("PC 企业微信视觉采集诊断")
        title.setStyleSheet("font-size:16px;font-weight:700;color:#172235")
        layout.addWidget(title)
        note = QLabel("在最大化的企业微信中打开测试会话。诊断将对可见界面截图和本地文字识别。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.read_button = QPushButton("读取当前企业微信可见界面")
        self.read_button.setObjectName("primaryButton")
        self.read_button.clicked.connect(self.start)
        actions = QHBoxLayout()
        actions.addWidget(self.read_button)
        self.delayed_capture = QCheckBox("延时 3 秒")
        self.delayed_capture.setToolTip("可在倒计时内手动把鼠标移至 B2 或 C2，观察该区域滚动条。")
        actions.addWidget(self.delayed_capture)
        actions.addStretch()
        layout.addLayout(actions)
        controls = QHBoxLayout()
        self.region = QComboBox()
        for key, title in _CALIBRATION_LABELS.items():
            self.region.addItem(title, key)
        self.calibrate_button = QPushButton("在截图中标定")
        self.calibrate_button.clicked.connect(self.calibrate)
        self.use_calibration = QCheckBox("使用标定")
        self.use_calibration.setChecked(True)
        controls.addWidget(self.region)
        controls.addWidget(self.calibrate_button)
        controls.addWidget(self.use_calibration)
        controls.addStretch()
        layout.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumHeight(6)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel("等待采集")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.monitor_panel = VisualMonitorPanel(run_worker, self._make_monitor, self)
        self.monitor_panel.busyChanged.connect(self._monitor_busy_changed)
        layout.addWidget(self.monitor_panel)
        self.collection_panel = VisualCollectionPanel(run_worker, self._make_collection, self)
        self.use_calibration.toggled.connect(self._refresh_collection_calibration)
        self.collection_panel.busyChanged.connect(self._monitor_busy_changed)
        layout.addWidget(self.collection_panel)

    def _busy(self, busy: bool) -> None:
        self._manual_busy = busy
        self._monitor_busy_changed()
        self.progress.setVisible(busy)

    def _monitor_busy_changed(self, _busy: bool = False) -> None:
        self.monitor_panel.set_available(not self._manual_busy and not self.collection_panel.busy)
        self.collection_panel.set_available(not self._manual_busy and not self.monitor_panel.busy)
        enabled = not self.monitor_panel.busy and not self.collection_panel.busy and not self._manual_busy
        for widget in (self.read_button, self.calibrate_button, self.delayed_capture, self.use_calibration):
            widget.setEnabled(enabled)

    def _make_monitor(self, interval: float) -> VisualMonitor:
        path = self.calibration.path if self.use_calibration.isChecked() else None
        return VisualMonitor(path, self.calibration.path.with_name("wecom-pc-visual-thresholds.json"), interval=interval)

    def _make_collection(self, profile, store) -> CollectionService:
        path = self.calibration.path if self.use_calibration.isChecked() else None
        reader = CollectionReader(path, self.calibration.path.with_name("wecom-pc-visual-thresholds.json"))
        return CollectionService(profile, store, reader)

    def _refresh_collection_calibration(self) -> None:
        self.collection_panel.set_calibration(self.calibration.path if self.use_calibration.isChecked() else None)

    def stop_monitoring(self) -> None:
        self.monitor_panel.stop()
        self.collection_panel.stop()

    def start(self) -> None:
        if self._manual_busy or self.monitor_panel.busy or self.collection_panel.busy:
            return
        if self.result_dialog is not None:
            self.result_dialog.close()
        self._busy(True)
        if self.delayed_capture.isChecked():
            self.status.setText("3 秒后采集。可将鼠标手动移至目标区域。")
            QTimer.singleShot(3000, self, self._read)
        else:
            self._read()

    def _read(self) -> None:
        self.status.setText("正在采集目标窗口并识别…")
        path = self.calibration.path if self.use_calibration.isChecked() else None
        thresholds = self.calibration.path.with_name("wecom-pc-visual-thresholds.json")
        self.run_worker(read_visible_conversation, path, thresholds, on_result=self._show, on_error=self._failed)

    def calibrate(self) -> None:
        if self._manual_busy or self.monitor_panel.busy or self.collection_panel.busy:
            return
        self._pending_region = self.region.currentData()
        self._busy(True)
        self.status.setText("正在读取标定截图…")
        self.run_worker(capture_visible_window, on_result=self._calibrate_frame, on_error=self._failed)

    def _calibrate_frame(self, frame: VisualFrame) -> None:
        self._busy(False)
        key = self._pending_region
        dialog = ScreenshotSelectionDialog(_pixmap(frame), mode="region",
                                           title=_CALIBRATION_LABELS[key], parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_normalized_rect:
            self.status.setText("标定已取消")
            return
        try:
            self.calibration.save_region(key, dialog.selected_normalized_rect)
            self._refresh_collection_calibration()
            total = sum(name in self.calibration.load() for name in LEAF_REGIONS)
        except (OSError, ValueError) as exc:
            self._failed(f"无法保存标定（{type(exc).__name__}）")
            return
        self.status.setText(f"已标定 {_CALIBRATION_LABELS[key]} · 分区 {total}/{len(LEAF_REGIONS)}")

    def _failed(self, message: str) -> None:
        self._busy(False)
        self.status.setText(message)

    def _show(self, observation: VisualObservation) -> None:
        self._busy(False)
        self.status.setText(f"{observation.status} · {len(observation.contacts)} 个可见联系人 · "
                            f"{len(observation.messages)} 条消息候选 · {observation.frame.captured_at:%H:%M:%S}")
        dialog = VisualResultDialog(observation, self)
        self.result_dialog = dialog
        dialog.finished.connect(self._forget_dialog)
        dialog.show()

    def _forget_dialog(self) -> None:
        self.result_dialog = None
