from __future__ import annotations

import json
from typing import Any

from .base import Tool


class ToolSearchTool(Tool):
    name = "tool_search"
    description = "在已注册工具目录中按关键词搜索工具；也支持 select:tool_a,tool_b 精确查看 Schema。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "功能关键词，或 select:工具名"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def execute(self, **arguments: Any) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return json.dumps({"matched": [], "unlocked": [], "tip": "query 不能为空"}, ensure_ascii=False)
        if query.lower().startswith("select:"):
            names = list(dict.fromkeys(n.strip() for n in query[7:].split(",") if n.strip()))
            matched = [doc for name in names if (doc := self._registry.document(name)) is not None]
            missing = [name for name in names if not self._registry.has_tool(name)]
            result: dict[str, Any] = {"matched": matched, "unlocked": [doc["name"] for doc in matched]}
            if missing:
                result["missing"] = missing
            return json.dumps(result, ensure_ascii=False)
        matched = self._registry.search(query, top_k=int(arguments.get("top_k", 5)))
        return json.dumps({"matched": matched, "unlocked": [item["name"] for item in matched], "next_action": "直接调用匹配工具" if matched else "换关键词重试"}, ensure_ascii=False)

