"""上下文构建器（M1b 桩）。

原版 `ContextBuilder.render` 组装 system 段 + 历史 + 用户消息。M1b 直接
拼接成 messages 列表，不调 LLM、不做检索，只保证结构走完整 7 阶段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.core.types import ContextRequest


@dataclass
class RenderedContext:
    messages: list[dict[str, Any]] = field(default_factory=list)


class ContextBuilder:
    def render(
        self,
        request: ContextRequest,
        *,
        system_sections_top: list[Any] | None = None,
        system_sections_bottom: list[Any] | None = None,
    ) -> RenderedContext:
        messages: list[dict[str, Any]] = []
        for item in request.history:
            if isinstance(item, dict):
                messages.append(dict(item))
        if request.current_message:
            messages.append({"role": "user", "content": request.current_message})
        return RenderedContext(messages=messages)
