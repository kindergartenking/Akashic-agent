"""运行时支持（M1b 桩）。

承载 SessionLike / TurnRunResult / HistoryUnit / ContextStore，供 phase 模块链
与 turn pipeline 引用。ContextStore 原版定义在 passive_turn.py，M1b 收敛到此处。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.core.types import ContextBundle


class SessionLike(Protocol):
    key: str
    metadata: dict[str, Any]

    def add_message(self, role: str, content: str, **kwargs: Any) -> dict[str, Any]: ...

    def history_units(self) -> list[Any]: ...


@dataclass
class HistoryUnit:
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TurnRunResult:
    reply: str | None = None
    tools_used: list[str] = field(default_factory=list)
    tool_chain: list[dict[str, Any]] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    thinking: str | None = None
    streamed: bool = False
    context_retry: dict[str, Any] = field(default_factory=dict)
    model_state: Any | None = None
    mobile_attention: Any | None = None


class ContextStore:
    """M1b 桩：prepare 返回空 ContextBundle，真实检索 M4 落地。"""

    async def prepare(
        self,
        *,
        msg: Any,
        session_key: str,
        session: SessionLike,
    ) -> ContextBundle:
        return ContextBundle()
