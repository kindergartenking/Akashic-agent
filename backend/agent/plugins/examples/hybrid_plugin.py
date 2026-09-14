"""混合插件范例：同一个插件文件同时贡献 handler 和 module。

展示 akashic 插件的两条接入通道可以共存于一个 Plugin 子类：

1. handler 通道 —— @on_* 装饰器标记的方法，注册进 plugin_registry，由 loader 桥接到
   EventBus，在 emit/fanout 时触发（只能看/改单个 ctx）；
2. module 通道 —— 覆写 before_turn_modules() 等方法返回 PhaseModule 实例，注入模块链，
   按拓扑排序在阶段内执行（能看整个 frame、读写数据槽）。

两者互不干扰，因为走的是完全不同的两条装载路径：
- handler：plugin_registry._handlers → loader → bus.on；
- module：Plugin.before_turn_modules() → default_*_modules(plugin_modules=...) → topo_sort。
"""

from __future__ import annotations

from agent.lifecycle.phases.before_turn import BeforeTurnFrame
from agent.lifecycle.types import BeforeTurnCtx
from agent.plugins import Plugin, on_before_turn


class HybridModule:
    """module 通道：注入 before_turn 模块链（排在 build_ctx 之后、emit 之前）。

    因为插件模块（slot 非 builtin 前缀）排在同依赖的 builtin 之前，而它和
    emit 都依赖 build_ctx，所以它落在 emit 之前——即 handler 触发之前跑。
    """

    slot = "hybrid.inject"
    requires = ("before_turn.build_ctx",)

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        print("  [module] hybrid.inject: 模块通道生效（emit 之前跑）")
        return frame


class HybridPlugin(Plugin):
    name = "hybrid-demo"

    # ① handler 通道：@on_* 装饰器
    @on_before_turn()
    async def inject_skill(self, event: BeforeTurnCtx) -> BeforeTurnCtx:
        print("  [handler] before_turn GATE: handler 通道生效，注入 skill")
        event.skill_names.append("hybrid_skill")
        return event

    # ② module 通道：覆写 before_turn_modules() 返回模块实例
    def before_turn_modules(self) -> list[object]:
        return [HybridModule()]
