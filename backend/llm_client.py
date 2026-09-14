"""Small OpenAI-compatible streaming client used by the MVP."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .model_config import ModelConfig
from agent.model_runtime.auth.codex import CODEX_CLIENT_VERSION, CodexAuthDriver
from agent.model_runtime.auth.store import CredentialStore


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()


class LLMClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def chat(
        self,
        config: ModelConfig,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Run one model step and return text and structured tool calls.

        ``on_delta`` 若提供，在流式读取过程中对每个文本增量回调，实现逐 token 输出。
        """

        if config.provider == "codex":
            return await self._chat_codex(
                config,
                messages,
                system_prompt=system_prompt,
                tools=tools or [],
                on_delta=on_delta,
            )
        return await self._chat_openai_compatible(
            config,
            messages,
            system_prompt=system_prompt,
            tools=tools or [],
            on_delta=on_delta,
        )

    async def _chat_openai_compatible(
        self,
        config: ModelConfig,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str,
        tools: list[dict[str, Any]],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if not config.api_key:
            raise LLMError("未配置模型 API key。请设置 AKASHIC_API_KEY 或完善 model-registry.sqlite3")
        if not config.base_url:
            raise LLMError("未配置模型 base URL")
        request_messages = list(messages)
        if system_prompt.strip():
            request_messages.insert(0, {"role": "system", "content": system_prompt})
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": request_messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        content_parts: list[str] = []
        call_buffers: dict[int, dict[str, str]] = {}
        url = config.base_url.rstrip("/") + "/chat/completions"
        try:
            async with self.http.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:1000]
                    raise LLMError(f"模型请求失败 ({response.status_code}): {detail}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") if isinstance(chunk, dict) else None
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                    content = delta.get("content")
                    if isinstance(content, str):
                        content_parts.append(content)
                        if on_delta is not None and content:
                            await on_delta(content)
                    raw_calls = delta.get("tool_calls")
                    if not isinstance(raw_calls, list):
                        continue
                    for position, raw_call in enumerate(raw_calls):
                        if not isinstance(raw_call, dict):
                            continue
                        index = raw_call.get("index", position)
                        if not isinstance(index, int):
                            index = position
                        buffer = call_buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if isinstance(raw_call.get("id"), str):
                            buffer["id"] = raw_call["id"]
                        function = raw_call.get("function")
                        if isinstance(function, dict):
                            if isinstance(function.get("name"), str):
                                buffer["name"] += function["name"]
                            if isinstance(function.get("arguments"), str):
                                buffer["arguments"] += function["arguments"]
        except httpx.HTTPError as exc:
            raise LLMError(f"模型网络请求失败: {exc}") from exc
        return LLMResponse(
            content="".join(content_parts),
            tool_calls=self._build_tool_calls(call_buffers),
        )

    @staticmethod
    def _build_tool_calls(call_buffers: dict[int, dict[str, str]]) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        for index in sorted(call_buffers):
            item = call_buffers[index]
            raw_arguments = item["arguments"].strip() or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise LLMError(f"模型返回的工具参数不是合法 JSON：{raw_arguments[:300]}") from exc
            if not isinstance(arguments, dict):
                raise LLMError("模型返回的工具参数必须是对象")
            name = item["name"].strip()
            if not name:
                raise LLMError("模型返回了缺少名称的工具调用")
            calls.append(
                ToolCall(
                    id=item["id"].strip() or f"call-{uuid.uuid4().hex}",
                    name=name,
                    arguments=arguments,
                )
            )
        return tuple(calls)

    async def stream(
        self,
        config: ModelConfig,
        text: str,
        *,
        system_prompt: str = "",
    ) -> AsyncIterator[str]:
        if config.provider == "codex":
            async for content in self._stream_codex(config, text, system_prompt=system_prompt):
                yield content
            return
        if not config.api_key:
            raise LLMError("未配置模型 API key。请设置 AKASHIC_API_KEY 或完善 model-registry.sqlite3")
        if not config.base_url:
            raise LLMError("未配置模型 base URL")
        url = config.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
        messages: list[dict[str, str]] = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": text})
        payload = {
            "model": config.model,
            "messages": messages,
            "stream": True,
        }
        try:
            async with self.http.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:1000]
                    raise LLMError(f"模型请求失败 ({response.status_code}): {detail}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") if isinstance(chunk, dict) else None
                    if not isinstance(choices, list) or not choices:
                        continue
                    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(content, str) and content:
                        yield content
        except httpx.HTTPError as exc:
            raise LLMError(f"模型网络请求失败: {exc}") from exc

    async def complete(
        self,
        config: ModelConfig,
        text: str,
        *,
        system_prompt: str = "",
    ) -> str:
        """Collect one streaming model call into a complete text response."""

        chunks: list[str] = []
        async for delta in self.stream(config, text, system_prompt=system_prompt):
            chunks.append(delta)
        return "".join(chunks)

    async def _chat_codex(
        self,
        config: ModelConfig,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str,
        tools: list[dict[str, Any]],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if config.registry_path is None or not config.auth_id:
            raise LLMError("Codex 凭据存储未配置")
        auth = CodexAuthDriver(CredentialStore(config.registry_path), config.auth_id)
        try:
            return await self._chat_codex_once(
                auth,
                config,
                messages,
                system_prompt=system_prompt,
                tools=tools,
                force_refresh=False,
                on_delta=on_delta,
            )
        except _CodexUnauthorized:
            return await self._chat_codex_once(
                auth,
                config,
                messages,
                system_prompt=system_prompt,
                tools=tools,
                force_refresh=True,
                on_delta=on_delta,
            )

    async def _chat_codex_once(
        self,
        auth: CodexAuthDriver,
        config: ModelConfig,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str,
        tools: list[dict[str, Any]],
        force_refresh: bool,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        import asyncio

        auth_headers = await asyncio.to_thread(auth.headers, force_refresh=force_refresh)
        request_id = str(uuid.uuid4())
        headers = {
            **auth_headers,
            "Content-Type": "application/json",
            "originator": "codex_cli_rs",
            "User-Agent": f"codex_cli_rs/{CODEX_CLIENT_VERSION}",
            "x-codex-installation-id": request_id,
            "session-id": request_id,
            "thread-id": request_id,
            "x-codex-window-id": request_id,
        }
        if config.use_responses_lite:
            headers["x-openai-internal-codex-responses-lite"] = "true"
        payload: dict[str, Any] = {
            "model": config.model,
            "instructions": system_prompt,
            "input": self._responses_input(messages),
            "tool_choice": "auto",
            "parallel_tool_calls": config.supports_parallel_tool_calls and not config.use_responses_lite,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if tools:
            payload["tools"] = self._responses_tools(tools)
        reasoning: dict[str, str] = {}
        if config.reasoning_effort:
            reasoning["effort"] = config.reasoning_effort
        if config.reasoning_summary != "none":
            reasoning["summary"] = config.reasoning_summary
        if reasoning:
            payload["reasoning"] = reasoning
        content_parts: list[str] = []
        raw_calls: list[dict[str, Any]] = []
        completed = False
        url = config.base_url.rstrip("/") + "/responses"
        try:
            async with self.http.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code == 401:
                    raise _CodexUnauthorized()
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:1000]
                    raise LLMError(f"Codex 请求失败 ({response.status_code}): {detail}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    event_type = str(event.get("type") or "") if isinstance(event, dict) else ""
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            content_parts.append(delta)
                            if on_delta is not None and delta:
                                await on_delta(delta)
                    elif event_type == "response.output_item.done":
                        item = event.get("item")
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            raw_calls.append(item)
                    elif event_type == "response.completed":
                        completed = True
                        if not raw_calls:
                            response_body = event.get("response")
                            output = response_body.get("output") if isinstance(response_body, dict) else None
                            if isinstance(output, list):
                                raw_calls.extend(
                                    item for item in output
                                    if isinstance(item, dict) and item.get("type") == "function_call"
                                )
                        break
                    elif event_type in {"response.failed", "response.incomplete"}:
                        raise LLMError(f"Codex 流式请求失败: {event.get('response') or event}")
        except _CodexUnauthorized:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(f"Codex 网络请求失败: {exc}") from exc
        if not completed:
            raise LLMError("Codex Responses 在 completed 事件前断流")
        calls: list[ToolCall] = []
        for item in raw_calls:
            raw_arguments = str(item.get("arguments") or "{}")
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise LLMError(f"Codex 返回的工具参数不是合法 JSON：{raw_arguments[:300]}") from exc
            if not isinstance(arguments, dict):
                raise LLMError("Codex 返回的工具参数必须是对象")
            calls.append(ToolCall(
                id=str(item.get("call_id") or item.get("id") or f"call-{uuid.uuid4().hex}"),
                name=str(item.get("name") or ""),
                arguments=arguments,
            ))
        return LLMResponse(content="".join(content_parts), tool_calls=tuple(calls))

    @staticmethod
    def _responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for schema in tools:
            function = schema.get("function") if isinstance(schema, dict) else None
            if not isinstance(function, dict):
                continue
            converted.append({
                "type": "function",
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {}),
            })
        return converted

    @staticmethod
    def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": str(message.get("content") or ""),
                })
                continue
            if role == "assistant" and isinstance(message.get("tool_calls"), list):
                content = str(message.get("content") or "")
                if content:
                    items.append({"role": "assistant", "content": content})
                for call in message["tool_calls"]:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    items.append({
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    })
                continue
            items.append({"role": role, "content": str(message.get("content") or "")})
        return items

    async def _stream_codex(
        self,
        config: ModelConfig,
        text: str,
        *,
        system_prompt: str = "",
    ) -> AsyncIterator[str]:
        if config.registry_path is None or not config.auth_id:
            raise LLMError("Codex 凭据存储未配置")
        auth = CodexAuthDriver(CredentialStore(config.registry_path), config.auth_id)
        try:
            async for delta in self._stream_codex_once(
                auth, config, text, system_prompt=system_prompt, force_refresh=False
            ):
                yield delta
        except _CodexUnauthorized:
            async for delta in self._stream_codex_once(
                auth, config, text, system_prompt=system_prompt, force_refresh=True
            ):
                yield delta

    async def _stream_codex_once(
        self,
        auth: CodexAuthDriver,
        config: ModelConfig,
        text: str,
        *,
        system_prompt: str,
        force_refresh: bool,
    ) -> AsyncIterator[str]:
        import asyncio

        auth_headers = await asyncio.to_thread(auth.headers, force_refresh=force_refresh)
        request_id = str(uuid.uuid4())
        headers = {
            **auth_headers,
            "Content-Type": "application/json",
            "originator": "codex_cli_rs",
            "User-Agent": f"codex_cli_rs/{CODEX_CLIENT_VERSION}",
            "x-codex-installation-id": request_id,
            "session-id": request_id,
            "thread-id": request_id,
            "x-codex-window-id": request_id,
        }
        if config.use_responses_lite:
            headers["x-openai-internal-codex-responses-lite"] = "true"
        payload: dict[str, object] = {
            "model": config.model,
            "instructions": system_prompt,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            "tool_choice": "auto",
            "parallel_tool_calls": config.supports_parallel_tool_calls and not config.use_responses_lite,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        reasoning: dict[str, str] = {}
        if config.reasoning_effort:
            reasoning["effort"] = config.reasoning_effort
        if config.reasoning_summary != "none":
            reasoning["summary"] = config.reasoning_summary
        if reasoning:
            payload["reasoning"] = reasoning
        url = config.base_url.rstrip("/") + "/responses"
        try:
            async with self.http.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code == 401:
                    raise _CodexUnauthorized()
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:1000]
                    raise LLMError(f"Codex 请求失败 ({response.status_code}): {detail}")
                completed = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    event_type = str(event.get("type") or "") if isinstance(event, dict) else ""
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield delta
                    elif event_type == "response.output_text.done":
                        # Deltas are authoritative; accepting done text here would duplicate them.
                        continue
                    elif event_type == "response.completed":
                        completed = True
                        break
                    elif event_type in {"response.failed", "response.incomplete"}:
                        raise LLMError(f"Codex 流式请求失败: {event.get('response') or event}")
                if not completed:
                    raise LLMError("Codex Responses 在 completed 事件前断流")
        except _CodexUnauthorized:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(f"Codex 网络请求失败: {exc}") from exc


class _CodexUnauthorized(Exception):
    pass
