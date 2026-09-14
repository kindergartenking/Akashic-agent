"""插件探针 demo：跑一次真实 turn，用 LifecycleProbePlugin 观察 7 阶段数据流。

不依赖真实 LLM / DB。分两段演示：
- Part 1：探针插件纯观察一次含工具调用的 turn，展示 7 阶段顺序 + GATE/TAP 区别。
- Part 2：额外挂一个 after_reasoning 的 GATE handler 改写 reply，展示「GATE 能改
  最终结果，TAP 不能」——同一份数据流，插件改写后 OutboundMessage 跟着变。

运行（项目根目录）：
    PYTHONPATH=backend <venv python> backend/demo_plugin_probe.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.core.reasoner import LLMResponse  # noqa: E402
from agent.core.wiring import build_wiring  # noqa: E402
from agent.lifecycle.phase import topo_sort_modules  # noqa: E402
from agent.lifecycle.phases.before_turn import default_before_turn_modules  # noqa: E402
from agent.lifecycle.types import AfterReasoningCtx, AfterStepCtx, BeforeReasoningCtx  # noqa: E402
from agent.plugins.examples.audit_module import AuditModule  # noqa: E402
from agent.plugins.examples.lifecycle_probe import LifecycleProbePlugin  # noqa: E402
from agent.plugins.loader import load_lifecycle_handlers, load_plugin  # noqa: E402
from agent.tools.base import Tool  # noqa: E402
from agent.tools.registry import ToolRegistry  # noqa: E402
from bus.event_bus import EventBus  # noqa: E402
from bus.events import InboundMessage  # noqa: E402
from session.manager import SessionManager  # noqa: E402


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


def _msg(text: str = "你好") -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender="user",
        chat_id="c1",
        content=text,
        timestamp=datetime.now(timezone.utc),
    )


def _make_stub_llm():
    """注入 stub LLM：第 1 次发起一次工具调用，第 2 次给出最终回复。

    这样一次 turn 里 before_step/after_step 会循环一次，能观察 loop 层。
    """
    state = {"n": 0}

    async def stub_llm(messages: Any, schemas: Any) -> LLMResponse:
        state["n"] += 1
        if state["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[{"name": "echo", "arguments": {"x": "hello"}}],
            )
        return LLMResponse(content="你好，我已回显", thinking="短暂思考")

    return stub_llm


async def part1_observe() -> None:
    print("=" * 60)
    print("Part 1：探针插件观察一次 turn（含一次工具调用）")
    print("=" * 60)

    tools = ToolRegistry()
    tools.register(EchoTool())
    wiring = build_wiring(_make_stub_llm(), tools=tools)

    probe = LifecycleProbePlugin()
    loaded = load_lifecycle_handlers(wiring.bus, probe)
    print(f"已挂载探针插件 handler：{len(loaded)} 个\n")

    out = await wiring.pipeline.run(_msg(), "cli:c1")
    print(f"\n最终 OutboundMessage.content = {out.content!r}")


async def part2_gate_mutate() -> None:
    print("\n" + "=" * 60)
    print("Part 2：GATE 能改最终结果（after_reasoning 改写 reply）")
    print("=" * 60)

    tools = ToolRegistry()
    tools.register(EchoTool())
    wiring = build_wiring(_make_stub_llm(), tools=tools)

    probe = LifecycleProbePlugin()
    load_lifecycle_handlers(wiring.bus, probe)

    # 额外挂一个 GATE handler：在 after_reasoning 给 reply 加后缀。
    # GATE 由 bus.emit 串行调用，返回新 ctx 会替换原 ctx，影响后续阶段。
    async def reply_suffix(ctx: AfterReasoningCtx) -> AfterReasoningCtx:
        ctx.reply += " 【已被插件改写】"
        return ctx

    wiring.bus.on(AfterReasoningCtx, reply_suffix)

    print("\n（after_reasoning 上挂了 reply_suffix GATE handler）\n")
    out = await wiring.pipeline.run(_msg(), "cli:c1")
    print(f"\n最终 OutboundMessage.content = {out.content!r}")


async def part3_module() -> None:
    print("\n" + "=" * 60)
    print("Part 3：加模块（AuditModule 注入 before_turn 模块链）")
    print("=" * 60)

    # 先展示注入后模块链的拓扑排序结果（plugin 模块插在 build_ctx 与 emit 之间）
    chain = default_before_turn_modules(
        EventBus(), SessionManager(), plugin_modules=[AuditModule()]
    )
    order = [str(m.slot) for m in topo_sort_modules(chain)]
    print(f"\nbefore_turn 模块链排序:\n  {' → '.join(order)}\n")

    # 真实跑一次 turn，同时挂探针 handler（对比：模块在 emit 之前跑，handler 在 emit 时跑）
    tools = ToolRegistry()
    tools.register(EchoTool())
    wiring = build_wiring(
        _make_stub_llm(),
        tools=tools,
        before_turn_plugin_modules=[AuditModule()],
    )
    probe = LifecycleProbePlugin()
    load_lifecycle_handlers(wiring.bus, probe)

    out = await wiring.pipeline.run(_msg(), "cli:c1")
    print(f"\n最终 OutboundMessage.content = {out.content!r}")


async def part4_hybrid() -> None:
    print("\n" + "=" * 60)
    print("Part 4：一个插件同时贡献 handler + module")
    print("=" * 60)

    from agent.plugins.examples.hybrid_plugin import HybridPlugin

    plugin = HybridPlugin()
    tools = ToolRegistry()
    tools.register(EchoTool())

    # 装载顺序：先 build_wiring（传模块），再 load handler
    wiring = build_wiring(
        _make_stub_llm(),
        tools=tools,
        before_turn_plugin_modules=plugin.before_turn_modules(),
    )
    load_lifecycle_handlers(wiring.bus, plugin)

    # 验证 handler 注入的 skill 是否流到了 before_reasoning
    async def check_skill(ctx: BeforeReasoningCtx) -> None:
        print(f"  [验证] before_reasoning.skill_names = {ctx.skill_names}")

    wiring.bus.on(BeforeReasoningCtx, check_skill)

    out = await wiring.pipeline.run(_msg(), "cli:c1")
    print(f"\n最终 OutboundMessage.content = {out.content!r}")


async def part5_skill_tool() -> None:
    print("\n" + "=" * 60)
    print("Part 5：带 skill + tool 的插件装配")
    print("=" * 60)

    from agent.plugins.examples.skillful_plugin import SkillfulPlugin

    plugin = SkillfulPlugin()
    print(f"\n① skill_roots() 声明: {plugin.skill_roots()}  （复刻仅接口占位，无扫描加载）")

    tools = ToolRegistry()
    tools.register(EchoTool())

    # stub LLM：第 1 次调用 lookup_price 工具，第 2 次给最终回复
    state = {"n": 0}

    async def skill_llm(messages: Any, schemas: Any) -> LLMResponse:
        state["n"] += 1
        if state["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[{"name": "lookup_price", "arguments": {"item": "apple"}}],
            )
        return LLMResponse(content="apple 的价格是 3元")

    wiring = build_wiring(skill_llm, tools=tools)
    handlers, tools_loaded = load_plugin(wiring.bus, tools, plugin)
    print(f"\n② load_plugin 装配: handler={len(handlers)} 个, tool={len(tools_loaded)} 个")
    print(f"   ToolRegistry 现在注册的工具: {sorted(tools.get_registered_names())}")

    # 验证 skill_names 流转
    async def check_skill(ctx: BeforeReasoningCtx) -> None:
        print(f"③ [验证] before_reasoning.skill_names = {ctx.skill_names}")

    # 验证工具被 LLM 调用
    async def check_tool(ctx: AfterStepCtx) -> None:
        print(f"④ [验证] after_step.tools_called = {ctx.tools_called}")

    wiring.bus.on(BeforeReasoningCtx, check_skill)
    wiring.bus.on(AfterStepCtx, check_tool)

    print("\n跑 turn（LLM 会调用 lookup_price 工具）...\n")
    out = await wiring.pipeline.run(_msg("查一下 apple 的价格"), "cli:c1")
    print(f"\n最终 OutboundMessage.content = {out.content!r}")


async def main() -> None:
    await part1_observe()
    await part2_gate_mutate()
    await part3_module()
    await part4_hybrid()
    await part5_skill_tool()
    print("\n演示结束。")


if __name__ == "__main__":
    asyncio.run(main())
