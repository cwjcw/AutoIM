"""Safe, user-visible Windows automation for Enterprise WeChat."""

from autoim.wecom.driver import WeComDriver
from autoim.wecom.window_manager import WeComWindowInfo, WeComWindowManager

__all__ = ["WeComDriver", "WeComWindowInfo", "WeComWindowManager"]
