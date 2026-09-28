from __future__ import annotations

import unittest

from autoim.safety import mark_blocked_by_safety
from scripts.check_security_surface import inspect_security_surface


class SecuritySurfaceTests(unittest.TestCase):
    def test_source_and_dependencies_have_no_flagged_techniques(self):
        self.assertEqual(inspect_security_surface(), [])

    def test_safety_block_is_explicitly_logged(self):
        with self.assertLogs("autoim.safety", level="ERROR") as captured:
            self.assertFalse(mark_blocked_by_safety("example capability", "no supported API"))
        self.assertIn("BLOCKED_BY_SAFETY", captured.output[0])
        self.assertIn("当前没有找到符合 AutoIM 安全原则的实现方式", captured.output[0])


if __name__ == "__main__":
    unittest.main()
