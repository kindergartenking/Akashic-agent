"""control 端口类型（M1b 桩）。

原版 control 子系统为「control turn」提供输入锁定。M1b 只保留类型契约，
供 after_reasoning 阶段的 `_turn_user_inputs` 引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class TurnUserInput:
    item_id: str
    ordinal: int
    content: str
    media: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime | None = None


class InputLock(Protocol):
    def lock(self) -> None: ...

    def used_inputs(self) -> tuple[TurnUserInput, ...]: ...
