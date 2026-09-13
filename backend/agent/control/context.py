"""控制上下文 ContextVar（M1b 桩）。

原版 `running_turn_id` 由 control 子系统维护，用于贯穿工具执行/诊断日志。
M1b 只保留 ContextVar 本体。
"""

from __future__ import annotations

from contextvars import ContextVar

running_turn_id: ContextVar[str] = ContextVar("akashic_running_turn_id", default="")
