"""Deterministic retrieval benchmark for the plugin-free Akasha runtime.

This is intentionally an engine-level benchmark, not an end-to-end agent QA
benchmark.  The case taxonomy follows the useful LongMemEval slices in the
source project (user fact, preference and knowledge update), while adding the
two properties specific to Akasha: cross-session evidence and temporal
completion.  A fixed embedding provider isolates retrieval regressions from
changes in a remote embedding service.

Run from the repository root::

    python -m test.backend.akasha_retrieval_eval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import httpx

from backend.memory import AkashaMemoryConfig, AkashaMemoryRuntime
from backend.session_store import SessionStore


@dataclass(frozen=True)
class CorpusTurn:
    """One completed canonical conversation turn used as retrieval evidence."""

    turn_id: str
    session_id: str
    user_text: str
    assistant_text: str
    embedding_topic: str


@dataclass(frozen=True)
class RecallCase:
    """A query and the exact source turns that must be recovered."""

    case_id: str
    question_type: str
    query: str
    expected_turn_ids: tuple[str, ...]
    embedding_topic: str


@dataclass(frozen=True)
class RecallCaseResult:
    case_id: str
    question_type: str
    expected_turn_ids: tuple[str, ...]
    retrieved_turn_ids: tuple[str, ...]
    lanes: tuple[str, ...]
    recall_at: dict[int, float]
    hit_at: dict[int, float]
    reciprocal_rank: float
    ndcg_at: dict[int, float]


class FixedTopicEmbedder:
    """Stable semantic boundary for a retrieval-engine test.

    It does not claim to be a realistic embedding model.  A source turn and a
    paraphrased question assigned to the same topic get the same unit basis
    vector; the engine is still responsible for blending dense/sparse scores,
    ordering results and adding completion evidence.
    """

    def __init__(self, text_topics: dict[str, str]) -> None:
        topics = sorted(set(text_topics.values()))
        self._vectors = {
            topic: [1.0 if index == position else 0.0 for index in range(len(topics))]
            for position, topic in enumerate(topics)
        }
        self._text_topics = dict(text_topics)

    async def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(self._vectors[self._text_topics[str(text)]]) for text in texts]


def benchmark_corpus() -> tuple[tuple[CorpusTurn, ...], tuple[RecallCase, ...]]:
    """Return a compact, inspectable LongMemEval-inspired corpus.

    ``knowledge-update`` expects the newest fact, rather than merely any
    matching historical record.  ``temporal-completion`` expects a successor
    turn that has no semantic overlap with the query, so it exercises the
    Akasha completion lane rather than duplicate vector hits.
    """

    corpus = (
        CorpusTurn(
            "user-location", "eval:identity", "我常住杭州滨江区。", "已记录你的常住区域是杭州滨江区。", "location"
        ),
        CorpusTurn(
            "hotel-preference", "eval:travel", "旅行订酒店时，我只考虑步行可到地铁站的酒店。", "以后会优先筛选靠近地铁站的酒店。", "hotel"
        ),
        CorpusTurn(
            "region-beijing", "eval:ops", "生产数据库主区域目前在北京。", "当前生产数据库主区域是北京。", "region"
        ),
        CorpusTurn(
            "region-shanghai", "eval:ops", "刚完成迁移，生产数据库主区域改为上海。", "更新后生产数据库主区域是上海。", "region"
        ),
        CorpusTurn(
            "aurora-release", "eval:aurora", "Aurora 服务今天完成发布。", "Aurora 发布已完成，开始收集运行指标。", "release"
        ),
        CorpusTurn(
            "aurora-docs", "eval:aurora", "请整理基准报告。", "会处理基准报告。", "docs"
        ),
        CorpusTurn(
            "cross-session-tax", "eval:finance", "报税资料保存在加密文件夹 taxes-2026。", "税务资料的位置是加密文件夹 taxes-2026。", "finance"
        ),
        CorpusTurn(
            "lexical-canary", "eval:delivery", "发布开关名称是 canary-release-17。", "确认，canary-release-17 用于灰度发布。", "lexical-source"
        ),
        # Distractors deliberately share neither terms nor topic with targets.
        CorpusTurn("distractor-legal", "eval:legal", "合同修订由法务团队审核。", "法务审核尚未完成。", "legal"),
        CorpusTurn("distractor-design", "eval:design", "仪表盘使用紧凑表格布局。", "已采用紧凑表格布局。", "design"),
        CorpusTurn("distractor-security", "eval:security", "密钥每九十天轮换一次。", "密钥轮换策略已启用。", "security"),
        CorpusTurn("distractor-archive", "eval:archive", "旧日志将在三十天后归档。", "归档计划已登记。", "archive"),
    )
    cases = (
        RecallCase(
            "single-session-user", "single-session-user", "我以前说过自己住在哪个城区？", ("user-location",), "location"
        ),
        RecallCase(
            "single-session-preference", "single-session-preference", "帮我找住宿时，要遵守怎样的交通偏好？", ("hotel-preference",), "hotel"
        ),
        RecallCase(
            "knowledge-update", "knowledge-update", "迁移完成后，生产数据库的主区域在哪里？", ("region-shanghai",), "region"
        ),
        RecallCase(
            "cross-session-fact", "cross-session-fact", "报税文件放在什么地方？", ("cross-session-tax",), "finance"
        ),
        RecallCase(
            "temporal-completion", "temporal-completion", "Aurora 完成发布后的后继操作是什么？", ("aurora-docs",), "release"
        ),
        # The query vector is intentionally unrelated.  Only the literal
        # ``canary`` evidence can retrieve this target, testing BM25 fallback.
        RecallCase(
            "lexical-fallback", "lexical-fallback", "canary 的发布开关是哪一个？", ("lexical-canary",), "lexical-query"
        ),
    )
    return corpus, cases


async def run_recall_benchmark(
    workspace: Path,
    *,
    cutoffs: Iterable[int] = (1, 3, 5),
) -> dict[str, object]:
    """Build a private corpus, run every recall query, and aggregate metrics."""

    normalized_cutoffs = tuple(sorted({int(value) for value in cutoffs if int(value) > 0}))
    if not normalized_cutoffs:
        raise ValueError("至少需要一个正数 Recall@K cutoff")
    corpus, cases = benchmark_corpus()
    topics = {
        text: topic
        for turn in corpus
        for text, topic in (
            (turn.user_text, turn.embedding_topic),
            (turn.assistant_text, turn.embedding_topic),
        )
    }
    topics.update({case.query: case.embedding_topic for case in cases})
    maximum = max(normalized_cutoffs)
    workspace.mkdir(parents=True, exist_ok=True)
    store = SessionStore(workspace / "sessions.db")
    http = httpx.AsyncClient()
    memory = AkashaMemoryRuntime(
        store,
        workspace,
        http,
        config=AkashaMemoryConfig(context_recall_limit=max(10, maximum)),
        embedder=FixedTopicEmbedder(topics),
    )
    try:
        await memory.start_or_rebuild()
        for turn in corpus:
            user_id = store.record_user_message(turn.session_id, turn.turn_id, turn.user_text)
            assistant_id = store.complete_turn(turn.session_id, turn.turn_id, turn.assistant_text)
            await memory.commit_turn(
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                user_message_id=user_id,
                assistant_message_id=assistant_id,
                ticket=None,
            )

        results: list[RecallCaseResult] = []
        for case in cases:
            recalled = await memory.recall(
                session_id=f"eval:question:{case.case_id}",
                turn_id=f"question:{case.case_id}",
                text=case.query,
                limit=maximum,
            )
            retrieved = tuple(hit.turn_id for hit in recalled.records)
            results.append(_score_case(case, retrieved, tuple(hit.lane for hit in recalled.records), normalized_cutoffs))
        return _aggregate_results(results, normalized_cutoffs)
    finally:
        await memory.aclose()
        await http.aclose()


def _score_case(
    case: RecallCase,
    retrieved: tuple[str, ...],
    lanes: tuple[str, ...],
    cutoffs: tuple[int, ...],
) -> RecallCaseResult:
    expected = set(case.expected_turn_ids)
    first_rank = next((index + 1 for index, turn_id in enumerate(retrieved) if turn_id in expected), None)
    recall_at: dict[int, float] = {}
    hit_at: dict[int, float] = {}
    ndcg_at: dict[int, float] = {}
    for cutoff in cutoffs:
        top = retrieved[:cutoff]
        relevant = [turn_id for turn_id in top if turn_id in expected]
        recall_at[cutoff] = len(set(relevant)) / len(expected)
        hit_at[cutoff] = float(bool(relevant))
        dcg = sum(1.0 / math.log2(index + 2) for index, turn_id in enumerate(top) if turn_id in expected)
        ideal = sum(1.0 / math.log2(index + 2) for index in range(min(cutoff, len(expected))))
        ndcg_at[cutoff] = dcg / ideal if ideal else 0.0
    return RecallCaseResult(
        case_id=case.case_id,
        question_type=case.question_type,
        expected_turn_ids=case.expected_turn_ids,
        retrieved_turn_ids=retrieved,
        lanes=lanes,
        recall_at=recall_at,
        hit_at=hit_at,
        reciprocal_rank=0.0 if first_rank is None else 1.0 / first_rank,
        ndcg_at=ndcg_at,
    )


def _aggregate_results(results: Sequence[RecallCaseResult], cutoffs: tuple[int, ...]) -> dict[str, object]:
    groups: dict[str, list[RecallCaseResult]] = defaultdict(list)
    for result in results:
        groups[result.question_type].append(result)

    def aggregate(items: Sequence[RecallCaseResult]) -> dict[str, object]:
        count = len(items)
        return {
            "n": count,
            "recall_at": {str(k): round(sum(item.recall_at[k] for item in items) / count, 4) for k in cutoffs},
            "hit_at": {str(k): round(sum(item.hit_at[k] for item in items) / count, 4) for k in cutoffs},
            "mrr": round(sum(item.reciprocal_rank for item in items) / count, 4),
            "ndcg_at": {str(k): round(sum(item.ndcg_at[k] for item in items) / count, 4) for k in cutoffs},
        }

    return {
        "benchmark": "akasha-retrieval-v1",
        "scope": "engine-only; fixed embedding boundary; no LLM answer generation",
        "overall": aggregate(results),
        "by_type": {name: aggregate(items) for name, items in sorted(groups.items())},
        "cases": [
            {
                "case_id": result.case_id,
                "question_type": result.question_type,
                "expected_turn_ids": list(result.expected_turn_ids),
                "retrieved_turn_ids": list(result.retrieved_turn_ids),
                "lanes": list(result.lanes),
                "recall_at": {str(k): value for k, value in result.recall_at.items()},
                "hit_at": {str(k): value for k, value in result.hit_at.items()},
                "reciprocal_rank": result.reciprocal_rank,
                "ndcg_at": {str(k): value for k, value in result.ndcg_at.items()},
            }
            for result in results
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic Akasha retrieval evaluation")
    parser.add_argument("--workspace", type=Path, help="保留评测数据库的目录；默认使用临时目录")
    parser.add_argument("--cutoffs", nargs="+", type=int, default=[1, 3, 5])
    args = parser.parse_args()

    if args.workspace is not None:
        result = asyncio.run(run_recall_benchmark(args.workspace, cutoffs=args.cutoffs))
    else:
        with tempfile.TemporaryDirectory(prefix="akasha-recall-eval-") as directory:
            result = asyncio.run(run_recall_benchmark(Path(directory), cutoffs=args.cutoffs))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
