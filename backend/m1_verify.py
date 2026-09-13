"""M1 最小验证脚本：生命周期编排框架。

验证：
1. TurnLifecycle 门面把 7 个阶段映射到正确的 Ctx 类型（5 GATE + 2 TAP）
2. GATE 阶段经 emit 可改写 ctx；TAP 阶段经 observe 只读不打断
3. Phase 管道：slot/requires/produces 拓扑排序 + 循环依赖检测 + run 产出 output
4. 7 个 turn 级 Ctx + 3 个 tool 级 Ctx 契约存在
5. 合成 turn 贯穿 7 阶段（GATE 可中断/改写，TAP 旁路观察）

运行方式（项目根目录）：
    <venv python> backend/m1_verify.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.lifecycle import Phase, PhaseFrame, TurnLifecycle  # noqa: E402
from agent.lifecycle.phase import topo_sort_modules  # noqa: E402
from agent.lifecycle.types import (  # noqa: E402
    AfterReasoningCtx,
    AfterStepCtx,
    AfterToolResultCtx,
    AfterTurnCtx,
    BeforeReasoningCtx,
    BeforeStepCtx,
    BeforeToolCallCtx,
    BeforeTurnCtx,
    PreToolCtx,
    PromptRenderCtx,
)
from agent.prompting import PromptSectionRender  # noqa: E402
from bus.event_bus import EventBus  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


NOW = datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# 1：TurnLifecycle 门面 + GATE/TAP 语义
# ─────────────────────────────────────────────────────────────────────────────
section("1. TurnLifecycle 门面（7 阶段 GATE/TAP）")


async def _test_facade() -> None:
    bus = EventBus()
    lc = TurnLifecycle(bus)

    taps: list[str] = []

    # GATE：before_turn 可改写（设置 abort）
    def gate_bt(ctx: BeforeTurnCtx) -> BeforeTurnCtx:
        ctx.abort = True
        ctx.abort_reply = "blocked"
        return ctx

    # TAP：after_step 只观察
    async def tap_as(ctx: AfterStepCtx) -> None:
        taps.append(f"after_step:{ctx.iteration}")

    lc.on_before_turn(gate_bt)
    lc.on_after_step(tap_as)

    bt = BeforeTurnCtx(
        session_key="s", channel="cli", chat_id="c", content="hi",
        timestamp=NOW, retrieved_memory_block="", history_messages=(),
    )
    out = await bus.emit(bt)
    check("before_turn 走 GATE：emit 返回被改写的 ctx（abort=True）", out.abort is True and out.abort_reply == "blocked", f"abort={out.abort}")

    ac = AfterStepCtx(
        session_key="s", channel="cli", chat_id="c", iteration=0,
        context_tokens_estimate=100, tools_called=(), partial_reply="",
        tools_used_so_far=(), tool_chain_partial=(), partial_thinking=None,
        has_more=True,
    )
    await bus.observe(ac)
    check("after_step 走 TAP：observe 只观察", taps == ["after_step:0"], f"got={taps!r}")


asyncio.run(_test_facade())


# ─────────────────────────────────────────────────────────────────────────────
# 2：Phase 管道（topo 排序 + 循环依赖 + output）
# ─────────────────────────────────────────────────────────────────────────────
section("2. Phase 管道（slot/requires/produces）")


@dataclass
class CalcFrame(PhaseFrame[int, int]):
    pass


class InitModule:
    slot = "calc.init"
    produces = ("calc:value",)

    async def run(self, frame: CalcFrame) -> CalcFrame:
        frame.slots["calc:value"] = frame.input
        return frame


class DoubleModule:
    slot = "calc.double"
    requires = ("calc.init",)
    produces = ("calc:value",)

    async def run(self, frame: CalcFrame) -> CalcFrame:
        frame.slots["calc:value"] = frame.slots["calc:value"] * 2
        return frame


class ReturnModule:
    slot = "calc.return"
    requires = ("calc.double",)

    async def run(self, frame: CalcFrame) -> CalcFrame:
        frame.output = frame.slots["calc:value"]
        return frame


async def _test_phase() -> None:
    # 2.1 打乱顺序传入，topo_sort_modules 应恢复依赖序
    scrambled = [ReturnModule(), InitModule(), DoubleModule()]
    ordered = topo_sort_modules(scrambled)
    slots = [getattr(m, "slot") for m in ordered]
    check("topo 排序按 requires 恢复依赖序", slots == ["calc.init", "calc.double", "calc.return"], f"got={slots!r}")

    # 2.2 Phase.run 产出 output
    phase = Phase(ordered, frame_factory=lambda i: CalcFrame(input=i))
    result = await phase.run(21)
    check("Phase.run 顺序执行模块并产出 output（21→42）", result == 42, f"got={result!r}")

    # 2.3 循环依赖应抛错
    class A:
        slot = "cyc.a"
        requires = ("cyc.b",)

        async def run(self, frame: CalcFrame) -> CalcFrame:
            return frame

    class B:
        slot = "cyc.b"
        requires = ("cyc.a",)

        async def run(self, frame: CalcFrame) -> CalcFrame:
            return frame

    try:
        topo_sort_modules([A(), B()])
        check("循环依赖抛 RuntimeError", False)
    except RuntimeError as exc:
        check("循环依赖抛 RuntimeError", "循环依赖" in str(exc))


asyncio.run(_test_phase())


# ─────────────────────────────────────────────────────────────────────────────
# 3：7+3 Context 契约存在性
# ─────────────────────────────────────────────────────────────────────────────
section("3. Context 契约（7 turn 级 + 3 tool 级）")

turn_ctxs = [
    BeforeTurnCtx, BeforeReasoningCtx, PromptRenderCtx, BeforeStepCtx,
    AfterStepCtx, AfterReasoningCtx, AfterTurnCtx,
]
tool_ctxs = [BeforeToolCallCtx, AfterToolResultCtx, PreToolCtx]

check("7 个 turn 级 Ctx 类型齐全", len(turn_ctxs) == 7)
check("3 个 tool 级 Ctx 类型齐全", len(tool_ctxs) == 3)


# ─────────────────────────────────────────────────────────────────────────────
# 4：合成 turn 贯穿 7 阶段
# ─────────────────────────────────────────────────────────────────────────────
section("4. 合成 turn 贯穿 7 阶段（GATE 中断/改写 + TAP 旁路）")


async def _test_full_turn() -> None:
    bus = EventBus()
    lc = TurnLifecycle(bus)
    log: list[str] = []

    # before_turn (GATE)：注入 skill 名
    def h_before_turn(ctx: BeforeTurnCtx) -> BeforeTurnCtx:
        log.append("before_turn")
        ctx.skill_names.append("weather")
        return ctx

    # before_reasoning (GATE)：无改动
    def h_before_reasoning(ctx: BeforeReasoningCtx) -> BeforeReasoningCtx:
        log.append("before_reasoning")
        return ctx

    # prompt_render (GATE)：加一段 system section
    def h_prompt_render(ctx: PromptRenderCtx) -> PromptRenderCtx:
        log.append("prompt_render")
        ctx.system_sections_top.append(PromptSectionRender("rules", "be brief", True))
        return ctx

    # before_step (GATE)：无改动
    def h_before_step(ctx: BeforeStepCtx) -> BeforeStepCtx:
        log.append("before_step")
        return ctx

    # after_step (TAP)：观察
    def h_after_step(ctx: AfterStepCtx) -> None:
        log.append("after_step")

    # after_reasoning (GATE)：改写 reply
    def h_after_reasoning(ctx: AfterReasoningCtx) -> AfterReasoningCtx:
        log.append("after_reasoning")
        ctx.reply = ctx.reply + " [polished]"
        return ctx

    # after_turn (TAP)：观察
    def h_after_turn(ctx: AfterTurnCtx) -> None:
        log.append("after_turn")

    lc.on_before_turn(h_before_turn)
    lc.on_before_reasoning(h_before_reasoning)
    lc.on_prompt_render(h_prompt_render)
    lc.on_before_step(h_before_step)
    lc.on_after_step(h_after_step)
    lc.on_after_reasoning(h_after_reasoning)
    lc.on_after_turn(h_after_turn)

    # 驱动 turn（顺序与 Core 写死的一致）
    bt = await bus.emit(BeforeTurnCtx("s", "cli", "c", "hi", NOW, "", ()))
    check("before_turn GATE 注入 skill", bt.skill_names == ["weather"], f"got={bt.skill_names!r}")

    await bus.emit(BeforeReasoningCtx("s", "cli", "c", "hi", NOW, [], ""))

    pr = await bus.emit(PromptRenderCtx("s", "cli", "c", "hi", None, NOW, [], None, "", set(), ""))
    check("prompt_render GATE 加 system section", len(pr.system_sections_top) == 1 and pr.system_sections_top[0].name == "rules", f"got={pr.system_sections_top!r}")

    await bus.emit(BeforeStepCtx("s", "cli", "c", 0, 100, None))
    await bus.observe(AfterStepCtx("s", "cli", "c", 0, 100, ("web_search",), "", ("web_search",), (), None, True))

    ar = await bus.emit(AfterReasoningCtx("s", "cli", "c", ("web_search",), None, None, False, (), {}, "raw reply"))
    check("after_reasoning GATE 改写 reply", ar.reply == "raw reply [polished]", f"got={ar.reply!r}")

    await bus.observe(AfterTurnCtx("s", "cli", "c", ar.reply, ("web_search",), None, True))

    expected = ["before_turn", "before_reasoning", "prompt_render", "before_step", "after_step", "after_reasoning", "after_turn"]
    check("7 阶段按 Core 顺序贯穿", log == expected, f"got={log!r}")


asyncio.run(_test_full_turn())


# ─────────────────────────────────────────────────────────────────────────────
section("结果")
print(f"PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("M1 验证通过。")
