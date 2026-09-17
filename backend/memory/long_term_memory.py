"""长期记忆（MEMORY.md 那一层）：提炼 → PENDING 缓冲 → 记忆审计合并。

对齐原版 core/memory 的「两级沉淀」：

1. 提炼（extract_pending）：LLM「记忆提取代理」从对话提取长期记忆候选
   pending_items（7 种 tag），幂等追加到 PENDING.md；
2. 合并（consolidate）：LLM「记忆审计」把「现有 MEMORY.md + PENDING」整体重写
   为精炼档案，写回 MEMORY.md，清空 PENDING；
3. 召回（get_context）：返回 MEMORY.md 全文（精炼档案，直接注入 system）。

铁律：MEMORY.md 只靠「整体重写」更新，绝不增量 append（避免事实堆砌）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ALLOWED_TAGS = (
    "identity",
    "preference",
    "key_info",
    "health_long_term",
    "requested_memory",
    "correction",
    "agent_context",
)


class LongTermMemoryStore:
    """MEMORY.md + PENDING.md 的文件读写。"""

    def __init__(self, memory_dir: Path) -> None:
        memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = memory_dir / "MEMORY.md"
        self.pending_file = memory_dir / "PENDING.md"
        if not self.memory_file.exists():
            self.memory_file.write_text("", encoding="utf-8")
        if not self.pending_file.exists():
            self.pending_file.write_text("", encoding="utf-8")

    def read_memory(self) -> str:
        return self.memory_file.read_text(encoding="utf-8")

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def read_pending(self) -> str:
        return self.pending_file.read_text(encoding="utf-8")

    def append_pending(self, text: str) -> None:
        if not text or not text.strip():
            return
        with open(self.pending_file, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")

    def clear_pending(self) -> None:
        self.pending_file.write_text("", encoding="utf-8")


_EXTRACT_PROMPT = """你是记忆提取代理（Memory Extraction Agent）。从对话中提取用户的长期记忆候选，返回 JSON。

## 允许的 tag（只有这 7 个）
- "identity"：稳定背景事实（身份、学校/专业、长期技术方向、实习/工作经历、长期设备）
- "preference"：稳定偏好、禁忌、审美、价值取向
- "key_info"：用户明确允许保存的 key/token/id/账号
- "health_long_term"：长期健康状态的一阶事实
- "requested_memory"：用户明确要求"长期记住"的关键内容
- "correction"：对现有长期记忆的明确纠正
- "agent_context"：助手操作用户环境所需的工具性配置（已部署的服务、端口、环境变量等）

## 硬规则（任一触发即不提取）
- 只提取 USER 明确表达的内容，ASSISTANT 的建议/解释不提取
- 不提取瞬时状态（带"最近""这周""目前""正在"等）
- 不提取网络运维细节（内网 IP、路由模式、运营商、MAC）
- 不提取时效性数字和瞬时情绪
- 不提取 agent 执行规则、SOP、工具调用顺序
- 不提取方案讨论、架构设计中的端口和地址
- 不提取短期计划、日程、一次性操作

## 输出格式
只输出 JSON 数组，每项形如 {{"tag": "...", "content": "..."}}；没有则输出 []。

对话内容：
{conversation}
"""

_CONSOLIDATE_PROMPT = """你是记忆审计代理。把「现有长期记忆」和「待合并事实」整合成一份精炼的长期记忆档案。

要求：
- 合并同类，同一方向的偏好合并为一条方向性陈述
- 若待合并事实的 tag 是 correction，用它纠正现有记忆中的对应事实
- 丢弃过期、瞬时、矛盾的内容
- 身份事实（机构、部门、岗位、学校/专业）不抽象化，保留具体信息
- 输出纯 Markdown，用 bullet 分点，不要任何解释和标题外的废话

现有长期记忆：
{memory}

待合并事实：
{pending}
"""


class LongTermMemory:
    """长期记忆：提炼 + 合并 + 召回。"""

    def __init__(self, store: LongTermMemoryStore, llm: Any) -> None:
        self._store = store
        self._llm = llm

    async def extract_pending(
        self,
        config: Any,
        user_text: str,
        assistant_text: str,
    ) -> int:
        """LLM 提炼 pending_items，追加到 PENDING.md，返回新增条数。"""
        conversation = f"USER: {user_text}\nASSISTANT: {assistant_text}"
        prompt = _EXTRACT_PROMPT.format(conversation=conversation)
        response = await self._llm.chat(
            config, [{"role": "user", "content": prompt}], tools=[]
        )
        items = self._parse_items(response.content)
        if not items:
            return 0
        lines = [f"- [{item['tag']}] {item['content']}" for item in items]
        self._store.append_pending("\n".join(lines))
        return len(items)

    async def consolidate(self, config: Any) -> bool:
        """记忆审计：LLM 重写 MEMORY.md = 现有 MEMORY + PENDING。返回是否更新。"""
        memory = self._store.read_memory()
        pending = self._store.read_pending()
        if not pending.strip():
            return False
        prompt = _CONSOLIDATE_PROMPT.format(memory=memory or "（空）", pending=pending)
        response = await self._llm.chat(
            config, [{"role": "user", "content": prompt}], tools=[]
        )
        merged = response.content.strip()
        if not merged:
            return False
        self._store.write_memory(merged)
        self._store.clear_pending()
        return True

    def get_context(self) -> str:
        """返回 MEMORY.md 全文（召回注入用）。"""
        return self._store.read_memory()

    @staticmethod
    def _parse_items(text: str) -> list[dict[str, str]]:
        """解析 LLM 返回的 JSON 数组，过滤非法 tag。"""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        items = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            tag = str(entry.get("tag", "")).strip()
            content = str(entry.get("content", "")).strip()
            if tag in _ALLOWED_TAGS and content:
                items.append({"tag": tag, "content": content})
        return items
