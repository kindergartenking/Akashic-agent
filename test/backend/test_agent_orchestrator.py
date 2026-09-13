from __future__ import annotations

import asyncio

from backend.agent_orchestrator import AgentOrchestrator, ReactRunner
from backend.llm_client import LLMResponse, ToolCall
from backend.model_config import ModelConfig
from backend.tools import Tool, ToolRegistry


class FakeLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def chat(self, config, messages, *, system_prompt: str = "", tools=None):
        names = [item["function"]["name"] for item in (tools or [])]
        self.calls.append({"messages": messages, "system_prompt": system_prompt, "tools": names})
        return self.responses.pop(0)


class EchoTool(Tool):
    name = "echo"
    description = "回显文本"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }

    async def execute(self, **arguments) -> str:
        return f"echo:{arguments['text']}"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def _config() -> ModelConfig:
    return ModelConfig(
        provider="openai",
        model="test-model",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        runtime_id="test",
        source_name="test",
    )


def test_main_agent_can_answer_directly_and_sees_spawn() -> None:
    async def run() -> None:
        llm = FakeLLM([LLMResponse("直接回答")])
        events: list[dict[str, object]] = []

        async def emit(event: dict[str, object]) -> None:
            events.append(event)

        result = await AgentOrchestrator(llm, _registry()).run(
            _config(), "你好", session_id="s1", turn_id="t1", emit=emit
        )
        assert result.reply == "直接回答"
        assert result.delegated is False
        assert llm.calls[0]["tools"] == ["echo", "spawn"]
        assert [event["type"] for event in events] == ["answer.delta"]

    asyncio.run(run())


def test_main_agent_can_use_tools_for_multiple_rounds() -> None:
    async def run() -> None:
        llm = FakeLLM([
            LLMResponse("", (ToolCall("call-1", "echo", {"text": "one"}),)),
            LLMResponse("", (ToolCall("call-2", "echo", {"text": "two"}),)),
            LLMResponse("主 Agent 最终答案"),
        ])
        events: list[dict[str, object]] = []

        async def emit(event: dict[str, object]) -> None:
            events.append(event)

        result = await AgentOrchestrator(llm, _registry()).run(
            _config(), "执行两步", session_id="s1", turn_id="t1", emit=emit
        )
        assert result.reply == "主 Agent 最终答案"
        assert result.delegated is False
        assert len(llm.calls) == 3
        assert len(result.tool_chain) == 2
        assert [event["type"] for event in events] == [
            "react.tool.started",
            "react.tool.completed",
            "react.tool.started",
            "react.tool.completed",
            "answer.delta",
        ]

    asyncio.run(run())


def test_spawn_is_a_main_tool_and_child_has_its_own_react_loop() -> None:
    async def run() -> None:
        llm = FakeLLM([
            LLMResponse("", (ToolCall("spawn-1", "spawn", {"task": "子任务", "label": "研究"}),)),
            LLMResponse("", (ToolCall("child-1", "echo", {"text": "child"}),)),
            LLMResponse("子 Agent 结果"),
            LLMResponse("主 Agent 汇总结果"),
        ])
        events: list[dict[str, object]] = []

        async def emit(event: dict[str, object]) -> None:
            events.append(event)

        result = await AgentOrchestrator(llm, _registry()).run(
            _config(), "复杂任务", session_id="s1", turn_id="t1", emit=emit
        )
        assert result.reply == "主 Agent 汇总结果"
        assert result.delegated is True
        assert result.child_agent_id is not None
        assert len(llm.calls) == 4
        assert llm.calls[0]["tools"] == ["echo", "spawn"]
        assert llm.calls[1]["tools"] == ["echo"]
        assert len(result.tool_chain) == 2
        assert [event["type"] for event in events] == [
            "react.tool.started",
            "agent.delegation.started",
            "react.tool.started",
            "react.tool.completed",
            "agent.delegation.completed",
            "react.tool.completed",
            "answer.delta",
        ]

    asyncio.run(run())


def test_budget_exhaustion_forces_tools_disabled_summary() -> None:
    async def run() -> None:
        llm = FakeLLM([
            LLMResponse("", (ToolCall("call-1", "echo", {"text": "one"}),)),
            LLMResponse("", (ToolCall("call-2", "echo", {"text": "two"}),)),
            LLMResponse("预算总结"),
        ])

        async def emit(_event: dict[str, object]) -> None:
            return None

        result = await ReactRunner(llm, max_iterations=2).run(
            _config(),
            "任务",
            system_prompt="test",
            tools=_registry(),
            emit=emit,
            session_id="s1",
            turn_id="t1",
            agent_id="main",
            emit_answer=True,
        )
        assert result.reply == "预算总结"
        assert result.exit_reason == "max_iterations"
        assert llm.calls[-1]["tools"] == []

    asyncio.run(run())


def test_repeated_identical_call_is_blocked_then_forced_to_finish() -> None:
    async def run() -> None:
        same_call = lambda call_id: LLMResponse(
            "", (ToolCall(call_id, "echo", {"text": "same"}),)
        )
        llm = FakeLLM([
            same_call("call-1"),
            same_call("call-2"),
            same_call("call-3"),
            LLMResponse("循环保护后的答案"),
        ])
        events: list[dict[str, object]] = []

        async def emit(event: dict[str, object]) -> None:
            events.append(event)

        result = await ReactRunner(llm, max_iterations=5).run(
            _config(),
            "任务",
            system_prompt="test",
            tools=_registry(),
            emit=emit,
            session_id="s1",
            turn_id="t1",
            agent_id="main",
            emit_answer=True,
        )
        completed = [event for event in events if event["type"] == "react.tool.completed"]
        assert completed[-1]["status"] == "blocked"
        assert result.exit_reason == "tool_loop"
        assert result.reply == "循环保护后的答案"
        assert llm.calls[-1]["tools"] == []

    asyncio.run(run())
