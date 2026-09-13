"""Session context assembly with a 74% model-window budget."""

from __future__ import annotations

from typing import Any

from .llm_client import LLMClient
from .model_config import ModelConfig
from .session_store import SessionStore


class SessionContextManager:
    def __init__(self, store: SessionStore, llm: LLMClient) -> None:
        self._store = store
        self._llm = llm

    @staticmethod
    def estimate_tokens(messages: list[dict[str, Any]]) -> int:
        # Conservative, dependency-free estimate; four UTF-8 characters ~= one token.
        return max(1, sum(len(str(m.get("content", ""))) for m in messages) // 4)

    async def build(self, config: ModelConfig, session_id: str, *, exclude_turn_id: str | None = None) -> list[dict[str, Any]]:
        history = self._store.all_context_messages(session_id)
        messages = [
            {"role": str(item["role"]), "content": str(item.get("content", ""))}
            for item in history
            if not exclude_turn_id or str(item.get("turn_id")) != exclude_turn_id
            if str(item.get("role")) in {"user", "assistant"} and str(item.get("content", ""))
        ]
        if not messages:
            return []

        budget = max(256, int((config.context_window or 8192) * 0.74))
        if self.estimate_tokens(messages) <= budget:
            return messages

        # A round is represented by a user/assistant pair. Keep the latest five
        # rounds verbatim, and compress everything before that once.
        recent = messages[-10:]
        older = messages[:-10]
        summary = await self._compress(config, older, budget)
        result: list[dict[str, Any]] = []
        if summary:
            result.append({"role": "system", "content": "以下是更早历史对话的压缩摘要，只可作为背景参考：\n" + summary})
        result.extend(recent)
        # Keep the hard cap even if the five latest rounds themselves are large.
        while len(result) > 1 and self.estimate_tokens(result) > budget:
            if result[0].get("role") == "system":
                result.pop(0)
            else:
                result.pop(0)
        return result

    async def _compress(self, config: ModelConfig, older: list[dict[str, Any]], budget: int) -> str:
        if not older:
            return ""
        # Keep summarization request bounded; the persisted history remains intact.
        source = older
        while self.estimate_tokens(source) > max(1024, budget * 2):
            source = source[2:]
        prompt = (
            "请压缩以下历史对话，保留事实、用户偏好、已完成工作、未完成事项和关键标识。"
            "不要编造，不要逐字复述，使用中文简洁分点，输出纯摘要：\n\n"
            + "\n".join(f"{m['role']}: {m['content']}" for m in source)
        )
        response = await self._llm.chat(config, [{"role": "user", "content": prompt}], tools=[])
        return response.content.strip()
