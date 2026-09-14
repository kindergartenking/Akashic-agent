"""Reasoner（改造自 ReactRunner，M1b）。

原版 `DefaultReasoner` 把 prompt_render 一次 + before_step/after_step 每步循环
包进 `run_turn`。MVP 的 `ReactRunner` 是硬编码 while 循环，这里改造成同样的
三阶段结构。LLM 调用留可注入桩，M3 接真实 llm_client。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bus.event_bus import EventBus
from agent.core.runtime_support import TurnRunResult
from agent.lifecycle.phase import Phase
from agent.lifecycle.types import (
    AfterStepCtx,
    AfterToolResultCtx,
    BeforeStepInput,
    BeforeToolCallCtx,
    PromptRenderInput,
)
from agent.lifecycle.phases.after_step import (
    AfterStepFrame,
    default_after_step_modules,
)
from agent.lifecycle.phases.before_step import (
    BeforeStepFrame,
    default_before_step_modules,
)
from agent.lifecycle.phases.prompt_render import (
    PromptRenderFrame,
    default_prompt_render_modules,
)
from agent.tools.registry import ToolRegistry
from agent.tools.base import ToolResult


@dataclass
class LLMResponse:
    """可注入 LLM 的响应契约（M1b 桩）。"""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking: str | None = None


# 可注入 LLM 签名：async (messages, tool_schemas) -> LLMResponse
InjectLLM = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]],
    Awaitable[LLMResponse],
]


class Reasoner:
    def __init__(
        self,
        *,
        bus: EventBus,
        context: Any,
        tools: ToolRegistry,
        llm: InjectLLM | None = None,
        max_iterations: int = 10,
    ) -> None:
        self._bus = bus
        self._context = context
        self._tools = tools
        self._llm = llm
        self._max_iterations = max_iterations

        self._prompt_render = Phase(
            default_prompt_render_modules(self._bus, self._context),
            frame_factory=lambda input: PromptRenderFrame(input=input),
        )
        self._before_step = Phase(
            default_before_step_modules(self._bus),
            frame_factory=lambda input: BeforeStepFrame(input=input),
        )
        self._after_step = Phase(
            default_after_step_modules(self._bus),
            frame_factory=lambda input: AfterStepFrame(input=input),
        )

    async def run_turn(
        self,
        *,
        msg: Any,
        session: Any,
        skill_names: list[str] | None = None,
        retrieved_memory_block: str = "",
        extra_hints: list[str] | None = None,
    ) -> TurnRunResult:
        if self._llm is None:
            raise RuntimeError("Reasoner requires an injectable llm")

        # Phase 3: prompt_render（一次）
        history = [
            message
            for unit in session.history_units()
            for message in unit.messages
        ]
        prompt = await self._prompt_render.run(
            PromptRenderInput(
                session_key=session.key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                media=msg.media if msg.media else None,
                timestamp=msg.timestamp,
                history=history,
                skill_names=skill_names,
                retrieved_memory_block=retrieved_memory_block,
                disabled_sections=set(),
                turn_injection_prompt="",
                extra_hints=extra_hints,
            )
        )
        messages = list(prompt.messages)

        tools_used: list[str] = []
        tool_chain: list[dict[str, Any]] = []
        thinking: str | None = None

        # Phase 4: before_step / after_step 循环
        for iteration in range(self._max_iterations):
            before_step = await self._before_step.run(
                BeforeStepInput(
                    session_key=session.key,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    iteration=iteration,
                    messages=messages,
                    visible_names=None,
                )
            )
            if before_step.early_stop:
                return self._result(
                    before_step.early_stop_reply,
                    tools_used,
                    tool_chain,
                    thinking,
                )

            response = await self._llm(messages, self._tools.get_schemas())
            thinking = response.thinking

            if response.tool_calls:
                called_names: list[str] = []
                for call in response.tool_calls:
                    name = str(call.get("name", ""))
                    arguments = call.get("arguments", {})
                    if not isinstance(arguments, dict):
                        arguments = {}
                    # 工具调用前广播（TAP）：@on_tool_call 观察者在此触发，只读不阻塞。
                    await self._bus.fanout(
                        BeforeToolCallCtx(
                            session_key=session.key,
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            tool_name=name,
                            arguments=arguments,
                        )
                    )
                    result = await self._tools.execute(name, arguments)
                    result_text = (
                        result.text
                        if isinstance(result, ToolResult)
                        else str(result)
                    )
                    # 工具调用后广播（TAP）：@on_tool_result 观察者在此触发。
                    # status 恒为 success——execute 是 fail-soft，错误已转成字符串返回；
                    # 失败态细分待 ToolExecutor 接线后细化。
                    await self._bus.fanout(
                        AfterToolResultCtx(
                            session_key=session.key,
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            tool_name=name,
                            arguments=arguments,
                            result=result_text,
                            status="success",
                        )
                    )
                    called_names.append(name)
                    tools_used.append(name)
                    tool_chain.append(
                        {
                            "tool": name,
                            "arguments": arguments,
                            "result": result_text,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_name": name, "content": result_text}
                    )
                has_more = iteration < self._max_iterations - 1
                after_step = await self._after_step.run(
                    AfterStepCtx(
                        session_key=session.key,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        iteration=iteration,
                        context_tokens_estimate=0,
                        tools_called=tuple(called_names),
                        partial_reply="",
                        tools_used_so_far=tuple(tools_used),
                        tool_chain_partial=tuple(tool_chain),
                        partial_thinking=thinking,
                        has_more=has_more,
                    )
                )
                if after_step.early_stop:
                    return self._result(
                        after_step.early_stop_reason,
                        tools_used,
                        tool_chain,
                        thinking,
                    )
                continue

            return self._result(response.content, tools_used, tool_chain, thinking)

        # 预算耗尽 force_final
        return self._result("", tools_used, tool_chain, thinking)

    def _result(
        self,
        reply: str | None,
        tools_used: list[str],
        tool_chain: list[dict[str, Any]],
        thinking: str | None,
    ) -> TurnRunResult:
        return TurnRunResult(
            reply=reply,
            tools_used=tools_used,
            tool_chain=tool_chain,
            thinking=thinking,
        )
