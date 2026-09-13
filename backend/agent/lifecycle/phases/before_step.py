"""before_step 阶段（5 模块已完整）。

before_step 是 7 阶段生命周期的第四个阶段（loop 层 · GATE），位于 prompt_render
之后、after_step 之前，且**每轮循环跑一次**（reasoner 的 for 循环里，每次调 LLM /
工具前先过它）。完整职责是「每一轮推理开始前，组好本轮的 ctx + 注入 hints + 可
early_stop」。

  build_ctx → emit → collect_exports → inject_hints → return

  · build_ctx —— 从 input 组装 BeforeStepCtx（含 iteration / token 估算 / 可见工具）。
  · emit —— GATE 门控，插件 handler 可改写 ctx 字段（如 extra_hints / early_stop）。
  · collect_exports —— 回收插件外溢的 extra_hint / abort_reply（映射为 early_stop）。
  · inject_hints —— 把 hints 塞进 frame.input.messages（副作用型，无 produces）。
  · return —— 把 BeforeStepCtx 设为阶段 output。

本阶段两个独特之处：
1. inject_hints 是「副作用型业务模块」——直接改 frame.input.messages，不产 slot，
   影响的是下一轮 LLM 看到的输入（这是 7 阶段里第二个副作用型模块，第一个是
   before_reasoning 的 sync_tools）。
2. 用 early_stop（提前结束当前 tool loop）而非 abort（中断整个 turn）；slot 后缀仍
   统一叫 abort_reply，内部映射为 early_stop。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias, cast

from agent.core.passive_support import (
    build_context_hint_message,
    estimate_messages_tokens,
)
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModule,
    append_string_exports,
    collect_prefixed_slots,
    topo_sort_modules,
)
from agent.lifecycle.types import BeforeStepCtx, BeforeStepInput
from bus.event_bus import EventBus


@dataclass
class BeforeStepFrame(PhaseFrame[BeforeStepInput, BeforeStepCtx]):
    """before_step 的帧：input = BeforeStepInput，output = BeforeStepCtx。"""

    pass


BeforeStepModules: TypeAlias = list[PhaseModule[BeforeStepFrame]]


_CTX_SLOT = "step:ctx"
_EXTRA_HINT_PREFIX = "step:extra_hint:"
# slot suffix 统一用 abort_reply；step 内部映射为 early_stop，只终止当前 tool loop。
_ABORT_REPLY_SLOT = "step:abort_reply"


class _BuildBeforeStepCtxModule:
    """build_ctx：从 input 组装 BeforeStepCtx。

    - slot     ：before_step.build_ctx；
    - requires ：无依赖（链上第一个模块）；
    - produces ：step:ctx；
    - run      ：把 input 的字段搬进 BeforeStepCtx，并做两处转换——
      input_tokens_estimate 用 estimate_messages_tokens 估算，visible_names 转
      frozenset（不可变，防止下游篡改可见工具集）。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "before_step.build_ctx"
    requires: tuple[str, ...] = ()
    produces = (_CTX_SLOT,)

    async def run(self, frame: BeforeStepFrame) -> BeforeStepFrame:
        input = frame.input
        frame.slots[_CTX_SLOT] = BeforeStepCtx(
            session_key=input.session_key,
            channel=input.channel,
            chat_id=input.chat_id,
            iteration=input.iteration,
            input_tokens_estimate=estimate_messages_tokens(input.messages),
            visible_tool_names=(
                frozenset(input.visible_names)
                if input.visible_names is not None
                else None
            ),
        )
        return frame


class _EmitBeforeStepCtxModule:
    """GATE 门控：把 ctx 交给 EventBus，插件 handler 可改写字段或整体替换 ctx。

    - slot     ：before_step.emit；
    - requires ：before_step.build_ctx + step:ctx；
    - produces ：step:ctx（emit 可能替换 ctx，替换后写回槽）；
    - run      ：frame.slots[step:ctx] = await bus.emit(ctx)。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "before_step.emit"
    requires = ("before_step.build_ctx", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: BeforeStepFrame) -> BeforeStepFrame:
        ctx = cast(BeforeStepCtx, frame.slots[_CTX_SLOT])
        frame.slots[_CTX_SLOT] = await self._bus.emit(ctx)
        return frame


class _CollectBeforeStepExportSlotsModule:
    """回收插件外溢：把 extra_hint / abort_reply 从 slot 合并回 ctx。

    - slot     ：before_step.collect_exports；
    - requires ：before_step.emit + step:ctx；
    - produces ：step:ctx（原地改 ctx 字段，不替换对象）；
    - run      ：收 step:extra_hint: 前缀的槽合并进 ctx.extra_hints；读 step:abort_reply
      控制槽，若有则置 ctx.early_stop + early_stop_reply（slot 后缀统一叫 abort_reply，
      内部语义映射为 early_stop——只终止当前 tool loop）。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "before_step.collect_exports"
    requires = ("before_step.emit", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    async def run(self, frame: BeforeStepFrame) -> BeforeStepFrame:
        ctx = cast(BeforeStepCtx, frame.slots[_CTX_SLOT])
        append_string_exports(
            ctx.extra_hints,
            collect_prefixed_slots(frame.slots, _EXTRA_HINT_PREFIX),
        )
        early_stop_reply = frame.slots.get(_ABORT_REPLY_SLOT)
        if isinstance(early_stop_reply, str) and early_stop_reply:
            ctx.early_stop = True
            ctx.early_stop_reply = early_stop_reply
        return frame


class _InjectHintsModule:
    """注入 hints：把 ctx.extra_hints 塞进 frame.input.messages（副作用型）。

    - slot     ：before_step.inject_hints；
    - requires ：before_step.collect_exports + step:ctx（保证排在 collect 之后）；
    - produces ：无（副作用型模块，直接改 frame.input.messages，不产数据槽）；
    - run      ：若 ctx.extra_hints 非空，用 build_context_hint_message 包成一条带
      [plugin_hints] 标记的消息，追加到 frame.input.messages 末尾。

    这是 7 阶段里第二个「副作用型模块」（第一个是 before_reasoning 的 sync_tools）：
    它不通过 slot 传递产物，而是直接改 frame.input.messages，影响下一轮 LLM 看到的
    输入。BeforeStepInput 虽 frozen，但 messages 是可变 list，append 不受 frozen 约束。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "before_step.inject_hints"
    requires = ("before_step.collect_exports", _CTX_SLOT)

    async def run(self, frame: BeforeStepFrame) -> BeforeStepFrame:
        ctx = cast(BeforeStepCtx, frame.slots[_CTX_SLOT])
        if ctx.extra_hints:
            frame.input.messages.append(
                build_context_hint_message(
                    "plugin_hints",
                    "\n".join(ctx.extra_hints),
                )
            )
        return frame


class _ReturnBeforeStepCtxModule:
    """产出 output：把链上最终 ctx（经 emit 替换 + collect 回收后）设为阶段 output。

    - slot     ：before_step.return；
    - requires ：before_step.inject_hints + step:ctx（保证排在最后）；
    - run      ：frame.output = frame.slots[step:ctx]。

    本模块是 before_step 的链尾，也是「output 职责」的最终归宿——之前的 build_ctx /
    emit / collect_exports / inject_hints 都不碰 output。
    """

    slot = "before_step.return"
    requires = ("before_step.inject_hints", _CTX_SLOT)

    async def run(self, frame: BeforeStepFrame) -> BeforeStepFrame:
        frame.output = cast(BeforeStepCtx, frame.slots[_CTX_SLOT])
        return frame


def default_before_step_modules(
    bus: EventBus,
    plugin_modules: BeforeStepModules | None = None,
) -> BeforeStepModules:
    """装配 before_step 的内置模块链（build_ctx → emit → collect_exports → inject_hints → return）。"""
    builtins: BeforeStepModules = [
        _BuildBeforeStepCtxModule(),
        _EmitBeforeStepCtxModule(bus),
        _CollectBeforeStepExportSlotsModule(),
        _InjectHintsModule(),
        _ReturnBeforeStepCtxModule(),
    ]
    return cast(
        BeforeStepModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )
