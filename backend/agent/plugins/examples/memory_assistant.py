"""记忆助手插件（MemoryHelperPlugin）——一个有实际意义的完整插件范例。

本插件按「记忆助手」蓝图落地，覆盖插件的三条贡献通道 + 一条技能注入：

1. tool ×2（@tool，走 ToolRegistry）：
   - remember(content, category) —— 写入一条记忆（read-write）
   - recall(query)                —— 按关键词检索记忆（read-only）
2. skill（规则文本，经 before_turn 模块注入 extra_hints）：
   说明「何时用 remember/recall、category 怎么选」。复刻 runtime 不渲染
   skill_names，所以技能以「规则文本」形式直接进 prompt，而非 skill_roots()
   目录扫描。
3. module ×2（PhaseModule，注入模块链）：
   - _InjectMemoryModule   —— before_turn，把技能规则 + 当前记忆块塞进 extra_hints；
   - _ExtractMemoryModule  —— after_reasoning，用二次 LLM 从回复里提取记忆写库。
4. handler ×1（@on_after_turn，TAP）：广播「本次 turn 记忆库 +N 条」。

存储：JSON 文件（数组 [{content, category, created_at}]），category 限
preference / fact / decision 三类。跨 turn 持久化，实现真正的「记忆」语义。

一条 turn 的数据流：
  before_turn 注入记忆块+规则 → LLM（可调 recall/remember）→
  after_reasoning 二次 LLM 提取 → after_turn 广播 +N。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from agent.core.reasoner import InjectLLM, LLMResponse
from agent.lifecycle.phases.after_reasoning import AfterReasoningFrame
from agent.lifecycle.phases.before_turn import BeforeTurnFrame
from agent.lifecycle.types import AfterReasoningCtx, AfterTurnCtx, BeforeTurnCtx
from agent.plugins import Plugin, on_after_turn, tool


# 允许的记忆分类（偏好 / 事实 / 决定）
VALID_CATEGORIES = ("preference", "fact", "decision")

# 技能规则文本：注入 extra_hints，告诉 LLM 何时该用两个记忆工具。
SKILL_RULE = (
    "你有两个记忆工具：remember(content, category) 用于写入一条长期记忆，"
    "recall(query) 用于按关键词检索已存记忆。category 只能是 "
    "preference(用户偏好)、fact(客观事实)、decision(用户决定) 之一。"
    "当用户明确表达偏好、陈述稳定事实、或做出决定时，应调用 remember 记住；"
    "当需要回忆用户说过的话时，先用 recall 检索。"
)


class MemoryStore:
    """JSON 文件记忆库：封装读 / 写 / 检索 / 计数。

    数据模型：JSON 数组，每项 {"content": str, "category": str, "created_at": str}。
    同时维护一个「本次 turn 新增条数」计数（last_added），供 after_turn handler
    广播——tool 和模块都引用同一个 store 实例，计数自然跨阶段共享。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._last_added = 0

    def load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, content: str, category: str) -> dict[str, Any]:
        """写入一条记忆，返回写入项。计数 last_added +1。"""
        item = {
            "content": content.strip(),
            "category": category,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        items = self.load()
        items.append(item)
        self._save(items)
        self._last_added += 1
        return item

    def add_many(self, entries: list[dict[str, Any]]) -> int:
        """批量写入（供提取模块用），返回实际写入条数。计数按条累加。"""
        items = self.load()
        added = 0
        for entry in entries:
            content = str(entry.get("content", "")).strip()
            category = str(entry.get("category", "")).strip()
            if not content or category not in VALID_CATEGORIES:
                continue
            items.append(
                {
                    "content": content,
                    "category": category,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            added += 1
        if added:
            self._save(items)
            self._last_added += added
        return added

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """按空格分词做关键词匹配，任一关键词命中 content 即返回。"""
        keywords = [k for k in query.split() if k]
        hits: list[dict[str, Any]] = []
        for item in self.load():
            content = str(item.get("content", ""))
            if not keywords or any(k in content for k in keywords):
                hits.append(item)
        return hits[:limit]

    def pop_last_added(self) -> int:
        """读取并清零「本次 turn 新增条数」（供 after_turn 广播后复位）。"""
        n = self._last_added
        self._last_added = 0
        return n


class _InjectMemoryModule:
    """before_turn 注入模块：把技能规则 + 当前记忆块塞进 extra_hints。

    - slot     : memory.inject（插件模块，非 builtin 前缀）
    - requires : before_turn.build_ctx（排在 build_ctx 之后；又因插件模块
      排在同依赖的 builtin 之前，故落在 emit 之前——即在 GATE handler 之前
      就把记忆注入 ctx）。
    - 产出     : 直接改 ctx.extra_hints（append 规则 + 记忆块），走后续
      before_reasoning.build_ctx → prompt_render.render 的 extra_hints 链路，
      最终被 build_context_hint_message 包成 [plugin_hints] 消息进 prompt。
    """

    slot = "memory.inject"
    requires = ("before_turn.build_ctx",)

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        ctx = cast(BeforeTurnCtx, frame.slots["session:ctx"])
        # ① 注入技能规则
        ctx.extra_hints.append(SKILL_RULE)
        # ② 注入当前记忆块（供 LLM 直接看到已有的记忆）
        items = self._store.load()
        if items:
            lines = [f"- [{it['category']}] {it['content']}" for it in items]
            ctx.extra_hints.append("当前记忆库：\n" + "\n".join(lines))
        else:
            ctx.extra_hints.append("当前记忆库为空。")
        print(
            f"  [模块 memory.inject] 注入技能规则 + {len(items)} 条记忆到 extra_hints"
        )
        return frame


class _ExtractMemoryModule:
    """after_reasoning 提取模块：用二次 LLM 从回复里提取记忆，写库。

    - slot     : memory.extract（插件模块）
    - requires : after_reasoning.build_ctx（排在 build_ctx 之后、emit 之前，
      即提取的是 LLM 原始回复，未经下游插件改写）。
    - 行为     : 把 ctx.reply 喂给二次 LLM（extractor_llm），要求输出 JSON 数组
      [{content, category}]，解析后 add_many 写库；无 extractor 则跳过。
    """

    slot = "memory.extract"
    requires = ("after_reasoning.build_ctx",)

    def __init__(self, store: MemoryStore, extractor_llm: InjectLLM | None) -> None:
        self._store = store
        self._extractor_llm = extractor_llm

    async def run(self, frame: AfterReasoningFrame) -> AfterReasoningFrame:
        if self._extractor_llm is None:
            return frame
        ctx = cast(AfterReasoningCtx, frame.slots["reasoning:ctx"])
        reply = ctx.reply or ""
        if not reply.strip():
            return frame

        prompt = [
            {
                "role": "user",
                "content": (
                    "从以下助手回复中提取值得长期记住的信息。"
                    "category 只能是 preference / fact / decision 之一。\n"
                    f"回复：{reply}\n\n"
                    "只输出 JSON 数组，每项形如 "
                    '{"content": "...", "category": "..."}；没有则输出 []。'
                ),
            }
        ]
        response: LLMResponse = await self._extractor_llm(prompt, [])
        entries = self._parse_entries(response.content)
        if entries:
            added = self._store.add_many(entries)
            print(f"  [模块 memory.extract] 二次 LLM 提取并写入 {added} 条记忆")
        return frame

    @staticmethod
    def _parse_entries(text: str) -> list[dict[str, Any]]:
        """从二次 LLM 的输出里解析出 JSON 数组（容错：掐掉可能的 markdown 围栏）。"""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []


class MemoryHelperPlugin(Plugin):
    """记忆助手插件：tool + skill + module + handler 四条通道合一。"""

    name = "memory-assistant"
    version = "0.1.0"
    desc = "让 agent 具备跨 turn 的长期记忆：remember / recall 工具 + 自动提取"

    def __init__(
        self,
        storage_path: str | Path,
        *,
        extractor_llm: InjectLLM | None = None,
    ) -> None:
        self._store = MemoryStore(storage_path)
        self._extractor_llm = extractor_llm

    # ── ① tool 通道 ──────────────────────────────────────────────

    @tool("remember", risk="read-write")
    async def remember(self, event: object, content: str, category: str) -> str:
        """写入一条长期记忆。

        :param content: 要记住的内容，一句话概括
        :param category: 分类，preference / fact / decision 之一
        """
        if category not in VALID_CATEGORIES:
            return f"分类无效：{category}（应为 {', '.join(VALID_CATEGORIES)}）"
        item = self._store.add(content, category)
        return f"已记住 [{item['category']}] {item['content']}"

    @tool("recall", risk="read-only")
    async def recall(self, event: object, query: str) -> str:
        """按关键词检索已存记忆。

        :param query: 检索关键词（可多个，空格分隔）
        """
        hits = self._store.search(query)
        if not hits:
            return "没有匹配的记忆"
        lines = [f"- [{it['category']}] {it['content']}" for it in hits]
        return "匹配到的记忆：\n" + "\n".join(lines)

    # ── ② module 通道 ────────────────────────────────────────────

    def before_turn_modules(self) -> list[object]:
        return [_InjectMemoryModule(self._store)]

    def after_reasoning_modules(self) -> list[object]:
        if self._extractor_llm is None:
            return []
        return [_ExtractMemoryModule(self._store, self._extractor_llm)]

    # ── ③ handler 通道 ───────────────────────────────────────────

    @on_after_turn()
    async def broadcast_memory_change(self, event: AfterTurnCtx) -> None:
        added = self._store.pop_last_added()
        total = len(self._store.load())
        print(
            f"  [handler after_turn TAP] 记忆库本次 +{added} 条，当前共 {total} 条"
        )
