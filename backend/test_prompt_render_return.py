"""prompt_render「return」的简单测试（5 模块链端到端 + section 回收单元检查）。

return 模块的职责是「把渲染好的 PromptRenderResult 设为阶段 output」。本测试分两部分：

A. 端到端（走完整 5 模块链）：
   1. build_ctx 搬运 history → messages 里应含 history 的 system 消息；
   2. emit handler 替换 content → messages 里 user 消息 content 应是被替换后的值；
   3. 插件外溢 extra_hint → collect 回收后 render 包成 hint message 追加到 messages 末尾；
   4. output 是 PromptRenderResult。

B. section 回收单元检查（桩 render 不消费 section，端到端观察不到，故单独直测 collect_exports 模块）：
   外溢 prompt:section_top: 前缀的槽 → ctx.system_sections_top 应被填（对象直用 / 字符串包装）。

运行：<python> backend/test_prompt_render_return.py
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

from agent.context import ContextBuilder
from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.prompt_render import (
    PromptRenderFrame,
    default_prompt_render_modules,
)
from agent.lifecycle.types import (
    PromptRenderCtx,
    PromptRenderInput,
    PromptRenderResult,
)
from agent.prompting import PromptSectionRender
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


def _input() -> PromptRenderInput:
    ts = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    return PromptRenderInput(
        session_key="cli:c1",
        channel="cli",
        chat_id="c1",
        content="hello",
        media=None,
        timestamp=ts,
        history=[{"role": "system", "content": "base"}],
        skill_names=["skill_a"],
        retrieved_memory_block="记忆块",
        disabled_sections=set(),
        turn_injection_prompt="",
        extra_hints=None,
    )


class _SpillPluginModule:
    """测试用插件模块：往 slot 外溢 extra_hint。"""

    slot = "plugin.spill"
    requires = ("prompt_render.emit",)

    async def run(self, frame: PromptRenderFrame) -> PromptRenderFrame:
        frame.slots["prompt:extra_hint:1"] = "提示A"
        return frame


async def main() -> None:
    bus = EventBus()
    context = ContextBuilder()

    async def replace_ctx(ctx: PromptRenderCtx) -> PromptRenderCtx:
        return replace(ctx, content="REPLACED")

    bus.on(PromptRenderCtx, replace_ctx)

    phase = Phase(
        default_prompt_render_modules(
            bus,
            context,
            plugin_modules=[_SpillPluginModule()],
        ),
        frame_factory=lambda input: PromptRenderFrame(input=input),
    )
    out = await phase.run(_input())

    # 1. output 类型
    check(
        "output 是 PromptRenderResult",
        isinstance(out, PromptRenderResult),
        f"type={type(out)}",
    )
    # 2. build_ctx 搬运 history → messages 含 system 消息
    check(
        "build_ctx 搬运 history 生效",
        out.messages[0] == {"role": "system", "content": "base"},
        f"got={out.messages[0] if out.messages else None}",
    )
    # 3. emit 替换 content 生效
    user_msg = next((m for m in out.messages if m.get("role") == "user"), None)
    check(
        "emit 替换 content 生效",
        user_msg is not None and user_msg.get("content") == "REPLACED",
        f"got={user_msg}",
    )
    # 4. collect 回收 extra_hint → 末尾 hint message
    hint_msg = out.messages[-1] if out.messages else None
    check(
        "collect 回收 extra_hint 生效",
        hint_msg is not None
        and hint_msg.get("role") == "user"
        and "[plugin_hints]" in str(hint_msg.get("content", "")),
        f"got={hint_msg}",
    )

    # B. section 回收单元检查（桩 render 不消费 section，端到端看不到，直测 collect_exports）
    from agent.lifecycle.phases.prompt_render import _CollectPromptExportSlotsModule

    frame = PromptRenderFrame(input=_input())
    frame.slots["prompt:ctx"] = PromptRenderCtx(
        session_key="cli:c1",
        channel="cli",
        chat_id="c1",
        content="hello",
        media=None,
        timestamp=_input().timestamp,
        history=[],
        skill_names=None,
        retrieved_memory_block="",
        disabled_sections=set(),
        turn_injection_prompt="",
    )
    frame.slots["prompt:section_top:1"] = PromptSectionRender(
        name="sec_obj", content="对象段", is_static=False
    )
    frame.slots["prompt:section_top:2"] = "字符串段"
    await _CollectPromptExportSlotsModule().run(frame)
    ctx = frame.slots["prompt:ctx"]
    top_names = [s.name for s in ctx.system_sections_top]
    check(
        "collect 回收 section_top（对象直用）",
        "sec_obj" in top_names,
        f"got={top_names}",
    )
    check(
        "collect 回收 section_top（字符串包装）",
        any(s.name == "2" and s.content == "字符串段" for s in ctx.system_sections_top),
        f"got={[(s.name, s.content) for s in ctx.system_sections_top]}",
    )


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("prompt_render return 测试通过。")
