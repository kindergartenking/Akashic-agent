"""真实后端流式验证：用 Fake LLMClient 模拟流式 chat，验证 ReactRunner 逐 token 发 answer.delta。

两个场景：
- 场景1：最终回复轮流式 —— 逐 token 发 answer.delta；
- 场景2：工具调用轮 + 最终回复轮流式 —— 工具轮 content 空不误发，最终轮逐 token。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_real_stream.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.agent_orchestrator import ReactRunner
from backend.llm_client import LLMResponse, ToolCall
from backend.model_config import ModelConfig
from backend.tools.base import Tool
from backend.tools.registry import ToolRegistry


class EchoTool(Tool):
    name = "echo"
    description = "回显输入"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **arguments: Any) -> str:
        return f"echo:{arguments}"


def _config() -> ModelConfig:
    return ModelConfig(
        provider="openai",
        model="gpt-test",
        base_url="http://localhost",
        api_key="sk-test",
        runtime_id="test",
        source_name="test",
    )


async def _collect(runner: ReactRunner, config: ModelConfig, text: str, tools: ToolRegistry) -> tuple[list[str], str]:
    frames: list[dict[str, object]] = []

    async def emit(frame: dict[str, object]) -> None:
        frames.append(frame)

    result = await runner.run(
        config,
        text,
        system_prompt="",
        tools=tools,
        emit=emit,
        session_id="s1",
        turn_id="t1",
        agent_id="main",
        emit_answer=True,
    )
    deltas = [str(f["delta"]) for f in frames if f["type"] == "answer.delta"]
    return deltas, result.reply


async def scenario1_final_stream() -> None:
    class FakeLLM:
        async def chat(self, config, messages, *, system_prompt="", tools=None, on_delta=None):
            for token in ["你好", "，", "世界", "！"]:
                if on_delta is not None:
                    await on_delta(token)
            return LLMResponse(content="你好，世界！", tool_calls=())

    deltas, reply = await _collect(ReactRunner(FakeLLM(), max_iterations=10), _config(), "打招呼", ToolRegistry())
    print("[场景1] answer.delta 序列:", deltas)
    assert deltas == ["你好", "，", "世界", "！"], deltas
    assert reply == "你好，世界！"
    print("[场景1] OK：4 个 token 逐个发出\n")


async def scenario2_tool_then_stream() -> None:
    class FakeLLM:
        def __init__(self) -> None:
            self._n = 0

        async def chat(self, config, messages, *, system_prompt="", tools=None, on_delta=None):
            self._n += 1
            if self._n == 1:
                # 工具调用轮：content 空，不触发 on_delta
                return LLMResponse(content="", tool_calls=(ToolCall(id="c1", name="echo", arguments={}),))
            for token in ["结果", "已", "得到"]:
                if on_delta is not None:
                    await on_delta(token)
            return LLMResponse(content="结果已得到", tool_calls=())

    tools = ToolRegistry()
    tools.register(EchoTool())
    deltas, reply = await _collect(ReactRunner(FakeLLM(), max_iterations=10), _config(), "echo 一下", tools)
    print("[场景2] answer.delta 序列:", deltas)
    assert deltas == ["结果", "已", "得到"], deltas
    assert reply == "结果已得到"
    print("[场景2] OK：工具轮未误发，最终轮 3 个 token 逐个发出\n")


async def main() -> None:
    await scenario1_final_stream()
    await scenario2_tool_then_stream()
    print("真实后端流式验证全部通过")


if __name__ == "__main__":
    asyncio.run(main())
