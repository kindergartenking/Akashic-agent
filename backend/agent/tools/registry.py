"""工具注册表（M1b 最小版）。

原版 `agent/tools/registry.py` 765 行，依赖 search_backend / control.ids / snapshot
view。M1b 只 port 本次 phase 链需要的 `set_context` / `get_source_tool_names` 以及
最小注册/查找/执行。以下原版能力 M3 补齐：fork / _runtime_view / search / deferred /
mcp / turn_search_scope / new_operation_id。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, cast

from agent.tools.base import (
    Tool,
    ToolExecutionContext,
    ToolResult,
    tool_execution_context_scope,
)
from contextvars import ContextVar

logger = logging.getLogger(__name__)


@dataclass
class ToolMeta:
    risk: str = "read-only"
    always_on: bool = False
    preloadable: bool = True
    requires_turn_search: bool = False
    search_hint: str | None = None


@dataclass
class ToolDocument:
    name: str
    description: str
    risk: str
    always_on: bool
    search_hint: str | None
    source_type: str
    source_name: str

    @classmethod
    def from_tool_and_meta(
        cls,
        tool: Tool,
        meta: ToolMeta,
        source_type: str = "builtin",
        source_name: str = "",
    ) -> "ToolDocument":
        return cls(
            name=tool.name,
            description=tool.description,
            risk=meta.risk,
            always_on=meta.always_on,
            search_hint=meta.search_hint,
            source_type=source_type,
            source_name=source_name,
        )


class ToolRegistry:
    """管理所有可用工具（M1b 最小版）。"""

    def __init__(self, *, validate_semantic_schema: bool = True) -> None:
        self._tools: dict[str, Tool] = {}
        self._metadata: dict[str, ToolMeta] = {}
        self._documents: dict[str, ToolDocument] = {}
        self._execution_context: ContextVar[ToolExecutionContext | None] = ContextVar(
            f"akashic_tool_registry_context_{id(self)}",
            default=None,
        )
        self._validate_semantic_schema = validate_semantic_schema

    def set_context(self, **kwargs: str) -> None:
        """为当前 async task 绑定不可变 runtime provenance（逐行对齐原版）。"""

        allowed = {
            "channel",
            "chat_id",
            "session_key",
            "turn_id",
            "current_timestamp",
            "current_user_source_ref",
            "origin_channel",
            "origin_chat_id",
            "origin_session_key",
        }
        unknown = sorted(set(kwargs) - allowed)
        if unknown:
            raise TypeError(f"工具上下文包含未知字段: {', '.join(unknown)}")
        legacy_fields = {"channel", "chat_id", "session_key"}
        origin_fields = {
            "origin_channel",
            "origin_chat_id",
            "origin_session_key",
        }
        if legacy_fields.intersection(kwargs) and origin_fields.intersection(kwargs):
            raise TypeError(
                "工具上下文不能同时使用 legacy channel/chat_id/session_key "
                "和 origin_* 字段"
            )
        self_context = ToolExecutionContext(
            origin_channel=str(
                kwargs.get("origin_channel", kwargs.get("channel", ""))
                or ""
            ),
            origin_chat_id=str(
                kwargs.get("origin_chat_id", kwargs.get("chat_id", ""))
                or ""
            ),
            origin_session_key=str(
                kwargs.get(
                    "origin_session_key",
                    kwargs.get("session_key", ""),
                )
                or ""
            ),
            turn_id=str(kwargs.get("turn_id", "") or ""),
            current_timestamp=str(
                kwargs.get("current_timestamp", "") or ""
            ),
            current_user_source_ref=str(
                kwargs.get(
                    "current_user_source_ref", ""
                )
                or ""
            ),
        )
        _ = self._execution_context.set(self_context)

    def get_context(self) -> dict[str, str]:
        context = self._execution_context.get()
        if context is None:
            return {}
        return {
            "channel": context.origin_channel,
            "chat_id": context.origin_chat_id,
            "session_key": context.origin_session_key,
            "turn_id": context.turn_id,
            "current_timestamp": context.current_timestamp,
            "current_user_source_ref": context.current_user_source_ref,
        }

    def get_execution_context(self) -> ToolExecutionContext | None:
        return self._execution_context.get()

    def register(
        self,
        tool: Tool,
        *,
        risk: str = "read-only",
        always_on: bool = False,
        preloadable: bool = True,
        requires_turn_search: bool = False,
        search_hint: str | None = None,
        source_type: str = "builtin",
        source_name: str = "",
    ) -> None:
        self._tools[tool.name] = tool
        meta = ToolMeta(
            risk=risk,
            always_on=always_on,
            preloadable=preloadable,
            requires_turn_search=requires_turn_search,
            search_hint=search_hint,
        )
        self._metadata[tool.name] = meta
        self._documents[tool.name] = ToolDocument.from_tool_and_meta(
            tool, meta, source_type=source_type, source_name=source_name
        )
        logger.debug(f"注册工具: {tool.name}")

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._metadata.pop(name, None)
        self._documents.pop(name, None)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_registered_names(self) -> set[str]:
        return set(self._tools.keys())

    def get_source_tool_names(
        self,
        source_type: str,
        source_name: str,
        *,
        risk: str | None = None,
    ) -> set[str]:
        """按来源与 risk 返回工具名（逐行对齐原版，去 snapshot view）。"""

        names: set[str] = set()
        for name, document in self._documents.items():
            if document.source_type != source_type:
                continue
            if document.source_name != source_name:
                continue
            if risk is not None and document.risk != risk:
                continue
            names.add(name)
        return names

    def get_schemas(self, names: set[str] | list[str] | None = None) -> list[dict[str, Any]]:
        """返回 OpenAI function calling 格式的工具定义列表（无 progress 注入）。"""

        if names is None:
            return [tool.to_schema() for tool in self._tools.values()]
        if isinstance(names, set):
            return [
                tool.to_schema()
                for name, tool in self._tools.items()
                if name in names
            ]
        return [
            tool.to_schema()
            for name in names
            if (tool := self._tools.get(name)) is not None
        ]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        raise_errors: bool = False,
        execution_timeout: float | None = None,
    ) -> str | ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            if raise_errors:
                raise RuntimeError(f"工具 '{name}' 不存在")
            return f"工具 '{name}' 不存在"
        if not isinstance(arguments, dict):
            message = "工具参数必须是对象"
            if raise_errors:
                raise TypeError(message)
            return message
        if self._validate_semantic_schema:
            validation_errors = tool.validate_params(arguments)
            if validation_errors:
                message = "; ".join(validation_errors)
                if raise_errors:
                    raise ValueError(message)
                return f"工具参数无效: {message}"
        try:
            base_context = self._execution_context.get()
            execution_context = replace(
                base_context or ToolExecutionContext(),
            )
            with tool_execution_context_scope(execution_context):
                return await tool.execute_with_timeout(
                    arguments,
                    execution_timeout=execution_timeout,
                )
        except Exception as e:
            logger.error(f"工具 {name} 执行出错: {e}", exc_info=True)
            if raise_errors:
                raise
            return f"工具执行出错: {e}"
