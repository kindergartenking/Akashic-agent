"""before_reasoning 阶段（5 模块已完整）。

before_reasoning 是 7 阶段生命周期的第二个阶段（reasoning 层 · GATE），位于
before_turn 之后、reasoner 之前。完整职责是「把 before_turn 的决策转交给推理层，
并为工具调用铺设正确的会话上下文」。按「逐 module 重建」已落地完整 5 模块链：

  sync_tools → build_ctx → emit → collect_exports → return

  · sync_tools —— 把本 turn 的会话身份灌进 ToolRegistry（零 slot 副作用）。
  · build_ctx —— 把 before_turn 的决策（skill_names / 记忆块 / 提示）搬进
    BeforeReasoningCtx。
  · emit —— GATE 门控，让插件 handler 改写 / 替换 ctx。
  · collect_exports —— 回收插件外溢到 slot 的 extra_hints / abort_reply。
  · return —— 把最终 ctx 设为阶段 output，交给下一阶段。

与 before_turn 的同构点：后 3 个模块（emit / collect_exports / return）是 GATE
骨架，逻辑与 before_turn 完全一致，仅命名空间 session: → reasoning:、ctx 类型
BeforeTurnCtx → BeforeReasoningCtx、export 前缀同步替换。
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
    """before_reasoning 的帧：input = BeforeReasoningInput，output = BeforeReasoningCtx。"""

    pass


BeforeReasoningModules: TypeAlias = list[PhaseModule[BeforeReasoningFrame]]


_CTX_SLOT = "reasoning:ctx"
_EXTRA_HINT_PREFIX = "reasoning:extra_hint:"
_ABORT_REPLY_SLOT = "reasoning:abort_reply"


class _SyncToolContextModule:
    """同步工具上下文：把本 turn 的会话身份灌进 ToolRegistry。

    - slot     ：before_reasoning.sync_tools；
    - requires ：无依赖（链上第一个模块）；
    - produces ：无（零 slot 模块，只做副作用，不产生数据槽）；
    - run      ：读 frame.input 的 state / before_turn，调 tools.set_context(...)。

    这是 7 个阶段里唯一的「零 slot 模块」：不读不写任何数据槽，产物是写入
    ToolRegistry（外部可变对象）的副作用，不通过 slot 传递。
    """

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


class _BuildCtxModule:
    """build_ctx：把 before_turn 的决策搬进 BeforeReasoningCtx。

    - slot     ：before_reasoning.build_ctx；
    - requires ：before_reasoning.sync_tools（模块槽依赖，保证排在 sync_tools 后）；
    - produces ：reasoning:ctx；
    - run      ：读 frame.input.before_turn，组装 BeforeReasoningCtx 并写入数据槽。

    核心动作是「有选择的搬」而非「复用 BeforeTurnCtx」：
    - 搬 5 个身份事实（session_key / channel / chat_id / content / timestamp）；
    - 搬 3 个决策字段（skill_names / retrieved_memory_block / extra_hints），
      其中 list(...) 是浅拷贝，避免后续阶段改本阶段的字段时反噬 before_turn 的 ctx；
    - 不搬 history_messages（推理阶段不直接消费历史）；
    - 不搬 abort / abort_reply（本阶段重新门控，默认 False / ""）。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "before_reasoning.build_ctx"
    requires = ("before_reasoning.sync_tools",)
    produces = (_CTX_SLOT,)

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        before_turn = frame.input.before_turn
        ctx = BeforeReasoningCtx(
            session_key=before_turn.session_key,
            channel=before_turn.channel,
            chat_id=before_turn.chat_id,
            content=before_turn.content,
            timestamp=before_turn.timestamp,
            skill_names=list(before_turn.skill_names),
            retrieved_memory_block=before_turn.retrieved_memory_block,
            extra_hints=list(before_turn.extra_hints),
        )
        frame.slots[_CTX_SLOT] = ctx
        return frame


class _EmitBeforeReasoningCtxModule:
    """GATE 门控：把 ctx 交给 EventBus，插件 handler 可改写字段或整体替换 ctx。

    - slot     ：before_reasoning.emit；
    - requires ：before_reasoning.build_ctx + reasoning:ctx（保证排在 build_ctx 后）；
    - produces ：reasoning:ctx（emit 可能替换 ctx，替换后写回槽）；
    - run      ：frame.slots[reasoning:ctx] = await bus.emit(ctx)。

    依赖 EventBus.emit 的语义：依次执行 on(BeforeReasoningCtx, handler) 注册的
    handler，handler 返回 None 则保持原 ctx，返回新对象则整体替换；无 handler 时
    原样返回。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "before_reasoning.emit"
    requires = ("before_reasoning.build_ctx", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        ctx = cast(BeforeReasoningCtx, frame.slots[_CTX_SLOT])
        ctx = await self._bus.emit(ctx)
        frame.slots[_CTX_SLOT] = ctx
        return frame


class _CollectBeforeReasoningExportSlotsModule:
    """回收插件外溢：把 extra_hints / abort_reply 从 slot 合并回 ctx。

    - slot     ：before_reasoning.collect_exports；
    - requires ：before_reasoning.emit + reasoning:ctx（保证排在 emit 后）；
    - produces ：reasoning:ctx（原地改 ctx 字段，不替换对象）；
    - run      ：collect_prefixed_slots 收 reasoning:extra_hint: 前缀的槽合并进
      ctx.extra_hints；读 reasoning:abort_reply 控制槽，若有则置 ctx.abort + abort_reply。

    这是插件的第二条介入通道（区别于 emit 的返回值）：插件在 emit 里可以往
    frame.slots 写带前缀的键外溢数据，由本模块统一回收。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

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
    """产出 output：把链上最终 ctx（经 emit 替换 + collect_exports 回收后）
    设为 frame.output，交给下一阶段。

    - slot     ：before_reasoning.return；
    - requires ：before_reasoning.collect_exports + reasoning:ctx（保证排在最后）；
    - run      ：frame.output = frame.slots[reasoning:ctx]。

    本模块是 before_reasoning 的链尾，也是「output 职责」的最终归宿——之前的
    sync_tools / build_ctx / emit / collect_exports 都不碰 output，只有这里把
    slot 里的最终 ctx 交出去。
    """

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
    """装配 before_reasoning 的内置模块链（sync_tools → build_ctx → emit → collect_exports → return）。"""
    builtins: BeforeReasoningModules = [
        _SyncToolContextModule(tools, session_manager),
        _BuildCtxModule(),
        _EmitBeforeReasoningCtxModule(bus),
        _CollectBeforeReasoningExportSlotsModule(),
        _ReturnBeforeReasoningCtxModule(),
    ]
    return cast(
        BeforeReasoningModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )
