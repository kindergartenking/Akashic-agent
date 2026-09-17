"""图记忆（砍时间边 + 砍学习版）：GraphMemory。

在原版 akashic 图记忆的基础上，**只砍掉时间边（temporal）和 Oja 在线学习**，
保留其余所有"确定性计算"：

- hub 情景聚类 + membership 边（静态，建好固定）；
- surprise 去重：建边权 = 相似度 × surprise（新颖度），重复信息弱写；
- spread/unspread：PPR 扩散的质量守恒（出边弱则回灌 seed）；
- 召回链路：seed → PPR → basin pooling → Sparsemax → Entmax。

不保留的：时间边（时序扩散）、Oja 学习（边权演化）、continuation 建时序边。
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from backend.memory.static_graph import (
    _cosine,
    _entmax,
    _sparsemax,
    _weighted_logsumexp,
)


class GraphMemory:
    """hub 情景聚类图（无时间边、无学习），带 surprise 去重 + spread/unspread。"""

    def __init__(
        self,
        *,
        threshold: float = 0.3,
        restart: float = 0.25,
        tolerance: float = 1e-7,
    ) -> None:
        self.threshold = threshold
        self.restart = restart
        self.tolerance = tolerance

        self._turn_embedding: dict[int, np.ndarray] = {}
        self._hub_members: dict[int, list[int]] = {}
        self._hub_centroid: dict[int, np.ndarray] = {}
        self._turn_hub: dict[int, int] = {}
        self._membership_weight: dict[tuple[int, int], float] = {}
        self._next_hub_id = 0

    # ── 建图 ──────────────────────────────────────────────────

    def add_turn(self, turn_id: int, embedding: np.ndarray) -> int:
        """落库一个 turn：边权 = 相似度 × surprise（去重）。返回所属 hub_id。"""
        surprise = self._surprise(embedding)   # ★ 先算（此时不含自己，跟历史比）
        self._turn_embedding[turn_id] = embedding

        best_hub: int | None = None
        best_sim = -1.0
        for hub_id, centroid in self._hub_centroid.items():
            sim = _cosine(embedding, centroid)
            if sim > best_sim:
                best_sim = sim
                best_hub = hub_id

        if best_hub is not None and best_sim >= self.threshold:
            weight = best_sim * surprise       # ★ 边权乘 surprise，重复信息弱写
            self._turn_hub[turn_id] = best_hub
            self._hub_members[best_hub].append(turn_id)
            self._membership_weight[(turn_id, best_hub)] = weight
            self._update_centroid(best_hub)
            return best_hub

        hub_id = self._next_hub_id
        self._next_hub_id += 1
        self._hub_members[hub_id] = [turn_id]
        self._hub_centroid[hub_id] = embedding.copy()
        self._turn_hub[turn_id] = hub_id
        self._membership_weight[(turn_id, hub_id)] = 1.0
        return hub_id

    def _surprise(self, embedding: np.ndarray) -> float:
        """残差新颖度：1 - 与历史最相似 turn 的相似度。"""
        if not self._turn_embedding:
            return 1.0
        max_sim = max(
            _cosine(embedding, emb) for emb in self._turn_embedding.values()
        )
        return 1.0 - max_sim

    def _update_centroid(self, hub_id: int) -> None:
        members = self._hub_members[hub_id]
        if members:
            self._hub_centroid[hub_id] = np.mean(
                [self._turn_embedding[t] for t in members], axis=0
            )

    # ── 召回 ──────────────────────────────────────────────────

    def recall(
        self, query_embedding: np.ndarray, limit: int = 5
    ) -> list[tuple[int, float]]:
        if not self._turn_embedding:
            return []

        turn_ids = list(self._turn_embedding.keys())
        sims = np.array(
            [_cosine(query_embedding, self._turn_embedding[t]) for t in turn_ids]
        )
        sims = np.maximum(sims, 0.0)
        seed = {turn_ids[i]: w for i, w in _sparsemax(sims)}

        reserve = self._ppr(seed)

        hub_scores: dict[int, float] = {}
        for hub_id, members in self._hub_members.items():
            values = [reserve.get(t, 0.0) for t in members]
            if not any(v > 0.0 for v in values):
                continue
            weights = [self._membership_weight.get((t, hub_id), 0.0) for t in members]
            total_w = sum(weights)
            if total_w <= 0.0:
                continue
            normalized = [w / total_w for w in weights]
            hub_scores[hub_id] = _weighted_logsumexp(values, normalized)
        if not hub_scores:
            return []

        hub_ids = list(hub_scores.keys())
        hub_logits = np.array([hub_scores[h] for h in hub_ids])
        selected_hubs = _sparsemax(hub_logits)

        scored: dict[int, float] = defaultdict(float)
        for hub_idx, hub_weight in selected_hubs:
            hub_id = hub_ids[hub_idx]
            for turn_id in self._hub_members[hub_id]:
                scored[turn_id] += reserve.get(turn_id, 0.0) * hub_weight
        if not scored:
            return []

        scored_ids = list(scored.keys())
        scored_logits = np.array([scored[t] for t in scored_ids])
        results = [(scored_ids[i], w) for i, w in _entmax(scored_logits)]
        results.sort(key=lambda x: -x[1])
        return results[:limit]

    # ── PPR 扩散（带 spread/unspread 质量守恒）────────────────

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
            for target, probability in transitions:
                addition = propagated * probability
                if addition > 0.0:
                    residual[target] = residual.get(target, 0.0) + addition
            # ★ unspread 回灌 seed（质量守恒，无出边/出边弱时回到起点）
            if unspread > 0.0:
                for target, prob in seed.items():
                    addition = propagated * unspread * prob
                    if addition > 0.0:
                        residual[target] = residual.get(target, 0.0) + addition

        return dict(reserve)

    def _transitions(self, turn_id: int) -> tuple[list[tuple[int, float]], float]:
        """出边转移（带 spread 饱和）+ 未扩散比例 unspread。"""
        hub_id = self._turn_hub.get(turn_id)
        if hub_id is None:
            return [], 1.0
        edges: list[tuple[int, float]] = []
        for other in self._hub_members[hub_id]:
            if other == turn_id:
                continue
            w = self._membership_weight.get((other, hub_id), 0.0)
            if w > 0.0:
                edges.append((other, w))
        total = sum(w for _, w in edges)
        if total <= 0.0:
            return [], 1.0
        spread = 1.0 - math.exp(-total)          # 饱和传播
        transitions = [(n, spread * w / total) for n, w in edges]
        unspread = math.exp(-total)
        return transitions, unspread

    # ── 观察接口（测试用）──────────────────────────────────────

    @property
    def hub_count(self) -> int:
        return len(self._hub_members)

    def hubs(self) -> dict[int, list[int]]:
        return {h: list(ms) for h, ms in self._hub_members.items()}

    def membership_weight_of(self, turn_id: int) -> float:
        hub_id = self._turn_hub.get(turn_id)
        if hub_id is None:
            return 0.0
        return self._membership_weight.get((turn_id, hub_id), 0.0)
