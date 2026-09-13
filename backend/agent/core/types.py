"""core 类型契约（M1b 桩）。

原版 `agent.core.types` 承载 ContextRequest / ContextBundle 等跨阶段数据类型。
M1b 只保留 phase 模块链引用到的字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ContextRequest:
    history: list[dict[str, Any]] = field(default_factory=list)
    current_message: str = ""
    media: list[str] | None = None
    skill_names: list[str] | None = None
    channel: str = ""
    chat_id: str = ""
    message_timestamp: datetime | None = None
    retrieved_memory_block: str = ""
    disabled_sections: set[str] = field(default_factory=set)
    turn_injection_prompt: str = ""


@dataclass
class ContextBundle:
    skill_mentions: set[str] = field(default_factory=set)
    retrieved_memory_block: str = ""
    retrieval_trace_raw: object | None = None
    history_messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolCallGroup:
    tool_name: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)


def to_tool_call_groups(tool_chain: list[dict[str, Any]]) -> list[ToolCallGroup]:
    """把扁平 tool_chain 按 tool_name 分组（M1b 桩实现）。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for call in tool_chain:
        name = str(call.get("tool") or call.get("name") or "")
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(call)
    return [ToolCallGroup(tool_name=name, calls=groups[name]) for name in order]
