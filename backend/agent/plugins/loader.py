"""插件装载器：把 Plugin 实例上用 @on_* 装饰的方法桥接到 EventBus。

原版 akashic 在 bootstrap 阶段用 importlib 发现插件 → 实例化 → 把 LIFECYCLE handler
挂到 EventBus。M1b 复刻简化：显式实例化插件，本模块把它的 handler 按事件类型
`bus.on()` 挂载。

关键映射：decorator 的 PluginEventType（如 BEFORE_TURN）→ 生命周期阶段实际
emit/fanout 的 Ctx 类型（如 BeforeTurnCtx）。集中在这里维护，是「插件写的是
事件名，runtime 发的是 Ctx 对象」这道桥。

语义说明：
- 本模块只做「挂载」，不区分 GATE/TAP。GATE/TAP 由**调用点**决定——emit 调用时
  handler 返回值会替换 ctx（GATE），fanout/observe 调用时返回值被丢弃（TAP）。
"""

from __future__ import annotations

import inspect
from typing import Any

import docstring_parser

from agent.lifecycle.types import (
    AfterReasoningCtx,
    AfterStepCtx,
    AfterToolResultCtx,
    AfterTurnCtx,
    BeforeReasoningCtx,
    BeforeStepCtx,
    BeforeToolCallCtx,
    BeforeTurnCtx,
    PromptRenderCtx,
)
from agent.plugins.registry import (
    MetadataKind,
    PluginEventType,
    PluginHandlerMetadata,
    plugin_registry,
)
from agent.tools.base import Tool, ToolExecutionContext, get_current_tool_context
from bus.event_bus import EventBus


# decorator 的 PluginEventType → 生命周期阶段实际 emit/fanout 的 Ctx 类型。
EVENT_TYPE_MAP: dict[PluginEventType, type[Any]] = {
    PluginEventType.BEFORE_TURN: BeforeTurnCtx,
    PluginEventType.BEFORE_REASONING: BeforeReasoningCtx,
    PluginEventType.PROMPT_RENDER: PromptRenderCtx,
    PluginEventType.BEFORE_STEP: BeforeStepCtx,
    PluginEventType.AFTER_STEP: AfterStepCtx,
    PluginEventType.AFTER_REASONING: AfterReasoningCtx,
    PluginEventType.AFTER_TURN: AfterTurnCtx,
    PluginEventType.BEFORE_TOOL_CALL: BeforeToolCallCtx,
    PluginEventType.AFTER_TOOL_RESULT: AfterToolResultCtx,
}


def load_lifecycle_handlers(
    bus: EventBus,
    plugin: object,
) -> list[PluginHandlerMetadata]:
    """把 `plugin` 实例上用 @on_* 装饰的 LIFECYCLE handler 挂到 EventBus。

    - 只装载 MetadataKind.LIFECYCLE（跳过 @tool / @on_tool_pre，它们走 ToolExecutor
      的 tool 注册 / pre_hook 链，不经 EventBus）。
    - handler 是 Plugin 类的未绑定方法（签名 `(self, event)`），用 descriptor `__get__`
      绑定到实例，得到 `(event)` 形式，与 EventBus 的 `Handler[E]` 签名一致。
    - 返回成功挂载的元数据列表，便于调用方确认挂了几个、挂了哪些。

    注意：同名 handler 幂等（decorators 里 get_by_name 去重），重复装载不会叠加。
    """
    cls = type(plugin)
    module_path = cls.__module__
    loaded: list[PluginHandlerMetadata] = []
    for md in plugin_registry.get_handlers_by_module_path(module_path):
        if md.kind is not MetadataKind.LIFECYCLE:
            continue
        if md.event_type is None:
            continue
        event_cls = EVENT_TYPE_MAP.get(md.event_type)
        if event_cls is None:
            continue
        bound = md.handler.__get__(plugin, cls)
        bus.on(event_cls, bound)
        loaded.append(md)
    return loaded


def _tool_description(md: PluginHandlerMetadata) -> str:
    """从 @tool handler 的 docstring 提取工具描述（@tool 没有 description 参数）。"""
    doc = docstring_parser.parse(md.handler.__doc__ or "")
    return doc.short_description or md.tool_name or ""


class PluginToolAdapter(Tool):
    """把 @tool 装饰的方法包装成 Tool 实例，供 ToolRegistry.register 使用。

    @tool 的 handler 签名是 `(self, event, **params)`，而 Tool.execute 是
    `(**kwargs)`。适配器在 execute 里补上 event（当前工具执行上下文），再调 handler。

    注意：Tool 的 __init_subclass__ 要求 name/description/parameters 是**类属性**，
    而这里要动态取自 md，故用 property 暴露（Tool 对 property 字段跳过空值检查）。

    @tool 的 schema 由 decorators._derive_params_schema 派生（含 required /
    properties），这里直接复用，不再重复派生。
    """

    def __init__(self, md: PluginHandlerMetadata, plugin: object) -> None:
        self._md = md
        self._plugin = plugin

    @property
    def name(self) -> str:
        return self._md.tool_name or ""

    @property
    def description(self) -> str:
        return _tool_description(self._md)

    @property
    def parameters(self) -> dict[str, Any]:
        return self._md.tool_schema or {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        event = get_current_tool_context() or ToolExecutionContext()
        result = self._md.handler(self._plugin, event, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return str(result)


def load_plugin_tools(
    registry: Any,
    plugin: object,
) -> list[PluginHandlerMetadata]:
    """把 `plugin` 实例上用 @tool 装饰的方法包装成 Tool，注册进 ToolRegistry。

    - 只装载 MetadataKind.TOOL（跳过 LIFECYCLE / TOOL_HOOK）。
    - 每个 @tool handler 包装成 PluginToolAdapter，注册时沿用 handler 声明的
      risk / always_on / search_hint。
    - 返回成功注册的元数据列表。

    原版由 ToolExecutor 从 registry 收集 TOOL handler 并接管执行（含 pre_hook、
    超时、turn_search_scope 等）；M1b 砍掉 ToolExecutor，这里只做「注册进
    ToolRegistry」这一最小编排，让 LLM 能看到并调用插件工具。
    """
    cls = type(plugin)
    module_path = cls.__module__
    loaded: list[PluginHandlerMetadata] = []
    for md in plugin_registry.get_handlers_by_module_path(module_path):
        if md.kind is not MetadataKind.TOOL:
            continue
        adapter = PluginToolAdapter(md, plugin)
        registry.register(
            adapter,
            risk=md.tool_risk or "read-only",
            always_on=md.tool_always_on,
            search_hint=md.tool_search_hint,
            source_type="plugin",
            source_name=module_path,
        )
        loaded.append(md)
    return loaded


def load_plugin(
    bus: EventBus,
    registry: Any,
    plugin: object,
) -> tuple[list[PluginHandlerMetadata], list[PluginHandlerMetadata]]:
    """统一装配入口：一个调用同时装载 handler 与 tool。

    返回 (lifecycle_handlers, tools) 两个列表，便于调用方确认各装了几个。
    skill 不走本函数——skill 通过 skill_roots() 声明目录、通过 before_turn 的
    handler/module 把 skill_names 注入 ctx（复刻里 skill 只是名字流转，无加载机制）。
    """
    handlers = load_lifecycle_handlers(bus, plugin)
    tools = load_plugin_tools(registry, plugin)
    return handlers, tools
