"""before_reasoning 阶段（轻量版）。

before_reasoning 位于 before_turn 之后、reasoner 之前，是推理开始前最后一次
「准备 + 门控」的机会。它只做两件事：

  1. 同步工具上下文 —— 把本 turn 的 channel / chat_id / session_key / 时间戳 /
     user_source_ref 灌进 ToolRegistry，让后续工具调用在正确的会话上下文里执行；
  2. 准备 reasoning ctx —— 把 before_turn 的决策（skill_names / 记忆块 / 提示）
     搬进 BeforeReasoningCtx，作为本阶段的交接载体。

模块构成 = 2 个「准备」模块 + 3 个「GATE 骨架」模块（与 before_turn 完全同构）：

  sync_tools ──► build_ctx ──► emit ──► collect_exports ──► return
  (准备工具上下文) (准备 ctx)  (GATE 门控)  (回收插件外溢)   (产出 output)

相比原版（6 模块），轻量版砍掉一个「能力」模块：
  - warmup —— 用 context.render 做一次丢弃结果的 prompt 预热（提前校验 + 暖缓存，
              真正的渲染在 reasoner 内部的 prompt_render 阶段才发生）。属优化，砍。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, cast

from agent.core.passive_support import predict_current_user_source_ref
from agent.control.context import running_turn_id
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModule,
    append_string_exports,
    collect_prefixed_slots,
    topo_sort_modules,
)
from agent.lifecycle.types import BeforeReasoningCtx, BeforeReasoningInput
from bus.event_bus import EventBus

if TYPE_CHECKING:
    from agent.tools.registry import ToolRegistry
    from session.manager import SessionManager


@dataclass
class BeforeReasoningFrame(PhaseFrame[BeforeReasoningInput, BeforeReasoningCtx]):
    pass


BeforeReasoningModules: TypeAlias = list[PhaseModule[BeforeReasoningFrame]]


_CTX_SLOT = "reasoning:ctx"
_EXTRA_HINT_PREFIX = "reasoning:extra_hint:"
_ABORT_REPLY_SLOT = "reasoning:abort_reply"


class _SyncToolContextModule:
    """准备工具上下文：把本 turn 的会话身份灌进 ToolRegistry。"""

    slot = "before_reasoning.sync_tools"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        tools: ToolRegistry,
        session_manager: SessionManager,
    ) -> None:
        self._tools = tools
        self._session_manager = session_manager

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        state = frame.input.state
        before_turn = frame.input.before_turn
        if state.session is None:
            raise RuntimeError("BeforeReasoning requires TurnState.session")
        self._tools.set_context(
            channel=before_turn.channel,
            chat_id=before_turn.chat_id,
            session_key=before_turn.session_key,
            turn_id=running_turn_id.get(),
            current_timestamp=before_turn.timestamp.isoformat(),
            current_user_source_ref=predict_current_user_source_ref(
                session_manager=self._session_manager,
                session=state.session,
            ),
        )
        return frame


class _BuildBeforeReasoningCtxModule:
    """准备 ctx：把 before_turn 的决策搬进 BeforeReasoningCtx。"""

    slot = "before_reasoning.build_ctx"
    requires = ("before_reasoning.sync_tools",)
    produces = (_CTX_SLOT,)

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        before_turn = frame.input.before_turn
        frame.slots[_CTX_SLOT] = BeforeReasoningCtx(
            session_key=before_turn.session_key,
            channel=before_turn.channel,
            chat_id=before_turn.chat_id,
            content=before_turn.content,
            timestamp=before_turn.timestamp,
            skill_names=list(before_turn.skill_names),
            retrieved_memory_block=before_turn.retrieved_memory_block,
            extra_hints=list(before_turn.extra_hints),
        )
        return frame


class _EmitBeforeReasoningCtxModule:
    """GATE 门控：把 ctx 交给 EventBus，handler 可改写字段或设置 abort。"""

    slot = "before_reasoning.emit"
    requires = ("before_reasoning.build_ctx", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        ctx = cast(BeforeReasoningCtx, frame.slots[_CTX_SLOT])
        frame.slots[_CTX_SLOT] = await self._bus.emit(ctx)
        return frame


class _CollectBeforeReasoningExportSlotsModule:
    """回收插件外溢：把 extra_hints / abort_reply 溢出 slot 并回 ctx。"""

    slot = "before_reasoning.collect_exports"
    requires = ("before_reasoning.emit", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        ctx = cast(BeforeReasoningCtx, frame.slots[_CTX_SLOT])
        append_string_exports(
            ctx.extra_hints,
            collect_prefixed_slots(frame.slots, _EXTRA_HINT_PREFIX),
        )
        abort_reply = frame.slots.get(_ABORT_REPLY_SLOT)
        if isinstance(abort_reply, str) and abort_reply:
            ctx.abort = True
            ctx.abort_reply = abort_reply
        return frame


class _ReturnBeforeReasoningCtxModule:
    """产出 output：把最终 ctx 设为 frame.output。"""

    slot = "before_reasoning.return"
    requires = ("before_reasoning.collect_exports", _CTX_SLOT)

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        frame.output = cast(BeforeReasoningCtx, frame.slots[_CTX_SLOT])
        return frame


def default_before_reasoning_modules(
    bus: EventBus,
    tools: ToolRegistry,
    session_manager: SessionManager,
    *,
    plugin_modules: BeforeReasoningModules | None = None,
) -> BeforeReasoningModules:
    builtins: BeforeReasoningModules = [
        _SyncToolContextModule(tools, session_manager),
        _BuildBeforeReasoningCtxModule(),
        _EmitBeforeReasoningCtxModule(bus),
        _CollectBeforeReasoningExportSlotsModule(),
        _ReturnBeforeReasoningCtxModule(),
    ]
    return cast(
        BeforeReasoningModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )
