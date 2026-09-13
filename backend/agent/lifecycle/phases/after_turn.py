"""after_turn 阶段（6 模块已完整）。

after_turn 是 7 阶段生命周期的第七个阶段（turn 层 · TAP），位于 after_reasoning 之后，
是**一次 turn 的最后一步**。完整职责是「提交事件 + 派发 outbound + 广播快照」。

  build_committed → fanout_committed → build_ctx → fanout_ctx → dispatch → return

  · build_committed —— 组装 TurnCommitted（turn 权威终结事件）。
  · fanout_committed —— 广播 TurnCommitted（内部系统消费）。
  · build_ctx —— 组装 AfterTurnCtx（@on_after_turn 插件的 TAP 快照）。
  · fanout_ctx —— 广播 AfterTurnCtx（插件旁路观察）。
  · dispatch —— 把 outbound 真正派发出去。
  · return —— 把 outbound 设为阶段 output（整个 turn pipeline 的最终产物）。

本阶段三个独特之处：
1. 是 TAP 阶段，但**有 build_ctx**——input 是 TurnSnapshot 三元组，不是 AfterTurnCtx，
   需组装出快照；这与 after_step（input 就是快照，copy 即可）不同。
2. **双层广播**：fanout 两个东西——TurnCommitted（内部权威提交事件，heavy）+
   AfterTurnCtx（插件 TAP 快照，light）。
3. output 是 OutboundMessage（整个 turn pipeline 的最终产物），不是 ctx。

裁剪说明（相对 M1b 旧版 10 模块 ~400 行）：
- 砍 build_work（budget / react_stats / model_binding 计算）；
- 砍 collect_extras（turn:extra:）、log_budget（日志）、collect_telemetry（turn:telemetry:）；
- TurnCommitted 事件类型砍到 8 个核心字段；
- 删掉对已裁剪字段 context_retry / meme_tag 的 3 处引用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias, cast

from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModule,
    topo_sort_modules,
)
from agent.lifecycle.types import AfterTurnCtx, TurnSnapshot
from agent.turns.outbound import OutboundDispatch, OutboundPort
from bus.event_bus import EventBus
from bus.events import OutboundMessage
from bus.events_lifecycle import TurnCommitted


@dataclass
class AfterTurnFrame(PhaseFrame[TurnSnapshot, OutboundMessage]):
    """after_turn 的帧：input = TurnSnapshot（三元组），output = OutboundMessage。"""

    pass


AfterTurnModules: TypeAlias = list[PhaseModule[AfterTurnFrame]]


_TURN_COMMITTED_SLOT = "turn:committed"
_CTX_SLOT = "turn:ctx"


class _BuildTurnCommittedModule:
    """build_committed：组装 TurnCommitted（turn 权威终结事件）。

    - slot     ：after_turn.build_committed；
    - requires ：无依赖（链上第一个模块）；
    - produces ：turn:committed；
    - run      ：从 input（TurnSnapshot）提取核心字段组装 TurnCommitted——input 是用户
      输入、assistant_response 是回复、tools_used 是工具、assistant_message_id 是持久化
      稳定 ID、timestamp 是提交时间。

    这是「提交事件」的落点。相对旧版砍掉 budget / react_stats / meme_tag / model_binding /
    tool_call_groups 等扩展字段，只填核心语义。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_turn.build_committed"
    requires: tuple[str, ...] = ()
    produces = (_TURN_COMMITTED_SLOT,)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        snap = frame.input
        state = snap.state
        msg = state.msg
        frame.slots[_TURN_COMMITTED_SLOT] = TurnCommitted(
            session_key=state.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            input_message=msg.content,
            assistant_response=snap.ctx.reply,
            tools_used=list(snap.ctx.tools_used),
            assistant_message_id=snap.outbound.session_message_id,
            timestamp=msg.timestamp,
        )
        return frame


class _FanoutTurnCommittedModule:
    """fanout_committed：广播 TurnCommitted 给内部系统（记忆 / 统计）。

    - slot     ：after_turn.fanout_committed；
    - requires ：after_turn.build_committed + turn:committed；
    - produces ：无（旁路广播，不产数据槽）；
    - run      ：await bus.fanout(committed)。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_turn.fanout_committed"
    requires = ("after_turn.build_committed", _TURN_COMMITTED_SLOT)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        committed = cast(TurnCommitted, frame.slots[_TURN_COMMITTED_SLOT])
        await self._bus.fanout(committed)
        return frame


class _BuildAfterTurnCtxModule:
    """build_ctx：组装 AfterTurnCtx（@on_after_turn 插件的 TAP 快照）。

    - slot     ：after_turn.build_ctx；
    - requires ：after_turn.fanout_committed；
    - produces ：turn:ctx；
    - run      ：从 TurnSnapshot 组装 AfterTurnCtx——reply / tools_used / thinking 是快照
      内容，will_dispatch 标记是否随后派发（Tap handler 运行时 dispatch 尚未发生）。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_turn.build_ctx"
    requires = ("after_turn.fanout_committed",)
    produces = (_CTX_SLOT,)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        snap = frame.input
        state = snap.state
        frame.slots[_CTX_SLOT] = AfterTurnCtx(
            session_key=state.session_key,
            channel=snap.outbound.channel,
            chat_id=snap.outbound.chat_id,
            reply=snap.outbound.content,
            tools_used=snap.ctx.tools_used,
            thinking=snap.ctx.thinking,
            will_dispatch=state.dispatch_outbound,
        )
        return frame


class _FanoutAfterTurnCtxModule:
    """fanout_ctx：广播 AfterTurnCtx 给 @on_after_turn 插件（TAP 只读）。

    - slot     ：after_turn.fanout_ctx；
    - requires ：after_turn.build_ctx + turn:ctx；
    - produces ：无（旁路广播，不产数据槽）；
    - run      ：await bus.fanout(ctx)。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_turn.fanout_ctx"
    requires = ("after_turn.build_ctx", _CTX_SLOT)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        await self._bus.fanout(cast(AfterTurnCtx, frame.slots[_CTX_SLOT]))
        return frame


class _DispatchOutboundModule:
    """dispatch：把 outbound 真正派发出去。

    - slot     ：after_turn.dispatch；
    - requires ：after_turn.fanout_ctx；
    - produces ：无（副作用，直接调 OutboundPort.dispatch）；
    - run      ：若 state.dispatch_outbound，把 OutboundMessage 投影为 OutboundDispatch
      交给 outbound 端口派发。

    这是「派发 outbound」的落点。本模块不设 frame.output——output 由链尾的 return 产出。
    """

    slot = "after_turn.dispatch"
    requires = ("after_turn.fanout_ctx",)

    def __init__(self, outbound: OutboundPort) -> None:
        self._outbound = outbound

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        snap = frame.input
        outbound = snap.outbound
        if snap.state.dispatch_outbound:
            _ = await self._outbound.dispatch(
                OutboundDispatch(
                    channel=outbound.channel,
                    chat_id=outbound.chat_id,
                    content=outbound.content,
                    thinking=outbound.thinking,
                    metadata=outbound.metadata,
                    media=outbound.media,
                    session_message_id=outbound.session_message_id,
                    control_turn_id=outbound.control_turn_id,
                )
            )
        return frame


class _ReturnOutboundMessageModule:
    """return：把 outbound 设为阶段 output（整个 turn pipeline 的最终产物）。

    - slot     ：after_turn.return；
    - requires ：after_turn.dispatch；
    - run      ：frame.output = frame.input.outbound。

    本模块是 after_turn 的链尾，也是 7 阶段生命周期的终点——output 是 OutboundMessage，
    turn_pipeline.run() 最终返回它。
    """

    slot = "after_turn.return"
    requires = ("after_turn.dispatch",)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        frame.output = frame.input.outbound
        return frame


def default_after_turn_modules(
    bus: EventBus,
    outbound: OutboundPort,
    plugin_modules: AfterTurnModules | None = None,
) -> AfterTurnModules:
    """装配 after_turn 的内置模块链（build_committed → fanout_committed → build_ctx → fanout_ctx → dispatch → return）。"""
    builtins: AfterTurnModules = [
        _BuildTurnCommittedModule(),
        _FanoutTurnCommittedModule(bus),
        _BuildAfterTurnCtxModule(),
        _FanoutAfterTurnCtxModule(bus),
        _DispatchOutboundModule(outbound),
        _ReturnOutboundMessageModule(),
    ]
    return cast(
        AfterTurnModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )
