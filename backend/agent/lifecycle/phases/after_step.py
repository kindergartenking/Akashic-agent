"""after_step 阶段（5 实例已完整）。

after_step 是 7 阶段生命周期的第五个阶段（loop 层 · TAP），位于 before_step 之后、
after_reasoning 之前，且**每轮循环跑一次**（reasoner 每轮工具调用结束后过它）。
完整职责是「每一轮推理结束后，把本轮快照（AfterStepCtx）并发广播给所有观察者」。

  copy_input → collect_pre → fanout → collect_post → return

  · copy_input —— 把 input 快照（AfterStepCtx）原样放进 step:ctx（TAP 不组装新 ctx）。
  · collect_pre —— fanout 前收集插件已外溢的 telemetry（供观察者读取）。
  · fanout —— bus.fanout(ctx) 并发广播给所有 @on_after_step / @on_any 观察者。
  · collect_post —— fanout 后收集插件新补充的 telemetry，带回返回 ctx。
  · return —— 把（可能被 replace 补过 telemetry 的）AfterStepCtx 设为阶段 output。

本阶段三个独特之处：
1. 是 TAP 而非 GATE——没有 emit，用 fanout（返回值丢弃、并发、只读），插件不能改写
   ctx，只能旁路观察本轮快照。
2. input 本身就是 AfterStepCtx（frozen 快照），所以没有 build_ctx，只有 copy_input；
   需要补 telemetry 时用 dataclasses.replace 生成新实例写回 step:ctx，而不是原地改字段。
3. collect 模块被实例化两次（collect_pre / collect_post）夹住 fanout——fanout 前的
   telemetry 给观察者读，fanout 后的补充带回返回 ctx；用 collected 集合防止
   collect_post 覆盖观察者在 collect_pre 阶段已看到的同名 telemetry。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TypeAlias, cast

from bus.event_bus import EventBus
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModule,
    collect_prefixed_slots,
    topo_sort_modules,
)
from agent.lifecycle.types import AfterStepCtx


@dataclass
class AfterStepFrame(PhaseFrame[AfterStepCtx, AfterStepCtx]):
    """after_step 的帧：input = AfterStepCtx（快照），output = AfterStepCtx（补过 metadata）。"""

    pass


AfterStepModules: TypeAlias = list[PhaseModule[AfterStepFrame]]


_CTX_SLOT = "step:ctx"
_TELEMETRY_PREFIX = "step:telemetry:"
_COLLECTED_TELEMETRY_SLOT = "step:telemetry_collected"
_EARLY_STOP_REASON_SLOT = "step:early_stop_reason"


class _CopyInputToCtxModule:
    """copy_input：把 input 快照原样放进 step:ctx。

    - slot     ：after_step.copy_input；
    - requires ：无依赖（链上第一个模块）；
    - produces ：step:ctx；
    - run      ：frame.slots[step:ctx] = frame.input。

    TAP 阶段不「组装」ctx——input 本身就是 reasoner 构造好的 AfterStepCtx 快照，
    这里只是把它搬进数据槽，供后续 collect / fanout 使用。这是它与 GATE 阶段
    （都有 build_ctx 组装新 ctx）最直观的区别。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_step.copy_input"
    requires: tuple[str, ...] = ()
    produces = (_CTX_SLOT,)

    async def run(self, frame: AfterStepFrame) -> AfterStepFrame:
        frame.slots[_CTX_SLOT] = frame.input
        return frame


class _FanoutAfterStepCtxModule:
    """fanout：并发广播 AfterStepCtx 给所有观察者（TAP 语义的核心）。

    - slot     ：after_step.fanout；
    - requires ：after_step.collect_pre + step:ctx；
    - produces ：无（旁路观察，不产数据槽）；
    - run      ：await bus.fanout(frame.slots[step:ctx])。

    fanout 按 type(ctx) 查表 _handlers_for()，用 asyncio.gather 并发执行所有
    @on_after_step / @on_any 观察者；返回值丢弃、单个失败只计数不中断主流程。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_step.fanout"
    requires = ("after_step.collect_pre", _CTX_SLOT)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: AfterStepFrame) -> AfterStepFrame:
        ctx = cast(AfterStepCtx, frame.slots[_CTX_SLOT])
        await self._bus.fanout(ctx)
        return frame


class _CollectAfterStepExportSlotsModule:
    """collect：回收 step:telemetry: 前缀的槽，合并进 ctx.extra_metadata。

    - slot / requires ：由实例注入（见 default_after_step_modules 的两次实例化）；
    - produces ：step:ctx；
    - run      ：收集 step:telemetry: 前缀的槽 → ctx.extra_metadata；若有
      step:early_stop_reason 则置 early_stop=True。

    本模块被实例化两次，夹住 fanout：
    - collect_pre（fanout 前）：把插件已外溢的 telemetry 合并进 extra_metadata，
      让观察者能看到本轮 telemetry；
    - collect_post（fanout 后）：把 after_fanout 期间插件新补充的 telemetry 带回来。

    因为 AfterStepCtx 是 frozen，不能原地改字段，必须用 dataclasses.replace 生成
    新实例写回 step:ctx；同时用 collected 集合记录已合并过的 key，防止 collect_post
    覆盖观察者在 collect_pre 阶段已看到的同名 telemetry。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    produces = (_CTX_SLOT,)

    # 同一个模块类要在 fanout 前后各实例化一次，所以 slot / requires 由实例注入。
    def __init__(self, *, slot: str, requires: tuple[str, ...]) -> None:
        self.slot = slot
        self.requires = requires

    async def run(self, frame: AfterStepFrame) -> AfterStepFrame:
        ctx = cast(AfterStepCtx, frame.slots[_CTX_SLOT])
        collected = set(cast(set[str], frame.slots.get(_COLLECTED_TELEMETRY_SLOT, set())))
        exports = collect_prefixed_slots(frame.slots, _TELEMETRY_PREFIX)
        # after_fanout 可补充 telemetry，但不能覆盖 fanout handler 已看到的同名值。
        new_exports = {
            key: value
            for key, value in exports.items()
            if key not in collected
        }
        extra_metadata = dict(ctx.extra_metadata)
        extra_metadata.update(new_exports)
        early_stop_reason = frame.slots.get(_EARLY_STOP_REASON_SLOT)
        if isinstance(early_stop_reason, str) and early_stop_reason.strip():
            frame.slots[_CTX_SLOT] = replace(
                ctx,
                early_stop=True,
                early_stop_reason=early_stop_reason.strip(),
                extra_metadata=extra_metadata,
            )
        else:
            frame.slots[_CTX_SLOT] = replace(ctx, extra_metadata=extra_metadata)
        frame.slots[_COLLECTED_TELEMETRY_SLOT] = collected | set(new_exports)
        return frame


class _ReturnAfterStepCtxModule:
    """return：把链上最终 ctx（可能被 replace 补过 telemetry）设为阶段 output。

    - slot     ：after_step.return；
    - requires ：after_step.collect_post + step:ctx（保证排在最后）；
    - run      ：frame.output = frame.slots[step:ctx]。

    本模块是 after_step 的链尾，也是「output 职责」的最终归宿——之前的 copy_input /
    collect_pre / fanout / collect_post 都不碰 output。
    """

    slot = "after_step.return"
    requires = ("after_step.collect_post", _CTX_SLOT)

    async def run(self, frame: AfterStepFrame) -> AfterStepFrame:
        frame.output = cast(AfterStepCtx, frame.slots[_CTX_SLOT])
        return frame


def default_after_step_modules(
    bus: EventBus,
    plugin_modules: AfterStepModules | None = None,
) -> AfterStepModules:
    """装配 after_step 的内置模块链（copy_input → collect_pre → fanout → collect_post → return）。"""
    builtins: AfterStepModules = [
        _CopyInputToCtxModule(),
        # collect 两次：fanout 前给 handler 读，fanout 后把 after_fanout 的补充带回返回 ctx。
        _CollectAfterStepExportSlotsModule(
            slot="after_step.collect_pre",
            requires=("after_step.copy_input", _CTX_SLOT),
        ),
        _FanoutAfterStepCtxModule(bus),
        _CollectAfterStepExportSlotsModule(
            slot="after_step.collect_post",
            requires=("after_step.fanout", _CTX_SLOT),
        ),
        _ReturnAfterStepCtxModule(),
    ]
    return cast(
        AfterStepModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )
