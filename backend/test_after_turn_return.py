"""after_turn「return」的简单测试（6 模块链端到端）。

after_turn 的职责是「提交事件 + 派发 outbound + 广播快照」。本测试走完整 6 模块链
（build_committed → fanout_committed → build_ctx → fanout_ctx → dispatch → return），
同时挂上 TurnCommitted / AfterTurnCtx 观察者和 RecordingOutboundPort，验证：

1. build_committed：组装 TurnCommitted（核心字段）；
2. fanout_committed：广播 TurnCommitted（内部系统收到）；
3. build_ctx：组装 AfterTurnCtx（reply / will_dispatch）；
4. fanout_ctx：广播 AfterTurnCtx（插件收到）；
5. dispatch：派发 outbound（RecordingOutboundPort 记录）；
6. output 是 OutboundMessage（原 outbound 对象）。

运行：<python> backend/test_after_turn_return.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.core.response_parser import ResponseMetadata
from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.after_turn import (
    AfterTurnFrame,
    default_after_turn_modules,
)
from agent.lifecycle.types import (
    AfterReasoningCtx,
    AfterTurnCtx,
    TurnSnapshot,
    TurnState,
)
from agent.turns.outbound import RecordingOutboundPort
from bus.event_bus import EventBus
from bus.events import InboundMessage, OutboundMessage
from bus.events_lifecycle import TurnCommitted

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


def _make_snapshot() -> tuple[TurnSnapshot, OutboundMessage]:
    ctx = AfterReasoningCtx(
        session_key="cli:c1",
        channel="cli",
        chat_id="c1",
        tools_used=("web_search",),
        thinking="thinking...",
        response_metadata=ResponseMetadata(raw_text="raw reply"),
        streamed=False,
        tool_chain=({"name": "web_search", "result": "ok"},),
        reply="hello there",
    )
    outbound = OutboundMessage(
        channel="cli",
        chat_id="c1",
        content="hello there",
        thinking="thinking...",
        metadata={"tools_used": ["web_search"]},
        session_message_id="msg_1",
    )
    state = TurnState(
        msg=InboundMessage(channel="cli", sender="u1", chat_id="c1", content="hello"),
        session_key="cli:c1",
        dispatch_outbound=True,
    )
    return TurnSnapshot(state=state, outbound=outbound, ctx=ctx), outbound


async def main() -> None:
    bus = EventBus()
    committed: list[TurnCommitted] = []
    taps: list[AfterTurnCtx] = []
    port = RecordingOutboundPort()

    async def on_committed(event: TurnCommitted) -> None:
        committed.append(event)

    async def on_ctx(ctx: AfterTurnCtx) -> None:
        taps.append(ctx)

    bus.on(TurnCommitted, on_committed)
    bus.on(AfterTurnCtx, on_ctx)

    snap, outbound = _make_snapshot()
    phase = Phase(
        default_after_turn_modules(bus, port),
        frame_factory=lambda i: AfterTurnFrame(input=i),
    )
    out = await phase.run(snap)

    # 1. output 类型 + 引用
    check("output 是 OutboundMessage", isinstance(out, OutboundMessage), f"type={type(out)}")
    check("output 引用原 outbound 对象", out is outbound, f"got={out}")
    # 2. build_committed 组装
    check("fanout_committed 广播一次 TurnCommitted", len(committed) == 1, f"count={len(committed)}")
    if committed:
        c = committed[0]
        check("build_committed input_message", c.input_message == "hello", f"got={c.input_message}")
        check(
            "build_committed assistant_response",
            c.assistant_response == "hello there",
            f"got={c.assistant_response}",
        )
        check(
            "build_committed tools_used",
            c.tools_used == ["web_search"],
            f"got={c.tools_used}",
        )
        check(
            "build_committed assistant_message_id",
            c.assistant_message_id == "msg_1",
            f"got={c.assistant_message_id}",
        )
    # 3. build_ctx 组装 + fanout_ctx 广播
    check("fanout_ctx 广播一次 AfterTurnCtx", len(taps) == 1, f"count={len(taps)}")
    if taps:
        t = taps[0]
        check("build_ctx reply", t.reply == "hello there", f"got={t.reply}")
        check("build_ctx will_dispatch", t.will_dispatch is True, f"got={t.will_dispatch}")
        check(
            "build_ctx tools_used",
            t.tools_used == ("web_search",),
            f"got={t.tools_used}",
        )
    # 4. dispatch 派发
    check("dispatch 派发一次", len(port.dispatches) == 1, f"count={len(port.dispatches)}")
    if port.dispatches:
        d = port.dispatches[0]
        check("dispatch content 正确", d.content == "hello there", f"got={d.content}")
        check("dispatch channel 正确", d.channel == "cli", f"got={d.channel}")


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("after_turn return 测试通过。")
