"""Scoped Windows mouse actions for B2 and the signed-in profile."""

from __future__ import annotations

import ctypes
from concurrent.futures import CancelledError
import os
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from threading import Event, Lock

import numpy as np

from autoim.wecom.collection_runtime import safe_reason
from autoim.wecom.collection_profile import PopupEvidence, ProfileWindows, owner_chain
from autoim.wecom.collection_profile import ProfileAction, ProfileSurface, changed_regions
from autoim.wecom.visual_capture import (CaptureError, VisualFrame, _coverage, _render_client, _render_complete_client, capture_visible_window)
from autoim.wecom.visual_contacts import ContactObservation
from autoim.wecom.visual_layout import Box, VisualThresholds, detect_layout
from autoim.wecom.visual_observation import VisualObservation
from autoim.wecom.window_manager import WeComWindowManager
from autoim.wecom.window_state import WindowHealth, probe_window


class NavigationPaused(RuntimeError):
    """The next native action was refused before any input was issued."""

    def __init__(self, message: str, *, health: WindowHealth | None = None, local: bool = False) -> None:
        super().__init__(message)
        self.health = health
        self.local = local


class CollectionPermit:
    def __init__(self) -> None:
        self._lock = Lock()
        self._handle = None
        self._scroll = False
        self._revoked = Event()

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
            self._revoked.clear()

    def revoke(self) -> None:
        self._revoked.set()
        with self._lock:
            if self._handle is not None:
                self._handle.Close()
            self._handle, self._scroll = None, False

    def ensure_current(self):
        if self._revoked.is_set() or self._handle is None:
            raise NavigationPaused("自动操作许可已撤销")

    def perform(self, action, *, scroll: bool) -> None:
        with self._lock:
            self.ensure_current()
            if scroll and not self._scroll:
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


IDLE_THRESHOLD_MS = 1800
_WATCHED_KEYS = {1: "鼠标左键", 2: "鼠标右键", 4: "鼠标中键", 5: "鼠标侧键 1", 6: "鼠标侧键 2",
                 16: "Shift", 17: "Ctrl", 18: "Alt", 91: "左 Win", 92: "右 Win"}


@dataclass(frozen=True, slots=True)
class InputState:
    last_tick: int
    milliseconds: int
    held: tuple[str, ...] = ()


def _input_state() -> InputState:
    import win32api

    last = win32api.GetLastInputInfo() & 0xFFFFFFFF
    idle = (win32api.GetTickCount() - last) & 0xFFFFFFFF
    return InputState(last, idle if idle <= 0x7FFFFFFF else -1,
                      tuple(name for key, name in _WATCHED_KEYS.items()
                            if win32api.GetAsyncKeyState(key) & 0x8000))


def _input_idle() -> tuple[int, int]:
    sample = _input_state()
    return sample.last_tick, 0 if sample.held else max(0, sample.milliseconds)


def _window_facts(hwnd, gui, process) -> str:
    if not hwnd:
        return "HWND 无"
    cloaked = wintypes.DWORD()
    dwm = ctypes.windll.dwmapi.DwmGetWindowAttribute
    dwm.argtypes = (wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD)
    result = dwm(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return (f"HWND 0x{hwnd:X} · PID {process.GetWindowThreadProcessId(hwnd)[1]}"
            f" · Class {gui.GetClassName(hwnd)} · Rect {gui.GetWindowRect(hwnd)}"
            f" · 可见 {bool(gui.IsWindowVisible(hwnd))} · DWM cloaked {cloaked.value if result == 0 else '未知'}"
            f" · Owner 0x{gui.GetWindow(hwnd, 4):X} · Parent 0x{gui.GetParent(hwnd):X}"
            f" · Root 0x{gui.GetAncestor(hwnd, 2):X} · RootOwner 0x{gui.GetAncestor(hwnd, 3):X}")


def _occlusion_detail(hwnd, root, xy, gui, process) -> str:
    facts = []
    try:
        facts.append(_window_facts(hwnd, gui, process))
    except Exception as exc:
        facts.append(f"读取命中窗口属性失败 {type(exc).__name__}: {exc}")
    count = 0

    def inspect(window, _):
        nonlocal count
        count += 1
        return window != root

    try:
        gui.EnumWindows(inspect, None)
        facts.append(f"枚举至主窗检查 {count} 项")
    except Exception as exc:
        # pywin32 can report the deliberate callback stop as error zero.
        if getattr(exc, "winerror", None) != 0:
            facts.append(f"EnumWindows 失败 {type(exc).__name__}: {exc}")
    return f"目标屏幕坐标 {xy} · WindowFromPoint 0x{hwnd:X} · " + " · ".join(facts)


def row_target(observation: VisualObservation, row: ContactObservation) -> tuple[int, int]:
    """Only an independently recognized and fully visible B2 row is targetable."""
    area = observation.layout.get("B2")
    if (observation.page != "MESSAGES_CANDIDATE" or area is None or row not in observation.contacts
            or row.status != "CANDIDATE" or not row.name_reliable or row.is_selected is True
            or row.kind not in ("CONTACT", "GROUP_CANDIDATE")
            or sum(r.name_reliable and r.display_name.casefold() == row.display_name.casefold()
                   for r in observation.contacts) != 1
            or not row.has_unread_indicator or row.badge is None):
        raise NavigationPaused("B2 未读联系人行缺少可靠的可操作边界", local=True)
    box = row.box
    x = box.left + round(box.width * .53)
    y = box.top + round(box.height * .53)
    unit = observation.frame.state.dpi / 96
    margin = max(4, round(4 * unit))
    if (not (area.top <= box.top < box.bottom <= area.bottom and area.top + margin <= y < area.bottom - margin and
             area.left <= box.left < box.right <= area.right and box.height >= 22 * unit)
            or not area.contains((x, y)) or row.badge.contains((x, y))
            or (row.avatar and row.avatar.contains((x, y)))):
        raise NavigationPaused("点击点过于靠近视口、头像或未读控件", local=True)
    return x, y


def _identity(frame: VisualFrame) -> tuple:
    c, s = frame.capture, frame.state
    health = frame.diagnostics.window_health
    return c.pid, c.hwnd, c.rect, c.width, c.height, s.width, s.height, s.dpi, health.identity if health else None


class CollectionNavigation:
    def __init__(self, permit: CollectionPermit, *, manager: WeComWindowManager | None = None,
                 capture=None, idle=None, desktop=_default_desktop, window_probe=None) -> None:
        self.permit = permit
        self.manager = manager or WeComWindowManager()
        self.capture = capture or (lambda: capture_visible_window(self.manager))
        self._probe = window_probe or (lambda: probe_window(self.manager))
        self._idle = idle or _input_idle
        self._read_input = _input_state if idle is None else lambda: InputState(*idle())
        self._desktop = desktop
        self._own_input = None
        self._started = float("-inf")
        self.input_diagnostic = ""
        self.report = lambda *args, **kwargs: None

    def prepare_window(self, cancel):
        def check():
            if cancel.is_set():
                raise CancelledError()
            self.permit.ensure_current()
            if not self._desktop():
                raise NavigationPaused("Windows 输入桌面不可操作")

        def wait():
            cancel.wait(.15)
            check()

        check()
        return self.manager.prepare_collection_window(
            self._probe, self.permit.perform, check, wait,
            report=lambda code, result, details: self.report("WINDOW", code, result=result, details=details))

    def mark_started(self):
        self._started = monotonic()

    def _activity(self, need_idle):
        try:
            sample = self._read_input()
        except OSError as exc:
            raise NavigationPaused(f"GetLastInputInfo/GetAsyncKeyState 检查失败 · {exc}") from exc
        now = monotonic()
        if need_idle:
            self.input_diagnostic = (
                f"空闲 {sample.milliseconds} ms · 最近输入 tick {sample.last_tick}"
                f" · 阈值 {IDLE_THRESHOLD_MS} ms · 按住 {', '.join(sample.held) or '无'}"
                f" · 可能来自 AutoIM {self._own_input == sample.last_tick}"
                f" · 启动等待 {(now - self._started) * 1000 < IDLE_THRESHOLD_MS}")
            self.report("WINDOW", "INPUT_IDLE_SAMPLE", details={"idle_ms": sample.milliseconds,
                        "last_input_tick": sample.last_tick, "held": sample.held,
                        "possible_own_input": self._own_input == sample.last_tick})
            if sample.held or sample.milliseconds < IDLE_THRESHOLD_MS:
                raise NavigationPaused("等待用户空闲 · " + self.input_diagnostic)
        return sample.last_tick

    def verify_view(self, observation: VisualObservation, *, need_idle: bool = False):
        """Verify the existing foreground window without activating it."""
        import win32gui
        import win32process

        frame = observation.frame
        c, s = frame.capture, frame.state
        if not self._desktop():
            raise NavigationPaused("当前 Windows 输入桌面不可操作")
        activity = self._activity(need_idle)
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
                raise NavigationPaused(f"IsWindowEnabled 为 False · 主窗口 HWND 0x{c.hwnd:X}", health=health)
            top_left = win32gui.ClientToScreen(c.hwnd, (0, 0))
        except win32gui.error as exc:
            raise NavigationPaused(f"WINDOW_CHANGED · ClientToScreen/IsWindowEnabled 失败 · {exc}", health=health) from exc

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
            try:
                hit = win32gui.WindowFromPoint(xy)
                root = win32gui.GetAncestor(hit, 2) if hit else 0
            except OSError as exc:
                raise NavigationPaused(f"WindowFromPoint/GetAncestor 失败 · 坐标 {xy} · {exc}") from exc
            if root != c.hwnd:
                raise NavigationPaused(_occlusion_detail(hit, c.hwnd, xy, win32gui, win32process), local=True)
            return xy

        return unobscured, activity

    def _profile_inventory(self, health):
        import win32gui as gui
        import win32process as process

        found = {}
        root, pid = health.window.hwnd, health.window.pid
        origin = gui.ClientToScreen(root, (0, 0))
        client_width, client_height = health.last.client_size
        reached_root = False

        def inspect(hwnd, _):
            nonlocal reached_root
            if hwnd == root:
                reached_root = True
                return True
            try:
                thread, current_pid = process.GetWindowThreadProcessId(hwnd)
                if current_pid != pid:
                    return True
                visible = gui.IsWindowVisible(hwnd) and not gui.IsIconic(hwnd)
                owner, parent = gui.GetWindow(hwnd, 4), gui.GetParent(hwnd)
                top, root_owner = gui.GetAncestor(hwnd, 2), gui.GetAncestor(hwnd, 3)
                chain, valid = owner_chain(
                    hwnd, lambda window: gui.GetWindow(window, 4) or gui.GetParent(window),
                    lambda window: process.GetWindowThreadProcessId(window)[1] if gui.IsWindow(window) else None,
                    pid)
                if root_owner not in (hwnd, *chain):
                    valid = False
                class_name = gui.GetClassName(hwnd)
                _, _, width, height = gui.GetClientRect(hwnd)
                popup_origin = gui.ClientToScreen(hwnd, (0, 0))
                x, y = popup_origin
                same_client = (origin[0] <= x < x + width <= origin[0] + client_width
                               and origin[1] <= y < y + height <= origin[1] + client_height)
                visible_popup = root_owner == hwnd and not reached_root and same_client
                cloaked = wintypes.DWORD()
                dwm = ctypes.windll.dwmapi.DwmGetWindowAttribute
                dwm.argtypes = (wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD)
                result = dwm(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
                unit = health.last.dpi / 96
                reason = ("VISIBILITY" if not visible else "DWM" if result != 0 or cloaked.value else
                          "CLIENT_DIMENSIONS" if not (80 * unit <= width <= 700 * unit
                                                      and 55 * unit <= height <= 850 * unit) else "")
                found[hwnd] = (PopupEvidence(hwnd, pid, thread, class_name, owner, parent, top, root_owner, chain,
                                             (popup_origin, width, height), valid), visible_popup, reason)
            except OSError as exc:
                self.report("MENU", "POPUP_PROBE_FAILED",
                            details={"hwnd": hwnd, "reason": type(exc).__name__})
            return True

        try:
            gui.EnumWindows(inspect, None)
        except Exception as exc:
            raise NavigationPaused(f"核验资料界面时 EnumWindows 失败 · {type(exc).__name__}: {exc}") from exc
        return found

    def _owned_profile_windows(self, health, context=None):
        from dataclasses import asdict
        inventory = self._profile_inventory(health)
        root = health.window.hwnd
        accepted = {}
        if isinstance(context, ProfileWindows) and context.menu:
            current = inventory.get(context.menu.hwnd)
            if current and current[0].identity != context.menu.identity:
                context.retired.add(context.menu.hwnd)
        for hwnd, (popup, detached, reason) in inventory.items():
            if reason == "VISIBILITY":
                continue
            if isinstance(context, ProfileWindows):
                relationship = context.relation(popup, detached=detached) if context.lifetime == health.identity else None
                correlation = {"profile_session": context.id, "upstream_click": context.card_click or context.menu_click,
                               "step": "CARD" if context.card_click else "MENU", "new": hwnd not in context.baseline,
                               "validated": relationship in {"VERIFIED_MENU", "VERIFIED_CARD"}}
            else:
                relationship = "MAIN_DESCENDANT" if popup.root_owner == root else "DETACHED" if detached else None
                correlation = {}
            details = {**asdict(popup), **correlation, "visible": True, "dpi": health.last.dpi}
            if not relationship or reason:
                self.report("MENU", "POPUP_EXCLUDED",
                            details={**details, "reason": reason or "PROFILE_RELATION_OR_STAGE"})
                continue
            accepted[hwnd] = popup
            self.report("MENU", "POPUP_ENUMERATED", details={**details, "relation": relationship})
            if isinstance(context, ProfileWindows) and context.card_click and relationship not in {"VERIFIED_MENU", "VERIFIED_CARD"}:
                self.report("CARD", "CARD_WINDOW_ADMITTED", result="AUTO_PASS", details={**details, "relation": relationship})
        if isinstance(context, ProfileWindows):
            # Retire truly missing handles, not baseline or temporarily noncandidate windows.
            context.observe(accepted, present=inventory)
        return tuple((popup.hwnd, popup.geometry) for popup in accepted.values())

    def confirm_profile_window(self, reference, surface, context, *, stage):
        if not isinstance(context, ProfileWindows):
            return
        health = self._profile_health(reference)
        if not context.valid or context.lifetime != health.identity:
            raise NavigationPaused("身份核验的窗口上下文已失效")
        if surface.embedded:
            if stage == "MENU":
                context.menu_embedded = surface.bounds
                context.before_card = frozenset(hwnd for hwnd, entry in self._profile_inventory(health).items()
                                               if entry[2] != "VISIBILITY")
            else:
                context.card_embedded = surface.bounds
            self.report(stage, "PROFILE_VISUALLY_VERIFIED",
                        details={"profile_session": context.id, "hwnd": surface.hwnd,
                                 "embedded": True, "upstream_click": context.card_click or context.menu_click})
            return
        inventory = self._profile_inventory(health)
        entry = inventory.get(surface.hwnd)
        if (entry is None or entry[0].geometry != surface.geometry or entry[2]
                or context.relation(entry[0], detached=entry[1]) is None):
            raise NavigationPaused("资料窗口的句柄、关系或位置已变化")
        if stage == "MENU":
            context.menu = entry[0]
            context.before_card = frozenset(hwnd for hwnd, entry in inventory.items() if entry[2] != "VISIBILITY")
        else:
            context.card = entry[0]
        self.report(stage, "PROFILE_VISUALLY_VERIFIED",
                    details={"profile_session": context.id, "hwnd": surface.hwnd,
                             "upstream_click": context.card_click or context.menu_click})

    def _profile_health(self, reference):
        if not self._desktop():
            raise NavigationPaused("Windows 输入桌面不可操作，暂停账号核验")
        health = self._probe()
        original = reference.diagnostics.window_health
        if (original is None or original.identity is None or health.identity != original.identity
                or health.geometry != original.geometry or health.capture_blocker):
            raise NavigationPaused("账号核验期间窗口身份、尺寸或 DPI 变化", health=health)
        return health

    def _profile_foreground(self, health, owned, known_windows):
        import win32gui as gui
        import win32process as process
        foreground = gui.GetForegroundWindow()
        context = known_windows if isinstance(known_windows, ProfileWindows) else None
        baseline = context.baseline if context else known_windows
        facts = {"hwnd": foreground, "pid": None, "class_name": "", "owner": 0, "parent": 0,
                 "root": 0, "root_owner": 0}
        if foreground and gui.IsWindow(foreground):
            try:
                facts.update(pid=process.GetWindowThreadProcessId(foreground)[1], class_name=gui.GetClassName(foreground),
                             owner=gui.GetWindow(foreground, 4), parent=gui.GetParent(foreground),
                             root=gui.GetAncestor(foreground, 2), root_owner=gui.GetAncestor(foreground, 3))
            except OSError:
                pass
        verified = {item.hwnd: label for item, label in
                    ((context.menu, "VERIFIED_MENU"), (context.card, "VERIFIED_CARD"))
                    if item and item.hwnd in dict(owned)} if context else {}
        allowed = foreground == health.window.hwnd or (foreground not in baseline and foreground in dict(owned))
        relationship = ("MAIN" if foreground == health.window.hwnd else verified.get(foreground, "PENDING_PROFILE")
                        if allowed else "OTHER_APP" if facts["pid"] not in (None, health.window.pid) else
                        "OTHER_WECOM" if facts["pid"] else "UNKNOWN")
        self.report("WINDOW", "PROFILE_FOREGROUND",
                    details={**facts, "relation": relationship, "allowed": allowed,
                             "verified": list(verified), "candidates": [hwnd for hwnd, _ in owned if hwnd not in baseline],
                             "profile_session": context.id if context else "",
                             "phase": "CARD" if context and context.card_click else "MENU"})
        if not allowed:
            raise NavigationPaused(f"账号核验等待前台 · {relationship} · HWND {foreground}", health=health)
        return foreground

    def profile_surfaces(self, reference, known_windows=()):
        """Capture the main client and newly opened, owned WeCom windows only."""
        import win32gui

        health = self._profile_health(reference)
        owned = self._owned_profile_windows(health, known_windows)
        self._profile_foreground(health, owned, known_windows)
        frame = self.capture()
        if _identity(frame) != _identity(reference):
            raise NavigationPaused("账号核验主窗口截图身份变化")
        surfaces = [ProfileSurface(frame.capture.hwnd, frame.pixels, box, (), "MAIN_DIFF")
                    for box in changed_regions(reference.pixels, frame.pixels, frame.state.dpi)]
        baseline = known_windows.baseline if isinstance(known_windows, ProfileWindows) else known_windows
        new_windows = [(hwnd, geometry) for hwnd, geometry in owned if hwnd not in baseline]
        if len(new_windows) > 4:
            raise NavigationPaused("本次出现多个资料窗口候选，等待人工复核")
        for hwnd, geometry in new_windows:
            _, width, height = geometry
            image = None
            for flags in (1, 3):
                try:
                    candidate = _render_complete_client(
                        hwnd, width, height, render=lambda w, x, y: _render_client(w, x, y, flags=flags)).pixels
                    if _coverage(candidate)[0]:
                        raise CaptureError("CAPTURE_INCOMPLETE", "菜单截图候选不完整")
                except CaptureError as exc:
                    self.report("MENU", "POPUP_CAPTURE_REJECTED",
                                details={"hwnd": hwnd, "flags": flags, "reason": safe_reason(exc)})
                    continue
                image = candidate
                card = (isinstance(known_windows, ProfileWindows) and known_windows.card_click
                        and (known_windows.menu is None or hwnd != known_windows.menu.hwnd))
                self.report("CARD" if card else "MENU", "CARD_POPUP_CAPTURED" if card else "POPUP_CAPTURED",
                            result="AUTO_PASS", details={"hwnd": hwnd, "flags": flags, "shape": image.shape,
                            "client": (width, height), "dpi": frame.state.dpi, "complete": True})
                break
            if image is None:
                continue
            if (hwnd, geometry) not in self._owned_profile_windows(self._profile_health(reference), known_windows):
                raise NavigationPaused("读取期间资料窗口已移动或关闭")
            rows, cols = image.shape[:2]
            surfaces.append(ProfileSurface(hwnd, image, Box(0, 0, cols, rows), geometry,
                                           f"PRINTWINDOW_{flags}", win32gui.GetClassName(hwnd)))
        return frame, tuple(surfaces), tuple(hwnd for hwnd, _ in owned)

    @staticmethod
    def profile_physical(frame, action):
        import win32gui as gui
        surface = action.surface
        if surface.embedded:
            health = frame.diagnostics.window_health
            width, height = health.last.client_size if health else (frame.state.width, frame.state.height)
            origin = gui.ClientToScreen(surface.hwnd, (0, 0))
        else:
            origin, width, height = surface.geometry
        rows, cols = surface.pixels.shape[:2]
        x, y = action.point
        return origin[0] + int((x + .5) * width / cols), origin[1] + int((y + .5) * height / rows)

    @classmethod
    def profile_extent(cls, frame, surface):
        return (*cls.profile_physical(frame, ProfileAction("CROP_CORNER", surface,
                    Box(surface.bounds.left, surface.bounds.top, surface.bounds.left+1, surface.bounds.top+1))),
                *cls.profile_physical(frame, ProfileAction("CROP_CORNER", surface,
                    Box(surface.bounds.right-1, surface.bounds.bottom-1, surface.bounds.right, surface.bounds.bottom))))

    def verify_normal_message(self, raw):
        """Inspect message-area hit targets and reject visible profile-like popups."""
        map_point, _ = self.verify_view(raw)
        health = self._profile_health(raw.frame)
        for hwnd, (popup, detached, reason) in self._profile_inventory(health).items():
            if reason:
                continue
            if (popup.class_name == "UserInfoWindow" or popup.root_owner == health.window.hwnd
                    or detached and popup.class_name == "WXworkWindow"):
                raise NavigationPaused(f"需要先手动关闭资料层 · HWND {hwnd}")
        for part in ("B1", "B2", "C1", "C2"):
            box = raw.layout.get(part)
            if box is None:
                raise NavigationPaused("正常消息页面区域未能确认")
            map_point(box.center)

    def profile_start(self, observation):
        frame = observation.frame
        self.verify_view(observation, need_idle=True)
        health = self._profile_health(frame)
        for hwnd, (popup, detached, reason) in self._profile_inventory(health).items():
            if not reason and (popup.class_name == "UserInfoWindow" or
                               detached and popup.class_name == "WXworkWindow"):
                raise NavigationPaused(f"请手动关闭先前的资料层再继续 · HWND {hwnd}")
        return ProfileWindows(health.identity, frozenset(
            hwnd for hwnd, entry in self._profile_inventory(health).items() if entry[2] != "VISIBILITY"))

    def profile_click(self, observation, action: ProfileAction, *, known_windows=(), menu=None):
        self._act(observation, action.point, action.control, scroll=False, profile=action,
                  known_windows=known_windows, menu=menu)

    def _profile_point(self, observation, action, known_windows, menu):
        import win32gui as gui
        import win32process as process

        frame = observation.frame
        if action.stage not in ("A1_OPEN", "MENU_AVATAR", "CARD_CLOSE", "A1_CLOSE"):
            raise NavigationPaused("未定义的账号核验动作")
        context = known_windows if isinstance(known_windows, ProfileWindows) else None
        if context:
            def confirmed(surface, window, embedded):
                if surface.embedded:
                    return surface.bounds == embedded
                return bool(window and surface.hwnd == window.hwnd and window.hwnd not in context.retired)
            if not context.valid:
                raise NavigationPaused("本轮资料窗口操作上下文已失效")
            if action.stage == "MENU_AVATAR" and (context.card_click or not confirmed(
                    action.surface, context.menu, context.menu_embedded)):
                raise NavigationPaused("菜单头像操作需要本轮已验证的菜单")
            if action.stage == "CARD_CLOSE" and (not context.card_click or not confirmed(
                    action.surface, context.card, context.card_embedded)):
                raise NavigationPaused("资料卡关闭需要本轮已验证的资料卡")
            if action.stage == "A1_CLOSE" and (not menu or not confirmed(
                    menu.surface, context.menu, context.menu_embedded)):
                raise NavigationPaused("菜单关闭需要核对本轮已验证的菜单")

        if not action.surface.bounds.contains(action.point):
            raise NavigationPaused("账号核验目标超出资料区域")
        if action.stage in ("A1_OPEN", "A1_CLOSE"):
            area = detect_layout(frame.pixels, {}, VisualThresholds()).get("A")
            if (area is None or action.control != observation.login.avatar_box or not area.contains(action.point)
                    or action.surface.hwnd != frame.capture.hwnd or not action.surface.embedded
                    or (action.stage == "A1_CLOSE" and (menu is None or menu.stage != "MENU_AVATAR"))):
                raise NavigationPaused("A1 头像关闭或打开位置无法独立确认")
        current, surfaces, _ = self.profile_surfaces(frame, known_windows)

        def unchanged(surface):
            control = menu.control if menu and menu.surface is surface else action.control
            return any(candidate.hwnd == surface.hwnd and candidate.geometry == surface.geometry
                       and candidate.pixels.shape == surface.pixels.shape
                       and candidate.bounds.contains(control.center)
                       and np.array_equal(control.crop(candidate.pixels), control.crop(surface.pixels))
                       for candidate in surfaces)

        if action.stage.startswith("A1_"):
            if not observation.login.unchanged(current.pixels) or (menu and not unchanged(menu.surface)):
                raise NavigationPaused("账号头像或待关闭菜单已变化")
        elif not unchanged(action.surface):
            raise NavigationPaused("资料菜单或卡片画面已变化，取消鼠标操作")

        health = self._profile_health(frame)
        owned = self._owned_profile_windows(health, known_windows)
        foreground = self._profile_foreground(health, owned, known_windows)
        hwnd = action.surface.hwnd
        if action.surface.embedded:
            width, height = health.last.client_size
        elif (hwnd, action.surface.geometry) in owned:
            _, width, height = action.surface.geometry
        else:
            raise NavigationPaused("资料窗口不再属于本次核验")
        if not gui.IsWindowEnabled(hwnd):
            raise NavigationPaused(f"资料操作目标 IsWindowEnabled 为 False · HWND 0x{hwnd:X}")
        height_px, width_px = action.surface.pixels.shape[:2]
        x, y = action.point
        xy = gui.ClientToScreen(hwnd, (int((x + .5) * width / width_px), int((y + .5) * height / height_px)))
        hit = gui.WindowFromPoint(xy)
        if not hit or gui.GetAncestor(hit, 2) != hwnd:
            raise NavigationPaused("资料目标遮挡 · " + _occlusion_detail(hit, hwnd, xy, gui, process))
        self.report("WINDOW", "TARGET_HIT", details={"hwnd": hit, "expected": hwnd, "point": xy})
        return xy, foreground, self._activity(True)

    def select(self, observation: VisualObservation, row: ContactObservation) -> None:
        self._act(observation, row_target(observation, row), row.box, scroll=False)

    def scroll_visible_contacts(self, observation: VisualObservation) -> None:
        area = observation.layout.get("B2")
        if observation.page != "MESSAGES_CANDIDATE" or area is None or len(observation.contacts) < 3:
            raise NavigationPaused("B2 页位置无法确认，停止滚动")
        point = (area.left + round(area.width * .52), area.top + round(area.height * .62))
        self._act(observation, point, area, scroll=True)

    def _act(self, observation: VisualObservation, point: tuple[int, int], target: Box, *, scroll: bool,
             profile: ProfileAction | None = None, known_windows=(), menu=None) -> None:
        frame = observation.frame
        if profile:
            profile_check = self._profile_point(observation, profile, known_windows, menu)
        else:
            area = observation.layout.get("B2")
            if area is None or not area.contains(point) or not target.contains(point):
                raise NavigationPaused("操作点未通过 B2 范围检查")
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
            if profile:
                xy, foreground, current_activity = profile_check
                expected_hwnd = profile.surface.hwnd
            else:
                map_point, current_activity = self.verify_view(observation, need_idle=True)
                xy = map_point(point)
                foreground = expected_hwnd = frame.capture.hwnd
                if current_activity != activity:
                    raise NavigationPaused("复核期间检测到新的用户操作")
            if profile:
                health = self._profile_health(frame)
                if health.last.foreground_hwnd != foreground:
                    raise NavigationPaused("资料操作前前台已变化")
                if expected_hwnd != frame.capture.hwnd and (expected_hwnd, profile.surface.geometry) not in self._owned_profile_windows(health, known_windows):
                    raise NavigationPaused("资料操作前窗口坐标已变化")
                if not win32gui.IsWindowEnabled(expected_hwnd):
                    raise NavigationPaused("资料操作前窗口已禁用")
            if self._idle()[0] != current_activity:
                raise NavigationPaused("复核期间检测到新的用户操作")
            self.permit.ensure_current()
            win32api.SetCursorPos(xy)
            if not profile:
                self.report("WINDOW", "CURSOR_MOVED", details={"point": point, "screen_point": xy, "action": "B2"})
            if win32gui.GetCursorPos() != xy or win32gui.GetForegroundWindow() != foreground:
                raise NavigationPaused("发送鼠标动作前，指针或前台窗口已变化")
            if profile:
                hit = win32gui.WindowFromPoint(xy)
                if not hit or win32gui.GetAncestor(hit, 2) != expected_hwnd:
                    raise NavigationPaused(f"资料点击前命中窗口变化 · {xy} · HWND 0x{hit:X}")
            elif map_point(point) != xy:
                raise NavigationPaused("鼠标就位后目标点已变化")
            self.permit.ensure_current()
            if scroll:
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -120, 0)
            else:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            try:
                self._own_input = self._idle()[0]
            except OSError:
                self._own_input = None

            self.report("WINDOW", "INPUT_EXECUTED", details={"point": point, "screen_point": xy,
                        "action": profile.stage if profile else "B2", "operation": "SCROLL" if scroll else "CLICK"})

        self.permit.perform(send, scroll=scroll)
