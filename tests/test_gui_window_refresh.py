from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from autoim.app import REAL_COPY_ENABLED, LogBus, MainWindow
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
        self.assertEqual((dialog.view.source_width, dialog.view.source_height), (640, 400))
        dialog.close()

    def test_screenshot_mouse_drag_maps_through_display_transform(self):
        pixmap = QPixmap(1920, 1080)
        pixmap.setDevicePixelRatio(1.5)
        dialog = ScreenshotSelectionDialog(pixmap, mode="region", title="拖框映射")
        dialog.show()
        self.app.processEvents()
        canvas = dialog.canvas
        t = canvas.transform
        start = QPoint(round(t.offset_x + .31*t.displayed_width), round(t.offset_y + .12*t.displayed_height))
        end = QPoint(round(t.offset_x + .68*t.displayed_width), round(t.offset_y + .72*t.displayed_height))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
        self.assertIsNotNone(canvas.selected_normalized_rect)
        self.assertTrue(canvas.selection_valid)
        self.assertAlmostEqual(canvas.selected_normalized_rect.left, .31, delta=.003)
        self.assertAlmostEqual(canvas.selected_normalized_rect.right, .68, delta=.003)
        self.assertLessEqual(canvas.round_trip_error_px, 2)
        dialog.close()

    def test_real_copy_remains_disabled_pending_manual_revalidation(self):
        window = MainWindow(LogBus())
        self.assertFalse(REAL_COPY_ENABLED)
        self.assertFalse(window.copy_visible_button.isEnabled())
        window.close()


if __name__ == "__main__":
    unittest.main()
