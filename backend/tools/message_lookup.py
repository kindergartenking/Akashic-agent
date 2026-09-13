from __future__ import annotations

import json
from typing import Any

from .base import Tool

_MAX_CONTEXT = 10
_MAX_PREVIEW_LINES = 50


def _expand_ref(value: object) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    prefix = raw.split("#", 1)[0].strip()
    try:
        parsed = json.loads(prefix)
    except (json.JSONDecodeError, ValueError):
        return [prefix]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [parsed.strip()] if isinstance(parsed, str) and parsed.strip() else []


class FetchMessagesTool(Tool):
    name = "fetch_messages"
    description = "根据消息 id 或 source_ref 读取历史消息原文；可用 context 扩展同一会话前后消息。"
    parameters = {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "string"}, "description": "消息 ID 列表"},
            "source_ref": {"type": "string", "description": "单个消息 id 或 session:seq"},
            "source_refs": {"type": "array", "items": {"type": "string"}, "description": "多个 source_ref"},
            "evidence": {"type": "array", "items": {"type": "object"}, "description": "包含 source_ref 的证据列表"},
            "context": {"type": "integer", "minimum": 0, "maximum": _MAX_CONTEXT, "description": "前后扩展条数，默认 0"},
        },
        "additionalProperties": False,
    }

    def __init__(self, store: Any) -> None:
        self._store = store

    async def execute(self, **arguments: Any) -> str:
        refs: list[str] = []
        refs.extend(str(value) for value in arguments.get("ids", []) or [])
        refs.extend(_expand_ref(arguments.get("source_ref")))
        for value in arguments.get("source_refs", []) or []:
            refs.extend(_expand_ref(value))
        for item in arguments.get("evidence", []) or []:
            if isinstance(item, dict):
                refs.extend(_expand_ref(item.get("source_ref")))
                raw_refs = item.get("refs")
                if isinstance(raw_refs, list):
                    for ref in raw_refs:
                        refs.extend(_expand_ref(ref))
                else:
                    refs.extend(_expand_ref(raw_refs))
        unique = list(dict.fromkeys(refs))
        context = max(0, min(int(arguments.get("context", 0)), _MAX_CONTEXT))
        if context:
            rows = self._store.fetch_by_ids_with_context(unique, context)
            matched = sum(1 for row in rows if row.get("in_source_ref"))
        else:
            rows = self._store.fetch_by_ids(unique)
            for row in rows:
                row["in_source_ref"] = True
            matched = len(rows)
        return json.dumps({"count": len(rows), "matched_count": matched, "messages": rows}, ensure_ascii=False)


class SearchMessagesTool(Tool):
    name = "search_messages"
    description = "按关键词 grep 式搜索已保存的原始历史消息，返回预览和 source_ref；需要完整原文时继续调用 fetch_messages。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词或短语"},
            "session_key": {"type": "string", "description": "限定会话（可选）"},
            "role": {"type": "string", "enum": ["user", "assistant"], "description": "限定角色"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, store: Any) -> None:
        self._store = store

    async def execute(self, **arguments: Any) -> str:
        query = str(arguments.get("query", "")).strip()
        limit = max(1, min(int(arguments.get("limit", 10)), 50))
        offset = max(0, int(arguments.get("offset", 0)))
        if not query:
            return json.dumps({"count": 0, "matched_count": 0, "messages": [], "has_more": False, "next_offset": None}, ensure_ascii=False)
        rows, total = self._store.search_messages(
            query,
            session_key=str(arguments.get("session_key") or "").strip() or None,
            role=str(arguments.get("role") or "").strip() or None,
            limit=limit,
            offset=offset,
        )
        terms = [term for term in query.split() if term]
        messages = []
        for row in rows:
            content = str(row.get("content", ""))
            lines = content.splitlines()
            preview = "\n".join(lines[:_MAX_PREVIEW_LINES])
            truncated = len(lines) > _MAX_PREVIEW_LINES
            if truncated:
                preview += f"\n...[已截断，剩余 {len(lines) - _MAX_PREVIEW_LINES} 行]"
            messages.append({
                "id": row["id"], "source_ref": row["id"], "session_key": row.get("session_key", ""),
                "seq": row["seq"], "role": row["role"], "timestamp": row["timestamp"],
                "matched_terms": [term for term in terms if term.lower() in content.lower()],
                "preview": preview, "preview_line_count": min(len(lines), _MAX_PREVIEW_LINES),
                "total_line_count": len(lines), "truncated": truncated,
            })
        next_offset = offset + len(messages)
        has_more = next_offset < total
        return json.dumps({"count": len(messages), "matched_count": total, "limit": limit, "offset": offset, "has_more": has_more, "next_offset": next_offset if has_more else None, "messages": messages}, ensure_ascii=False)
