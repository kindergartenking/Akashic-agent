"""诊断日志（M1b 桩）。

原版 `turn_milestone` 输出结构化 turn 里程碑。M1b 保留签名，退化为标准日志。
"""

from __future__ import annotations

import logging
from typing import Any


def turn_milestone(
    logger: logging.Logger,
    event: str,
    *,
    session_id: str = "",
    turn_id: str = "",
    client_message_id: str = "",
    duration_ms: float | None = None,
    counts: str = "",
    outcome: str = "",
    level: int = logging.INFO,
    **kwargs: Any,
) -> None:
    logger.log(
        level,
        "milestone event=%s session=%s turn=%s outcome=%s",
        event,
        session_id or "-",
        turn_id or "-",
        outcome or "-",
    )
