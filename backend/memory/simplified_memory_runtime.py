"""简化记忆的运行时适配器（SimplifiedMemoryRuntime）。

把「简化记忆架构」（backend.memory.simplified_memory.SimplifiedMemory）包装成
AkashaMemoryRuntime 兼容的接口（recall / commit_turn），供 app.py 直接替换。

- recall：query → embedding → SimplifiedMemory.recall → 拼 context_block（系统消息）。
- commit_turn：从 sessions.db 拿已完成 turn 的 user/assistant 文本 → 落库建图。

持久化到 memory/simplified_memory.db（sqlite）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from backend.memory.akasha import WorkspaceEmbeddingProvider
from backend.memory.simplified_memory import SimplifiedMemory

logger = logging.getLogger(__name__)


class SimplifiedMemoryRuntime:
    def __init__(self, sessions: Any, workspace: Any, http: Any) -> None:
        db_path = str(Path(workspace) / "memory" / "simplified_memory.db")
        self._mem = SimplifiedMemory(db_path=db_path)
        self._provider = WorkspaceEmbeddingProvider(workspace, http)
        self._sessions = sessions
        self._log_path = Path(workspace) / "memory" / "recall.log"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    async def recall(
        self,
        *,
        session_id: str,
        turn_id: str,
        text: str,
        limit: int = 10,
    ):
        emb = await self._one_embedding(text)
        if emb is None:
            return SimpleNamespace(context_block="", ticket=None)
        results = self._mem.recall(text, emb, limit=limit)
        if not results:
            return SimpleNamespace(context_block="", ticket=None)
        lines = []
        for tid, score in results:
            lines.append(f"- {self._mem.turns[tid]['user_text']}")
        context_block = "以下是与你当前问题相关的历史记忆（情景召回）：\n" + "\n".join(lines)
        self._log(f"[简化记忆·召回] query={text!r} 召回 {len(results)} 条：\n{context_block}")
        return SimpleNamespace(context_block=context_block, ticket=None)

    async def commit_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_message_id: str,
        assistant_message_id: str,
        ticket: Any,
    ):
        source = await asyncio.to_thread(
            self._sessions.completed_turn,
            session_id,
            turn_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
        )
        if source is None:
            raise ValueError("简化记忆只能提交已完成且已持久化的 turn")
        user_text = str(source.get("user_text") or "")
        assistant_text = str(source.get("assistant_text") or "")
        user_emb = await self._one_embedding(user_text)
        assistant_emb = (
            await self._one_embedding(assistant_text) if assistant_text else None
        )
        if user_emb is not None:
            hub = self._mem.add_turn(user_text, user_emb, assistant_text, assistant_emb)
            self._log(f"[简化记忆·落库] turn={turn_id} 已写入，建 hub={hub}")
        return SimpleNamespace(turn_id=turn_id)

    async def _one_embedding(self, text: str) -> np.ndarray | None:
        try:
            vectors = await self._provider.embed_many([text])
            return np.array(vectors[0]) if vectors else None
        except Exception:
            return None

    def _log(self, message: str) -> None:
        """召回/落库日志：同时打到 stdout（后台任务可见）并落盘到 recall.log。"""
        print(message, flush=True)
        try:
            with open(self._log_path, "a", encoding="utf-8") as handle:
                handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
        except Exception:
            logger.exception("写召回日志失败")

    async def aclose(self) -> None:
        """内存版无持久化资源，关闭为 no-op（对齐 AkashaMemoryRuntime.aclose）。"""
        return None

    @property
    def memory(self) -> SimplifiedMemory:
        return self._mem
