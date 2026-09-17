"""静态图记忆模块（StaticMemoryGraph）。

这是对原版 akashic 图记忆做「静态化」轻量后的最小实现：

- 图 = hub 情景聚类图（turn ↔ hub 的 membership 边），**无时间边**；
- 建图 = 落库时一次性计算（相似度挂 hub），**无 Oja 在线学习**（图建好就固定）；
- 召回 = seed（相似度）→ PPR 扩散 → basin pooling（log-sum-exp）
        → Sparsemax 选 hub → Entmax 稀疏读出。

只保留原版召回链路的「稀疏化」思想（Sparsemax/Entmax），砍掉时间边、
Oja 学习、以及 spread/unspread 的 exp 扭曲。当前为内存版（未落 sqlite），
先把建图 + 召回的核心逻辑跑通、测效果。
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np


# ── 稀疏化 / 池化算法（照原版实现）─────────────────────────────


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _sparsemax(logits: np.ndarray) -> list[tuple[int, float]]:
    """把 logits 投影到稀疏单纯形，只保留少数非零项（照原版）。"""
    if logits.size == 0 or not np.any(logits != 0.0):
        return []
    order = np.argsort(-logits, kind="stable")
    ordered = logits[order]
    cumulative = np.cumsum(ordered, dtype=np.float64)
    condition = 1.0 + np.arange(1, logits.size + 1) * ordered > cumulative
    support_size = int(np.flatnonzero(condition)[-1]) + 1
    threshold = (cumulative[support_size - 1] - 1.0) / support_size
    support = order[:support_size]
    return [
        (int(i), float(logits[i] - threshold))
        for i in np.sort(support)
        if logits[i] > threshold
    ]


def _entmax(logits: np.ndarray, alpha: float = 1.5) -> list[tuple[int, float]]:
    """Entmax 稀疏化，二分找阈值（照原版）。"""
    if logits.size == 0:
        return []
    exponent = 1.0 / (alpha - 1.0)
    lower = float(np.min(logits)) - exponent
    upper = float(np.max(logits))
    for _ in range(80):
        threshold = (lower + upper) / 2.0
        values = np.maximum((alpha - 1.0) * (logits - threshold), 0.0) ** exponent
        if float(np.sum(values)) > 1.0:
            lower = threshold
        else:
            upper = threshold
    values = np.maximum((alpha - 1.0) * (logits - upper), 0.0) ** exponent
    values /= float(np.sum(values))
    return [(int(i), float(values[i])) for i in np.flatnonzero(values > 0.0)]


def _weighted_logsumexp(values: list[float], weights: list[float]) -> float:
    """带权 log-sum-exp 池化：peak + log(Σ w_i · exp(v_i - peak))。

    与原版 _score_active_basins 对齐：weights 是成员的归一化 membership 权重。
    若所有 value 为 0，则结果为 log(Σ w)=log(1)=0，不会给「没被激活的 hub」虚假分数。
    """
    peak = max(values)
    return peak + math.log(
        sum(w * math.exp(v - peak) for v, w in zip(values, weights))
    )


# ── 静态图 ────────────────────────────────────────────────────


class StaticMemoryGraph:
    """hub 情景聚类图：turn 挂 hub，PPR 沿 membership 边扩散召回。"""

    def __init__(
        self,
        *,
        threshold: float = 0.6,
        restart: float = 0.25,
        tolerance: float = 1e-7,
    ) -> None:
        self.threshold = threshold
        self.restart = restart
        self.tolerance = tolerance

        self._turn_embedding: dict[int, np.ndarray] = {}
        self._hub_members: dict[int, list[int]] = {}       # hub_id -> [turn_id]
        self._hub_centroid: dict[int, np.ndarray] = {}      # hub_id -> 质心
        self._turn_hub: dict[int, int] = {}                 # turn_id -> hub_id
        self._membership_weight: dict[tuple[int, int], float] = {}  # (turn,hub) -> weight
        self._next_hub_id = 0

    # ── 建图 ──────────────────────────────────────────────────

    def add_turn(self, turn_id: int, embedding: np.ndarray) -> int:
        """落库一个 turn：挂到最相似的 hub，或建新 hub。返回所属 hub_id。"""
        self._turn_embedding[turn_id] = embedding

        best_hub: int | None = None
        best_sim = -1.0
        for hub_id, centroid in self._hub_centroid.items():
            sim = _cosine(embedding, centroid)
            if sim > best_sim:
                best_sim = sim
                best_hub = hub_id

        if best_hub is not None and best_sim >= self.threshold:
            self._turn_hub[turn_id] = best_hub
            self._hub_members[best_hub].append(turn_id)
            self._membership_weight[(turn_id, best_hub)] = best_sim
            self._update_centroid(best_hub)
            return best_hub

        hub_id = self._next_hub_id
        self._next_hub_id += 1
        self._hub_members[hub_id] = [turn_id]
        self._hub_centroid[hub_id] = embedding.copy()
        self._turn_hub[turn_id] = hub_id
        self._membership_weight[(turn_id, hub_id)] = 1.0
        return hub_id

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
        """召回：seed → PPR → basin pooling → Sparsemax → Entmax。"""
        if not self._turn_embedding:
            return []

        # 1. seed：query 跟所有 turn 的相似度，Sparsemax 稀疏化
        turn_ids = list(self._turn_embedding.keys())
        sims = np.array(
            [_cosine(query_embedding, self._turn_embedding[t]) for t in turn_ids]
        )
        sims = np.maximum(sims, 0.0)
        seed = {turn_ids[i]: w for i, w in _sparsemax(sims)}

        # 2. PPR 沿 membership 边扩散，得到每个 turn 的相关度 reserve
        reserve = self._ppr(seed)

        # 3. basin pooling：只对「有成员被激活」的 hub 算分数（用成员的
        #    membership 归一化权重做 log-sum-exp，与原版 _score_active_basins 对齐）。
        hub_scores: dict[int, float] = {}
        for hub_id, members in self._hub_members.items():
            values = [reserve.get(t, 0.0) for t in members]
            if not any(v > 0.0 for v in values):
                continue  # 没被激活的 hub，不参与召回
            weights = [self._membership_weight.get((t, hub_id), 0.0) for t in members]
            total_w = sum(weights)
            if total_w <= 0.0:
                continue
            normalized = [w / total_w for w in weights]
            hub_scores[hub_id] = _weighted_logsumexp(values, normalized)
        if not hub_scores:
            return []

        # 4. Sparsemax 选稀疏的 hub
        hub_ids = list(hub_scores.keys())
        hub_logits = np.array([hub_scores[h] for h in hub_ids])
        selected_hubs = _sparsemax(hub_logits)

        # 5. 对选中 hub 的成员，Entmax 稀疏读出
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

    # ── PPR 扩散（residual push 简化版）────────────────────────

    def _ppr(self, seed: dict[int, float]) -> dict[int, float]:
        """沿 membership 边扩散，返回每个 turn 节点的 reserve（相关度）。"""
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
            for neighbor, probability in self._out_edges(node):
                addition = propagated * probability
                if addition > 0.0:
                    residual[neighbor] = residual.get(neighbor, 0.0) + addition

        return dict(reserve)

    def _out_edges(self, turn_id: int) -> list[tuple[int, float]]:
        """turn 的归一化出边：它所属 hub 里的其他成员 turn（经 hub 中转）。

        静态图的边只有 membership（turn↔hub）。PPR 里 turn 节点只能走到 hub，
        再从 hub 走到同 hub 的其他 turn。这里把「turn → hub → 同 hub 其他 turn」
        两步合并成「turn → 同 hub 其他 turn」的一跳，边权 = 目标 turn 的 membership 权重。
        """
        hub_id = self._turn_hub.get(turn_id)
        if hub_id is None:
            return []
        edges = []
        for other in self._hub_members[hub_id]:
            if other == turn_id:
                continue
            w = self._membership_weight.get((other, hub_id), 0.0)
            if w > 0.0:
                edges.append((other, w))
        total = sum(w for _, w in edges)
        if total <= 0.0:
            return []
        return [(n, w / total) for n, w in edges]

    # ── 观察接口（测试用）──────────────────────────────────────

    @property
    def hub_count(self) -> int:
        return len(self._hub_members)

    def hubs(self) -> dict[int, list[int]]:
        """hub_id -> 成员 turn 列表。"""
        return {h: list(ms) for h, ms in self._hub_members.items()}
