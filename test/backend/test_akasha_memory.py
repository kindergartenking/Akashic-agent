from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx

from backend.memory import AkashaMemoryRuntime
from backend.session_store import SessionStore


class _FakeEmbedder:
    """Deterministic vectors so the memory tests never need a provider."""

    async def embed_many(self, texts):
        return [
            [1.0, 0.0] if "alpha" in str(text).lower() else [0.0, 1.0]
            for text in texts
        ]


class _UnavailableEmbedder:
    async def embed_many(self, texts):
        raise RuntimeError("embedding endpoint is offline")


async def _memory(tmp_path: Path, *, embedder=None) -> tuple[SessionStore, AkashaMemoryRuntime, httpx.AsyncClient]:
    sessions = SessionStore(tmp_path / "sessions.db")
    http = httpx.AsyncClient()
    memory = AkashaMemoryRuntime(
        sessions,
        tmp_path,
        http,
        embedder=embedder or _FakeEmbedder(),
    )
    return sessions, memory, http


def test_akasha_recall_is_read_only_then_matching_turn_commits(tmp_path: Path) -> None:
    async def run() -> None:
        sessions, memory, http = await _memory(tmp_path)
        try:
            user_one = sessions.record_user_message("web:one", "turn-1", "alpha 项目的部署约定")
            assistant_one = sessions.complete_turn("web:one", "turn-1", "alpha 使用蓝绿部署")
            await memory.start_or_rebuild()

            user_two = sessions.record_user_message("web:one", "turn-2", "请继续 alpha 的部署讨论")
            recalled = await memory.recall(
                session_id="web:one",
                turn_id="turn-2",
                text="alpha 部署",
            )
            assert recalled.ticket.state_version == 1
            assert recalled.records[0].turn_id == "turn-1"
            assert "alpha 使用蓝绿部署" in recalled.context_block

            # A recall is a preview only: the source and derived state still
            # contain just the earlier completed turn.
            with sqlite3.connect(memory.db_path) as connection:
                assert connection.execute("SELECT COUNT(*) FROM turn_nodes").fetchone()[0] == 1

            assistant_two = sessions.complete_turn("web:one", "turn-2", "继续沿用 alpha 的蓝绿部署")
            committed = await memory.commit_turn(
                session_id="web:one",
                turn_id="turn-2",
                user_message_id=user_two,
                assistant_message_id=assistant_two,
                ticket=recalled.ticket,
            )
            assert committed.state_version == 2
            assert committed.retrieval_recomputed is False

            # The source IDs that bind the causal commit are saved in the
            # derived node, not copied into a free-form summary.
            with sqlite3.connect(memory.db_path) as connection:
                row = connection.execute(
                    "SELECT user_message_id, assistant_message_id FROM turn_nodes WHERE turn_id = 'turn-2'"
                ).fetchone()
                event = connection.execute(
                    "SELECT retrieval_state_version, retrieval_recomputed FROM memory_events WHERE turn_node_id = 1"
                ).fetchone()
            assert row == (user_two, assistant_two)
            assert event == (1, 0)
            assert user_one and assistant_one
        finally:
            await memory.aclose()
            await http.aclose()

    asyncio.run(run())


def test_akasha_completion_lane_and_rebuild_use_sessions_as_source(tmp_path: Path) -> None:
    async def run() -> None:
        sessions, memory, http = await _memory(tmp_path)
        try:
            sessions.record_user_message("web:one", "turn-1", "alpha 功能已经发布")
            sessions.complete_turn("web:one", "turn-1", "alpha 发布记录已保存")
            sessions.record_user_message("web:one", "turn-2", "下一步改写测试文档")
            sessions.complete_turn("web:one", "turn-2", "测试文档安排在明天")
            await memory.start_or_rebuild()

            recalled = await memory.recall(
                session_id="web:other",
                turn_id="turn-new",
                text="alpha 发布",
                limit=5,
            )
            assert recalled.records[0].turn_id == "turn-1"
            assert any(hit.turn_id == "turn-2" and hit.lane == "completion" for hit in recalled.records)

            # Destroying/rebuilding the derived database never loses the
            # canonical conversation, and gives the same lexical recall.
            memory.db_path.unlink()
            await memory.rebuild_from_source()
            rebuilt = await memory.recall(
                session_id="web:other",
                turn_id="turn-new",
                text="alpha 发布",
            )
            assert rebuilt.records[0].turn_id == "turn-1"
            with sqlite3.connect(memory.db_path) as connection:
                assert connection.execute("SELECT value FROM metadata WHERE key = 'state_version'").fetchone() == ("2",)
        finally:
            await memory.aclose()
            await http.aclose()

    asyncio.run(run())


def test_akasha_stale_ticket_recomputes_and_embedding_failure_is_soft(tmp_path: Path) -> None:
    async def run() -> None:
        sessions, memory, http = await _memory(tmp_path, embedder=_UnavailableEmbedder())
        try:
            sessions.record_user_message("web:one", "turn-1", "alpha 原始事实")
            sessions.complete_turn("web:one", "turn-1", "已记录 alpha")
            await memory.start_or_rebuild()

            user_two = sessions.record_user_message("web:one", "turn-2", "alpha 的第二个问题")
            stale = await memory.recall(
                session_id="web:one", turn_id="turn-2", text="alpha"
            )
            assert stale.records[0].turn_id == "turn-1"  # lexical fallback

            user_three = sessions.record_user_message("web:two", "turn-3", "另一个已完成回合")
            assistant_three = sessions.complete_turn("web:two", "turn-3", "完成")
            intermediate = await memory.commit_turn(
                session_id="web:two",
                turn_id="turn-3",
                user_message_id=user_three,
                assistant_message_id=assistant_three,
                ticket=None,
            )
            assert intermediate.retrieval_recomputed is True

            assistant_two = sessions.complete_turn("web:one", "turn-2", "alpha 的第二个答案")
            committed = await memory.commit_turn(
                session_id="web:one",
                turn_id="turn-2",
                user_message_id=user_two,
                assistant_message_id=assistant_two,
                ticket=stale.ticket,
            )
            assert committed.retrieval_recomputed is True
            assert committed.state_version == 3
        finally:
            await memory.aclose()
            await http.aclose()

    asyncio.run(run())
