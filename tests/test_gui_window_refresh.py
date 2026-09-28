from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from autoim.app import LogBus, MainWindow
from autoim.wecom.window_manager import WeComWindowInfo
from autoim.workers import ClientInfo
from autoim.vision_widgets import ScreenshotSelectionDialog


class GuiWindowRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_gui_client_identity_refreshes_when_hwnd_changes(self):
        window = MainWindow(LogBus())
        window.client = ClientInfo(True, pid=10, hwnd=100, title="企业微信", class_name="WeWorkWindow")
        window._sync_window_info(WeComWindowInfo(11, 200, "企业微信", "WeWorkWindow", "C:/WXWork.exe"))
        self.assertEqual((window.client.pid, window.client.hwnd), (11, 200))
        self.assertEqual(window.info_labels["pid"].text(), "11")
        self.assertEqual(window.info_labels["hwnd"].text(), "0xC8")
        window.close()

    def test_status_panel_displays_rectangle_and_foreground_state(self):
        window = MainWindow(LogBus())
        window.status_refreshed(ClientInfo(True, pid=35, hwnd=0x30870, title="企业微信",
            class_name="WeWorkWindow", rect=(178, 134, 2350, 1434), is_foreground=True))
        self.assertEqual(window.info_labels["rect"].text(), "178,134 - 2350,1434")
        self.assertEqual(window.info_labels["foreground"].text(), "是")
        self.assertIn("已连接", window.connection_status.text())
        window.close()

    def test_read_clipboard_worker_does_not_activate_wecom(self):
        sentinel = object()
        with patch("autoim.app.WeComDriver") as driver:
            driver.return_value.read_clipboard.return_value = sentinel
            self.assertIs(MainWindow._driver_read_clipboard(), sentinel)
            driver.return_value.activate.assert_not_called()

    def test_screenshot_selection_dialog_constructs(self):
        dialog = ScreenshotSelectionDialog(QPixmap(640, 400), mode="region", title="区域标定")
        self.assertEqual((dialog.view.width_px, dialog.view.height_px), (640, 400))
        dialog.close()


if __name__ == "__main__":
    unittest.main()
