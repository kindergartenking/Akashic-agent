"""before_turn 阶段（5 模块已完整）。

before_turn 是 7 阶段生命周期的第一个阶段（turn 层 · GATE）。它的完整职责是
「为整个 turn 做入场准备」。按「逐 module 重建」已落地完整 5 模块链：

  acquire_session → build_ctx → emit → collect_exports → return

  · acquire_session —— get_or_create 拿到（或新建）会话，挂到 TurnState.session。
  · build_ctx —— 把消息身份 + 会话历史打包成 BeforeTurnCtx。
  · emit —— GATE 门控，让插件 handler 改写 / 替换 ctx。
  · collect_exports —— 回收插件外溢到 slot 的 extra_hints / abort_reply。
  · return —— 把最终 ctx 设为阶段 output，交给下一阶段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, cast

from agent.core.runtime_support import SessionLike
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModule,
    append_string_exports,
    collect_prefixed_slots,
    topo_sort_modules,
)
from agent.lifecycle.types import BeforeTurnCtx, TurnState
from bus.event_bus import EventBus

if TYPE_CHECKING:
    from session.manager import SessionManager


@dataclass
class BeforeTurnFrame(PhaseFrame[TurnState, BeforeTurnCtx]):
    """before_turn 的帧：input = TurnState，output = BeforeTurnCtx。"""

    pass


BeforeTurnModules: TypeAlias = list[PhaseModule[BeforeTurnFrame]]


_SESSION_SLOT = "session:session"
_CTX_SLOT = "session:ctx"
_EXTRA_HINT_PREFIX = "session:extra_hint:"
_ABORT_REPLY_SLOT = "session:abort_reply"


class _AcquireSessionModule:
    """取 session 的模块。

    - slot     ：before_turn.acquire_session（参与拓扑排序 / 依赖定位）；
    - requires ：无依赖；
    - produces ：产出数据槽 ``session:session``；
    - run      ：get_or_create 取 session，写回 state.session，并写入数据槽。

    本模块是纯中间模块，不设 frame.output（阶段 output 由链上最后一个
    build_ctx 产出）。
    """

    slot = "before_turn.acquire_session"
    requires: tuple[str, ...] = ()
    produces = (_SESSION_SLOT,)

    def __init__(self, session_manager: SessionManager) -> None:
        self._session_manager = session_manager

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        state = frame.input
        session = self._session_manager.get_or_create(state.session_key)
        state.session = session
        frame.slots[_SESSION_SLOT] = session
        return frame


class _BuildCtxModule:
    """build_ctx 的模块：把消息身份 + 会话历史打包成 BeforeTurnCtx。

    - slot     ：before_turn.build_ctx；
    - requires ：session:session（保证排在 acquire_session 之后）；
    - produces ：session:ctx；
    - run      ：读 frame.input（TurnState）与 frame.slots[session:session]，
      组装 BeforeTurnCtx 并写入数据槽。

    本模块不设 frame.output——output 由链尾的 emit 模块产出（待 return 模块
    落地后移给 return）。
    """

    slot = "before_turn.build_ctx"
    requires = (_SESSION_SLOT,)
    produces = (_CTX_SLOT,)

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        state = frame.input
        session = cast(SessionLike, frame.slots[_SESSION_SLOT])
        ctx = BeforeTurnCtx(
            session_key=state.session_key,
            channel=state.msg.channel,
            chat_id=state.msg.chat_id,
            content=state.msg.content,
            timestamp=state.msg.timestamp,
            # 砍掉 ContextStore 后无长期记忆召回，记忆块留空；
            # 历史消息直接取 session.history_units() 一行拿到。
            retrieved_memory_block="",
            history_messages=tuple(session.history_units()),
            extra_metadata=dict(state.extra_metadata),
        )
        frame.slots[_CTX_SLOT] = ctx
        return frame


class _EmitBeforeTurnCtxModule:
    """GATE 门控：把 ctx 交给 EventBus，插件 handler 可改写字段或整体替换 ctx。

    - slot     ：before_turn.emit；
    - requires ：before_turn.build_ctx + session:ctx（保证排在 build_ctx 之后）；
    - produces ：session:ctx（emit 可能替换 ctx，替换后写回槽）；
    - run      ：frame.slots[session:ctx] = await bus.emit(ctx)。

    依赖 EventBus.emit 的语义：依次执行 on(BeforeTurnCtx, handler) 注册的 handler，
    handler 返回 None 则保持原 ctx，返回新对象则整体替换；无 handler 时原样返回。

    本模块不设 frame.output——output 由链尾的 collect_exports 模块产出（待
    return 模块落地后移给 return）。
    """

    slot = "before_turn.emit"
    requires = ("before_turn.build_ctx", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        ctx = cast(BeforeTurnCtx, frame.slots[_CTX_SLOT])
        ctx = await self._bus.emit(ctx)
        frame.slots[_CTX_SLOT] = ctx
        return frame


class _CollectBeforeTurnExportSlotsModule:
    """回收插件外溢：把 extra_hints / abort_reply 从 slot 合并回 ctx。

    - slot     ：before_turn.collect_exports；
    - requires ：before_turn.emit + session:ctx（保证排在 emit 之后）；
    - produces ：session:ctx（原地改 ctx 字段，不替换对象）；
    - run      ：collect_prefixed_slots 收 session:extra_hint: 前缀的槽合并进
      ctx.extra_hints；读 session:abort_reply 控制槽，若有则置 ctx.abort + abort_reply。

    这是插件的第二条介入通道（区别于 emit 的返回值）：插件在 emit 里可以往
    frame.slots 写带前缀的键外溢数据，由本模块统一回收。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "before_turn.collect_exports"
    requires = ("before_turn.emit", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        ctx = cast(BeforeTurnCtx, frame.slots[_CTX_SLOT])
        append_string_exports(
            ctx.extra_hints,
            collect_prefixed_slots(frame.slots, _EXTRA_HINT_PREFIX),
        )
        abort_reply = frame.slots.get(_ABORT_REPLY_SLOT)
        if isinstance(abort_reply, str) and abort_reply:
            ctx.abort = True
            ctx.abort_reply = abort_reply
        return frame


class _ReturnBeforeTurnCtxModule:
    """产出 output：把链上最终 ctx（经 emit 替换 + collect_exports 回收后）
    设为 frame.output，交给下一阶段。

    - slot     ：before_turn.return；
    - requires ：before_turn.collect_exports + session:ctx（保证排在最后）；
    - run      ：frame.output = frame.slots[session:ctx]。

    本模块是 before_turn 的链尾，也是「output 职责」的最终归宿——之前的
    acquire_session / build_ctx / emit / collect_exports 都不碰 output，
    只有这里把 slot 里的最终 ctx 交出去。
    """

    slot = "before_turn.return"
    requires = ("before_turn.collect_exports", _CTX_SLOT)

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        frame.output = cast(BeforeTurnCtx, frame.slots[_CTX_SLOT])
        return frame


def default_before_turn_modules(
    bus: EventBus,
    session_manager: SessionManager,
    *,
    plugin_modules: BeforeTurnModules | None = None,
) -> BeforeTurnModules:
    """装配 before_turn 的内置模块链（acquire_session → build_ctx → emit → collect_exports → return）。"""
    builtins: BeforeTurnModules = [
        _AcquireSessionModule(session_manager),
        _BuildCtxModule(),
        _EmitBeforeTurnCtxModule(bus),
        _CollectBeforeTurnExportSlotsModule(),
        _ReturnBeforeTurnCtxModule(),
    ]
    return cast(
        BeforeTurnModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )
