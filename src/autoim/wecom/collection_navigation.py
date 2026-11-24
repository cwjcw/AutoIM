"""Narrow Windows input capability for the visible B2 contact list."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from datetime import datetime
from threading import Lock

import numpy as np

from autoim.wecom.visual_capture import VisualFrame, capture_visible_window
from autoim.wecom.visual_contacts import ContactObservation
from autoim.wecom.visual_layout import Box, VisualThresholds, detect_layout
from autoim.wecom.visual_observation import VisualObservation
from autoim.wecom.window_manager import WeComWindowManager
from autoim.wecom.window_state import WindowHealth, probe_window


class NavigationPaused(RuntimeError):
    """The next native action was refused before any input was issued."""

    def __init__(self, message: str, *, health: WindowHealth | None = None) -> None:
        super().__init__(message)
        self.health = health


class CollectionPermit:
    def __init__(self) -> None:
        self._lock = Lock()
        self._handle = None
        self._scroll = False

    def grant(self, *, scroll: bool = False) -> None:
        if os.name != "nt":
            raise NavigationPaused("自动采集仅支持 Windows")
        import win32api
        import win32event
        with self._lock:
            if self._handle is not None:
                raise NavigationPaused("自动采集许可已开启")
            ctypes.set_last_error(0)
            handle = win32event.CreateMutex(None, False, r"Local\AutoIM.WeComVisualCollection")
            if win32api.GetLastError() == 183:
                handle.Close()
                raise NavigationPaused("已有 AutoIM 实例获得企业微信自动采集许可")
            self._handle, self._scroll = handle, scroll

    def revoke(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.Close()
            self._handle, self._scroll = None, False

    def perform(self, action, *, scroll: bool) -> None:
        with self._lock:
            if self._handle is None or (scroll and not self._scroll):
                raise NavigationPaused("本轮 B2 自动操作许可未开启或已撤销")
            action()


def _default_desktop() -> bool:
    import win32service

    try:
        desk = win32service.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
    except OSError:
        return False
    try:
        read_name = ctypes.windll.user32.GetUserObjectInformationW
        read_name.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD))
        read_name.restype = wintypes.BOOL
        name = ctypes.create_unicode_buffer(128)
        needed = wintypes.DWORD()
        return bool(read_name(int(desk), 2, name, ctypes.sizeof(name), ctypes.byref(needed))
                    and name.value.casefold() == "default")
    finally:
        desk.CloseDesktop()


def _input_idle() -> tuple[int, int]:
    import win32api

    last = win32api.GetLastInputInfo()
    idle = (win32api.GetTickCount() - last) & 0xFFFFFFFF
    if idle > 0x7FFFFFFF or any(win32api.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18, 91, 92)):
        return last, 0
    return last, idle


def row_target(observation: VisualObservation, row: ContactObservation) -> tuple[int, int]:
    """Only an independently recognized and fully visible B2 row is targetable."""
    area = observation.layout.get("B2")
    if (observation.page != "MESSAGES_CANDIDATE" or area is None or row not in observation.contacts
            or row.status != "CANDIDATE" or not row.name_reliable or row.is_selected is not False
            or sum(contact.is_selected is True for contact in observation.contacts) != 1
            or not row.has_unread_indicator or row.badge is None):
        raise NavigationPaused("B2 未读联系人行缺少可靠的可操作边界")
    box = row.box
    x = box.left + round(box.width * .53)
    y = box.top + round(box.height * .53)
    unit = observation.frame.state.dpi / 96
    margin = max(4, round(4 * unit))
    if (not (area.top <= box.top < box.bottom <= area.bottom and area.top + margin <= y < area.bottom - margin and
             area.left <= box.left < box.right <= area.right and box.height >= 22 * unit)
            or not area.contains((x, y)) or row.badge.contains((x, y))
            or (row.avatar and row.avatar.contains((x, y)))):
        raise NavigationPaused("点击点过于靠近视口、头像或未读控件")
    return x, y


def _identity(frame: VisualFrame) -> tuple:
    c, s = frame.capture, frame.state
    health = frame.diagnostics.window_health
    return c.pid, c.hwnd, c.rect, c.width, c.height, s.width, s.height, s.dpi, health.identity if health else None


class CollectionNavigation:
    def __init__(self, permit: CollectionPermit, *, manager: WeComWindowManager | None = None,
                 capture=None, idle=_input_idle, desktop=_default_desktop, window_probe=None) -> None:
        self.permit = permit
        self.manager = manager or WeComWindowManager()
        self.capture = capture or (lambda: capture_visible_window(self.manager))
        self._probe = window_probe or (lambda: probe_window(self.manager))
        self._idle = idle
        self._desktop = desktop

    def verify_view(self, observation: VisualObservation, *, need_idle: bool = False):
        """Verify the existing foreground window without activating it."""
        import win32gui
        import win32process

        frame = observation.frame
        c, s = frame.capture, frame.state
        if not self._desktop():
            raise NavigationPaused("当前 Windows 输入桌面不可操作")
        activity, milliseconds = self._idle()
        if need_idle and milliseconds < 1800:
            raise NavigationPaused("检测到近期鼠标或键盘操作，等待用户空闲")
        health = self._probe()
        if health.capture_blocker or health.status != "WINDOW_VALID":
            code = health.capture_blocker or health.status
            raise NavigationPaused(f"{code} · {health.reason}，请手动切回已最大化的企业微信", health=health)
        captured_health = frame.diagnostics.window_health
        if ((health.window.pid, health.window.hwnd) != (c.pid, c.hwnd)
                or health.geometry != (c.rect, (s.width, s.height), s.dpi)
                or (captured_health and captured_health.identity != health.identity)):
            raise NavigationPaused("WINDOW_CHANGED · 窗口身份或坐标系变化，需重新采集", health=health)
        cw, ch = health.last.client_size
        try:
            if not win32gui.IsWindowEnabled(c.hwnd):
                raise NavigationPaused("窗口不可操作，可能存在弹窗", health=health)
            top_left = win32gui.ClientToScreen(c.hwnd, (0, 0))
        except win32gui.error as exc:
            raise NavigationPaused("WINDOW_CHANGED · 读取实时客户区失败", health=health) from exc

        def screen(point: tuple[int, int]) -> tuple[int, int]:
            x, y = point
            if not (0 <= x < c.width and 0 <= y < c.height):
                raise NavigationPaused("视觉位置超出原图")
            cx, cy = round((x + .5) * cw / c.width - .5), round((y + .5) * ch / c.height - .5)
            if not (0 <= cx < cw and 0 <= cy < ch):
                raise NavigationPaused("映射点超出实时客户区")
            return top_left[0] + cx, top_left[1] + cy

        def unobscured(point: tuple[int, int]) -> tuple[int, int]:
            xy = screen(point)
            hit = win32gui.WindowFromPoint(xy)
            if not hit or win32gui.GetAncestor(hit, 2) != c.hwnd:
                raise NavigationPaused("检测到企业微信窗口遮挡或弹窗")
            return xy

        def check_popup(hwnd, _):
            if hwnd == c.hwnd or not win32gui.IsWindowVisible(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == c.pid:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                if left < top_left[0] + cw and right > top_left[0] and top < top_left[1] + ch and bottom > top_left[1]:
                    raise NavigationPaused("企业微信存在覆盖客户区的独立窗口")
            return True

        try:
            win32gui.EnumWindows(check_popup, None)
        except Exception as exc:
            raise NavigationPaused("存在企业微信弹窗，或无法排除窗口遮挡") from exc
        for name in ("B1", "B2", "C1", "C2"):
            box = observation.layout.get(name)
            if box is None:
                raise NavigationPaused("缺少消息页的必要分区")
            unobscured(box.center)
        return unobscured, activity

    def select(self, observation: VisualObservation, row: ContactObservation) -> None:
        self._act(observation, row_target(observation, row), row.box, scroll=False)

    def scroll_visible_contacts(self, observation: VisualObservation) -> None:
        area = observation.layout.get("B2")
        if observation.page != "MESSAGES_CANDIDATE" or area is None or len(observation.contacts) < 3:
            raise NavigationPaused("B2 页位置无法确认，停止滚动")
        point = (area.left + round(area.width * .52), area.top + round(area.height * .62))
        self._act(observation, point, area, scroll=True)

    def _act(self, observation: VisualObservation, point: tuple[int, int], target: Box, *, scroll: bool) -> None:
        area = observation.layout.get("B2")
        if area is None or not area.contains(point) or not target.contains(point):
            raise NavigationPaused("操作点未通过 B2 范围检查")
        frame = observation.frame
        age = (datetime.now(frame.captured_at.tzinfo) - frame.captured_at).total_seconds()
        if not 0 <= age <= 30:
            raise NavigationPaused("联系人 OCR 结果已过期")
        _, activity = self.verify_view(observation, need_idle=True)
        fresh = self.capture()
        if _identity(frame) != _identity(fresh):
            raise NavigationPaused("操作前窗口或坐标系已变化")
        health = fresh.diagnostics.window_health
        if health and health.status != "WINDOW_VALID":
            raise NavigationPaused(f"{health.status} · 操作前复查截图时窗口前台未确认", health=health)
        if observation.login.name and not observation.login.unchanged(fresh.pixels):
            raise NavigationPaused("操作前 A1 登录账号区域已变化")
        automatic_b2 = detect_layout(fresh.pixels, {}, VisualThresholds()).get("B2")
        if automatic_b2 is None or not automatic_b2.contains(point):
            raise NavigationPaused("自动分区无法独立确认该位置属于 B2")
        for name in ("B1", "B2", "C1"):
            region = observation.layout.get(name)
            if region is None or not np.array_equal(region.crop(frame.pixels), region.crop(fresh.pixels)):
                raise NavigationPaused("操作前联系人列表或标题已变化，重新定位")

        def send() -> None:
            import win32api
            import win32con
            import win32gui
            map_point, current_activity = self.verify_view(observation, need_idle=True)
            xy = map_point(point)
            if current_activity != activity or self._idle()[0] != activity:
                raise NavigationPaused("复核期间检测到新的用户操作")
            win32api.SetCursorPos(xy)
            if win32gui.GetCursorPos() != xy or win32gui.GetForegroundWindow() != frame.capture.hwnd:
                raise NavigationPaused("发送鼠标动作前，指针或前台窗口已变化")
            if scroll:
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -120, 0)
            else:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        self.permit.perform(send, scroll=scroll)
