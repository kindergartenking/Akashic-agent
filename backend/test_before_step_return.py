"""before_step「return」的简单测试（5 模块链端到端）。

before_step 的职责是「每轮组 ctx + 注入 hints + 可 early_stop」。本测试走完整
5 模块链，同时挂上 emit handler 和插件模块，验证 output 是「build_ctx 组装 +
emit 改字段 + collect 回收（extra_hint + abort_reply→early_stop）+ inject_hints
副作用」之后的最终 ctx：

1. build_ctx：iteration / token 估算 / visible_names 转 frozenset；
2. emit：handler 改 extra_hints → 生效；
3. collect：外溢 extra_hint 回收、abort_reply 映射为 early_stop；
4. inject_hints：extra_hints 被包成 [plugin_hints] 消息塞进 input.messages（副作用）；
5. output 是 BeforeStepCtx。

运行：<python> backend/test_before_step_return.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.before_step import (
    BeforeStepFrame,
    default_before_step_modules,
)
from agent.lifecycle.types import BeforeStepCtx, BeforeStepInput
from bus.event_bus import EventBus

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


def _input() -> BeforeStepInput:
    return BeforeStepInput(
        session_key="cli:c1",
        channel="cli",
        chat_id="c1",
        iteration=0,
        messages=[{"role": "user", "content": "hello"}],
        visible_names={"echo", "search"},
    )


class _SpillPluginModule:
    """测试用插件模块：往 slot 外溢 extra_hint + abort_reply。"""

    slot = "plugin.spill"
    requires = ("before_step.emit",)

    async def run(self, frame: BeforeStepFrame) -> BeforeStepFrame:
        frame.slots["step:extra_hint:1"] = "外溢提示"
        frame.slots["step:abort_reply"] = "提前结束"
        return frame


async def main() -> None:
    bus = EventBus()

    async def add_hint(ctx: BeforeStepCtx) -> BeforeStepCtx:
        ctx.extra_hints.append("emit提示")
        return ctx

    bus.on(BeforeStepCtx, add_hint)

    inp = _input()
    phase = Phase(
        default_before_step_modules(
            bus,
            plugin_modules=[_SpillPluginModule()],
        ),
        frame_factory=lambda i: BeforeStepFrame(input=i),
    )
    out = await phase.run(inp)

    # 1. output 类型
    check("output 是 BeforeStepCtx", isinstance(out, BeforeStepCtx), f"type={type(out)}")
    # 2. build_ctx 组装
    check("build_ctx iteration 正确", out.iteration == 0, f"got={out.iteration}")
    check(
        "build_ctx visible_names 转 frozenset",
        out.visible_tool_names == frozenset({"echo", "search"}),
        f"got={out.visible_tool_names}",
    )
    check(
        "build_ctx token 估算 >= 0",
        out.input_tokens_estimate >= 0,
        f"got={out.input_tokens_estimate}",
    )
    # 3. emit 改字段生效
    check("emit 改 extra_hints 生效", "emit提示" in out.extra_hints, f"got={out.extra_hints}")
    # 4. collect 回收
    check("collect 回收 extra_hint 生效", "外溢提示" in out.extra_hints, f"got={out.extra_hints}")
    check(
        "collect 回收 abort_reply → early_stop",
        out.early_stop is True and out.early_stop_reply == "提前结束",
        f"early_stop={out.early_stop} reply={out.early_stop_reply}",
    )
    # 5. inject_hints 副作用：extra_hints 被塞进 input.messages
    hint_msg = inp.messages[-1] if len(inp.messages) > 1 else None
    check(
        "inject_hints 塞入 [plugin_hints] 消息",
        hint_msg is not None
        and hint_msg.get("role") == "user"
        and "[plugin_hints]" in str(hint_msg.get("content", "")),
        f"got={hint_msg}",
    )


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("before_step return 测试通过。")
