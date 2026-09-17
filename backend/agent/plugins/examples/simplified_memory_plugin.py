"""简化记忆架构插件（SimplifiedMemoryPlugin）。

把「去时间边 + 去边权演化」的简化记忆架构（hub 情景聚类图 + dense+BM25 recall）
封装成插件，挂载到生命周期：

- 召回：before_reasoning 阶段，用当前用户输入召回相关情景记忆，写入
  ctx.retrieved_memory_block（专用字段，随 prompt_render 进 prompt）。
- 落库：after_turn 阶段，把本次 turn（用户输入 + 回复）写入记忆图、建 hub。

embedding 通过依赖注入（embed_fn），插件不绑定具体 provider；不传则跳过。

数据流：
  before_reasoning 召回记忆 → LLM 推理（可见召回的记忆块）→
  after_turn 落库建图。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Awaitable, Callable, cast

from agent.lifecycle.phases.after_turn import AfterTurnFrame
from agent.lifecycle.phases.before_reasoning import BeforeReasoningFrame
from agent.lifecycle.types import AfterTurnCtx, BeforeReasoningCtx
from agent.plugins import Plugin

from backend.memory.simplified_memory import SimplifiedMemory

# embed_fn：text -> embedding 向量（异步）。None 表示不启用 embedding（跳过）。
EmbedFn = Callable[[str], Awaitable[Any]]


class _RecallModule:
    """before_reasoning 召回模块：把相关情景记忆写入 retrieved_memory_block。

    - slot     ：memory.recall（插件模块）
    - requires ：before_reasoning.build_ctx（排在 build_ctx 后、emit 前，
      即召回在 GATE handler 之前、LLM 推理之前完成）。
    """

    slot = "memory.recall"
    requires = ("before_reasoning.build_ctx",)

    def __init__(self, plugin: "SimplifiedMemoryPlugin") -> None:
        self._plugin = plugin

    async def run(self, frame: BeforeReasoningFrame) -> BeforeReasoningFrame:
        ctx = cast(BeforeReasoningCtx, frame.slots["reasoning:ctx"])
        query = ctx.content

        # 记录用户输入，供 after_turn 落库使用（跨阶段共享 plugin 实例）
        self._plugin.last_input = query

        if self._plugin.embed_fn is None:
            return frame

        qemb = await self._plugin.embed_fn(query)
        if qemb is None:
            return frame

        results = self._plugin.memory.recall(query, qemb, limit=5)
        if results:
            lines = []
            for turn_id, score in results:
                text = self._plugin.memory.turns[turn_id]["text"]
                lines.append(f"- [{score:.3f}] {text}")
            block = "\n".join(lines)
            ctx = replace(ctx, retrieved_memory_block=block)
            frame.slots["reasoning:ctx"] = ctx
            print(f"  [模块 memory.recall] 召回 {len(results)} 条情景记忆")
        return frame


class _CommitModule:
    """after_turn 落库模块：把本次 turn 写入记忆图、建 hub。

    - slot     ：memory.commit（插件模块）
    - requires ：after_turn.build_ctx（排在 build_ctx 后，拿到 AfterTurnCtx）。
    """

    slot = "memory.commit"
    requires = ("after_turn.build_ctx",)

    def __init__(self, plugin: "SimplifiedMemoryPlugin") -> None:
        self._plugin = plugin

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        ctx = cast(AfterTurnCtx, frame.slots["turn:ctx"])
        if self._plugin.embed_fn is None:
            return frame

        # 记忆文本 = 用户输入 + 回复（截断回复，聚焦用户关心的话题）
        reply = (ctx.reply or "").strip()
        text = self._plugin.last_input
        if reply and len(reply) < 200:
            text = f"{text}\n{reply}"

        emb = await self._plugin.embed_fn(text)
        if emb is None:
            return frame

        hub = self._plugin.memory.add_turn(text, emb)
        if hub is not None:
            print(f"  [模块 memory.commit] 落库，建 hub{hub}（{len(self._plugin.memory.hubs[hub])} 成员）")
        return frame


class SimplifiedMemoryPlugin(Plugin):
    """简化记忆架构插件：before_reasoning 召回 + after_turn 落库。"""

    name = "simplified-memory"
    version = "0.1.0"
    desc = "简化记忆架构（hub 情景聚类图）：情景记忆召回 + 自动落库建图"

    def __init__(self, *, embed_fn: EmbedFn | None = None) -> None:
        self.memory = SimplifiedMemory()
        self.embed_fn = embed_fn
        self.last_input = ""

    def before_reasoning_modules(self) -> list[object]:
        return [_RecallModule(self)]

    def after_turn_modules(self) -> list[object]:
        return [_CommitModule(self)]
