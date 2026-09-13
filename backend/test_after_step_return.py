"""after_step「return」的简单测试（5 实例链端到端）。

after_step 的职责是「每轮广播快照」。本测试走完整 5 实例链（copy_input →
collect_pre → fanout → collect_post → return），同时挂上 fanout 观察者和插件模块，
验证 output 是「copy_input 搬快照 + collect_pre 回收 pre telemetry + fanout 并发广播
+ collect_post 回收 post telemetry + return」之后的最终 ctx：

1. copy_input：input 快照字段原样传下去；
2. fanout：观察者被广播、读到快照；一个观察者抛异常不中断、不影响另一个；
3. collect_pre：fanout 前外溢的 step:telemetry:pre 合并进 extra_metadata；
4. collect_post：fanout 后外溢的 step:telemetry:post 也合并进 extra_metadata；
5. early_stop_reason 槽 → early_stop=True；
6. output 是 AfterStepCtx。

运行：<python> backend/test_after_step_return.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.after_step import (
    AfterStepFrame,
    default_after_step_modules,
)
from agent.lifecycle.types import AfterStepCtx
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


def _input() -> AfterStepCtx:
    return AfterStepCtx(
        session_key="cli:c1",
        channel="cli",
        chat_id="c1",
        iteration=0,
        context_tokens_estimate=1280,
        tools_called=("web_search",),
        partial_reply="",
        tools_used_so_far=("web_search",),
        tool_chain_partial=({"name": "web_search", "result": "ok"},),
        partial_thinking=None,
        has_more=True,
    )


class _PreSpillModule:
    """测试用插件模块：fanout 前外溢 telemetry + early_stop_reason。"""

    slot = "plugin.pre_spill"
    requires = ("after_step.copy_input",)

    async def run(self, frame: AfterStepFrame) -> AfterStepFrame:
        frame.slots["step:telemetry:pre"] = "before_fanout"
        frame.slots["step:early_stop_reason"] = "用户中断"
        return frame


class _PostSpillModule:
    """测试用插件模块：fanout 后补充 telemetry。"""

    slot = "plugin.post_spill"
    requires = ("after_step.fanout",)

    async def run(self, frame: AfterStepFrame) -> AfterStepFrame:
        frame.slots["step:telemetry:post"] = "after_fanout"
        return frame


async def main() -> None:
    bus = EventBus()
    seen: list[tuple[int, tuple[str, ...]]] = []

    async def observer(ctx: AfterStepCtx) -> None:
        seen.append((ctx.iteration, ctx.tools_called))

    async def boom(ctx: AfterStepCtx) -> None:
        raise RuntimeError("boom")

    bus.on(AfterStepCtx, observer)
    bus.on(AfterStepCtx, boom)

    inp = _input()
    phase = Phase(
        default_after_step_modules(
            bus,
            plugin_modules=[_PreSpillModule(), _PostSpillModule()],
        ),
        frame_factory=lambda i: AfterStepFrame(input=i),
    )
    out = await phase.run(inp)

    # 1. output 类型
    check("output 是 AfterStepCtx", isinstance(out, AfterStepCtx), f"type={type(out)}")
    # 2. copy_input 字段传递
    check("copy_input session_key 正确", out.session_key == "cli:c1", f"got={out.session_key}")
    check("copy_input iteration 正确", out.iteration == 0, f"got={out.iteration}")
    check(
        "copy_input tools_called 正确",
        out.tools_called == ("web_search",),
        f"got={out.tools_called}",
    )
    # 3. fanout 广播：观察者被调用、读到快照
    check(
        "fanout 广播给观察者（读到快照）",
        seen == [(0, ("web_search",))],
        f"got={seen}",
    )
    # 4. 失败隔离：boom 抛异常，phase 不中断、observer 仍被执行
    check("fanout 失败隔离（boom 不中断主流程）", len(seen) == 1, f"seen={seen}")
    # 5. collect_pre 回收 fanout 前 telemetry
    check(
        "collect_pre 回收 pre telemetry",
        out.extra_metadata.get("pre") == "before_fanout",
        f"got={out.extra_metadata}",
    )
    # 6. collect_post 回收 fanout 后 telemetry
    check(
        "collect_post 回收 post telemetry",
        out.extra_metadata.get("post") == "after_fanout",
        f"got={out.extra_metadata}",
    )
    # 7. early_stop_reason → early_stop
    check(
        "early_stop_reason → early_stop",
        out.early_stop is True and out.early_stop_reason == "用户中断",
        f"early_stop={out.early_stop} reason={out.early_stop_reason}",
    )


asyncio.run(main())
print(f"\n结果: PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
print("after_step return 测试通过。")
