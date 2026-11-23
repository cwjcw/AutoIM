from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
from PySide6.QtCore import QObject, QRunnable, Signal

from autoim.wecom.window_state import WindowHealth, probe_window

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ClientInfo:
    connected: bool
    pid: int | None = None
    path: str = ""
    title: str = ""
    hwnd: int | None = None
    uia_accessible: bool = False
    detail: str = ""
    class_name: str = ""
    rect: tuple[int, int, int, int] | None = None
    is_foreground: bool = False
    window_health: WindowHealth | None = None


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal(object)


class FunctionWorker(QRunnable):
    """Execute a callable away from the GUI thread and report its result."""

    def __init__(self, function: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            self.signals.result.emit(self.function(*self.args, **self.kwargs))
        except Exception as exc:  # deliver failures instead of killing a worker
            logger.exception("后台任务失败")
            self.signals.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.signals.finished.emit(self)


def detect_wecom() -> ClientInfo:
    """Find a running WeCom process and probe its visible window with UIA."""
    info = refresh_wecom_status()
    if not info.hwnd or info.window_health.status not in ("WINDOW_VALID", "WINDOW_NOT_FOREGROUND"):
        return info
    try:
        import uiautomation as auto

        control = auto.ControlFromHandle(info.hwnd)
        info.uia_accessible = bool(control and control.Exists(maxSearchSeconds=1))
        if not info.uia_accessible:
            info.detail += " · 窗口存在，但 UI Automation 未返回可访问控件。"
    except Exception as exc:
        info.detail += f" · UI Automation 不可访问：{exc}"
    return info


def refresh_wecom_status() -> ClientInfo:
    """Refresh window identity and foreground status without activating WeCom."""
    if os.name != "nt":
        return ClientInfo(False, detail="企业微信检测仅支持 Windows。")
    health = probe_window()
    window = health.window
    detail = f"{health.status} · {health.reason}"
    if window:
        return ClientInfo(True, window.pid, window.process_path, window.title, window.hwnd,
                          False, detail, window.class_name, health.last.rect if health.last else None,
                          health.foreground is True, health)
    process_names = {"wxwork.exe", "wecom.exe", "wxworklocal.exe"}
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if str(proc.info.get("name") or "").casefold() in process_names:
                return ClientInfo(True, int(proc.info["pid"]), str(proc.info.get("exe") or ""),
                                  detail=detail, window_health=health)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return ClientInfo(False, detail=detail, window_health=health)


def scan_uia_tree(hwnd: int, max_depth: int, output_path: Path) -> dict[str, Any]:
    """Walk a window's UIA descendants and save the formatted tree to disk."""
    if platform.system() != "Windows":
        raise RuntimeError("UI Automation 扫描仅支持 Windows。")
    import uiautomation as auto

    root = auto.ControlFromHandle(hwnd)
    if not root:
        raise RuntimeError("无法从窗口句柄创建 UIA 控件。")

    lines = [f"AutoIM 企业微信 UI Automation 树 | HWND=0x{hwnd:X} | 最大深度={max_depth}"]
    count = 0

    def read(control: Any, depth: int) -> None:
        nonlocal count
        indent = "  " * depth
        try:
            name = str(control.Name or "").replace("\r", " ").replace("\n", " ")
        except Exception:
            name = "<读取失败>"
        try:
            control_type = str(control.ControlTypeName or "")
        except Exception:
            control_type = "<读取失败>"
        try:
            automation_id = str(control.AutomationId or "")
        except Exception:
            automation_id = "<读取失败>"
        try:
            class_name = str(control.ClassName or "")
        except Exception:
            class_name = "<读取失败>"
        try:
            rectangle = str(control.BoundingRectangle)
        except Exception:
            rectangle = "<读取失败>"
        lines.append(
            f"{indent}Name={name!r} | ControlType={control_type} | AutomationId={automation_id!r} "
            f"| ClassName={class_name!r} | BoundingRectangle={rectangle}"
        )
        count += 1
        if depth >= max_depth:
            return
        try:
            children = control.GetChildren()
        except Exception as exc:
            lines.append(f"{indent}  <子控件读取失败：{exc}>")
            return
        for child in children:
            read(child, depth + 1)

    read(root, 0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("UIA 扫描完成：%s 个控件，保存到 %s", count, output_path)
    return {"count": count, "text": "\n".join(lines), "path": str(output_path)}
