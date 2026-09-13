"""before_turn「collect_exports」的简单测试。

验证 collect_exports 模块能回收插件外溢到 slot 的数据：
1. 插件模块往 slot 写 session:extra_hint:* → 合并进 ctx.extra_hints；
2. 插件模块往 slot 写 session:abort_reply → 置 ctx.abort + ctx.abort_reply。

插件介入方式：通过 plugin_modules 参数插入一个「外溢模块」到模块链，
该模块在 emit 之后往 frame.slots 写数据，由 collect_exports 回收。
（emit 的 handler 只有 ctx、拿不到 frame，所以 slot 外溢走的是插件模块这条通道。）

运行：<python> backend/test_before_turn_collect_exports.py
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


def _state(key: str = "cli:c1") -> TurnState:
    msg = InboundMessage(channel="cli", sender="u", chat_id="c1", content="hello")
    return TurnState(msg=msg, session_key=key, dispatch_outbound=True)


class _SpillPluginModule:
    """测试用插件模块：往 slot 外溢 extra_hints + abort_reply。"""

    slot = "plugin.spill"
    requires = ("before_turn.emit",)   # 排在 emit 之后、collect_exports 之前

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        frame.slots["session:extra_hint:1"] = "提示A"
        frame.slots["session:extra_hint:2"] = ["提示B", "提示C"]
        frame.slots["session:abort_reply"] = "被插件中断"
        return frame


async def main() -> None:
    manager = SessionManager()
    bus = EventBus()
    phase = Phase(
        default_before_turn_modules(
            bus,
            manager,
            plugin_modules=[_SpillPluginModule()],
        ),
        frame_factory=lambda state: BeforeTurnFrame(input=state),
    )
    ctx = await phase.run(_state())

    # 1. extra_hints 被回收（str 和 list 两种形态都要收）
    check("extra_hints 回收 3 条", len(ctx.extra_hints) == 3, f"got={ctx.extra_hints}")
    check(
        "extra_hints 内容正确",
        ctx.extra_hints == ["提示A", "提示B", "提示C"],
        f"got={ctx.extra_hints}",
    )

    # 2. abort_reply 被回收，置 abort
    check("abort 置 True", ctx.abort is True, f"got={ctx.abort}")
    check(
        "abort_reply 内容正确",
        ctx.abort_reply == "被插件中断",
        f"got={ctx.abort_reply}",
    )


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("before_turn collect_exports 测试通过。")
