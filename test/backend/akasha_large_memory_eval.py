"""Build and query a 10,000-turn Akasha memory fixture.

The fixture has 100 topical sessions with 100 completed turns each.  It is
written to the canonical ``sessions.db`` in one transaction, then the real
Akasha rebuild path creates ``memory/akasha.db``.  A deterministic one-hot
embedder keeps the experiment reproducible and avoids measuring a remote
embedding provider instead of the memory engine.

Run from the repository root::

    python -m test.backend.akasha_large_memory_eval --reset
    python -m test.backend.akasha_large_memory_eval --case knowledge-update

The default workspace is ``tmp/akasha-large-eval`` so the normal chat database
is never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import httpx

from backend.memory import AkashaMemoryConfig, AkashaMemoryRuntime
from backend.session_store import SessionStore


TOPIC_COUNT = 100
TURNS_PER_TOPIC = 100
TOTAL_TURNS = TOPIC_COUNT * TURNS_PER_TOPIC


@dataclass(frozen=True)
class FixtureTurn:
    turn_id: str
    session_id: str
    user_message_id: str
    assistant_message_id: str
    user_text: str
    assistant_text: str
    embedding_topic: str
    topic_index: int
    ordinal: int
    completed_at: str


@dataclass(frozen=True)
class BadCase:
    name: str
    query: str
    expected_turn_id: str
    embedding_topic: str
    why_bad: str
    trigger: str


class FixedTopicEmbedder:
    """Deterministic semantic boundary for the large-memory experiment."""

    def __init__(self, text_topics: dict[str, str]) -> None:
        topics = sorted(set(text_topics.values()))
        self._vectors = {
            topic: [1.0 if index == position else 0.0 for index in range(len(topics))]
            for position, topic in enumerate(topics)
        }
        self._text_topics = text_topics

    async def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(self._vectors[self._text_topics[str(text)]]) for text in texts]


def _topic_label(index: int) -> str:
    domains = (
        "支付", "身份", "搜索", "消息", "部署", "监控", "数据库", "缓存", "网络", "安全",
        "账单", "合同", "客户", "仓储", "物流", "推荐", "内容", "媒体", "分析", "报表",
    )
    aspects = ("配置", "故障", "策略", "指标", "流程")
    return f"{domains[index // len(aspects)]}-{aspects[index % len(aspects)]}"


def build_fixture() -> tuple[tuple[FixtureTurn, ...], tuple[BadCase, ...], dict[str, str]]:
    """Create exactly 100 topics x 100 turns and six diagnostic queries."""

    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    turns: list[FixtureTurn] = []
    for topic_index in range(TOPIC_COUNT):
        topic = _topic_label(topic_index)
        session_id = f"large:topic:{topic_index:03d}"
        for ordinal in range(TURNS_PER_TOPIC):
            turn_id = f"large-{topic_index:03d}-{ordinal:03d}"
            user_id = f"large-user-{topic_index:03d}-{ordinal:03d}"
            assistant_id = f"large-assistant-{topic_index:03d}-{ordinal:03d}"
            user_text = (
                f"主题{topic_index:03d}（{topic}）第{ordinal:03d}条记忆："
                f"业务事实编号 F-{topic_index:03d}-{ordinal:03d}，请保留该配置。"
            )
            assistant_text = (
                f"已记录主题{topic_index:03d}（{topic}）的事实 F-{topic_index:03d}-{ordinal:03d}。"
            )
            completed_at = (base_time + timedelta(seconds=topic_index * 1000 + ordinal)).isoformat()
            turns.append(FixtureTurn(
                turn_id, session_id, user_id, assistant_id, user_text, assistant_text,
                f"topic-{topic_index:03d}", topic_index, ordinal, completed_at,
            ))

    def replace(topic_index: int, ordinal: int, **changes: str) -> None:
        position = topic_index * TURNS_PER_TOPIC + ordinal
        current = turns[position]
        turns[position] = FixtureTurn(
            turn_id=changes.get("turn_id", current.turn_id),
            session_id=changes.get("session_id", current.session_id),
            user_message_id=current.user_message_id,
            assistant_message_id=current.assistant_message_id,
            user_text=changes.get("user_text", current.user_text),
            assistant_text=changes.get("assistant_text", current.assistant_text),
            embedding_topic=changes.get("embedding_topic", current.embedding_topic),
            topic_index=current.topic_index,
            ordinal=current.ordinal,
            completed_at=current.completed_at,
        )

    # Same topic, contradictory values.  A generic query can rank the stale
    # record first because both records have equal dense support.
    replace(7, 0, user_text="Northstar 的部署区域是北京。", assistant_text="已记录 Northstar 部署在北京。", embedding_topic="northstar")
    replace(7, 1, user_text="Northstar 已完成迁移，新的部署区域是上海。", assistant_text="更新完成，Northstar 现在部署在上海。", embedding_topic="northstar")
    # Same semantic topic and nearly identical vocabulary: the target is not
    # guaranteed to outrank its sibling when the query omits the unique code.
    replace(8, 0, user_text="Atlas 账单策略采用月度结算，版本 A。", assistant_text="Atlas 月度结算策略版本 A 已登记。", embedding_topic="atlas-billing")
    replace(8, 1, user_text="Atlas 账单策略采用月度结算，版本 B。", assistant_text="Atlas 月度结算策略版本 B 已登记。", embedding_topic="atlas-billing")
    # Successor has an unrelated embedding and vocabulary; it is recoverable
    # only because the completion lane follows the release seed's edge.
    replace(9, 0, user_text="Orion 发布已完成。", assistant_text="Orion 发布完成，进入运行观察。", embedding_topic="orion-release")
    replace(9, 1, user_text="安排值班表。", assistant_text="值班表已经发给运维团队。", embedding_topic="orion-successor")
    # Query vector is intentionally unrelated; this checks BM25 fallback.
    replace(10, 0, user_text="发布开关是 CANARY-2026-17。", assistant_text="确认 CANARY-2026-17 用于灰度。", embedding_topic="lexical-source")

    cases = (
        BadCase(
            "knowledge-update", "Northstar 的部署区域是什么？", "large-007-001", "northstar",
            "旧北京和新上海同时存在；当前排序未显式建模事实有效期。",
            "python -m test.backend.akasha_large_memory_eval --case knowledge-update --limit 5",
        ),
        BadCase(
            "near-topic-collision", "Atlas 账单策略采用什么结算周期？", "large-008-001", "atlas-billing",
            "版本 A/B 语义几乎相同，缺少版本约束时会出现同分或错误版本。",
            "python -m test.backend.akasha_large_memory_eval --case near-topic-collision --limit 5",
        ),
        BadCase(
            "temporal-successor", "Orion 发布完成后的后续安排是什么？", "large-009-001", "orion-release",
            "后继回合词面和向量都不相似，只能依赖 temporal completion，通常排名靠后。",
            "python -m test.backend.akasha_large_memory_eval --case temporal-successor --limit 5",
        ),
        BadCase(
            "lexical-fallback", "CANARY-2026-17 对应哪个发布开关？", "large-010-000", "lexical-query",
            "查询向量与目标向量不一致，只有 BM25 词法命中才能找回。",
            "python -m test.backend.akasha_large_memory_eval --case lexical-fallback --limit 5",
        ),
        BadCase(
            "short-query-noise", "配置", "large-050-099", "topic-050",
            "短查询命中大量主题，前 K 结果缺乏足够区分度；这里故意期望主题 050 的最新回合。",
            "python -m test.backend.akasha_large_memory_eval --case short-query-noise --limit 10",
        ),
        BadCase(
            "cross-session-fact", "主题031 的业务事实编号 F-031-042 是什么？", "large-031-042", "topic-031",
            "证据在另一个 session；当前 Akasha 是全局召回，若产品要求 session 隔离则会成为边界风险。",
            "python -m test.backend.akasha_large_memory_eval --case cross-session-fact --limit 5",
        ),
    )
    text_topics = {
        text: turn.embedding_topic
        for turn in turns
        for text in (turn.user_text, turn.assistant_text)
    }
    text_topics.update({case.query: case.embedding_topic for case in cases})
    return tuple(turns), cases, text_topics


def populate_source(store: SessionStore, turns: Sequence[FixtureTurn]) -> None:
    """Bulk-load the canonical source schema in one transaction."""

    with sqlite3.connect(store.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for turn in turns:
            metadata = json.dumps({"fixture": "akasha-large-v1", "user_id": "eval"}, separators=(",", ":"))
            connection.execute(
                "INSERT INTO sessions(key, created_at, updated_at, metadata, user_id) VALUES (?, ?, ?, ?, 'eval') "
                "ON CONFLICT(key) DO NOTHING",
                (turn.session_id, turn.completed_at, turn.completed_at, metadata),
            )
            connection.execute(
                "INSERT INTO turns(id, session_key, status, input_json, final_response, created_at, started_at, completed_at) "
                "VALUES (?, ?, 'completed', ?, ?, ?, ?, ?)",
                (turn.turn_id, turn.session_id, json.dumps({"text": turn.user_text}, ensure_ascii=False), turn.assistant_text,
                 turn.completed_at, turn.completed_at, turn.completed_at),
            )
            connection.execute(
                "INSERT INTO messages(id, session_key, seq, turn_id, role, content, ts) VALUES (?, ?, ?, ?, 'user', ?, ?)",
                (turn.user_message_id, turn.session_id, turn.ordinal * 2 + 1, turn.turn_id, turn.user_text, turn.completed_at),
            )
            connection.execute(
                "INSERT INTO messages(id, session_key, seq, turn_id, role, content, ts) VALUES (?, ?, ?, ?, 'assistant', ?, ?)",
                (turn.assistant_message_id, turn.session_id, turn.ordinal * 2 + 2, turn.turn_id, turn.assistant_text, turn.completed_at),
            )
        connection.commit()


def _rank(retrieved: Sequence[str], expected: str) -> int | None:
    try:
        return list(retrieved).index(expected) + 1
    except ValueError:
        return None


async def run_large_eval(
    workspace: Path,
    *,
    selected_case: str | None = None,
    limit: int = 10,
    reset: bool = False,
) -> dict[str, object]:
    turns, cases, text_topics = build_fixture()
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    sessions_path = workspace / "sessions.db"
    memory_path = workspace / "memory" / "akasha.db"
    if reset:
        for path in (sessions_path, memory_path):
            if path.exists():
                path.unlink()
    store = SessionStore(sessions_path)
    with sqlite3.connect(sessions_path) as connection:
        existing = int(connection.execute("SELECT COUNT(*) FROM turns WHERE id LIKE 'large-%'").fetchone()[0])
    if existing == 0:
        populate_source(store, turns)
    elif existing != TOTAL_TURNS:
        raise RuntimeError(f"检测到不完整的大型 fixture（{existing}/{TOTAL_TURNS}），请使用 --reset 重建")
    http = httpx.AsyncClient()
    memory = AkashaMemoryRuntime(
        store, workspace, http,
        config=AkashaMemoryConfig(context_recall_limit=max(1, min(40, limit))),
        embedder=FixedTopicEmbedder(text_topics),
    )
    try:
        await memory.start_or_rebuild()
        # ``start_or_rebuild`` intentionally avoids re-embedding an entire
        # history in normal runtime operation.  This benchmark owns a fixed,
        # local embedder, so populate vectors explicitly to exercise dense
        # ranking as well as the lexical index.  The same atomic rebuild path
        # is used; no production code path is changed.
        source_rows = await asyncio.to_thread(store.completed_turns)
        vectors = await memory._embedder.embed_many(  # type: ignore[attr-defined]
            [text for row in source_rows for text in (str(row["user_text"]), str(row["assistant_text"]))]
        )
        if vectors is None or len(vectors) != len(source_rows) * 2:
            raise RuntimeError("固定 embedding fixture 生成数量不匹配")
        new_vectors = {
            str(row["turn_id"]): {"user": vectors[index * 2], "assistant": vectors[index * 2 + 1]}
            for index, row in enumerate(source_rows)
        }
        await asyncio.to_thread(memory._rebuild_sync, source_rows, new_vectors)  # type: ignore[attr-defined]
        target_cases = [case for case in cases if selected_case is None or case.name == selected_case]
        if not target_cases:
            raise ValueError(f"未知 case: {selected_case}")
        outputs = []
        for case in target_cases:
            result = await memory.recall(
                session_id="large:query",
                turn_id=f"query-{case.name}",
                text=case.query,
                limit=max(1, min(40, limit)),
            )
            ids = [hit.turn_id for hit in result.records]
            outputs.append({
                "name": case.name,
                "query": case.query,
                "expected_turn_id": case.expected_turn_id,
                "actual_rank": _rank(ids, case.expected_turn_id),
                "retrieved_turn_ids": ids,
                "lanes": [hit.lane for hit in result.records],
                "scores": [hit.score for hit in result.records],
                "hit_at_1": float(bool(ids and ids[0] == case.expected_turn_id)),
                "hit_at_k": float(case.expected_turn_id in ids),
                "why_bad": case.why_bad,
                "trigger": case.trigger,
                "trace": result.trace,
            })
        with sqlite3.connect(memory.db_path) as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM turn_nodes").fetchone()[0])
        return {
            "benchmark": "akasha-large-memory-v1",
            "fixture": {"topics": TOPIC_COUNT, "turns_per_topic": TURNS_PER_TOPIC, "total_turns": count},
            "workspace": str(workspace),
            "limit": limit,
            "cases": outputs,
        }
    finally:
        await memory.aclose()
        await http.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build/query 10,000-turn Akasha memory fixture")
    parser.add_argument("--workspace", type=Path, default=Path("tmp/akasha-large-eval"))
    parser.add_argument("--case", choices=[case.name for case in build_fixture()[1]])
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--reset", action="store_true", help="清理本脚本 workspace 后重新导入")
    args = parser.parse_args()
    result = asyncio.run(run_large_eval(args.workspace, selected_case=args.case, limit=args.limit, reset=args.reset))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
