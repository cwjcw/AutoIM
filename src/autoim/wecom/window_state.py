"""Passive, time-bracketed Windows state for screenshot and B2 navigation."""

from __future__ import annotations

import ctypes
import ntpath
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import datetime
from time import perf_counter

import psutil

from autoim.wecom.window_manager import (WeComWindowInfo, WeComWindowManager, WindowNotFoundError,
                                         _MAIN_WINDOW_CLASS, _MAIN_WINDOW_TITLE, _PROCESS_NAMES)


@dataclass(frozen=True, slots=True)
class WindowSample:
    exists: bool | None = None
    owner: tuple[int, int] | None = None
    class_name: str = ""
    title: str = ""
    visible: bool | None = None
    iconic: bool | None = None
    zoomed: bool | None = None
    placement: tuple | None = None
    rect: tuple[int, int, int, int] | None = None
    client_size: tuple[int, int] | None = None
    dpi: int | None = None
    foreground_hwnd: int | None = None
    error: str = ""

    @property
    def show_command(self) -> int | None:
        return self.placement[1] if self.placement and len(self.placement) > 1 else None


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    name: str
    path: str
    started: float


@dataclass(frozen=True, slots=True)
class WindowHealth:
    status: str
    reason: str
    checked_at: datetime
    elapsed_ms: float = 0
    window: WeComWindowInfo | None = None
    first: WindowSample | None = None
    last: WindowSample | None = None
    process: ProcessIdentity | None = None

    @property
    def foreground(self) -> bool | None:
        if not self.window or not self.last or self.last.foreground_hwnd is None:
            return None
        return self.last.foreground_hwnd == self.window.hwnd

    @property
    def minimized(self) -> bool | None:
        if self.status == "WINDOW_MINIMIZED":
            return True
        if self.status in ("WINDOW_VALID", "WINDOW_NOT_FOREGROUND"):
            return False
        return None

    @property
    def capture_blocker(self) -> str | None:
        if self.status not in ("WINDOW_VALID", "WINDOW_NOT_FOREGROUND"):
            return self.status
        return None if self.last and self.last.zoomed else "NOT_MAXIMIZED"

    @property
    def identity(self) -> tuple | None:
        if self.window and self.process:
            return (self.window.pid, self.window.hwnd, self.process.started,
                    ntpath.normcase(self.process.path or self.process.name))
        return None

    @property
    def geometry(self) -> tuple | None:
        return (self.last.rect, self.last.client_size, self.last.dpi) if self.last else None


def _native_sample(hwnd: int, gui, process) -> WindowSample:
    facts = {}
    try:
        facts["foreground_hwnd"] = int(gui.GetForegroundWindow())
        facts["exists"] = bool(gui.IsWindow(hwnd))
        if not facts["exists"]:
            return WindowSample(**facts)
        facts["owner"] = tuple(process.GetWindowThreadProcessId(hwnd))
        facts["class_name"] = gui.GetClassName(hwnd)
        facts["title"] = gui.GetWindowText(hwnd)
        facts["visible"] = bool(gui.IsWindowVisible(hwnd))
        facts["iconic"] = bool(gui.IsIconic(hwnd))
        facts["placement"] = tuple(gui.GetWindowPlacement(hwnd))
        facts["rect"] = tuple(gui.GetWindowRect(hwnd))
        left, top, right, bottom = gui.GetClientRect(hwnd)
        facts["client_size"] = (right - left, bottom - top)
        zoomed = ctypes.windll.user32.IsZoomed
        zoomed.argtypes = (wintypes.HWND,)
        zoomed.restype = wintypes.BOOL
        facts["zoomed"] = bool(zoomed(hwnd))
        dpi = ctypes.windll.user32.GetDpiForWindow
        dpi.argtypes = (wintypes.HWND,)
        dpi.restype = wintypes.UINT
        facts["dpi"] = dpi(hwnd)
    except Exception as exc:
        facts["error"] = f"{type(exc).__name__} · {exc}"
    return WindowSample(**facts)


def _process_identity(pid: int) -> ProcessIdentity:
    proc = psutil.Process(pid)
    with proc.oneshot():
        name = proc.name()
        started = proc.create_time()
        try:
            path = proc.exe()
        except psutil.AccessDenied:
            path = ""
    return ProcessIdentity(name, path, started)


def _parked_rectangle(sample: WindowSample) -> bool:
    if not sample.rect:
        return False
    x, y = sample.rect[:2]
    if x <= -32000 and y <= -32000:
        return True
    # Coordinate virtualization can scale the parked point. Corroborate a
    # far-offscreen origin with WINDOWPLACEMENT.ptMinPosition in that sample.
    return (x <= -8000 and y <= -8000 and sample.placement is not None
            and len(sample.placement) > 2 and sample.placement[2] == (x, y))


def probe_window(manager: WeComWindowManager | None = None, *, read=None, inspect_process=_process_identity) -> WindowHealth:
    """Resolve anew, bracket Win32 calls and distinguish ambiguity from closure."""
    manager = manager or WeComWindowManager()
    checked_at, started = datetime.now().astimezone(), perf_counter()
    window = first = last = process = None

    def result(status: str, reason: str) -> WindowHealth:
        return WindowHealth(status, reason, checked_at, (perf_counter() - started) * 1000,
                            window, first, last, process)

    try:
        window = manager.resolve_window(quiet=True)
    except WindowNotFoundError as exc:
        return result("WINDOW_CLOSED", str(exc))
    except Exception as exc:
        return result("WINDOW_STATE_UNCERTAIN", f"主窗口发现失败 · {exc}")
    try:
        if read is None:
            gui, win32process = manager._windows_api()
            read = lambda hwnd: _native_sample(hwnd, gui, win32process)
        first = read(window.hwnd)
        try:
            if first.exists:
                process = inspect_process(window.pid)
        finally:
            last = read(window.hwnd)
    except Exception as exc:
        return result("WINDOW_STATE_UNCERTAIN", f"采样或进程身份检查失败 · {exc}")
    if first.exists is False and last.exists is False:
        return result("WINDOW_CLOSED", "主窗口句柄在本次复核中已失效")
    if first.error or last.error:
        return result("WINDOW_STATE_UNCERTAIN", f"Win32 采样失败 · {first.error or last.error}")
    if (not first.exists or not last.exists or first.owner is None or last.owner is None
            or first.owner[1] != window.pid or last.owner[1] != window.pid):
        return result("WINDOW_STATE_UNCERTAIN", "采样期间 HWND 消失或窗口所有 PID 不符")
    if replace(first, foreground_hwnd=None) != replace(last, foreground_hwnd=None):
        return result("WINDOW_STATE_UNCERTAIN", "前后两次窗口身份、放置状态或几何采样不一致")
    try:
        if process is None or inspect_process(window.pid) != process:
            return result("WINDOW_STATE_UNCERTAIN", "两次 Win32 采样之间进程身份或启动时间变化")
    except Exception as exc:
        return result("WINDOW_STATE_UNCERTAIN", f"进程身份复核失败 · {exc}")
    if (not last.visible or last.class_name.casefold() != _MAIN_WINDOW_CLASS.casefold()
            or last.title != _MAIN_WINDOW_TITLE or process is None
            or process.name.casefold() not in _PROCESS_NAMES
            or (process.path and ntpath.basename(process.path).casefold() not in _PROCESS_NAMES)
            or (process.path and window.process_path
                and ntpath.normcase(process.path) != ntpath.normcase(window.process_path))):
        return result("WINDOW_STATE_UNCERTAIN", "主窗口可见性、类名、标题或可执行文件身份未通过复核")
    if window.rect != last.rect:
        return result("WINDOW_STATE_UNCERTAIN", "枚举矩形与本次 GetWindowRect 复核不一致")
    show = last.show_command
    if last.iconic is None or last.zoomed is None or show not in (1, 2, 3):
        return result("WINDOW_STATE_UNCERTAIN", "缺少有效的 IsIconic、IsZoomed 或 GetWindowPlacement")
    if last.iconic != (show == 2):
        return result("WINDOW_STATE_UNCERTAIN", "IsIconic 与 GetWindowPlacement.showCmd 不一致")
    if last.iconic:
        return result("WINDOW_MINIMIZED", "两次 IsIconic=True 且 showCmd=SW_SHOWMINIMIZED")
    if _parked_rectangle(last):
        return result("WINDOW_STATE_UNCERTAIN", "IsIconic=False 但 GetWindowRect 仍在最小化停放位置")
    if (last.zoomed != (show == 3) or not last.rect or last.rect[2] <= last.rect[0]
            or last.rect[3] <= last.rect[1] or not last.client_size or min(last.client_size) <= 0 or not last.dpi):
        return result("WINDOW_STATE_UNCERTAIN", "最大化、窗口几何或 DPI 证据不一致")
    if first.foreground_hwnd != window.hwnd or last.foreground_hwnd != window.hwnd:
        return result("WINDOW_NOT_FOREGROUND", "窗口状态已核对，采样期间未持续处于前台")
    return result("WINDOW_VALID", "窗口身份、放置状态和矩形一致，且处于前台")


def window_diagnostic(health: WindowHealth) -> str:
    def flag(value):
        return "未知" if value is None else str(bool(value))

    w, a, b = health.window, health.first, health.last
    base = f"{health.checked_at:%Y-%m-%d %H:%M:%S%z} · {health.elapsed_ms:.1f} ms · {health.status} · {health.reason}"
    if w is None:
        return base + "\n检查目标 PID — · HWND —"
    process = health.process
    exe = (ntpath.basename(process.path) or process.name) if process else "未知"
    line = (f"检查目标 PID {w.pid} · HWND 0x{w.hwnd:X} · "
            f"ClassName {b.class_name if b else w.class_name} · 可执行文件 {exe}")
    if b is None:
        return f"{base}\n{line}"
    first = (f"首轮 IsWindow {flag(a.exists)} · IsWindowVisible {flag(a.visible)} · IsIconic {flag(a.iconic)} · "
             f"IsZoomed {flag(a.zoomed)} · showCmd {a.show_command} · rect {a.rect} · "
             f"foreground HWND {a.foreground_hwnd} · owner {a.owner}") if a else "首轮 未取得"
    foreground_hwnd = f"0x{b.foreground_hwnd:X}" if b.foreground_hwnd is not None else "未知"
    return (f"{base}\n{line}\n"
            f"IsWindow {flag(b.exists)} · IsWindowVisible {flag(b.visible)} · IsIconic {flag(b.iconic)} · "
            f"IsZoomed {flag(b.zoomed)} · GetForegroundWindow {foreground_hwnd} · 前台 {flag(health.foreground)}\n"
            f"GetWindowRect {b.rect} · GetWindowPlacement {b.placement} · 客户区 {b.client_size} · DPI {b.dpi}\n"
            f"{first}；末轮 owner {b.owner}")
