"""before_reasoning「sync_tools」的简单测试。

sync_tools 是零 slot 副作用模块（不产生 output），无法走完整 Phase 链（Phase.run
要求 output 非 None），故本测试直接单元测试 sync_tools.run(frame)，验证它把会话
身份正确灌进 ToolRegistry（tools.get_context 能读回）。

验证点：
1. channel / chat_id / session_key 取自 before_turn；
2. turn_id 来自 running_turn_id ContextVar（默认空串）；
3. current_timestamp 是 before_turn.timestamp 的 ISO 字符串；
4. current_user_source_ref 来自 predict_current_user_source_ref（M1b 桩返回空串）。

运行：<python> backend/test_before_reasoning_sync_tools.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.lifecycle.phases.before_reasoning import (
    BeforeReasoningFrame,
    default_before_reasoning_modules,
)
from agent.lifecycle.types import BeforeReasoningInput, BeforeTurnCtx, TurnState
from agent.tools.registry import ToolRegistry
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


async def main() -> None:
    tools = ToolRegistry()
    manager = SessionManager()

    # 构造 input：state（含 session）+ before_turn（模拟 before_turn 的 output）
    session = manager.get_or_create("cli:c1")
    msg = InboundMessage(channel="cli", sender="u", chat_id="c1", content="hello")
    state = TurnState(msg=msg, session_key="cli:c1", dispatch_outbound=True)
    state.session = session
    ts = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    before_turn = BeforeTurnCtx(
        session_key="cli:c1",
        channel="cli",
        chat_id="c1",
        content="hello",
        timestamp=ts,
        retrieved_memory_block="",
        history_messages=(),
    )
    frame = BeforeReasoningFrame(
        input=BeforeReasoningInput(state=state, before_turn=before_turn)
    )

    # 拿 sync_tools 模块（链上第一个模块，无依赖），直接调 run
    modules = default_before_reasoning_modules(EventBus(), tools, manager)
    await modules[0].run(frame)

    ctx = tools.get_context()
    check("channel 正确", ctx["channel"] == "cli", f"got={ctx['channel']!r}")
    check("chat_id 正确", ctx["chat_id"] == "c1", f"got={ctx['chat_id']!r}")
    check(
        "session_key 正确",
        ctx["session_key"] == "cli:c1",
        f"got={ctx['session_key']!r}",
    )
    check("turn_id 默认空串", ctx["turn_id"] == "", f"got={ctx['turn_id']!r}")
    check(
        "current_timestamp 正确",
        ctx["current_timestamp"] == ts.isoformat(),
        f"got={ctx['current_timestamp']!r}",
    )
    check(
        "user_source_ref 默认空串",
        ctx["current_user_source_ref"] == "",
        f"got={ctx['current_user_source_ref']!r}",
    )


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("before_reasoning sync_tools 测试通过。")
