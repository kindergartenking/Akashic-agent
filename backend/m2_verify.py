"""M1b 验证：7 阶段生命周期顺序 + emit/fanout + 短路。

不依赖真实 LLM / DB。用注入 LLM + 探针插件验证 7 阶段顺序正确、GATE 可改写、
abort/early_stop 短路、TurnCommitted fanout、出站 dispatch。

运行：cd 项目根 && PYTHONPATH=backend <venv python> backend/m2_verify.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from bus.event_bus import EventBus
from bus.events import InboundMessage, TurnDisposition
from bus.events_lifecycle import TurnCommitted
from agent.core.reasoner import LLMResponse
from agent.core.wiring import build_wiring
from agent.lifecycle.types import (
    AfterReasoningCtx,
    AfterStepCtx,
    AfterTurnCtx,
    BeforeReasoningCtx,
    BeforeStepCtx,
    BeforeTurnCtx,
    PromptRenderCtx,
)
from agent.tools.base import Tool

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}")


def _msg(text: str = "hello") -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender="u",
        chat_id="c1",
        content=text,
        timestamp=datetime.now(timezone.utc),
    )


# 7 个 Ctx 类型的探针记录器（通过 bus.on 挂到每个阶段的 emit/fanout）。
def _order_probe(bus: EventBus, record: list[str]) -> None:
    def make(label: str):
        async def rec(event: Any) -> None:
            record.append(label)

        return rec

    bus.on(BeforeTurnCtx, make("before_turn"))
    bus.on(BeforeReasoningCtx, make("before_reasoning"))
    bus.on(PromptRenderCtx, make("prompt_render"))
    bus.on(BeforeStepCtx, make("before_step"))
    bus.on(AfterStepCtx, make("after_step"))
    bus.on(AfterReasoningCtx, make("after_reasoning"))
    bus.on(AfterTurnCtx, make("after_turn"))


async def _final_llm(messages: Any, schemas: Any) -> LLMResponse:
    return LLMResponse(content="这是最终回复", thinking="thought")


async def check_1_phases_build() -> None:
    print("\n[1] 7 个 phase 均可构建（default_*_modules 非空且 topo 排序无环）")
    bus = EventBus()
    from session.manager import SessionManager
    from agent.context import ContextBuilder
    from agent.tools.registry import ToolRegistry
    from agent.turns.outbound import RecordingOutboundPort
    from agent.looping.ports import SessionServices
    from agent.lifecycle.phases.before_turn import default_before_turn_modules
    from agent.lifecycle.phases.before_reasoning import default_before_reasoning_modules
    from agent.lifecycle.phases.prompt_render import default_prompt_render_modules
    from agent.lifecycle.phases.before_step import default_before_step_modules
    from agent.lifecycle.phases.after_step import default_after_step_modules
    from agent.lifecycle.phases.after_reasoning import default_after_reasoning_modules
    from agent.lifecycle.phases.after_turn import default_after_turn_modules

    sm = SessionManager()
    ctx = ContextBuilder()
    tools = ToolRegistry()
    outbound = RecordingOutboundPort()
    services = SessionServices(session_manager=sm)

    modules = [
        ("before_turn", default_before_turn_modules(bus, sm)),
        ("before_reasoning", default_before_reasoning_modules(bus, tools, sm)),
        ("prompt_render", default_prompt_render_modules(bus, ctx)),
        ("before_step", default_before_step_modules(bus)),
        ("after_step", default_after_step_modules(bus)),
        ("after_reasoning", default_after_reasoning_modules(bus, services)),
        ("after_turn", default_after_turn_modules(bus, outbound)),
    ]
    for name, chain in modules:
        check(f"  {name} 模块链非空", len(chain) > 0, f"len={len(chain)}")


async def check_2_order() -> None:
    print("\n[2] 全链路顺序 = 7 阶段固定相对顺序（含一次工具调用触发 after_step）")
    from agent.tools.registry import ToolRegistry

    class EchoTool(Tool):
        name = "echo"
        description = "回显输入"
        parameters = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": [],
        }

        async def execute(self, **kwargs: Any) -> str:
            return f"echo:{kwargs.get('x', '')}"

    state = {"n": 0}

    async def one_tool_llm(messages: Any, schemas: Any) -> LLMResponse:
        state["n"] += 1
        if state["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[{"name": "echo", "arguments": {"x": "1"}}],
            )
        return LLMResponse(content="done")

    record: list[str] = []
    tools = ToolRegistry()
    tools.register(EchoTool())
    w = build_wiring(one_tool_llm, tools=tools)
    _order_probe(w.bus, record)
    await w.pipeline.run(_msg(), "cli:c1")

    expected = [
        "before_turn",
        "before_reasoning",
        "prompt_render",
        "before_step",
        "after_step",
        "after_reasoning",
        "after_turn",
    ]

    def first_appearance(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    check("  7 阶段首次出现顺序正确", first_appearance(record) == expected, f"got={record}")
    check("  7 阶段全部出现", set(record) == set(expected), f"got={set(record)}")


async def check_3_step_loop() -> None:
    print("\n[3] before_step/after_step 按工具调用次数循环")
    from agent.tools.registry import ToolRegistry

    class EchoTool(Tool):
        name = "echo"
        description = "回显输入"
        parameters = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": [],
        }

        async def execute(self, **kwargs: Any) -> str:
            return f"echo:{kwargs.get('x', '')}"

    state = {"n": 0}

    async def tool_llm(messages: Any, schemas: Any) -> LLMResponse:
        state["n"] += 1
        if state["n"] <= 2:
            return LLMResponse(
                content="",
                tool_calls=[{"name": "echo", "arguments": {"x": "1"}}],
            )
        return LLMResponse(content="done")

    record: list[str] = []
    tools = ToolRegistry()
    tools.register(EchoTool())
    w = build_wiring(tool_llm, tools=tools)
    _order_probe(w.bus, record)
    await w.pipeline.run(_msg(), "cli:c1")

    before_step = record.count("before_step")
    after_step = record.count("after_step")
    check("  after_step 次数 == 工具调用次数(2)", after_step == 2, f"got={after_step}")
    check("  before_step 次数 == 工具调用+1(3)", before_step == 3, f"got={before_step}")


async def check_4_gate_mutate() -> None:
    print("\n[4] GATE 可改写（before_turn 改 skill_names → before_reasoning 可见）")
    seen: list[list[str]] = []

    async def gate(ctx: BeforeTurnCtx) -> BeforeTurnCtx:
        ctx.skill_names.append("probe_skill")
        return ctx

    async def capture(ctx: BeforeReasoningCtx) -> None:
        seen.append(list(ctx.skill_names))

    w = build_wiring(_final_llm)
    w.bus.on(BeforeTurnCtx, gate)
    w.bus.on(BeforeReasoningCtx, capture)
    await w.pipeline.run(_msg(), "cli:c1")
    check(
        "  before_reasoning.skill_names 含 probe_skill",
        seen and "probe_skill" in seen[0],
        f"seen={seen}",
    )


async def check_5_abort_short_circuit() -> None:
    print("\n[5] abort 短路（before_turn abort → 不触发 reasoning）")
    record: list[str] = []

    async def abort(ctx: BeforeTurnCtx) -> BeforeTurnCtx:
        ctx.abort = True
        ctx.abort_reply = "ABORTED"
        return ctx

    w = build_wiring(_final_llm)
    _order_probe(w.bus, record)
    w.bus.on(BeforeTurnCtx, abort)
    out = await w.pipeline.run(_msg(), "cli:c1")
    check("  返回 abort_reply", out.content == "ABORTED", f"got={out.content!r}")
    check(
        "  turn_disposition == SHORT_CIRCUITED",
        out.turn_disposition is TurnDisposition.SHORT_CIRCUITED,
        f"got={out.turn_disposition}",
    )
    check(
        "  未进入 before_reasoning",
        "before_reasoning" not in record,
        f"record={record}",
    )


async def check_6_early_stop() -> None:
    print("\n[6] early_stop 短路（before_step → 只终止 tool loop，仍走 after 阶段）")
    record: list[str] = []

    async def early(ctx: BeforeStepCtx) -> BeforeStepCtx:
        ctx.early_stop = True
        ctx.early_stop_reply = "STOP"
        return ctx

    w = build_wiring(_final_llm)
    _order_probe(w.bus, record)
    w.bus.on(BeforeStepCtx, early)
    out = await w.pipeline.run(_msg(), "cli:c1")
    check("  返回 early_stop_reply", out.content == "STOP", f"got={out.content!r}")
    check("  after_reasoning 仍触发", "after_reasoning" in record, f"record={record}")
    check("  after_turn 仍触发", "after_turn" in record, f"record={record}")


async def check_7_turn_committed_fanout() -> None:
    print("\n[7] after_turn 内 TurnCommitted fanout 一次")
    committed: list[TurnCommitted] = []

    async def tap(event: TurnCommitted) -> None:
        committed.append(event)

    w = build_wiring(_final_llm)
    w.bus.on(TurnCommitted, tap)
    await w.pipeline.run(_msg(), "cli:c1")
    check("  TurnCommitted 触发一次", len(committed) == 1, f"count={len(committed)}")
    if committed:
        check(
            "  assistant_response 正确",
            committed[0].assistant_response == "这是最终回复",
            f"got={committed[0].assistant_response!r}",
        )


async def check_8_dispatch() -> None:
    print("\n[8] 出站 dispatch 一次且 content == reply")
    from agent.turns.outbound import RecordingOutboundPort

    outbound = RecordingOutboundPort()
    w = build_wiring(_final_llm, outbound=outbound)
    await w.pipeline.run(_msg(), "cli:c1")
    check("  dispatch 一次", len(outbound.dispatches) == 1, f"count={len(outbound.dispatches)}")
    if outbound.dispatches:
        check(
            "  content == reply",
            outbound.dispatches[0].content == "这是最终回复",
            f"got={outbound.dispatches[0].content!r}",
        )


async def main() -> None:
    await check_1_phases_build()
    await check_2_order()
    await check_3_step_loop()
    await check_4_gate_mutate()
    await check_5_abort_short_circuit()
    await check_6_early_stop()
    await check_7_turn_committed_fanout()
    await check_8_dispatch()

    print(f"\n结果: PASS={len(PASS)} FAIL={len(FAIL)}")
    if FAIL:
        print("失败项:", ", ".join(FAIL))
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
