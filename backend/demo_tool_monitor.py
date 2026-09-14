"""工具监控验证：确认 @on_tool_call / @on_tool_result 发射点已接通。

跑一次含工具调用的 turn，验证：
- @on_tool_call 在工具执行前触发（能拿到 tool_name + arguments）；
- @on_tool_result 在工具执行后触发（能拿到 result + status）；
- @on_after_turn 汇总本轮所有工具调用。

运行（项目根目录）：
    PYTHONPATH=backend <venv python> backend/demo_tool_monitor.py
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
from agent.lifecycle.types import (  # noqa: E402
    AfterToolResultCtx,
    AfterTurnCtx,
    BeforeToolCallCtx,
)
from agent.plugins import Plugin, on_after_turn, on_tool_call, on_tool_result, tool  # noqa: E402
from agent.plugins.loader import load_plugin  # noqa: E402
from agent.tools.registry import ToolRegistry  # noqa: E402
from bus.events import InboundMessage  # noqa: E402


def _msg(text: str = "查价格") -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender="user",
        chat_id="c1",
        content=text,
        timestamp=datetime.now(timezone.utc),
    )


class ToolMonitorPlugin(Plugin):
    """带 @tool + @on_tool_call + @on_tool_result + @on_after_turn 的监控插件。"""

    name = "tool-monitor"

    def __init__(self) -> None:
        self._calls: dict[str, list[dict]] = {}

    # 声明一个工具，让监控有东西可监控
    @tool("lookup_price", risk="read-only")
    async def lookup_price(self, event: object, item: str) -> str:
        """查询商品价格。:param item: 商品名"""
        return {"apple": "3元", "banana": "2元"}.get(item, "未知商品")

    @on_tool_call()
    async def on_call(self, event: BeforeToolCallCtx) -> None:
        self._calls.setdefault(event.session_key, []).append(
            {"tool": event.tool_name, "args": dict(event.arguments), "result": None}
        )
        print(f"  [on_tool_call] 调用前  {event.tool_name}{event.arguments}")

    @on_tool_result()
    async def on_result(self, event: AfterToolResultCtx) -> None:
        for record in reversed(self._calls.get(event.session_key, [])):
            if record["tool"] == event.tool_name and record["result"] is None:
                record["result"] = event.result
                break
        print(f"  [on_tool_result] 调用后  {event.tool_name} -> {event.status} {event.result!r}")

    @on_after_turn()
    async def summarize(self, event: AfterTurnCtx) -> None:
        calls = self._calls.pop(event.session_key, [])
        print(f"\n  ── 本轮共 {len(calls)} 次工具调用 ──")
        for i, r in enumerate(calls, 1):
            print(f"  {i}. {r['tool']}{r['args']} -> {r['result']}")


async def main() -> None:
    # stub LLM：第 1 次调用 lookup_price 工具，第 2 次给最终回复
    state = {"n": 0}

    async def llm(messages: Any, schemas: Any) -> LLMResponse:
        state["n"] += 1
        if state["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[{"name": "lookup_price", "arguments": {"item": "apple"}}],
            )
        return LLMResponse(content="apple 的价格是 3元")

    plugin = ToolMonitorPlugin()
    tools = ToolRegistry()
    wiring = build_wiring(llm, tools=tools)
    handlers, tools_loaded = load_plugin(wiring.bus, tools, plugin)
    print(f"装配: handler={len(handlers)} 个, tool={len(tools_loaded)} 个\n")

    out = await wiring.pipeline.run(_msg("查一下 apple 的价格"), "cli:c1")
    print(f"\n最终回复: {out.content!r}")


if __name__ == "__main__":
    asyncio.run(main())
