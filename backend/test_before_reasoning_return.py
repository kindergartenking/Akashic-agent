"""before_reasoning「return」的简单测试（5 模块链端到端）。

return 模块的职责是「把链上最终 ctx 设为阶段 output」。因此这个测试走完整
5 模块链，同时挂上 emit handler 和插件模块，验证 output 是「build_ctx 搬运 +
emit 替换 + collect_exports 回收」之后的最终 ctx：

1. build_ctx 正确搬运 before_turn 的决策字段（skill_names / retrieved_memory_block
   / extra_hints）；
2. emit handler 整体替换 content → output.content 应是替换后的值；
3. 插件模块外溢 extra_hints + abort_reply → output 应回收了这些；
4. 三者叠加后，output 是 BeforeReasoningCtx（return 从 slot 取出）。

运行：<python> backend/test_before_reasoning_return.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.before_reasoning import (
    BeforeReasoningFrame,
    default_before_reasoning_modules,
)
from agent.lifecycle.types import (
    BeforeReasoningCtx,
    BeforeReasoningInput,
    BeforeTurnCtx,
    TurnState,
)
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


def _input(manager: SessionManager) -> BeforeReasoningInput:
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
        retrieved_memory_block="记忆块",
        history_messages=(),
        skill_names=["skill_a"],
        extra_hints=["提示A"],
    )
    return BeforeReasoningInput(state=state, before_turn=before_turn)


class _SpillPluginModule:
    """测试用插件模块：往 slot 外溢 extra_hints + abort_reply。"""

    slot = "plugin.spill"
    requires = ("before_reasoning.emit",)

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        frame.slots["reasoning:extra_hint:1"] = "提示B"
        frame.slots["reasoning:abort_reply"] = "被插件中断"
        return frame


async def main() -> None:
    manager = SessionManager()
    bus = EventBus()
    tools = ToolRegistry()

    async def replace_ctx(ctx: BeforeReasoningCtx) -> BeforeReasoningCtx:
        return replace(ctx, content="REPLACED")

    bus.on(BeforeReasoningCtx, replace_ctx)

    phase = Phase(
        default_before_reasoning_modules(
            bus,
            tools,
            manager,
            plugin_modules=[_SpillPluginModule()],
        ),
        frame_factory=lambda input: BeforeReasoningFrame(input=input),
    )
    out = await phase.run(_input(manager))

    # 1. output 是最终 ctx（return 从 slot 取出）
    check(
        "output 是 BeforeReasoningCtx",
        isinstance(out, BeforeReasoningCtx),
        f"type={type(out)}",
    )
    # 2. build_ctx 正确搬运决策字段
    check(
        "build_ctx 搬运 skill_names",
        out.skill_names == ["skill_a"],
        f"got={out.skill_names}",
    )
    check(
        "build_ctx 搬运 retrieved_memory_block",
        out.retrieved_memory_block == "记忆块",
        f"got={out.retrieved_memory_block!r}",
    )
    check(
        "build_ctx 搬运 extra_hints（浅拷贝）",
        "提示A" in out.extra_hints,
        f"got={out.extra_hints}",
    )
    # 3. emit 替换生效
    check("emit 替换生效（content）", out.content == "REPLACED", f"got={out.content}")
    # 4. collect_exports 回收生效
    check(
        "collect 回收生效（extra_hints）",
        "提示B" in out.extra_hints,
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
print("before_reasoning return 测试通过。")
