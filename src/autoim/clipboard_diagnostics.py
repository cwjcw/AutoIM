from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from autoim.wecom.driver import WeComDriver
from autoim.wecom.window_manager import WeComWindowInfo

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CopyAttempt:
    name: str
    success: bool
    clipboard_changed: bool
    has_text: bool
    text: str
    text_length: int
    content_type: str
    elapsed_ms: int
    operation_time: str
    error: str = ""


def _attempt(driver: WeComDriver, name: str, timeout: float, combined: bool) -> tuple[CopyAttempt, WeComWindowInfo | None]:
    started = time.perf_counter()
    operation_time = datetime.now().astimezone().isoformat(timespec="seconds")
    info: WeComWindowInfo | None = None
    try:
        info, _ = driver.clear_clipboard()
        info, baseline = driver.clipboard_sequence_number()
        info = driver.send_shortcut("ctrl_a_c" if combined else "ctrl_c")
        info, changed = driver.clipboard_after_change(baseline, timeout=timeout)
        text = changed.snapshot.text or ""
        return CopyAttempt(
            name=name,
            success=True,
            clipboard_changed=changed.changed,
            has_text=bool(text.strip()),
            text=text,
            text_length=len(text),
            content_type=changed.snapshot.content_type,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            operation_time=operation_time,
            error="",
        ), info
    except Exception as exc:
        logger.exception("剪贴板诊断步骤失败：%s", name)
        return CopyAttempt(
            name=name,
            success=False,
            clipboard_changed=False,
            has_text=False,
            text="",
            text_length=0,
            content_type="无法读取",
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            operation_time=operation_time,
            error=f"{type(exc).__name__}: {exc}",
        ), info


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _attempt_section(result: CopyAttempt, contact_name: str,
                     chat_message_state: str, unrelated_state: str) -> list[str]:
    contact_present = _yes_no(bool(contact_name and contact_name in result.text)) if contact_name else "未填写联系人名称，无法判断"
    return [
        result.name,
        f"操作时间：{result.operation_time}",
        f"操作成功：{_yes_no(result.success)}",
        f"剪贴板是否变化：{_yes_no(result.clipboard_changed)}",
        f"是否获得文本：{_yes_no(result.has_text)}",
        f"文本长度：{result.text_length}",
        f"剪贴板内容类型：{result.content_type}",
        f"是否出现联系人名称（{contact_name or '未提供'}）：{contact_present}",
        f"是否出现聊天消息：{chat_message_state}",
        f"是否出现界面无关文本：{unrelated_state}",
        f"耗时：{result.elapsed_ms} ms",
        f"错误：{result.error or '无'}",
        "文本内容（完整）：",
        result.text if result.text else "<无文本>",
        "",
    ]


def _render_outputs(results: list[dict[str, Any]], metadata: dict[str, Any],
                    contact_name: str, chat_message_state: str,
                    unrelated_state: str) -> tuple[str, str]:
    hwnd = int(metadata["hwnd"])
    pid = metadata.get("pid")
    window_title = metadata.get("window_title", "")
    class_name = metadata.get("class_name", "")
    records = [CopyAttempt(**item) for item in results]
    transcript = [
        "AutoIM 企业微信剪贴板诊断详情",
        f"PID: {pid or ''}",
        f"HWND: 0x{hwnd:X}",
        f"窗口标题: {window_title}",
        f"ClassName: {class_name}",
        "提示：复制文本由用户手动选择目标区域；请确认其中不含非预期内容。",
        "",
    ]
    for result in records:
        transcript.extend(_attempt_section(result, contact_name, chat_message_state, unrelated_state))

    lines = [
        "AutoIM 企业微信剪贴板诊断汇总",
        f"操作时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"企业微信 PID: {pid or ''}",
        f"HWND: 0x{hwnd:X}",
        f"窗口标题: {window_title}",
        f"ClassName: {class_name}",
        f"联系人名称检查值: {contact_name or '未提供'}",
        "",
    ]
    for result in records:
        contact_present = _yes_no(bool(contact_name and contact_name in result.text)) if contact_name else "无法判断（未填写联系人名称）"
        lines.extend([
            result.name,
            f"操作成功：{_yes_no(result.success)}",
            f"剪贴板是否变化：{_yes_no(result.clipboard_changed)}",
            f"是否获得文本：{_yes_no(result.has_text)}",
            f"文本长度：{result.text_length}",
            f"文本内容前 500 字符：{result.text[:500] or '<无文本>'}",
            f"是否出现联系人名称：{contact_present}",
            f"是否出现聊天消息：{chat_message_state}",
            f"是否出现界面无关文本：{unrelated_state}",
            f"剪贴板内容类型：{result.content_type}",
            f"耗时：{result.elapsed_ms} ms",
            f"错误：{result.error or '无'}",
            "",
        ])
    return "\n".join(transcript), "\n".join(lines)


def update_manual_copy_report(result: dict[str, Any], contact_name: str,
                              chat_message_state: str,
                              unrelated_state: str) -> dict[str, str]:
    transcript, summary = _render_outputs(
        result["results"], result["metadata"], contact_name,
        chat_message_state, unrelated_state,
    )
    output_path = Path(result["output_path"])
    summary_path = Path(result["summary_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(transcript, encoding="utf-8")
    summary_path.write_text(summary, encoding="utf-8")
    return {"transcript": transcript, "summary": summary}


def run_manual_copy_test(contact_name: str,
                         output_path: Path, summary_path: Path,
                         timeout: float = 3.0,
                         driver: WeComDriver | None = None) -> dict[str, Any]:
    """Run Ctrl+C and Ctrl+A+Ctrl+C after the user manually focuses a chat area."""
    driver = driver or WeComDriver()
    attempts = [
        _attempt(driver, "Ctrl+C", timeout=timeout, combined=False),
        _attempt(driver, "Ctrl+A + Ctrl+C", timeout=timeout, combined=True),
    ]
    info = next((item for _attempt_result, item in reversed(attempts) if item is not None), None)
    if info is None:
        # Preserve useful environment identity even if both activation attempts fail.
        try:
            info = driver.resolve_window()
        except Exception:
            info = None
    metadata = {
        "pid": info.pid if info else None,
        "hwnd": info.hwnd if info else 0,
        "window_title": info.title if info else "",
        "class_name": info.class_name if info else "",
        "process_path": info.process_path if info else "",
    }
    result = {
        "results": [asdict(attempt) for attempt, _info in attempts],
        "metadata": metadata,
        "output_path": str(output_path),
        "summary_path": str(summary_path),
    }
    rendered = update_manual_copy_report(result, contact_name, "待人工核对", "待人工核对")
    result.update(rendered)
    logger.info("手工复制诊断完成，结果=%s 汇总=%s", output_path, summary_path)
    return result
