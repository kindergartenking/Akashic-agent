# 插件兼容接口（M0 缩减版）：只重导出 M0 已落地的对象。
# 原版 `agent/plugins/__init__.py` 还重导出 config/context/scope/generation/jobs/specs，
# 这些随 M2 补齐后在此追加。
from agent.plugins.base import Plugin
from agent.plugins.decorators import (
    on_before_turn,
    on_before_reasoning,
    on_before_step,
    on_prompt_render,
    on_after_step,
    on_after_reasoning,
    on_after_turn,
    on_tool_call,
    on_tool_pre,
    on_tool_result,
    tool,
)

__all__ = [
    "Plugin",
    "on_before_turn",
    "on_before_reasoning",
    "on_before_step",
    "on_prompt_render",
    "on_after_step",
    "on_after_reasoning",
    "on_after_turn",
    "on_tool_call",
    "on_tool_pre",
    "on_tool_result",
    "tool",
]
