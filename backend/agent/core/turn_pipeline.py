"""turn pipeline（缩减版 PassiveTurnPipeline）。

7 阶段顺序硬编码在此：before_turn → before_reasoning → reasoner(内部 3 阶段)
→ after_reasoning → after_turn。

当前处于「逐 module 重建」过渡态：run() 只执行 before_turn 的「取 session」，
其余阶段（before_reasoning / reasoner / after_*）已装配但尚未接回，随逐个
module 实现逐步启用。
"""

from __future__ import annotations

from typing import Any

from bus.event_bus import EventBus
from bus.events import InboundMessage
from agent.lifecycle.phase import Phase
from agent.lifecycle.types import TurnState
from agent.lifecycle.phases.after_reasoning import (
    AfterReasoningFrame,
    default_after_reasoning_modules,
)
from agent.lifecycle.phases.after_turn import (
    AfterTurnFrame,
    default_after_turn_modules,
)
from agent.lifecycle.phases.before_reasoning import (
    BeforeReasoningFrame,
    default_before_reasoning_modules,
)
from agent.lifecycle.phases.before_turn import (
    BeforeTurnFrame,
    default_before_turn_modules,
)
from agent.tools.registry import ToolRegistry
from agent.turns.outbound import OutboundPort


class TurnPipeline:
    def __init__(
        self,
        *,
        bus: EventBus,
        session_manager: Any,
        tools: ToolRegistry,
        context: Any,
        outbound: OutboundPort,
        session_services: Any,
        reasoner: Any,
    ) -> None:
        self._bus = bus
        self._session_manager = session_manager
        self._tools = tools
        self._context = context
        self._outbound = outbound
        self._session_services = session_services
        self._reasoner = reasoner

        self._before_turn = Phase(
            default_before_turn_modules(self._bus, self._session_manager),
            frame_factory=lambda state: BeforeTurnFrame(input=state),
        )
        # 以下阶段当前仅「装配」、尚未在 run() 中执行，随逐个 module 实现逐步接回。
        self._before_reasoning = Phase(
            default_before_reasoning_modules(
                self._bus, self._tools, self._session_manager
            ),
            frame_factory=lambda input: BeforeReasoningFrame(input=input),
        )
        self._after_reasoning = Phase(
            default_after_reasoning_modules(self._bus, self._session_services),
            frame_factory=lambda input: AfterReasoningFrame(input=input),
        )
        self._after_turn = Phase(
            default_after_turn_modules(self._bus, self._outbound, self._context),
            frame_factory=lambda input: AfterTurnFrame(input=input),
        )

    async def run(
        self,
        msg: InboundMessage,
        key: str,
        *,
        dispatch_outbound: bool = True,
    ) -> Any:
        state = TurnState(
            msg=msg,
            session_key=key,
            dispatch_outbound=dispatch_outbound,
        )

        # Phase 1: before_turn —— 已实现「取 session」+「build_ctx」。
        # session 写回 state.session；阶段 output 为打包好的 BeforeTurnCtx。
        ctx = await self._before_turn.run(state)

        # TODO: 后续阶段随逐个 module 实现逐步接回：
        #   before_reasoning → reasoner(prompt_render + before/after_step 循环)
        #   → after_reasoning → after_turn；最终 run() 返回 OutboundMessage。
        return ctx
