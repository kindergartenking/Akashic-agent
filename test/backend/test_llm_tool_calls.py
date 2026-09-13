from __future__ import annotations

import asyncio
import json

import httpx

from backend.llm_client import LLMClient
from backend.model_config import ModelConfig


def test_openai_stream_assembles_fragmented_tool_call() -> None:
    async def run() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["tool_choice"] == "auto"
            assert payload["tools"][0]["function"]["name"] == "web_search"
            chunks = [
                {"choices": [{"delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-1",
                    "function": {"name": "web_search", "arguments": "{\"query\":\"北"},
                }]}}]},
                {"choices": [{"delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": "京天气\"}"},
                }]}}]},
            ]
            body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
            body += "data: [DONE]\n\n"
            return httpx.Response(200, text=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            response = await LLMClient(http).chat(
                ModelConfig(
                    provider="openai",
                    model="test",
                    base_url="https://model.test/v1",
                    api_key="secret",
                    runtime_id="test",
                    source_name="test",
                ),
                [{"role": "user", "content": "搜索"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "搜索",
                        "parameters": {"type": "object"},
                    },
                }],
            )
        assert response.content == ""
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].id == "call-1"
        assert response.tool_calls[0].name == "web_search"
        assert response.tool_calls[0].arguments == {"query": "北京天气"}

    asyncio.run(run())
