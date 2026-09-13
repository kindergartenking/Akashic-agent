from __future__ import annotations

import json
from typing import Any

import httpx

from .base import Tool


class WebSearchTool(Tool):
    """Search via Exa's public MCP endpoint, matching the original project."""

    name = "web_search"
    description = (
        "搜索互联网并返回标题、摘要和 URL。适合新闻、天气、价格、人物动态等"
        "需要外部或时效性信息的问题。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "num_results": {
                "type": "integer",
                "description": "返回数量，默认 8，最大 20",
                "minimum": 1,
                "maximum": 20,
            },
            "livecrawl": {
                "type": "string",
                "enum": ["fallback", "preferred"],
            },
            "type": {
                "type": "string",
                "enum": ["auto", "fast", "deep"],
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def execute(self, **arguments: Any) -> str:
        query = str(arguments["query"])
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {
                    "query": query,
                    "numResults": min(int(arguments.get("num_results", 8)), 20),
                    "livecrawl": str(arguments.get("livecrawl", "fallback")),
                    "type": str(arguments.get("type", "auto")),
                },
            },
        }
        response = await self._http.post(
            "https://mcp.exa.ai/mcp",
            json=payload,
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            timeout=25.0,
        )
        response.raise_for_status()
        for line in response.text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            content = data.get("result", {}).get("content", [])
            if isinstance(content, list) and content:
                first = content[0] if isinstance(content[0], dict) else {}
                return json.dumps(
                    {"query": query, "result": first.get("text", "")},
                    ensure_ascii=False,
                )
        return json.dumps({"query": query, "results": []}, ensure_ascii=False)
