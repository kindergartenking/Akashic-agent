"""before_turn「emit」的简单测试。

验证 emit 模块通过 Phase 装配后，能把 ctx 交给 EventBus 供插件改写/替换：
1. 无 handler 时，ctx 原样穿过（output 是正常 BeforeTurnCtx）；
2. handler 改写字段（skill_names）→ 改动生效；
3. handler 整体替换 ctx（返回新对象）→ output 用新对象。

运行：<python> backend/test_before_turn_emit.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.before_turn import (
    BeforeTurnFrame,
    default_before_turn_modules,
)
from agent.lifecycle.types import BeforeTurnCtx, TurnState
from bus.event_bus import EventBus
from bus.events import InboundMessage
from session.manager import SessionManager

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def _state(key: str = "cli:c1") -> TurnState:
    msg = InboundMessage(channel="cli", sender="u", chat_id="c1", content="hello")
    return TurnState(msg=msg, session_key=key, dispatch_outbound=True)


def _phase(bus: EventBus, manager: SessionManager) -> Phase:
    return Phase(
        default_before_turn_modules(bus, manager),
        frame_factory=lambda state: BeforeTurnFrame(input=state),
    )


async def main() -> None:
    manager = SessionManager()
    bus = EventBus()

    # 1. 无 handler：ctx 原样穿过
    ctx1 = await _phase(bus, manager).run(_state())
    check("无 handler 时 ctx 正常产出", isinstance(ctx1, BeforeTurnCtx), f"type={type(ctx1)}")

    # 2. handler 改写字段（GATE 可变 ctx，直接改字段）
    async def add_skill(ctx: BeforeTurnCtx) -> BeforeTurnCtx:
        ctx.skill_names.append("web_search")
        return ctx

    bus.on(BeforeTurnCtx, add_skill)
    ctx2 = await _phase(bus, manager).run(_state())
    check("handler 改写 skill_names 生效", "web_search" in ctx2.skill_names, f"got={ctx2.skill_names}")

    # 3. handler 整体替换 ctx（返回新对象）
    bus.off(BeforeTurnCtx, add_skill)

    async def replace_ctx(ctx: BeforeTurnCtx) -> BeforeTurnCtx:
        return replace(ctx, content="REPLACED")

    bus.on(BeforeTurnCtx, replace_ctx)
    ctx3 = await _phase(bus, manager).run(_state())
    check("handler 整体替换 ctx 生效", ctx3.content == "REPLACED", f"got={ctx3.content}")


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("before_turn emit 测试通过。")
