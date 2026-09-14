"""记忆助手插件 demo：端到端跑三轮 turn，验证四条通道 + 跨 turn 持久化。

分三轮，每轮聚焦一条通道（store 跨轮共享，模拟真实记忆累积）：

- Turn 1（自动提取通道）：用户说「我喜欢喝美式咖啡」。LLM 不调工具，直接回复。
  → after_reasoning 的 _ExtractMemoryModule 用二次 LLM 从回复里提取出
    preference，写库。after_turn 广播 +1。
- Turn 2（remember 工具通道）：用户说「我还是一名软件工程师」。LLM 主动调
    remember(content, category="fact") 工具写库。after_turn 广播 +1。
- Turn 3（注入 + recall 通道）：用户问「我之前说过我喜欢什么、职业是什么」。
  → before_turn 注入记忆块（前两轮累计 2 条），LLM 调 recall(query) 检索，
    答出两条记忆，证明跨 turn 持久化生效。

运行（项目根目录）：
    PYTHONPATH=backend <venv python> backend/demo_memory_assistant.py
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
from agent.plugins.examples.memory_assistant import MemoryHelperPlugin  # noqa: E402
from agent.plugins.loader import load_plugin  # noqa: E402
from agent.tools.registry import ToolRegistry  # noqa: E402
from bus.events import InboundMessage  # noqa: E402

# 记忆库 JSON 文件（项目内 data 目录，便于查看）
STORE_PATH = BACKEND_DIR / "data" / "memory_demo.json"


def _msg(text: str) -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender="user",
        chat_id="c1",
        content=text,
        timestamp=datetime.now(timezone.utc),
    )


# ── 二次 LLM：从回复提取结构化记忆 ──────────────────────────────
# 真实场景这里接 LLM 做 NER/关系抽取；demo 用脚本按回复内容返回 JSON。


def _make_extractor() -> Any:
    async def extractor(messages: Any, schemas: Any) -> LLMResponse:
        # messages[-1] 是提取 prompt，内含回复文本；这里按关键字回脚本 JSON
        text = str(messages[-1].get("content", ""))
        if "美式咖啡" in text:
            return LLMResponse(
                content='[{"content": "用户喜欢喝美式咖啡", "category": "preference"}]'
            )
        return LLMResponse(content="[]")

    return extractor


# ── 主 LLM：三轮各自的脚本 ──────────────────────────────────────


def _turn1_llm() -> Any:
    """不调工具，直接回复（让提取模块去自动提取）。"""

    async def llm(messages: Any, schemas: Any) -> LLMResponse:
        return LLMResponse(content="当然，我记住了：你喜欢喝美式咖啡。")

    return llm


def _turn2_llm() -> Any:
    """第 1 次调 remember 工具，第 2 次给最终回复。"""
    state = {"n": 0}

    async def llm(messages: Any, schemas: Any) -> LLMResponse:
        state["n"] += 1
        if state["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    {
                        "name": "remember",
                        "arguments": {
                            "content": "用户是软件工程师",
                            "category": "fact",
                        },
                    }
                ],
            )
        return LLMResponse(content="好的，已记录你是软件工程师。")

    return llm


def _turn3_llm() -> Any:
    """第 1 次调 recall 检索，第 2 次根据结果作答。"""
    state = {"n": 0}

    async def llm(messages: Any, schemas: Any) -> LLMResponse:
        state["n"] += 1
        if state["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[{"name": "recall", "arguments": {"query": "喜欢 工程师"}}],
            )
        return LLMResponse(content="你喜欢喝美式咖啡，职业是软件工程师。")

    return llm


async def run_turn(plugin: MemoryHelperPlugin, llm: Any, user_text: str) -> None:
    tools = ToolRegistry()
    wiring = build_wiring(
        llm,
        tools=tools,
        before_turn_plugin_modules=plugin.before_turn_modules(),
        after_reasoning_plugin_modules=plugin.after_reasoning_modules(),
    )
    handlers, tools_loaded = load_plugin(wiring.bus, tools, plugin)
    assert handlers and tools_loaded, "插件装配失败"
    out = await wiring.pipeline.run(_msg(user_text), "cli:c1")
    print(f"  最终回复: {out.content!r}\n")


async def main() -> None:
    # 清空旧记忆，保证每次 demo 从空库开始
    if STORE_PATH.exists():
        STORE_PATH.unlink()

    plugin = MemoryHelperPlugin(
        STORE_PATH,
        extractor_llm=_make_extractor(),
    )

    print("=" * 60)
    print("Turn 1：自动提取通道（二次 LLM 从回复提取 preference 写库）")
    print("=" * 60)
    await run_turn(plugin, _turn1_llm(), "我喜欢喝美式咖啡")

    print("=" * 60)
    print("Turn 2：remember 工具通道（LLM 主动调 remember 写 fact）")
    print("=" * 60)
    await run_turn(plugin, _turn2_llm(), "我还是一名软件工程师")

    print("=" * 60)
    print("Turn 3：注入 + recall 通道（before_turn 注入记忆块，recall 检索）")
    print("=" * 60)
    await run_turn(plugin, _turn3_llm(), "我之前说过我喜欢什么、职业是什么？")

    print("=" * 60)
    print(f"最终记忆库文件: {STORE_PATH}")
    print(STORE_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    asyncio.run(main())
