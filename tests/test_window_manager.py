from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from autoim.wecom.driver import WeComDriver
from autoim.wecom.window_manager import WeComWindowManager, WindowResolutionError
from autoim.workers import detect_wecom


class FakeWindows:
    def __init__(self) -> None:
        self.windows = {100: {"pid": 10, "title": "企业微信", "class": "WeWorkWindow", "rect": (1, 2, 901, 702), "visible": True}}
        self.foreground = 0
        self.minimized: set[int] = set()
        self.activate = True
        self.calls: list[tuple] = []

    def IsWindow(self, hwnd): return hwnd in self.windows
    def IsWindowVisible(self, hwnd): return self.windows[hwnd].get("visible", True)
    def GetClassName(self, hwnd): return self.windows[hwnd]["class"]
    def GetWindowText(self, hwnd): return self.windows[hwnd]["title"]
    def GetWindowRect(self, hwnd): return self.windows[hwnd]["rect"]
    def GetForegroundWindow(self): return self.foreground
    def IsIconic(self, hwnd): return hwnd in self.minimized
    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("show", hwnd, cmd))
        self.minimized.discard(hwnd)
    def SetForegroundWindow(self, hwnd):
        self.calls.append(("foreground", hwnd))
        if self.activate:
            self.foreground = hwnd
        return True
    def EnumWindows(self, callback, extra):
        for hwnd in tuple(self.windows):
            callback(hwnd, extra)

    def win32process_module(self):
        return types.SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, self.windows[hwnd]["pid"]))


class WindowManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWindows()
        self.gui_module = types.SimpleNamespace(**{
            name: getattr(self.api, name)
            for name in ("IsWindow", "IsWindowVisible", "GetClassName", "GetWindowText", "GetWindowRect", "GetForegroundWindow", "IsIconic", "ShowWindow", "SetForegroundWindow", "EnumWindows")
        })
        self.modules = patch.dict(sys.modules, {
            "win32gui": self.gui_module,
            "win32process": self.api.win32process_module(),
        })
        self.modules.start()
        self.processes = patch("autoim.wecom.window_manager.psutil.process_iter", return_value=[
            types.SimpleNamespace(info={"pid": 10, "name": "WXWork.exe", "exe": r"C:\WXWork\WXWork.exe"})
        ])
        self.processes.start()
        self.sleep = patch("autoim.wecom.window_manager.time.sleep")
        self.sleep.start()
        self.manager = WeComWindowManager()

    def tearDown(self) -> None:
        self.sleep.stop()
        self.processes.stop()
        self.modules.stop()

    def test_resolve_and_valid_handle(self):
        info = self.manager.resolve_window()
        self.assertEqual((info.pid, info.hwnd), (10, 100))
        self.assertTrue(self.manager.is_valid(100))

    def test_stale_handle_is_dynamically_rediscovered(self):
        self.assertEqual(self.manager.resolve_window().hwnd, 100)
        self.api.windows = {200: {"pid": 10, "title": "企业微信", "class": "WeWorkWindow", "rect": (0, 0, 800, 600), "visible": True}}
        self.assertFalse(self.manager.is_valid(100))
        self.assertEqual(self.manager.resolve_window().hwnd, 200)

    def test_minimized_window_is_restored_and_foregrounded(self):
        self.api.minimized.add(100)
        info = self.manager.restore()
        self.assertEqual(info.hwnd, 100)
        self.assertNotIn(100, self.api.minimized)
        self.assertIn(("show", 100, 9), self.api.calls)
        self.assertEqual(self.manager.activate().hwnd, 100)
        self.assertEqual(self.api.foreground, 100)

    def test_foreground_failure_is_bounded(self):
        self.api.activate = False
        with self.assertLogs("autoim.wecom.window_manager", level="ERROR") as captured:
            with self.assertRaises(WindowResolutionError):
                self.manager.ensure_foreground()
        attempts = [item for item in self.api.calls if item[0] == "foreground"]
        self.assertEqual(len(attempts), 3)
        self.assertTrue(any("无法确认企业微信处于前台" in item for item in captured.output))

    def test_foreground_attempt_limit_cannot_be_unbounded(self):
        with self.assertRaises(ValueError):
            WeComWindowManager(max_activation_attempts=4)

    def test_passive_foreground_check_does_not_steal_focus(self):
        self.api.foreground = 999
        with self.assertRaises(WindowResolutionError):
            self.manager.verify_foreground()
        self.assertFalse(any(item[0] == "foreground" for item in self.api.calls))

    def test_is_foreground_is_passive(self):
        self.api.foreground = 100
        self.assertTrue(self.manager.is_foreground(100))
        self.api.foreground = 999
        self.assertFalse(self.manager.is_foreground(100))
        self.assertFalse(any(item[0] == "foreground" for item in self.api.calls))

    def test_clipboard_action_is_not_reached_if_foreground_guard_fails(self):
        self.api.activate = False
        with patch("autoim.wecom.driver.clipboard.clear_clipboard") as clear:
            with self.assertRaises(WindowResolutionError):
                WeComDriver(self.manager).clear_clipboard()
        clear.assert_not_called()

    def test_clipboard_is_not_read_after_focus_moves_away(self):
        self.api.foreground = 999
        with patch("autoim.wecom.driver.clipboard.get_sequence_number") as sequence, \
             patch("autoim.wecom.driver.clipboard.read_clipboard") as read:
            with self.assertRaises(WindowResolutionError):
                WeComDriver(self.manager).clipboard_after_change(0, timeout=0)
        sequence.assert_not_called()
        read.assert_not_called()

    def test_clipboard_action_runs_after_foreground_verification(self):
        order: list[str] = []
        original = self.gui_module.SetForegroundWindow
        self.gui_module.SetForegroundWindow = lambda hwnd: (original(hwnd), order.append("foreground"))[-1]
        with patch("autoim.wecom.driver.clipboard.clear_clipboard", side_effect=lambda: order.append("clear")), \
             patch("autoim.wecom.driver.clipboard.read_clipboard", return_value="snapshot"):
            _info, snapshot = WeComDriver(self.manager).clear_clipboard()
        self.assertEqual(snapshot, "snapshot")
        self.assertLess(order.index("foreground"), order.index("clear"))

    def test_get_rect_uses_fresh_window(self):
        self.assertEqual(self.manager.get_rect(), (1, 2, 901, 702))

    def test_ambiguous_windows_fail_closed(self):
        self.api.windows[200] = {"pid": 11, "title": "企业微信", "class": "WeWorkWindow", "rect": (0, 0, 400, 400), "visible": True}
        self.processes.stop()
        self.processes = patch("autoim.wecom.window_manager.psutil.process_iter", return_value=[
            types.SimpleNamespace(info={"pid": pid, "name": "WXWork.exe", "exe": ""})
            for pid in (10, 11)
        ])
        self.processes.start()
        with self.assertRaises(WindowResolutionError):
            self.manager.resolve_window()

    def test_hidden_helper_window_is_not_selected(self):
        self.api.windows[200] = {"pid": 11, "title": "企业微信", "class": "WeWorkWindow", "rect": (0, 0, 400, 400), "visible": False}
        self.processes.stop()
        self.processes = patch("autoim.wecom.window_manager.psutil.process_iter", return_value=[
            types.SimpleNamespace(info={"pid": pid, "name": "WXWork.exe", "exe": ""})
            for pid in (10, 11)
        ])
        self.processes.start()
        self.assertEqual(self.manager.resolve_window().hwnd, 100)

    def test_initial_client_detection_uses_visible_main_window_pid(self):
        uia = types.SimpleNamespace(
            ControlFromHandle=lambda hwnd: types.SimpleNamespace(Exists=lambda maxSearchSeconds: True)
        )
        with patch.dict(sys.modules, {"uiautomation": uia}):
            info = detect_wecom()
        self.assertEqual((info.pid, info.hwnd, info.class_name), (10, 100, "WeWorkWindow"))


if __name__ == "__main__":
    unittest.main()
