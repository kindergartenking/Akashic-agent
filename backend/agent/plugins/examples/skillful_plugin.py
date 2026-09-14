"""带 skill + tool 的插件范例（SkillfulPlugin）。

展示 akashic 插件的三条贡献通道如何声明，以及复刻 runtime 的装配现状：

1. skill  —— 通过 `skill_roots()` 类方法声明技能根目录（原版会扫描目录加载技能，
   复刻 M1b 没有加载机制，此声明目前只是接口占位）；实际的 skill 注入走
   `@on_before_turn` handler 往 `skill_names` 塞名字（这条通道已通）。
2. tool   —— 通过 `@tool(name, risk=...)` 装饰器声明工具，由 loader 的
   `load_plugin_tools` 包装成 Tool 注册进 ToolRegistry。
3. handler—— 通过 `@on_*` 装饰器声明生命周期介入点，由 `load_lifecycle_handlers`
   桥接到 EventBus。
"""

from __future__ import annotations

from agent.lifecycle.types import BeforeTurnCtx
from agent.plugins import Plugin, on_before_turn, tool


class SkillfulPlugin(Plugin):
    name = "skillful"

    # ① skill：声明技能根目录（复刻里仅接口占位，无扫描加载）
    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ("skills/demo",)

    # ② skill：实际注入走 handler —— 往 skill_names 塞名字（已通）
    @on_before_turn()
    async def activate_skill(self, event: BeforeTurnCtx) -> BeforeTurnCtx:
        event.skill_names.append("demo-price-skill")
        return event

    # ③ tool：@tool 声明的工具，由 loader 包装注册进 ToolRegistry
    @tool("lookup_price", risk="read-only")
    async def lookup_price(self, event: object, item: str) -> str:
        """查询商品价格。

        :param item: 商品名
        """
        prices = {"apple": "3元", "banana": "2元"}
        return prices.get(item, "未知商品")
