"""after_reasoning「return」的简单测试（5 模块链端到端）。

after_reasoning 的职责是「解析回复 + 持久化 + 组 outbound + 可改 reply」。本测试走完整
5 模块链（build_ctx → emit → persist → build_outbound → return），同时挂上 emit handler，
验证 output 是「build_ctx 解析 + emit 改 reply + persist 落库 + build_outbound 组装 +
return 打包」之后的 TurnSnapshot 三元组：

1. build_ctx：parse_response 解析 + 组装 AfterReasoningCtx；
2. emit：handler 改 reply → 生效；
3. persist：user + assistant 消息落库；
4. build_outbound：content 用改后的 reply，metadata 带 tools 信息，session_message_id 回填；
5. output 是 TurnSnapshot(state, outbound, ctx)。

运行：<python> backend/test_after_reasoning_return.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.core.runtime_support import TurnRunResult
from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.after_reasoning import (
    AfterReasoningFrame,
    default_after_reasoning_modules,
)
from agent.lifecycle.types import (
    AfterReasoningCtx,
    AfterReasoningInput,
    TurnPersistencePolicy,
    TurnSnapshot,
    TurnState,
)
from agent.looping.ports import SessionServices
from bus.event_bus import EventBus
from bus.events import InboundMessage
from session.manager import Session, SessionManager

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


def _make_input() -> tuple[AfterReasoningInput, Session, SessionManager]:
    session = Session(key="cli:c1")
    manager = SessionManager()
    manager._sessions["cli:c1"] = session
    state = TurnState(
        msg=InboundMessage(channel="cli", sender="u1", chat_id="c1", content="hello"),
        session_key="cli:c1",
        dispatch_outbound=True,
        session=session,
        persistence=TurnPersistencePolicy(persist_user=True, persist_assistant=True),
    )
    turn_result = TurnRunResult(
        reply="hello there",
        tools_used=["web_search"],
        tool_chain=[{"name": "web_search", "result": "ok"}],
        media=[],
        thinking="thinking...",
        streamed=False,
    )
    return AfterReasoningInput(state=state, turn_result=turn_result), session, manager


async def main() -> None:
    bus = EventBus()

    async def polish(ctx: AfterReasoningCtx) -> AfterReasoningCtx:
        ctx.reply = ctx.reply + " [polished]"
        return ctx

    bus.on(AfterReasoningCtx, polish)

    inp, session, manager = _make_input()
    services = SessionServices(session_manager=manager)
    phase = Phase(
        default_after_reasoning_modules(bus, services),
        frame_factory=lambda i: AfterReasoningFrame(input=i),
    )
    out = await phase.run(inp)

    # 1. output 类型
    check("output 是 TurnSnapshot", isinstance(out, TurnSnapshot), f"type={type(out)}")
    # 2. snapshot.state 是原 state
    check("snapshot.state 引用原 state", out.state is inp.state, f"got={out.state}")
    # 3. build_ctx 解析 + emit 改 reply
    check(
        "emit 改 reply 生效（build_ctx 解析后）",
        out.ctx.reply == "hello there [polished]",
        f"got={out.ctx.reply}",
    )
    check(
        "build_ctx 解析 response_metadata",
        out.ctx.response_metadata.raw_text == "hello there",
        f"got={out.ctx.response_metadata.raw_text}",
    )
    # 4. build_ctx 组装 tools_used / tool_chain
    check(
        "build_ctx tools_used 正确",
        out.ctx.tools_used == ("web_search",),
        f"got={out.ctx.tools_used}",
    )
    check(
        "build_ctx tool_chain 正确",
        out.ctx.tool_chain == ({"name": "web_search", "result": "ok"},),
        f"got={out.ctx.tool_chain}",
    )
    # 5. persist 落库（user + assistant 两条）
    units = session.history_units()
    persisted = units[0].messages if units else []
    check(
        "persist 落库 2 条消息（user + assistant）",
        len(persisted) == 2,
        f"got={len(persisted)}",
    )
    if persisted:
        check(
            "persist assistant 内容 = 改后 reply",
            persisted[1].get("role") == "assistant"
            and persisted[1].get("content") == "hello there [polished]",
            f"got={persisted[1]}",
        )
    # 6. build_outbound 组装
    check(
        "build_outbound content = 改后 reply",
        out.outbound.content == "hello there [polished]",
        f"got={out.outbound.content}",
    )
    check(
        "build_outbound metadata 带 tools_used",
        out.outbound.metadata.get("tools_used") == ["web_search"],
        f"got={out.outbound.metadata}",
    )
    check(
        "build_outbound 回填 session_message_id",
        isinstance(out.outbound.session_message_id, str)
        and bool(out.outbound.session_message_id),
        f"got={out.outbound.session_message_id}",
    )


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("after_reasoning return 测试通过。")
