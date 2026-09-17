"""M1b 接线：把 7 阶段 pipeline 从桩装配起来。

原版 `bootstrap/wiring.py` 用注册表 + importlib 动态装配，M1b 简化为显式
装配函数。返回 Wiring 容器，让调用方拿到 bus（挂探针插件）、outbound（断言
dispatch）、pipeline（跑 turn）。

注意：MVP 的 `agent_orchestrator.py` 仍走自己的 `llm_client` + `tools.ToolRegistry`
（API 与 akashic 接口不一致），本模块是并行的新路径，M3 用适配层收敛二者。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bus.event_bus import EventBus
from session.manager import SessionManager
from agent.context import ContextBuilder
from agent.core.reasoner import InjectLLM, Reasoner
from agent.core.turn_pipeline import TurnPipeline
from agent.looping.ports import SessionServices
from agent.tools.registry import ToolRegistry
from agent.turns.outbound import OutboundPort, RecordingOutboundPort


@dataclass
class Wiring:
    bus: EventBus
    session_manager: SessionManager
    tools: ToolRegistry
    outbound: OutboundPort
    pipeline: TurnPipeline


def build_wiring(
    llm: InjectLLM,
    *,
    tools: ToolRegistry | None = None,
    outbound: OutboundPort | None = None,
    max_iterations: int = 10,
    before_turn_plugin_modules: list[Any] | None = None,
    before_reasoning_plugin_modules: list[Any] | None = None,
    after_reasoning_plugin_modules: list[Any] | None = None,
    after_turn_plugin_modules: list[Any] | None = None,
) -> Wiring:
    bus = EventBus()
    session_manager = SessionManager()
    context = ContextBuilder()
    tools = tools or ToolRegistry()
    outbound = outbound or RecordingOutboundPort()
    session_services = SessionServices(session_manager=session_manager)
    reasoner = Reasoner(
        bus=bus,
        context=context,
        tools=tools,
        llm=llm,
        max_iterations=max_iterations,
    )
    pipeline = TurnPipeline(
        bus=bus,
        session_manager=session_manager,
        tools=tools,
        context=context,
        outbound=outbound,
        session_services=session_services,
        reasoner=reasoner,
        before_turn_plugin_modules=before_turn_plugin_modules,
        before_reasoning_plugin_modules=before_reasoning_plugin_modules,
        after_reasoning_plugin_modules=after_reasoning_plugin_modules,
        after_turn_plugin_modules=after_turn_plugin_modules,
    )
    return Wiring(
        bus=bus,
        session_manager=session_manager,
        tools=tools,
        outbound=outbound,
        pipeline=pipeline,
    )
