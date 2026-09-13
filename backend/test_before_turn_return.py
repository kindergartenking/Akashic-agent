"""before_turn「return」的简单测试（5 模块链端到端）。

return 模块的职责是「把链上最终 ctx 设为阶段 output」。因此这个测试走完整
5 模块链，同时挂上 emit handler 和插件模块，验证 output 是「经 emit 替换 +
collect_exports 回收」之后的最终 ctx：

1. emit handler 整体替换 content → output.content 应是替换后的值；
2. 插件模块外溢 extra_hints + abort_reply → output 应回收了这些；
3. 三者叠加后，output 仍是同一个 BeforeTurnCtx（return 从 slot 取出）。

运行：<python> backend/test_before_turn_return.py
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


class _SpillPluginModule:
    """测试用插件模块：往 slot 外溢 extra_hints + abort_reply。"""

    slot = "plugin.spill"
    requires = ("before_turn.emit",)

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        frame.slots["session:extra_hint:1"] = "提示A"
        frame.slots["session:abort_reply"] = "被插件中断"
        return frame


async def main() -> None:
    manager = SessionManager()
    bus = EventBus()

    async def replace_ctx(ctx: BeforeTurnCtx) -> BeforeTurnCtx:
        return replace(ctx, content="REPLACED")

    bus.on(BeforeTurnCtx, replace_ctx)

    phase = Phase(
        default_before_turn_modules(
            bus,
            manager,
            plugin_modules=[_SpillPluginModule()],
        ),
        frame_factory=lambda state: BeforeTurnFrame(input=state),
    )
    out = await phase.run(_state())

    # 1. output 是最终 ctx（return 从 slot 取出，emit 替换 + collect 回收都生效）
    check("output 是 BeforeTurnCtx", isinstance(out, BeforeTurnCtx), f"type={type(out)}")
    check("emit 替换生效（content）", out.content == "REPLACED", f"got={out.content}")
    check(
        "collect 回收生效（extra_hints）",
        "提示A" in out.extra_hints,
        f"got={out.extra_hints}",
    )
    check(
        "collect 回收生效（abort）",
        out.abort is True and out.abort_reply == "被插件中断",
        f"abort={out.abort} reply={out.abort_reply}",
    )


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("before_turn return 测试通过。")
