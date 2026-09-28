from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def mark_blocked_by_safety(capability: str, reason: str = "") -> bool:
    """Log an explicit fail-closed result when no permitted implementation exists."""
    detail = f"；原因：{reason}" if reason else ""
    logger.error(
        "BLOCKED_BY_SAFETY：%s。当前没有找到符合 AutoIM 安全原则的实现方式%s",
        capability,
        detail,
    )
    return False
