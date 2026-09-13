"""before_turn「build_ctx」的简单测试。

验证 build_ctx 模块通过 Phase 装配后能正确打包 BeforeTurnCtx：
1. phase output 是 BeforeTurnCtx；
2. ctx 的身份字段（session_key / channel / chat_id / content）取自入站消息；
3. 空历史时 history_messages 为空；
4. 先 append 消息再跑，历史能被正确带入 ctx。

运行：<python> backend/test_before_turn_build_ctx.py
"""

from __future__ import annotations

import asyncio
import sys
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


def _msg(text: str = "hello", channel: str = "cli", chat_id: str = "c1") -> InboundMessage:
    return InboundMessage(channel=channel, sender="u", chat_id=chat_id, content=text)


def _state(msg: InboundMessage, key: str) -> TurnState:
    return TurnState(msg=msg, session_key=key, dispatch_outbound=True)


async def main() -> None:
    manager = SessionManager()
    phase = Phase(
        default_before_turn_modules(EventBus(), manager),
        frame_factory=lambda state: BeforeTurnFrame(input=state),
    )

    # 1. output 是 BeforeTurnCtx，身份字段来自入站消息
    state = _state(_msg("hello world"), "cli:c1")
    ctx = await phase.run(state)
    check("output 是 BeforeTurnCtx", isinstance(ctx, BeforeTurnCtx), f"type={type(ctx)}")
    check("ctx.session_key 正确", ctx.session_key == "cli:c1", f"got={ctx.session_key}")
    check("ctx.channel 正确", ctx.channel == "cli", f"got={ctx.channel}")
    check("ctx.chat_id 正确", ctx.chat_id == "c1", f"got={ctx.chat_id}")
    check("ctx.content 正确", ctx.content == "hello world", f"got={ctx.content}")

    # 2. 空历史时 history_messages 为空
    check("空历史时 history_messages 为空", ctx.history_messages == ())

    # 3. 先 append 消息再跑，历史被带入 ctx
    session = manager.get_existing("cli:c1")
    await manager.append_messages(
        session,
        [
            {"id": "m1", "role": "user", "content": "hi"},
            {"id": "m2", "role": "assistant", "content": "hello"},
        ],
    )
    state2 = _state(_msg("again"), "cli:c1")
    ctx2 = await phase.run(state2)
    units = ctx2.history_messages
    check("历史被打包进 history_messages", len(units) == 1, f"len={len(units)}")
    check(
        "历史消息条数正确",
        len(units[0].messages) == 2,
        f"messages={len(units[0].messages)}",
    )


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("before_turn build_ctx 测试通过。")
