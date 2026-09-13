"""turn pipeline（缩减版 PassiveTurnPipeline）。

7 阶段顺序硬编码在此：before_turn → before_reasoning → reasoner(内部 3 阶段
prompt_render 一次 + before_step/after_step 循环) → after_reasoning → after_turn。

run() 端到端串起全部 7 阶段，返回最终 OutboundMessage。before_turn /
before_reasoning 的 abort 短路时，返回带 SHORT_CIRCUITED 标记的 OutboundMessage。
"""

from __future__ import annotations

from typing import Any

from bus.event_bus import EventBus
from bus.events import InboundMessage, OutboundMessage, TurnDisposition
from agent.lifecycle.phase import Phase
from agent.lifecycle.types import (
    AfterReasoningInput,
    BeforeReasoningInput,
    TurnState,
)
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
            default_after_turn_modules(self._bus, self._outbound),
            frame_factory=lambda input: AfterTurnFrame(input=input),
        )

    async def run(
        self,
        msg: InboundMessage,
        key: str,
        *,
        dispatch_outbound: bool = True,
    ) -> OutboundMessage:
        state = TurnState(
            msg=msg,
            session_key=key,
            dispatch_outbound=dispatch_outbound,
        )

        # Phase 1: before_turn（取 session + 组 ctx；session 写回 state.session）
        before_turn = await self._before_turn.run(state)
        if before_turn.abort:
            return self._short_circuit(msg, before_turn.abort_reply)

        # Phase 2: before_reasoning（同步工具上下文 + 组 ctx）
        before_reasoning = await self._before_reasoning.run(
            BeforeReasoningInput(state=state, before_turn=before_turn)
        )
        if before_reasoning.abort:
            return self._short_circuit(msg, before_reasoning.abort_reply)

        # Phase 3-5: reasoner（prompt_render 一次 + before_step/after_step 循环）
        turn_result = await self._reasoner.run_turn(
            msg=msg,
            session=state.session,
            skill_names=before_reasoning.skill_names,
            retrieved_memory_block=before_reasoning.retrieved_memory_block,
            extra_hints=before_reasoning.extra_hints,
        )

        # Phase 6: after_reasoning（解析回复 + 持久化 + 组 outbound）
        turn_snapshot = await self._after_reasoning.run(
            AfterReasoningInput(state=state, turn_result=turn_result)
        )

        # Phase 7: after_turn（提交事件 + 派发 outbound）
        return await self._after_turn.run(turn_snapshot)

    def _short_circuit(self, msg: InboundMessage, reply: str) -> OutboundMessage:
        """abort 短路：不进入后续阶段，直接返回带 SHORT_CIRCUITED 标记的消息。"""
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=reply,
            turn_disposition=TurnDisposition.SHORT_CIRCUITED,
        )
