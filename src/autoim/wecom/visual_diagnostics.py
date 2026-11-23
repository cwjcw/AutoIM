"""Format and explicitly copy diagnostics from the existing visual panels."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from autoim import __version__
from autoim.automation import clipboard
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_messages import VisibleMessageObservation
from autoim.wecom.visual_monitor import MonitorObservation, MonitorSnapshot
from autoim.wecom.visual_ocr import TextSpan

_BLOCKERS = {
    "LAYOUT_UNCERTAIN": "消息页区域证据不足",
    "SELECTED_ROW_UNCERTAIN": "B2 选中行证据不足",
    "SELECTED_NAME_UNCERTAIN": "B2 选中姓名证据不足",
    "TITLE_UNCERTAIN": "C1 标题证据不足",
    "NAME_MISMATCH": "C1 与 B2 姓名不同",
    "CONVERSATION_UNCERTAIN": "会话视觉核对未通过",
    "PAGE_UNCERTAIN": "消息页证据不足",
    "AMBIGUOUS_HOMONYMS": "同名会话连续性不明",
    "MESSAGES_UNRELIABLE": "C2 消息/OCR 证据不足",
}
_MONITOR_REASONS = {
    "PAUSED": "窗口或截图不可用",
    "RESYNC_REQUIRED": "窗口、会话或可见序列需要重同步",
    "OBSERVATION_UNRELIABLE": "当前观察无法建立可比较基线",
    "UNCONFIRMED_VISUAL_CHANGE": "新增区域、像素或顺序证据不足",
}


def _time(value: datetime | None) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else "—"


def _box(value: Box | None) -> str:
    return f"({value.left}, {value.top})–({value.right}, {value.bottom})" if value else "未定位"


def _ocr(span: TextSpan) -> str:
    return f"{span.text} · OCR {span.score:.1%} · {_box(span.box)}"


def file_details(item: VisibleMessageObservation) -> str:
    file = item.file
    if file is None:
        return ""
    failure = "；".join(file.failure_reasons)
    raw_blocks = "\n".join("  " + _ocr(span) for span in file.name_ocr_blocks)
    icon_blocks = " / ".join(_ocr(span) for span in file.icon_evidence) or "—"
    confidence = f"{file.confidence:.1%}" if file.confidence is not None else "—"
    return (f"文件名 {file.file_name or '未确认'} · 扩展名 {file.file_extension or '—'} · "
            f"文件大小 {file.file_size_text or '—'} · {file.parse_status} · 字段置信度 {confidence} · 附件内容未读取\n"
            f"文件名原始 OCR 块\n{raw_blocks or '  —'}\n图标 OCR {icon_blocks}"
            + (f"\n失败原因 {failure}" if failure else ""))


def _message_lines(item: VisibleMessageObservation, prefix: str = "") -> list[str]:
    confidence = f"{min(span.score for span in item.ocr_spans):.1%}" if item.ocr_spans else "—"
    lines = [f"{prefix}#{item.sequence} {item.direction} {item.kind} · {item.status} · "
             f"{_box(item.box)} · OCR 最低 {confidence}",
             "证据／原因 " + "；".join(item.evidence)]
    if item.file:
        lines.append(file_details(item))
    elif item.text:
        lines.append(item.text)
    return lines


def build_visual_report(observation: MonitorObservation | None = None, *,
                        state: MonitorSnapshot | None = None, full: bool = False) -> str:
    if state is not None:
        observation = state.latest_observation
    recent = observation or (state.observations[-1] if state and state.observations else None)
    lines = [
        f"AutoIM {__version__} · PC-V2.2 · {'完整识别明细（可能含敏感信息）' if full else '监测诊断报告（无名称和正文）'}",
        "数据来源 WINDOW_VISUAL_OCR",
        f"采集时间 {_time(state.latest_sample if state else recent.at if recent else None)}",
        f"监测状态 {state.status if state else '单次采集'}",
        "采集方式 Windows PrintWindow（PW_CLIENTONLY），本地 OCR",
    ]
    if recent:
        _, hwnd, _, width, height, dpi = recent.window_key
        lines.append(f"识别帧 {_time(recent.at)} · HWND 0x{hwnd:X} · 原图 {width}×{height} · DPI {dpi}"
                     + (" · 上次有效画面" if observation is None else ""))
    else:
        lines.append("HWND — · 原图 — · DPI — · 尚无有效观察")

    conv = observation.conversation if observation else None
    reliable = len(observation.reliable_messages) if observation else 0
    unresolved = sum(gap.box is not None for gap in observation.gaps) if observation else 0
    files = observation.file_messages if observation else ()
    parsed = sum(item.file.parse_status == "COMPLETE" for item in files)
    anchored = sum(item.status == "CANDIDATE" for item in files)
    unassigned = observation.unassigned_ocr if observation else ()
    lines.extend((
        f"页面 {observation.page if observation else '—'} · B2 选中行 {observation.selected_row_status if observation else '—'}"
        f" · C1 标题 {'可靠' if conv and conv.title_name_reliable else '未确认'}",
        f"视觉匹配 {observation.match_status if observation else '—'} · 唯一身份 {'已验证' if conv and conv.identity_unique else '未验证'}",
        f"基线 {state.baseline_type if state else '无'} · 基线消息 {state.baseline_count if state else 0}",
        f"当前可见消息候选 {reliable + unresolved} · 可靠 {reliable} · 不可靠 {unresolved}"
        f" · 完整性 {observation.completeness if observation else '—'}",
        f"新增候选 {state.candidate_count if state else 0} · 未确认变化 {state.unconfirmed_count if state else 0}",
        f"完整识别耗时 {state.recognition_ms if state else observation.recognition_ms if observation else 0:.0f} ms"
        f" · 最近 OCR {_time(state.latest_ocr if state else None)}",
        f"文件卡片候选 {len(files)} · 字段成功解析 {parsed} · 方向确认 {anchored} · 未归属 OCR {len(unassigned)}",
    ))
    reasons = []
    if state and state.status in _MONITOR_REASONS:
        reasons.append(_MONITOR_REASONS[state.status])
    if observation:
        if observation.blocker_code in _BLOCKERS:
            reasons.append(_BLOCKERS[observation.blocker_code])
        for kind, count in Counter(gap.kind for gap in observation.gaps).items():
            reasons.append(f"{kind} ×{count}")
        if files:
            reasons.extend(dict.fromkeys(reason for item in files for reason in item.file.failure_reasons))
    lines.append("主要失败原因 " + ("；".join(reasons) if reasons else "—"))
    if not full:
        return "\n".join(lines)

    if conv:
        lines.extend((f"B2 选中行原文 {conv.selected_raw_name or '—'}", f"C1 标题原文 {conv.raw_title_line or '—'}"))
    if state:
        lines.append(f"监测状态说明 {state.reason or '—'}")
    lines.extend(("", "当前可见消息（按原图顺序）"))
    if observation and observation.messages:
        for entry in observation.messages:
            lines.extend(_message_lines(entry.message))
    else:
        lines.append("无")
    lines.extend(("", "未归属 OCR"))
    if unassigned:
        lines.extend(f"{_ocr(entry.span)} · {entry.reason} · 最近候选 {entry.nearest_container or '—'}"
                     for entry in unassigned)
    else:
        lines.append("无")
    if observation and observation.gaps:
        lines.extend(("", "不完整观察"))
        lines.extend(f"{_box(gap.box)} · {gap.direction} · {gap.kind} · {gap.reason}" for gap in observation.gaps)
    lines.extend(("", "新增消息候选"))
    if state and state.candidates:
        for item in state.candidates:
            lines.extend(_message_lines(item.message, f"{_time(item.first_seen)} · "))
            lines.append(f"{item.status} · {item.evidence} · {item.integrity_risk}")
    else:
        lines.append("无")
    lines.extend(("", "未确认变化记录"))
    events = tuple(event for event in state.events if event.status == "UNCONFIRMED_VISUAL_CHANGE") if state else ()
    lines.extend((f"{_time(event.at)} · {event.status} · {event.reason}" for event in events) if events else ("无",))
    return "\n".join(lines)


class DiagnosticCopyControls(QWidget):
    def __init__(self, report: Callable[[bool], str], parent=None) -> None:
        super().__init__(parent)
        self._report = report
        self.summary_button = QPushButton("复制监测诊断报告")
        self.full_button = QPushButton("复制完整识别明细")
        self.full_button.setToolTip("可能包含客户姓名、聊天正文及附件名称")
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        actions.addWidget(self.summary_button)
        actions.addWidget(self.full_button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addWidget(self.feedback)
        self.summary_button.clicked.connect(lambda: self._copy(False))
        self.full_button.clicked.connect(lambda: self._copy(True))

    def _copy(self, full: bool) -> None:
        if full and QMessageBox.question(
                self, "复制完整识别明细",
                "明细可能包含客户姓名、手机号、聊天正文和附件名称。继续会将其写入本机 Windows 剪贴板。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            self.feedback.setText("已取消复制完整识别明细。")
            return
        try:
            clipboard.write_unicode_text(self._report(full))
        except Exception as exc:
            self.feedback.setText(f"复制失败 · {exc}")
        else:
            self.feedback.setText("已复制 Unicode 纯文本到 Windows 剪贴板。")
