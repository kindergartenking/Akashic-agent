"""生命周期探针插件：订阅 7 个阶段，打印一次 turn 的完整 runtime 过程。

这是理解 akashic 生命周期的最小插件范例。它本身**不改变任何行为**，只在每个
阶段打印一条带序号的日志，从而直观展示：

1. 7 个阶段的固定触发顺序：before_turn → before_reasoning → prompt_render →
   (before_step → after_step 循环) → after_reasoning → after_turn。
2. GATE 与 TAP 的区别：
   - GATE（before_turn / before_reasoning / prompt_render / before_step /
     after_reasoning）由 bus.emit 串行调用，handler 返回值会替换 ctx、影响后续；
   - TAP（after_step / after_turn）由 bus.fanout 并发调用，handler 返回值被丢弃，
     只能观察快照。
3. 一次工具调用会让 before_step / after_step 循环一次（loop 层）。
"""

from __future__ import annotations

from agent.lifecycle.types import (
    AfterReasoningCtx,
    AfterStepCtx,
    AfterTurnCtx,
    BeforeReasoningCtx,
    BeforeStepCtx,
    BeforeTurnCtx,
    PromptRenderCtx,
)
from agent.plugins import (
    Plugin,
    on_after_reasoning,
    on_after_step,
    on_after_turn,
    on_before_reasoning,
    on_before_step,
    on_before_turn,
    on_prompt_render,
)


class LifecycleProbePlugin(Plugin):
    """纯观察插件：在每个阶段打印一条日志，展示一次 turn 的数据流。"""

    name = "lifecycle-probe"
    version = "0.1.0"
    desc = "打印一次 turn 的 7 阶段数据流"

    # 阶段序号 → 展示名，用于统一日志前缀（loop 层 4/5 会循环出现）
    _stage: dict[str, tuple[int, str]] = {
        "before_turn": (1, "before_turn"),
        "before_reasoning": (2, "before_reasoning"),
        "prompt_render": (3, "prompt_render"),
        "before_step": (4, "before_step"),
        "after_step": (5, "after_step"),
        "after_reasoning": (6, "after_reasoning"),
        "after_turn": (7, "after_turn"),
    }

    def _log(self, name: str, kind: str, detail: str) -> None:
        idx, label = self._stage[name]
        print(f"  [{idx}/7 {label:<17} {kind}] {detail}")

    # ── GATE 阶段：handler 必须返回 ctx（原样返回 = 不改写，但语义上是"放行"）──

    @on_before_turn()
    async def before_turn(self, event: BeforeTurnCtx) -> BeforeTurnCtx:
        self._log(
            "before_turn", "GATE",
            f"content={event.content!r}  session={event.session_key}",
        )
        return event

    @on_before_reasoning()
    async def before_reasoning(self, event: BeforeReasoningCtx) -> BeforeReasoningCtx:
        self._log(
            "before_reasoning", "GATE",
            f"skill_names={event.skill_names}  memory={len(event.retrieved_memory_block)}字符",
        )
        return event

    @on_prompt_render()
    async def prompt_render(self, event: PromptRenderCtx) -> PromptRenderCtx:
        self._log(
            "prompt_render", "GATE",
            f"history={len(event.history)}条  content={event.content!r}",
        )
        return event

    @on_before_step()
    async def before_step(self, event: BeforeStepCtx) -> BeforeStepCtx:
        self._log(
            "before_step", "GATE",
            f"iteration={event.iteration}  tokens≈{event.input_tokens_estimate}",
        )
        return event

    @on_after_reasoning()
    async def after_reasoning(self, event: AfterReasoningCtx) -> AfterReasoningCtx:
        self._log(
            "after_reasoning", "GATE",
            f"reply={event.reply!r}  tools_used={event.tools_used}",
        )
        return event

    # ── TAP 阶段：handler 返回 None，返回值被 fanout 丢弃，只观察快照 ──

    @on_after_step()
    async def after_step(self, event: AfterStepCtx) -> None:
        self._log(
            "after_step", "TAP ",
            f"iteration={event.iteration}  tools={event.tools_called}  partial={event.partial_reply!r}",
        )

    @on_after_turn()
    async def after_turn(self, event: AfterTurnCtx) -> None:
        self._log(
            "after_turn", "TAP ",
            f"reply={event.reply!r}  will_dispatch={event.will_dispatch}",
        )
