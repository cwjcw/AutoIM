from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BACKENDS = (
    ("uiautomation", "uiautomation", "uiautomation.txt"),
    ("pywinauto-uia", "pywinauto-uia", "pywinauto-uia.txt"),
    ("pywinauto-win32", "pywinauto-win32", "pywinauto-win32.txt"),
    ("raw-view", "UIA Raw View", "raw-view.txt"),
)

FIELDS = (
    "Name", "ControlType", "AutomationId", "ClassName", "NativeWindowHandle",
    "BoundingRectangle", "IsEnabled", "IsOffscreen", "FrameworkId", "ProcessId",
)


@dataclass(slots=True)
class BackendResult:
    key: str
    label: str
    filename: str
    status: str
    count: int = 0
    max_depth: int = 0
    elapsed_ms: int = 0
    error: str = ""
    records: list[dict[str, Any]] | None = None


def _safe(getter, default: Any = "") -> Any:
    try:
        value = getter()
        if value is None:
            return default
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (tuple, list)):
            return list(value)
        if all(hasattr(value, name) for name in ("left", "top", "right", "bottom")):
            return [value.left, value.top, value.right, value.bottom]
        return str(value)
    except Exception:
        return default


def _uiautomation_scan(hwnd: int, max_depth: int, max_controls: int) -> list[dict[str, Any]]:
    import uiautomation as auto

    root = auto.ControlFromHandle(hwnd)
    if not root:
        raise RuntimeError("ControlFromHandle 未返回根控件")
    records: list[dict[str, Any]] = []

    def visit(control: Any, depth: int) -> None:
        if len(records) >= max_controls:
            return
        record = {
            "Name": _safe(lambda: control.Name),
            "ControlType": _safe(lambda: control.ControlTypeName),
            "AutomationId": _safe(lambda: control.AutomationId),
            "ClassName": _safe(lambda: control.ClassName),
            "NativeWindowHandle": _safe(lambda: control.NativeWindowHandle, 0),
            "BoundingRectangle": _safe(lambda: control.BoundingRectangle),
            "IsEnabled": _safe(lambda: bool(control.IsEnabled)),
            "IsOffscreen": _safe(lambda: bool(control.IsOffscreen)),
            "FrameworkId": _safe(lambda: control.FrameworkId),
            "ProcessId": _safe(lambda: control.ProcessId, 0),
            "Depth": depth,
        }
        records.append(record)
        if depth >= max_depth or len(records) >= max_controls:
            return
        for child in control.GetChildren():
            visit(child, depth + 1)
            if len(records) >= max_controls:
                break

    visit(root, 0)
    return records


def _pywinauto_scan(hwnd: int, backend: str, max_depth: int, max_controls: int) -> list[dict[str, Any]]:
    from pywinauto import Desktop

    root_wrapper = Desktop(backend=backend).window(handle=hwnd).wrapper_object()
    records: list[dict[str, Any]] = []

    def visit(wrapper: Any, depth: int) -> None:
        if len(records) >= max_controls:
            return
        info = getattr(wrapper, "element_info", wrapper)

        def prop(name: str, default: Any = "") -> Any:
            return _safe(lambda: getattr(info, name), default)

        rect = _safe(lambda: info.rectangle, "")
        if hasattr(rect, "left"):
            rect = [rect.left, rect.top, rect.right, rect.bottom]
        is_offscreen = _safe(lambda: info.is_offscreen) if backend == "uia" else ""
        records.append({
            "Name": prop("name"),
            "ControlType": prop("control_type"),
            "AutomationId": prop("automation_id"),
            "ClassName": prop("class_name"),
            "NativeWindowHandle": prop("handle", 0),
            "BoundingRectangle": rect,
            "IsEnabled": _safe(lambda: bool(wrapper.is_enabled())),
            "IsOffscreen": is_offscreen,
            "FrameworkId": prop("framework_id"),
            "ProcessId": prop("process_id", 0),
            "Depth": depth,
        })
        if depth >= max_depth or len(records) >= max_controls:
            return
        for child in wrapper.children():
            visit(child, depth + 1)
            if len(records) >= max_controls:
                break

    visit(root_wrapper, 0)
    return records


def _raw_view_scan(hwnd: int, max_depth: int, max_controls: int) -> list[dict[str, Any]]:
    import comtypes.client
    from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation

    automation = comtypes.client.CreateObject(CUIAutomation, interface=IUIAutomation)
    root = automation.ElementFromHandle(hwnd)
    if root is None:
        raise RuntimeError("UIA ElementFromHandle 未返回根元素")
    walker = automation.RawViewWalker
    records: list[dict[str, Any]] = []

    def next_element(method, element):
        try:
            return method(element)
        except Exception as exc:
            # comtypes may surface a null UIA element pointer as E_POINTER
            # instead of returning None. In walker navigation this means the
            # current node has no child / sibling.
            if getattr(exc, "hresult", None) == -2147467261:
                return None
            raise

    def visit(element: Any, depth: int) -> None:
        if element is None or len(records) >= max_controls:
            return
        rectangle = _safe(lambda: element.CurrentBoundingRectangle, None)
        if rectangle is not None and all(hasattr(rectangle, name) for name in ("left", "top", "right", "bottom")):
            rectangle = [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom]
        records.append({
            "Name": _safe(lambda: element.CurrentName),
            "ControlType": _safe(lambda: element.CurrentLocalizedControlType) or _safe(lambda: element.CurrentControlType),
            "AutomationId": _safe(lambda: element.CurrentAutomationId),
            "ClassName": _safe(lambda: element.CurrentClassName),
            "NativeWindowHandle": _safe(lambda: element.CurrentNativeWindowHandle, 0),
            "BoundingRectangle": rectangle,
            "IsEnabled": _safe(lambda: bool(element.CurrentIsEnabled)),
            "IsOffscreen": _safe(lambda: bool(element.CurrentIsOffscreen)),
            "FrameworkId": _safe(lambda: element.CurrentFrameworkId),
            "ProcessId": _safe(lambda: element.CurrentProcessId, 0),
            "Depth": depth,
        })
        if depth >= max_depth or len(records) >= max_controls:
            return
        child = next_element(walker.GetFirstChildElement, element)
        while child is not None and len(records) < max_controls:
            visit(child, depth + 1)
            child = next_element(walker.GetNextSiblingElement, child)

    visit(root, 0)
    return records


def scan_backend(key: str, hwnd: int, max_depth: int, max_controls: int) -> list[dict[str, Any]]:
    if key == "uiautomation":
        return _uiautomation_scan(hwnd, max_depth, max_controls)
    if key == "pywinauto-uia":
        return _pywinauto_scan(hwnd, "uia", max_depth, max_controls)
    if key == "pywinauto-win32":
        return _pywinauto_scan(hwnd, "win32", max_depth, max_controls)
    if key == "raw-view":
        return _raw_view_scan(hwnd, max_depth, max_controls)
    raise ValueError(f"未知 backend：{key}")


def _run_backend(key: str, label: str, filename: str, hwnd: int, max_depth: int,
                 max_controls: int, timeout_seconds: int, output_dir: Path) -> BackendResult:
    started = time.perf_counter()
    exchange_file = output_dir / f".{key}-{uuid.uuid4().hex}.json"
    command = [sys.executable, "-m", "autoim.backend_runner", key, str(hwnd), str(max_depth), str(max_controls), str(exchange_file)]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, creationflags=creation_flags, text=True)
        try:
            _, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            return BackendResult(key, label, filename, "超时", elapsed_ms=int((time.perf_counter() - started) * 1000),
                                 error=f"超过 {timeout_seconds} 秒，已终止 backend 子进程")
        if not exchange_file.exists():
            error = stderr.strip() if stderr else f"backend 子进程退出码 {process.returncode}"
            return BackendResult(key, label, filename, "失败", elapsed_ms=int((time.perf_counter() - started) * 1000), error=error)
        payload = json.loads(exchange_file.read_text(encoding="utf-8"))
        if not payload.get("success"):
            return BackendResult(key, label, filename, "失败", elapsed_ms=int((time.perf_counter() - started) * 1000),
                                 error=payload.get("error", "未知扫描错误"))
        records = payload["records"]
        return BackendResult(key, label, filename, "成功", len(records),
                             max((item["Depth"] for item in records), default=0),
                             int((time.perf_counter() - started) * 1000), records=records)
    except Exception as exc:
        return BackendResult(key, label, filename, "失败", elapsed_ms=int((time.perf_counter() - started) * 1000),
                             error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            exchange_file.unlink(missing_ok=True)
        except OSError:
            logger.warning("无法清理临时诊断文件：%s", exchange_file)


def _integrity_level(pid: int | None) -> tuple[str, int | None]:
    if os.name != "nt":
        return "仅支持 Windows", None
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    from ctypes import wintypes

    TOKEN_QUERY = 0x0008
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TokenIntegrityLevel = 25
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetSidSubAuthorityCount.argtypes = [wintypes.LPVOID]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(wintypes.BYTE)
    advapi32.GetSidSubAuthority.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    handle = kernel32.GetCurrentProcess() if pid is None else kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "无法打开目标进程（可能权限不足）", None
    token = ctypes.c_void_p()
    process_handle = handle
    try:
        if not advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
            return "无法打开进程 Token", None
        needed = ctypes.c_ulong()
        advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, TokenIntegrityLevel, buffer, needed.value, ctypes.byref(needed)):
            return "无法读取完整性级别", None
        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]

        class TOKEN_MANDATORY_LABEL(ctypes.Structure):
            _fields_ = [("Label", SID_AND_ATTRIBUTES)]

        label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        count = advapi32.GetSidSubAuthorityCount(label.Label.Sid)
        rid = advapi32.GetSidSubAuthority(label.Label.Sid, count.contents.value - 1).contents.value
        level = {0x1000: "低完整性", 0x2000: "中完整性", 0x3000: "高完整性", 0x4000: "系统完整性", 0x5000: "受保护"}.get(rid & 0xF000, f"未知(0x{rid:X})")
        return level, rid
    finally:
        if token:
            kernel32.CloseHandle(token)
        if pid is not None:
            kernel32.CloseHandle(process_handle)


def _render_record(record: dict[str, Any]) -> str:
    depth = record.get("Depth", 0)
    return "  " * depth + " | ".join(f"{field}={record.get(field, '')}" for field in FIELDS)


def _summary_text(client: dict[str, Any], results: list[BackendResult], integrity: dict[str, Any]) -> str:
    lines = [
        "AutoIM 企业微信 UI Automation 诊断报告", "", "企业微信：",
        f"PID: {client.get('pid') or ''}", f"HWND: 0x{client.get('hwnd'):X}" if client.get("hwnd") else "HWND: ",
        f"窗口标题: {client.get('title', '')}", f"ClassName: {client.get('class_name', '')}",
        "", "权限诊断：",
        f"AutoIM 完整性级别：{integrity.get('autoim', '未知')}",
        f"企业微信完整性级别：{integrity.get('wecom', '未知')}",
    ]
    if integrity.get("warning"):
        lines.append(f"权限提醒：{integrity['warning']}")
    lines += ["", "--------------------------------", ""]
    for result in results:
        lines += [result.label, f"状态：{result.status}"]
        if result.status == "成功":
            lines += [f"控件数量：{result.count}", f"最大实际深度：{result.max_depth}", f"耗时：{result.elapsed_ms} ms"]
            if result.count >= int(client.get("max_controls", 5000)):
                lines.append("提示：已达到控件数量上限")
        else:
            lines.append(f"原因：{result.error}")
        lines.append("")
    standard_results = [r for r in results if r.key in {"uiautomation", "pywinauto-uia", "raw-view"}]
    readable_descendants = [
        record for result in standard_results if result.status == "成功"
        for record in (result.records or []) if record.get("Depth", 0) > 0
        and any(record.get(field) not in (None, "", 0, False, []) for field in
                ("Name", "ControlType", "AutomationId", "ClassName", "NativeWindowHandle", "BoundingRectangle", "FrameworkId", "ProcessId"))
    ]
    if readable_descendants:
        lines += ["诊断结论：", "至少一种标准 UI Automation 接口发现了带可识别属性的根窗口后代元素，可继续检查该接口返回的结构。", ""]
    elif any(r.status == "成功" and r.count > 1 for r in standard_results):
        lines += ["诊断结论：", "UIA Raw View 遍历到根窗口之外的节点，但该节点没有可识别的 Name、ControlType、AutomationId、ClassName、句柄或进程等属性。", "uiautomation / pywinauto 未发现内部子控件；当前没有证据确认企业微信向标准 UIA 暴露了可用的内部控件结构。", "该空属性节点可能是 provider 的占位/无语义节点，不能据此判断消息界面是否自绘。", ""]
    elif standard_results and all(r.status != "成功" or r.count <= 1 for r in standard_results):
        lines += ["诊断结论：", "标准 UIA Tree / Raw View 未发现企业微信内部控件（或对应 backend 执行失败）。", "当前结果提示企业微信主体区域可能采用自绘界面，或该进程未向标准 UI Automation 暴露子控件。", "仅凭此结果无法区分自绘与 provider/UIA 访问限制。", ""]
    if any(r.key == "pywinauto-win32" and r.status == "成功" and r.count > 1 for r in results):
        lines += ["Win32 backend 发现原生窗口层级；这代表 HWND 子窗口，不等同于 UIA 内部语义控件。", ""]
    return "\n".join(lines).rstrip() + "\n"


def run_multi_backend_diagnostics(client: dict[str, Any], max_depth: int, max_controls: int,
                                  timeout_seconds: int, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    enriched_client = {**client, "max_controls": max_controls}
    auto_level, auto_rid = _integrity_level(None)
    wecom_level, wecom_rid = _integrity_level(client.get("pid"))
    integrity = {"autoim": auto_level, "wecom": wecom_level}
    if auto_rid is not None and wecom_rid is not None and (wecom_rid & 0xF000) > (auto_rid & 0xF000):
        integrity["warning"] = "企业微信完整性级别高于 AutoIM；权限差异可能影响 Windows UI Automation。未尝试提权。"
        logger.warning(integrity["warning"])

    results: list[BackendResult] = []
    for key, label, filename in BACKENDS:
        logger.info("开始 UIA backend 诊断：%s", label)
        result = _run_backend(key, label, filename, int(client["hwnd"]), max_depth, max_controls,
                              timeout_seconds, output_dir)
        results.append(result)
        content = [f"{label} | 状态：{result.status}", f"耗时：{result.elapsed_ms} ms"]
        if result.status == "成功":
            content += [f"控件数量：{result.count}", f"最大实际深度：{result.max_depth}", "", *(_render_record(r) for r in result.records or [])]
        else:
            content += [f"原因：{result.error}"]
        (output_dir / filename).write_text("\n".join(content) + "\n", encoding="utf-8")
        logger.info("UIA backend %s：%s，控件 %s，深度 %s，耗时 %s ms", label, result.status, result.count, result.max_depth, result.elapsed_ms)

    summary = _summary_text(enriched_client, results, integrity)
    (output_dir / "summary.txt").write_text(summary, encoding="utf-8")
    logger.info("UIA 多后端诊断完成，报告保存到 %s", output_dir)
    return {"results": [asdict(result) | {"records": None} for result in results], "summary": summary,
            "output_dir": str(output_dir), "integrity": integrity}
