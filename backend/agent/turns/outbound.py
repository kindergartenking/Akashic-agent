"""出站端口（M1b 桩）。

原版 `OutboundPort.dispatch` 把 OutboundMessage 提交给渠道 adapter。M1b 用
内存队列记录 dispatch，供验证脚本断言出站行为。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class OutboundDispatch:
    channel: str
    chat_id: str
    content: str
    thinking: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    media: list[str] = field(default_factory=list)
    session_message_id: str | None = None
    control_turn_id: str | None = None


class OutboundPort(Protocol):
    async def dispatch(self, dispatch: OutboundDispatch) -> Any: ...


class RecordingOutboundPort:
    """内存出站桩：记录每次 dispatch，供验证脚本断言。"""

    def __init__(self) -> None:
        self.dispatches: list[OutboundDispatch] = []

    async def dispatch(self, dispatch: OutboundDispatch) -> None:
        self.dispatches.append(dispatch)
