from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .base import Tool


@dataclass(frozen=True)
class ToolExecutionResult:
    status: str
    output: str


class ToolRegistry:
    """Name lookup, minimal JSON-schema validation and timeout control."""

    _TYPE_MAP: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    def __init__(self, *, execution_timeout: float = 30.0) -> None:
        self._tools: dict[str, Tool] = {}
        self._execution_timeout = execution_timeout

    def register(self, tool: Tool) -> None:
        if not tool.name.strip():
            raise ValueError("工具名称不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def fork(self) -> "ToolRegistry":
        """Create a turn-local registry sharing the immutable tool instances."""

        cloned = ToolRegistry(execution_timeout=self._execution_timeout)
        cloned._tools = dict(self._tools)
        return cloned

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def search(self, query: str, *, top_k: int = 5, excluded_names: set[str] | None = None) -> list[dict[str, Any]]:
        """Return lightweight keyword-ranked tool documents."""
        terms = [term.lower() for term in query.split() if term]
        excluded = excluded_names or set()
        scored: list[tuple[int, dict[str, Any]]] = []
        for name, tool in self._tools.items():
            if name in excluded:
                continue
            haystack = f"{name} {tool.description}".lower()
            score = sum((3 if term == name.lower() else 1) for term in terms if term in haystack)
            if score:
                scored.append((score, {"name": name, "description": tool.description, "parameters": tool.parameters}))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
        return [item for _, item in scored[: max(1, min(int(top_k), 10))]]

    def document(self, name: str) -> dict[str, Any] | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        return {"name": tool.name, "description": tool.description, "parameters": tool.parameters}

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecutionResult("error", f"工具不存在：{name}")
        validation_error = self._validate(tool, arguments)
        if validation_error:
            return ToolExecutionResult("error", f"工具参数无效：{validation_error}")
        try:
            timeout = getattr(tool, "execution_timeout", self._execution_timeout)
            execution = tool.execute(**arguments)
            output = (
                await execution
                if timeout is None
                else await asyncio.wait_for(execution, timeout=timeout)
            )
            return ToolExecutionResult("success", str(output))
        except asyncio.TimeoutError:
            return ToolExecutionResult("error", f"工具 {name} 执行超时")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolExecutionResult("error", f"工具 {name} 执行失败：{exc}")

    def _validate(self, tool: Tool, arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict):
            return "参数必须是对象"
        schema = tool.parameters
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return "工具 Schema properties 无效"
        required = schema.get("required", [])
        missing = [str(name) for name in required if name not in arguments]
        if missing:
            return f"缺少必填字段：{', '.join(missing)}"
        unknown = sorted(str(name) for name in arguments if name not in properties)
        if unknown and schema.get("additionalProperties", False) is False:
            return f"不允许额外字段：{', '.join(unknown)}"
        for name, value in arguments.items():
            field = properties.get(name)
            if not isinstance(field, dict):
                continue
            expected_name = field.get("type")
            expected = self._TYPE_MAP.get(str(expected_name))
            if expected is not None:
                # bool is an int subclass; do not accept it for numeric fields.
                if expected_name in {"integer", "number"} and isinstance(value, bool):
                    return f"字段 {name} 类型必须是 {expected_name}"
                if not isinstance(value, expected):
                    return f"字段 {name} 类型必须是 {expected_name}"
            if "enum" in field and value not in field["enum"]:
                return f"字段 {name} 不在允许值中"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if "minimum" in field and value < field["minimum"]:
                    return f"字段 {name} 不能小于 {field['minimum']}"
                if "maximum" in field and value > field["maximum"]:
                    return f"字段 {name} 不能大于 {field['maximum']}"
        return ""
