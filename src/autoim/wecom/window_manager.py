from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)

_PROCESS_NAMES = {"wxwork.exe", "wecom.exe", "wxworklocal.exe"}
_MAIN_WINDOW_CLASS = "WeWorkWindow"
_MAIN_WINDOW_TITLE = "企业微信"


class WindowResolutionError(RuntimeError):
    """No unique, verified Enterprise WeChat main window is available."""


@dataclass(frozen=True, slots=True)
class WeComWindowInfo:
    pid: int
    hwnd: int
    title: str
    class_name: str
    process_path: str = ""
    rect: tuple[int, int, int, int] | None = None


class WeComWindowManager:
    """Resolve and validate WeCom's current top-level window on every action.

    HWND values are treated as transient. Discovery uses the executable name,
    owner PID and WeWorkWindow class; ambiguous matches fail closed.
    """

    def __init__(self, max_activation_attempts: int = 3, retry_delay: float = 0.15) -> None:
        if not 1 <= max_activation_attempts <= 3:
            raise ValueError("前台激活重试次数必须在 1 到 3 之间")
        self.max_activation_attempts = max_activation_attempts
        self.retry_delay = retry_delay

    @staticmethod
    def _windows_api():
        if os.name != "nt":
            raise OSError("企业微信窗口管理仅支持 Windows")
        import win32gui
        import win32process

        return win32gui, win32process

    def _processes(self) -> dict[int, str]:
        matches: dict[int, str] = {}
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                info = proc.info
                name = str(info.get("name") or "").casefold()
                if name in _PROCESS_NAMES:
                    matches[int(info["pid"])] = str(info.get("exe") or "")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return matches

    def _candidates(self) -> list[WeComWindowInfo]:
        win32gui, win32process = self._windows_api()
        processes = self._processes()
        if not processes:
            return []
        candidates: list[WeComWindowInfo] = []

        def visit(hwnd: int, _extra: object) -> bool:
            try:
                _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
                if int(pid) not in processes or not win32gui.IsWindow(hwnd):
                    return True
                # WeCom may spawn hidden helper processes which also own a
                # WeWorkWindow-class top-level HWND. Never choose those as the
                # user's visible main window.
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                class_name = win32gui.GetClassName(hwnd)
                if class_name.casefold() != _MAIN_WINDOW_CLASS.casefold():
                    return True
                title = win32gui.GetWindowText(hwnd)
                if title != _MAIN_WINDOW_TITLE:
                    return True
                rect = tuple(int(value) for value in win32gui.GetWindowRect(hwnd))
                candidates.append(WeComWindowInfo(
                    pid=int(pid), hwnd=int(hwnd), title=title,
                    class_name=class_name, process_path=processes[int(pid)], rect=rect,
                ))
            except Exception:
                logger.exception("枚举企业微信顶层窗口时读取候选信息失败")
            return True

        try:
            win32gui.EnumWindows(visit, None)
        except Exception:
            logger.exception("枚举企业微信顶层窗口失败")
            raise
        return candidates

    def resolve_window(self) -> WeComWindowInfo:
        """Find the current exact main-window candidate, never reuse a cached HWND."""
        candidates = self._candidates()
        if not candidates:
            error = "没有找到标题和窗口类均匹配的企业微信主窗口。"
            logger.warning(error)
            raise WindowResolutionError(error)
        if len(candidates) > 1:
            win32gui, _ = self._windows_api()
            foreground = int(win32gui.GetForegroundWindow())
            foreground_matches = [item for item in candidates if item.hwnd == foreground]
            if len(foreground_matches) == 1:
                return foreground_matches[0]
            error = f"发现 {len(candidates)} 个企业微信主窗口，且无法唯一确认目标；操作已取消。"
            logger.warning(error)
            raise WindowResolutionError(error)
        return candidates[0]

    def is_valid(self, hwnd: int | None = None) -> bool:
        """Verify a handle against a fresh process/window resolution."""
        try:
            current = self.resolve_window()
            if hwnd is not None and current.hwnd != int(hwnd):
                return False
            win32gui, win32process = self._windows_api()
            if not win32gui.IsWindow(current.hwnd):
                return False
            _thread_id, pid = win32process.GetWindowThreadProcessId(current.hwnd)
            return int(pid) == current.pid and win32gui.GetClassName(current.hwnd).casefold() == _MAIN_WINDOW_CLASS.casefold()
        except Exception:
            logger.exception("企业微信窗口有效性检查失败")
            return False

    def restore(self) -> WeComWindowInfo:
        """Resolve and restore the current window if it is minimized."""
        info = self.resolve_window()
        win32gui, _ = self._windows_api()
        if not win32gui.IsWindow(info.hwnd):
            raise WindowResolutionError("企业微信窗口在恢复前已失效。")
        if win32gui.IsIconic(info.hwnd):
            # SW_RESTORE is a documented, ordinary window operation.
            win32gui.ShowWindow(info.hwnd, 9)
            logger.info("已恢复最小化的企业微信窗口 HWND=0x%X", info.hwnd)
        return info

    def activate(self) -> WeComWindowInfo:
        """Restore, foreground and verify the current WeCom main window."""
        return self.ensure_foreground()

    def verify_foreground(self) -> WeComWindowInfo:
        """Passively verify focus without taking it back from another app."""
        info = self.resolve_window()
        win32gui, _ = self._windows_api()
        foreground = int(win32gui.GetForegroundWindow())
        if foreground != info.hwnd:
            error = f"企业微信已不在前台（实际 HWND=0x{foreground:X}，目标 HWND=0x{info.hwnd:X}）；操作已取消。"
            logger.error(error)
            raise WindowResolutionError(error)
        if not self.is_valid(info.hwnd):
            error = "企业微信窗口身份在前台验证期间发生变化；操作已取消。"
            logger.error(error)
            raise WindowResolutionError(error)
        return info

    def is_foreground(self, hwnd: int | None = None) -> bool:
        """Passively report whether the freshly resolved WeCom window is foreground."""
        try:
            info = self.resolve_window()
            if hwnd is not None and info.hwnd != int(hwnd):
                return False
            win32gui, _ = self._windows_api()
            return bool(win32gui.IsWindow(info.hwnd) and int(win32gui.GetForegroundWindow()) == info.hwnd)
        except Exception:
            logger.debug("无法被动确认企业微信前台状态", exc_info=True)
            return False

    def ensure_foreground(self) -> WeComWindowInfo:
        last_foreground = 0
        for attempt in range(1, self.max_activation_attempts + 1):
            try:
                info = self.restore()  # resolve again before every attempt
                win32gui, _ = self._windows_api()
                if not win32gui.IsWindow(info.hwnd):
                    raise WindowResolutionError("目标窗口句柄在激活前失效。")
                win32gui.SetForegroundWindow(info.hwnd)
                time.sleep(self.retry_delay)
                last_foreground = int(win32gui.GetForegroundWindow())
                if last_foreground == info.hwnd and self.is_valid(info.hwnd):
                    logger.info("企业微信前台校验成功 PID=%d HWND=0x%X", info.pid, info.hwnd)
                    return info
                logger.warning(
                    "企业微信前台校验失败（%d/%d），目标=0x%X 实际=0x%X",
                    attempt, self.max_activation_attempts, info.hwnd, last_foreground,
                )
            except Exception:
                logger.exception("企业微信窗口激活失败（%d/%d）", attempt, self.max_activation_attempts)
                if attempt == self.max_activation_attempts:
                    raise
            if attempt < self.max_activation_attempts:
                time.sleep(self.retry_delay)
        error = f"无法确认企业微信处于前台（实际 HWND=0x{last_foreground:X}）；操作已取消。"
        logger.error(error)
        raise WindowResolutionError(error)

    def get_rect(self) -> tuple[int, int, int, int]:
        """Return the rectangle from a freshly resolved and validated HWND."""
        info = self.resolve_window()
        win32gui, win32process = self._windows_api()
        if not win32gui.IsWindow(info.hwnd):
            raise WindowResolutionError("企业微信窗口句柄已失效。")
        _thread_id, pid = win32process.GetWindowThreadProcessId(info.hwnd)
        if int(pid) != info.pid:
            raise WindowResolutionError("窗口所有者已变化；操作已取消。")
        return tuple(int(value) for value in win32gui.GetWindowRect(info.hwnd))
