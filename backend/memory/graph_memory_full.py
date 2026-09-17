"""图记忆（原版骨架，砍时间边 + 简化 Oja 学习）：GraphMemoryFull。

对齐原版的核心机制，但做了两处裁剪：
1. 砍时间边（temporal）；
2. Oja 学习砍掉重公式（遗忘曲线 erfc、可塑性 eligibility、支持度），只保留核心——
   「共同激活增强 + 不激活衰减 + 总和预算归一化」。

保留：多归属、surprise、dense+BM25 双 lane、PPR、spread/unspread、稀疏化读出。

建图 = 检索 → 激活场 → Oja 学习已有 hub → 建新 hub（多归属）
召回 = dense+BM25 seed → PPR → basin pooling → Sparsemax → Entmax
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

import numpy as np

from backend.memory.static_graph import (
    _cosine,
    _entmax,
    _sparsemax,
    _weighted_logsumexp,
)

_TOKEN_CHUNKS = re.compile(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]{2,}")


def _tokenize(text: str) -> Counter[str]:
    terms: Counter[str] = Counter()
    for chunk in _TOKEN_CHUNKS.findall(text.lower()):
        terms[chunk] += 1
        if any("\u3400" <= c <= "\u9fff" for c in chunk):
            for i in range(len(chunk) - 1):
                terms[chunk[i:i + 2]] += 1
    return terms


def _bm25(
    query: Counter[str],
    document: Counter[str],
    document_frequency: Counter[str],
    total: int,
) -> float:
    if not query or not document or total <= 0:
        return 0.0
    length = max(1, sum(document.values()))
    score = 0.0
    for term, query_tf in query.items():
        tf = document.get(term, 0)
        if not tf:
            continue
        df = document_frequency.get(term, 0)
        idf = math.log1p((total - df + 0.5) / (df + 0.5))
        score += query_tf * idf * (tf * 2.2) / (tf + 1.2 + 0.75 * length / length)
    return score


class GraphMemoryFull:
    """原版骨架：多归属 + Oja（简化） + dense+BM25，砍时间边。"""

    def __init__(
        self,
        *,
        threshold: float = 0.3,
        restart: float = 0.25,
        tolerance: float = 1e-7,
        activation_power: float = 2.0,
        recurrent_budget: float = 1.0,
        learning_rate: float = 0.5,
        lexical_weight: float = 0.40,
        dense_weight: float = 0.60,
    ) -> None:
        self.threshold = threshold
        self.restart = restart
        self.tolerance = tolerance
        self.activation_power = activation_power
        self.recurrent_budget = recurrent_budget
        self.learning_rate = learning_rate
        self.lexical_weight = lexical_weight
        self.dense_weight = dense_weight

        self._turn_embedding: dict[int, np.ndarray] = {}
        self._turn_terms: dict[int, Counter[str]] = {}
        self._document_frequency: Counter[str] = Counter()
        self._hub_members: dict[int, list[int]] = {}
        self._turn_hubs: dict[int, list[int]] = {}
        self._membership: dict[tuple[int, int], float] = {}
        self._next_hub_id = 0
        self._turn_count = 0

    # ── 建图 ──────────────────────────────────────────────────

    def add_turn(self, turn_id: int, embedding: np.ndarray, text: str) -> None:
        terms = _tokenize(text)

        # 先检索（此时不含当前 turn，即"跟历史比"）
        surprise = self._surprise(embedding)
        reserve = self._retrieve(embedding, terms)

        # 再存当前 turn
        self._turn_embedding[turn_id] = embedding
        self._turn_terms[turn_id] = terms
        self._document_frequency.update(terms.keys())
        self._turn_count += 1

        # 激活场：当前 turn(1.0) + reserve 归一化
        activity: dict[int, float] = {turn_id: 1.0}
        if reserve:
            peak = max(reserve.values())
            if peak > 0:
                for node, v in reserve.items():
                    activity[node] = max(activity.get(node, 0.0), v / peak)

        # ① Oja 学习已有 hub（共同激活增强、不激活衰减）
        self._adapt_exposed_hubs(activity)

        # ② 建新 hub（多归属）
        integrated = self._integrated_members(activity)
        if len(integrated) >= 2 and surprise > 0.0:
            self._create_hub(integrated, surprise)

    def _surprise(self, embedding: np.ndarray) -> float:
        if not self._turn_embedding:
            return 1.0
        max_sim = max(_cosine(embedding, emb) for emb in self._turn_embedding.values())
        return 1.0 - max_sim

    def _integrated_members(self, activity: dict[int, float]) -> list[tuple[int, float]]:
        nodes = sorted(activity)
        values = np.array([activity[n] for n in nodes], dtype=np.float64)
        powered = np.power(values, self.activation_power)
        scale = math.log1p(len(nodes))
        weights = _entmax(powered * scale, alpha=1.5)
        return [(nodes[i], w) for i, w in weights]

    def _create_hub(self, integrated: list[tuple[int, float]], surprise: float) -> None:
        values = [(tid, w * surprise) for tid, w in integrated]
        total = sum(w for _, w in values)
        scale = min(1.0, self.recurrent_budget / total) if total > 0 else 1.0

        hub_id = self._next_hub_id
        self._next_hub_id += 1
        members = []
        for tid, value in values:
            weighted = value * scale
            if weighted <= 0.0:
                continue
            self._membership[(tid, hub_id)] = weighted
            self._turn_hubs.setdefault(tid, []).append(hub_id)
            members.append(tid)
        if members:
            self._hub_members[hub_id] = members

    # ── Oja 学习（简化版）─────────────────────────────────────

    def _adapt_exposed_hubs(self, activity: dict[int, float]) -> None:
        """共同激活 → membership 边权增强；不激活 → 衰减；然后总和预算归一化。

        简化版 Oja：delta = lr · hub_activity · (member_activity − hub_activity · old)。
        砍掉原版的 eligibility（可塑性）、support（支持度）、遗忘曲线。
        """
        # 被激活的已有 hub
        exposed: set[int] = set()
        for turn_id, act in activity.items():
            if act <= 0.0:
                continue
            for hub_id in self._turn_hubs.get(turn_id, []):
                exposed.add(hub_id)

        affected: set[int] = set()
        for hub_id in exposed:
            members = self._hub_members[hub_id]
            hub_activity = sum(
                self._membership.get((m, hub_id), 0.0) * activity.get(m, 0.0)
                for m in members
            )
            if hub_activity <= 0.0:
                continue
            for member in members:
                old = self._membership.get((member, hub_id), 0.0)
                member_activity = activity.get(member, 0.0)
                delta = (
                    self.learning_rate
                    * hub_activity
                    * (member_activity - hub_activity * old)
                )
                self._membership[(member, hub_id)] = max(0.0, old + delta)
                affected.add(member)
            self._normalize_hub(hub_id)

        for member in affected:
            self._normalize_membership_source(member)

    def _normalize_hub(self, hub_id: int) -> None:
        """hub 边权总和超过 recurrent_budget 时，等比缩放（照原版）。"""
        members = self._hub_members[hub_id]
        total = sum(self._membership.get((m, hub_id), 0.0) for m in members)
        if total <= self.recurrent_budget:
            return
        scale = self.recurrent_budget / total
        for m in members:
            self._membership[(m, hub_id)] = self._membership.get((m, hub_id), 0.0) * scale

    def _normalize_membership_source(self, turn_id: int) -> None:
        """一个 turn 的所有 membership 边权总和超过预算时，等比缩放（照原版）。"""
        hub_ids = self._turn_hubs.get(turn_id, [])
        total = sum(self._membership.get((turn_id, h), 0.0) for h in hub_ids)
        if total <= self.recurrent_budget:
            return
        scale = self.recurrent_budget / total
        for h in hub_ids:
            self._membership[(turn_id, h)] = self._membership.get((turn_id, h), 0.0) * scale

    # ── 召回 ──────────────────────────────────────────────────

    def recall(
        self, query_embedding: np.ndarray, query_text: str, limit: int = 5
    ) -> list[tuple[int, float]]:
        if not self._turn_embedding:
            return []
        q_terms = _tokenize(query_text)
        reserve = self._retrieve(query_embedding, q_terms)

        hub_scores: dict[int, float] = {}
        for hub_id, members in self._hub_members.items():
            values = [reserve.get(t, 0.0) for t in members]
            if not any(v > 0.0 for v in values):
                continue
            weights = [self._membership.get((t, hub_id), 0.0) for t in members]
            total_w = sum(weights)
            if total_w <= 0.0:
                continue
            normalized = [w / total_w for w in weights]
            hub_scores[hub_id] = _weighted_logsumexp(values, normalized)
        if not hub_scores:
            return []

        hub_ids = list(hub_scores.keys())
        selected = _sparsemax(np.array([hub_scores[h] for h in hub_ids]))

        scored: dict[int, float] = defaultdict(float)
        for idx, hub_w in selected:
            hub_id = hub_ids[idx]
            for t in self._hub_members[hub_id]:
                scored[t] += reserve.get(t, 0.0) * hub_w
        if not scored:
            return []

        ids = list(scored.keys())
        results = [(ids[i], w) for i, w in _entmax(np.array([scored[t] for t in ids]))]
        results.sort(key=lambda x: -x[1])
        return results[:limit]

    # ── 检索 ──────────────────────────────────────────────────

    def _retrieve(self, embedding: np.ndarray, terms: Counter[str]) -> dict[int, float]:
        ids = list(self._turn_embedding.keys())
        if not ids:
            return {}

        dense = np.array([_cosine(embedding, self._turn_embedding[t]) for t in ids])
        dense = np.maximum(dense, 0.0)

        lexical = np.array([
            _bm25(terms, self._turn_terms[t], self._document_frequency, self._turn_count)
            for t in ids
        ])
        lexical_norm = lexical / (1.0 + lexical)

        w_total = self.dense_weight + self.lexical_weight
        combined = (dense * self.dense_weight + lexical_norm * self.lexical_weight) / w_total

        seed = {ids[i]: w for i, w in _sparsemax(combined)}
        return self._ppr(seed)

    def _ppr(self, seed: dict[int, float]) -> dict[int, float]:
        residual: dict[int, float] = dict(seed)
        reserve: dict[int, float] = defaultdict(float)

        while True:
            total = sum(residual.values())
            if total <= self.tolerance:
                break
            node = max(residual, key=residual.get)
            value = residual.pop(node)
            reserve[node] += self.restart * value
            propagated = (1.0 - self.restart) * value
            transitions, unspread = self._transitions(node)
            for target, prob in transitions:
                addition = propagated * prob
                if addition > 0.0:
                    residual[target] = residual.get(target, 0.0) + addition
            if unspread > 0.0:
                for target, prob in seed.items():
                    addition = propagated * unspread * prob
                    if addition > 0.0:
                        residual[target] = residual.get(target, 0.0) + addition

        return dict(reserve)

    def _transitions(self, turn_id: int) -> tuple[list[tuple[int, float]], float]:
        edges: dict[int, float] = defaultdict(float)
        for hub_id in self._turn_hubs.get(turn_id, []):
            for other in self._hub_members[hub_id]:
                if other == turn_id:
                    continue
                w = self._membership.get((other, hub_id), 0.0)
                if w > 0.0:
                    edges[other] += w
        items = list(edges.items())
        total = sum(w for _, w in items)
        if total <= 0.0:
            return [], 1.0
        spread = 1.0 - math.exp(-total)
        transitions = [(n, spread * w / total) for n, w in items]
        unspread = math.exp(-total)
        return transitions, unspread

    # ── 观察接口 ──────────────────────────────────────────────

    @property
    def hub_count(self) -> int:
        return len(self._hub_members)

    def hubs(self) -> dict[int, list[int]]:
        return {h: list(ms) for h, ms in self._hub_members.items()}

    def hubs_of(self, turn_id: int) -> list[int]:
        return list(self._turn_hubs.get(turn_id, []))
