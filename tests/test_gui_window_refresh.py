from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from autoim.app import LogBus, MainWindow
from autoim.wecom.window_manager import WeComWindowInfo
from autoim.workers import ClientInfo


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


if __name__ == "__main__":
    unittest.main()
