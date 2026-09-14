"""Main/sub-Agent orchestration with bounded multi-round ReAct loops."""

from __future__ import annotations

import json
from datetime import datetime
from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import uuid4

from .llm_client import LLMClient, LLMResponse
from .model_config import ModelConfig
from .tools import SpawnManager, SpawnTool, ToolRegistry


Emit = Callable[[dict[str, object]], Awaitable[None]]

_MAX_TOOL_RESULT_CHARS = 12_000
_REPEAT_CALL_LIMIT = 2
_MAIN_MAX_ITERATIONS = 10
_SYNC_SUBAGENT_MAX_ITERATIONS = 10


@dataclass(frozen=True)
class ReactResult:
    reply: str
    tool_chain: tuple[dict[str, object], ...] = ()
    iterations: int = 0
    exit_reason: str = "completed"


@dataclass(frozen=True)
class AgentResult:
    reply: str
    delegated: bool = False
    child_agent_id: str | None = None
    tool_chain: tuple[dict[str, object], ...] = ()


class ReactRunner:
    """Repeat LLM → tools → observation until final text or budget exhaustion."""

    def __init__(self, llm: LLMClient, *, max_iterations: int) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations 必须大于 0")
        self._llm = llm
        self._max_iterations = max_iterations

    async def run(
        self,
        config: ModelConfig,
        task: str,
        *,
        system_prompt: str,
        tools: ToolRegistry,
        emit: Emit,
        session_id: str,
        turn_id: str,
        agent_id: str,
        emit_answer: bool,
        initial_messages: list[dict[str, object]] | None = None,
    ) -> ReactResult:
        messages: list[dict[str, object]] = [*(initial_messages or []), {"role": "user", "content": task}]
        tool_chain: list[dict[str, object]] = []
        signature_counts: dict[str, int] = {}
        force_final = False

        for iteration in range(self._max_iterations):
            on_delta, stream_state = self._make_stream_emitter(
                emit, session_id, turn_id, agent_id, emit_answer
            )
            response = await self._llm.chat(
                config,
                messages,
                system_prompt=system_prompt,
                tools=[] if force_final else tools.schemas(),
                on_delta=on_delta,
            )
            if not response.tool_calls:
                reply = response.content.strip()
                if not reply:
                    raise RuntimeError("Agent 模型未返回文本或工具调用")
                if emit_answer and not stream_state["streamed"]:
                    await self._emit_answer(reply, emit, session_id, turn_id, agent_id)
                return ReactResult(
                    reply=reply,
                    tool_chain=tuple(tool_chain),
                    iterations=iteration + 1,
                    exit_reason="tool_loop" if force_final else "completed",
                )

            messages.append(self._assistant_tool_message(response))
            recorded_calls: list[dict[str, object]] = []
            for call in response.tool_calls:
                await emit({
                    "type": "react.tool.started",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "call_id": call.id,
                    "tool_name": call.name,
                    "arguments": call.arguments,
                })
                signature = self._call_signature(call.name, call.arguments)
                signature_counts[signature] = signature_counts.get(signature, 0) + 1
                if signature_counts[signature] > _REPEAT_CALL_LIMIT:
                    status = "blocked"
                    output = "检测到完全相同的工具调用重复出现，已停止工具循环并进入最终收尾。"
                    force_final = True
                else:
                    execution = await tools.execute(call.name, call.arguments)
                    status = execution.status
                    output = execution.output
                if len(output) > _MAX_TOOL_RESULT_CHARS:
                    original_length = len(output)
                    output = (
                        output[:_MAX_TOOL_RESULT_CHARS]
                        + f"\n...[工具结果已截断，原始长度 {original_length} 字符]"
                    )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": output,
                })
                await emit({
                    "type": "react.tool.completed",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "call_id": call.id,
                    "tool_name": call.name,
                    "status": status,
                    "result_preview": output[:1000],
                })
                recorded_calls.append({
                    "call_id": call.id,
                    "name": call.name,
                    "status": status,
                    "arguments": call.arguments,
                    "final_arguments": call.arguments,
                    "result": output,
                })
            tool_chain.append({"text": response.content, "calls": recorded_calls})

            remaining = self._max_iterations - iteration - 1
            if remaining > 0:
                messages.append({
                    "role": "user",
                    "content": self._reflection_prompt(remaining, force_final),
                })

        # Preserve progress and make one tools-disabled summarization call.
        messages.append({
            "role": "user",
            "content": (
                "工具迭代预算已耗尽。请根据已有工具结果总结当前进展并直接回答用户；"
                "明确说明任何尚未完成的部分，不要再请求工具。"
            ),
        })
        on_delta, stream_state = self._make_stream_emitter(
            emit, session_id, turn_id, agent_id, emit_answer
        )
        final = await self._llm.chat(
            config,
            messages,
            system_prompt=system_prompt,
            tools=[],
            on_delta=on_delta,
        )
        reply = final.content.strip() or "工具迭代预算已耗尽，模型未生成可用总结。"
        if emit_answer and not stream_state["streamed"]:
            await self._emit_answer(reply, emit, session_id, turn_id, agent_id)
        return ReactResult(
            reply=reply,
            tool_chain=tuple(tool_chain),
            iterations=self._max_iterations,
            exit_reason="max_iterations",
        )

    @staticmethod
    def _assistant_tool_message(response: LLMResponse) -> dict[str, object]:
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in response.tool_calls
            ],
        }

    @staticmethod
    def _call_signature(name: str, arguments: dict[str, object]) -> str:
        return name + ":" + json.dumps(arguments, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _reflection_prompt(remaining: int, force_final: bool) -> str:
        if force_final:
            return "重复工具调用已被阻止。请基于已有结果直接给出最终答案，不要再调用工具。"
        if remaining == 1:
            return "只剩最后一次推理机会。若信息足够请立即回答；否则仅调用完成任务必需的工具。"
        return f"请检查工具结果。若足够就直接回答，否则继续调用必要工具；剩余迭代预算 {remaining}。"

    @staticmethod
    async def _emit_answer(
        content: str,
        emit: Emit,
        session_id: str,
        turn_id: str,
        agent_id: str,
    ) -> None:
        await emit({
            "type": "answer.delta",
            "session_id": session_id,
            "turn_id": turn_id,
            "agent_id": agent_id,
            "delta": content,
        })

    def _make_stream_emitter(
        self,
        emit: Emit,
        session_id: str,
        turn_id: str,
        agent_id: str,
        emit_answer: bool,
    ) -> tuple[Callable[[str], Awaitable[None]], dict[str, bool]]:
        """构造 on_delta 回调 + 流式状态，实现「边读边发 answer.delta」。

        工具调用轮 content 为空，on_delta 不触发；最终回复轮 content 逐 token，
        每次回调发一个 answer.delta。state["streamed"] 标记是否已流式发过，
        供 chat 返回后判断是否还需兜底发完整 reply。
        """
        state = {"streamed": False}

        async def on_delta(delta: str) -> None:
            state["streamed"] = True
            if emit_answer:
                await self._emit_answer(delta, emit, session_id, turn_id, agent_id)

        return on_delta, state


class SubAgent:
    """Synchronous child Agent with tools but without recursive spawn."""

    _SYSTEM_PROMPT = (
        "你是主 Agent 派生的子 Agent。独立完成分配的任务。你可以多轮调用已提供工具，"
        "信息足够后直接给出完整结果。不要请求派生其他 Agent。"
    )

    def __init__(self, llm: LLMClient, config: ModelConfig, tools: ToolRegistry) -> None:
        self._config = config
        self._tools = tools
        self._runner = ReactRunner(llm, max_iterations=_SYNC_SUBAGENT_MAX_ITERATIONS)

    async def run(
        self,
        task: str,
        *,
        emit: Emit,
        initial_messages: list[dict[str, object]] | None = None,
        session_id: str,
        turn_id: str,
        agent_id: str,
    ) -> ReactResult:
        return await self._runner.run(
            self._config,
            task,
            system_prompt=self._SYSTEM_PROMPT,
            tools=self._tools,
            emit=emit,
            session_id=session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            emit_answer=False,
            initial_messages=initial_messages,
        )


class AgentOrchestrator:
    """Main Agent: use tools directly or choose the spawn tool when justified."""

    _SYSTEM_PROMPT = (
        "你是主 Agent，负责完成用户任务并决定是否派生子 Agent。你可以直接回答、调用工具，"
        "或调用 spawn。简单问答和一到三次工具调用由你直接完成；只有边界清晰、可独立完成、"
        "预计需要多步处理的任务才调用 spawn。spawn 返回的是子 Agent 结果，你必须继续阅读该"
        "结果并向用户给出最终回复。不要在文本里伪造工具调用。\n"
        "如果用户的问题明显匹配某个 Skill，必须先调用 load_skill 获取该 Skill 的完整指令，"
        "再按照 Skill 要求调用其它工具。例如询问天气适合穿什么时，先加载 clothing-recommendation，"
        "然后调用 web_search，不要跳过 Skill 直接回答。"
    )

    def __init__(self, llm: LLMClient, tools: ToolRegistry, spawn_manager: SpawnManager | None = None) -> None:
        self._llm = llm
        self._tools = tools
        self._spawn_manager = spawn_manager
        self._context_manager = None
        self._runner = ReactRunner(llm, max_iterations=_MAIN_MAX_ITERATIONS)

    async def run(
        self,
        config: ModelConfig,
        text: str,
        *,
        session_id: str,
        turn_id: str,
        emit: Emit,
        initial_messages: list[dict[str, object]] | None = None,
    ) -> AgentResult:
        delegated_ids: list[str] = []
        child_chains: list[dict[str, object]] = []

        async def spawn_child(task: str, label: str) -> str:
            agent_id = f"subagent-{uuid4().hex[:12]}"
            delegated_ids.append(agent_id)
            await emit({
                "type": "agent.delegation.started",
                "session_id": session_id,
                "turn_id": turn_id,
                "agent_id": agent_id,
                "task": task,
            })
            child = SubAgent(self._llm, config, self._tools)
            status = "completed"
            try:
                child_result = await child.run(
                    task,
                    emit=emit,
                    session_id=session_id,
                    turn_id=turn_id,
                    agent_id=agent_id,
                )
                child_chains.extend(child_result.tool_chain)
                return (
                    f"[子任务{f'「{label}」' if label else ''}结果]\n"
                    f"退出原因: {child_result.exit_reason}\n\n{child_result.reply}"
                )
            except BaseException:
                status = "error"
                raise
            finally:
                await emit({
                    "type": "agent.delegation.completed",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "agent_id": agent_id,
                    "status": status,
                })

        main_tools = self._tools.fork()
        main_tools.register(SpawnTool(spawn_child, self._spawn_manager))
        system_prompt = self._SYSTEM_PROMPT + (
            f"\n当前日期是 {datetime.now().strftime('%Y-%m-%d')}。"
            "用户说‘今天’或‘明天’时，必须以此日期换算后再搜索。"
        )
        skill_tool = main_tools.document("load_skill")
        if skill_tool is not None:
            system_prompt += "\n\n当前 Skill 目录信息：\n" + str(skill_tool.get("description", ""))
        main_result = await self._runner.run(
            config,
            text,
            system_prompt=system_prompt,
            tools=main_tools,
            emit=emit,
            session_id=session_id,
            turn_id=turn_id,
            agent_id="main",
            emit_answer=True,
            initial_messages=initial_messages,
        )
        return AgentResult(
            reply=main_result.reply,
            delegated=bool(delegated_ids),
            child_agent_id=delegated_ids[-1] if delegated_ids else None,
            tool_chain=tuple([*main_result.tool_chain, *child_chains]),
        )
