"""before_turn「取 session」的简单测试。

验证 acquire_session 模块通过 Phase 装配后能正确取 session：
1. 新 session_key → 创建新 Session（挂到 state.session）；
2. 同 session_key → 复用同一 Session（get_or_create 语义）；
3. 不同 session_key → 不同 Session；
4. SessionManager 能 get_existing 到该 session。

注意：此时 before_turn 链上已含 build_ctx，phase output 是 BeforeTurnCtx；
本测试只关注 session 语义，故一律通过 state.session 断言（不依赖 output）。

运行：<python> backend/test_before_turn_acquire_session.py
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
from agent.lifecycle.types import TurnState
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


def _msg(text: str = "hello") -> InboundMessage:
    return InboundMessage(channel="cli", sender="u", chat_id="c1", content=text)


def _state(key: str) -> TurnState:
    return TurnState(msg=_msg(), session_key=key, dispatch_outbound=True)


async def main() -> None:
    manager = SessionManager()
    phase = Phase(
        default_before_turn_modules(EventBus(), manager),
        frame_factory=lambda state: BeforeTurnFrame(input=state),
    )

    # 1. 新 key → 创建 session，写回 state.session
    s1 = _state("cli:c1")
    await phase.run(s1)
    check(
        "新 session_key 创建 session",
        s1.session is not None and s1.session.key == "cli:c1",
        f"s1.session={s1.session}",
    )
    check(
        "session 写回 state.session",
        s1.session is manager.get_existing("cli:c1"),
    )

    # 2. 同 key → 复用同一 session
    s2 = _state("cli:c1")
    await phase.run(s2)
    check("同 session_key 复用同一 session", s2.session is s1.session)

    # 3. 不同 key → 不同 session
    s3 = _state("cli:c2")
    await phase.run(s3)
    check(
        "不同 session_key 得到不同 session",
        s3.session is not s1.session and s3.session.key == "cli:c2",
        f"s3.session={s3.session}",
    )

    # 4. manager 能 get_existing 到
    check("manager.get_existing 能取到", manager.get_existing("cli:c1") is s1.session)


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("before_turn 取 session 测试通过。")
