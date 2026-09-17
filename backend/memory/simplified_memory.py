"""简化记忆架构（去时间边 + 去边权演化）· 真实 dense+BM25 版。

在简化版（simplified_memory_architecture.md）基础上，把 Jaccard 占位 evidence
替换为真实的 dense + BM25 完整链条，并补上正式的 recall 函数。

保留文档定义的完整建图闭环：
    evidence（dense+bm25 → _tail_surprisal → sparsemax seed）
    → 扩散（重启随机游走）→ activity（三路 max）
    → logits（activity²·ln(1+n)）→ entmax15 → 边权（integrated×surprise）→ 建 hub

砍掉：时间边、边权演化（Oja/归一化/可塑性/遗忘）。

依赖：numpy（embedding 是向量）+ 调用方提供的 embedding。
"""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_left
from collections import Counter

import numpy as np

_TOKEN_CHUNKS = re.compile(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]{2,}")


# ── 分词 + BM25 ──────────────────────────────────────────────


def _tokenize(text: str) -> Counter[str]:
    """中英文分词：完整 chunk + 中文重叠 2-gram（照原版）。"""
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


# ── 稀疏化 + 校准 ────────────────────────────────────────────


def _tail_surprisal(scores: list[float]) -> list[float]:
    """排名校准（文档 3.2）：score -> -log(尾部概率)，压到 [0, log n] 标尺。

    只保留排名、抹平绝对数值，使 cosine（有界可为负）和 BM25（无界为正）可加。
    """
    n = len(scores)
    if n == 0 or all(s == 0.0 for s in scores):
        return [0.0] * n
    ordered = sorted(scores)
    counts = [n - bisect_left(ordered, s) for s in scores]
    return [-math.log(c / n) for c in counts]


def _sparsemax(logits: list[float]) -> list[tuple[int, float]]:
    n = len(logits)
    if n == 0 or all(x == 0.0 for x in logits):
        return []
    order = sorted(range(n), key=lambda i: -logits[i])
    ordered = [logits[i] for i in order]
    cumulative = []
    acc = 0.0
    for x in ordered:
        acc += x
        cumulative.append(acc)
    support_size = 1
    for k in range(n):
        if 1.0 + (k + 1) * ordered[k] > cumulative[k]:
            support_size = k + 1
    threshold = (cumulative[support_size - 1] - 1.0) / support_size
    return [
        (i, logits[i] - threshold)
        for i in sorted(order[:support_size])
        if logits[i] > threshold
    ]


def _entmax15(logits: list[float], alpha: float = 1.5) -> list[tuple[int, float]]:
    if not logits:
        return []
    exponent = 1.0 / (alpha - 1.0)
    lower = min(logits) - exponent
    upper = max(logits)
    for _ in range(80):
        threshold = (lower + upper) / 2.0
        s = sum(max((alpha - 1.0) * (x - threshold), 0.0) ** exponent for x in logits)
        if s > 1.0:
            lower = threshold
        else:
            upper = threshold
    values = [max((alpha - 1.0) * (x - upper), 0.0) ** exponent for x in logits]
    total = sum(values)
    if total <= 0.0:
        return []
    return [(i, v / total) for i, v in enumerate(values) if v > 0.0]


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ── 简化记忆 ─────────────────────────────────────────────────


class SimplifiedMemory:
    """去时间边 + 去边权演化：完整建图闭环 + dense+BM25 recall。"""

    def __init__(
        self,
        *,
        restart: float = 0.25,
        activation_power: float = 2.0,
        recurrent_budget: float = 1.0,
        diffuse_steps: int = 30,
        db_path: str | None = None,
    ) -> None:
        self.restart = restart
        self.activation_power = activation_power
        self.recurrent_budget = recurrent_budget
        self.diffuse_steps = diffuse_steps
        self._db_path = db_path

        self.turns: list[dict] = []               # {text, terms, emb}
        self.hubs: list[list[tuple[int, float]]] = []  # hub_id -> [(turn_id, weight)]
        self.turn_to_hubs: dict[int, list[tuple[int, float]]] = {}
        self._document_frequency: Counter[str] = Counter()

        if db_path is not None:
            self._load()

    # ── 持久化（sqlite）──────────────────────────────────────

    def _load(self) -> None:
        """从 sqlite 加载 turns + hubs，重建 terms / turn_to_hubs / df。"""
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        try:
            # 旧 schema（text/embedding）不兼容，检测到就丢弃重建
            cols = [r[1] for r in conn.execute("PRAGMA table_info(turns)")]
            if cols and "user_text" not in cols:
                conn.execute("DROP TABLE turns")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS turns ("
                "turn_id INTEGER PRIMARY KEY, user_text TEXT, assistant_text TEXT, "
                "user_embedding BLOB, assistant_embedding BLOB)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hubs ("
                "hub_id INTEGER PRIMARY KEY, members TEXT)"
            )
            for turn_id, user_text, assistant_text, u_blob, a_blob in conn.execute(
                "SELECT turn_id, user_text, assistant_text, user_embedding, assistant_embedding "
                "FROM turns ORDER BY turn_id"
            ):
                user_emb = np.frombuffer(u_blob, dtype=np.float64).copy() if u_blob else None
                assistant_emb = np.frombuffer(a_blob, dtype=np.float64).copy() if a_blob else None
                terms = _tokenize(user_text)
                if assistant_text:
                    terms.update(_tokenize(assistant_text))
                self.turns.append(
                    {
                        "user_text": user_text,
                        "assistant_text": assistant_text or "",
                        "user_emb": user_emb,
                        "assistant_emb": assistant_emb,
                        "terms": terms,
                    }
                )
                self._document_frequency.update(terms.keys())
            for hub_id, members_json in conn.execute(
                "SELECT hub_id, members FROM hubs ORDER BY hub_id"
            ):
                members = [(int(t), float(w)) for t, w in json.loads(members_json)]
                self.hubs.append(members)
                for t, w in members:
                    self.turn_to_hubs.setdefault(t, []).append((hub_id, w))
        finally:
            conn.close()

    def _save(self) -> None:
        """全量写 sqlite（turns + hubs）。"""
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS turns ("
                "turn_id INTEGER PRIMARY KEY, user_text TEXT, assistant_text TEXT, "
                "user_embedding BLOB, assistant_embedding BLOB)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hubs ("
                "hub_id INTEGER PRIMARY KEY, members TEXT)"
            )
            conn.execute("DELETE FROM turns")
            conn.execute("DELETE FROM hubs")
            for turn_id, t in enumerate(self.turns):
                u_blob = np.asarray(t["user_emb"], dtype=np.float64).tobytes()
                a_blob = (
                    np.asarray(t["assistant_emb"], dtype=np.float64).tobytes()
                    if t["assistant_emb"] is not None
                    else None
                )
                conn.execute(
                    "INSERT INTO turns(turn_id, user_text, assistant_text, "
                    "user_embedding, assistant_embedding) VALUES (?, ?, ?, ?, ?)",
                    (turn_id, t["user_text"], t["assistant_text"], u_blob, a_blob),
                )
            for hub_id, members in enumerate(self.hubs):
                conn.execute(
                    "INSERT INTO hubs(hub_id, members) VALUES (?, ?)",
                    (hub_id, json.dumps(members)),
                )
            conn.commit()
        finally:
            conn.close()

    # ── 建图 ──────────────────────────────────────────────────

    def add_turn(
        self,
        user_text: str,
        user_emb: np.ndarray,
        assistant_text: str | None = None,
        assistant_emb: np.ndarray | None = None,
    ) -> int | None:
        turn_id = len(self.turns)
        # terms = user + assistant 合并（BM25 用，照原版字面合并）
        terms = _tokenize(user_text)
        if assistant_text:
            terms.update(_tokenize(assistant_text))

        # surprise = 1 - max(dense 相似度)（跟历史比，user/assistant 取 max）
        dense_scores = [self._turn_dense(user_emb, t) for t in self.turns]
        surprise = 1.0 - max(dense_scores) if dense_scores else 1.0

        # ① evidence → seed（在旧图上，不含当前 turn）
        seed = self._evidence(terms, user_emb)

        # ② 扩散（重启随机游走）
        reserve = self._diffuse(seed)

        # ③ activity 三路 max
        activity: dict[int, float] = {turn_id: 1.0}
        for t, w in seed.items():
            activity[t] = max(activity.get(t, 0.0), w)
        turn_reserve = {t: reserve.get(t, 0.0) for t in range(len(self.turns))}
        peak = max(turn_reserve.values()) if turn_reserve else 0.0
        if peak > 0.0:
            for t, r in turn_reserve.items():
                if r > 0.0:
                    activity[t] = max(activity.get(t, 0.0), r / peak)

        # ④ logits = activity^power · ln(1+n)
        active = [t for t, a in activity.items() if a > 0.0]
        scale = math.log1p(len(active))
        logits = [activity[t] ** self.activation_power * scale for t in active]

        # ⑤ entmax15 → integrated
        integrated = {active[i]: w for i, w in _entmax15(logits)}

        # ⑥ 建 hub（门控：integrated≥2 且 surprise>0）
        hub_id: int | None = None
        if len(integrated) >= 2 and surprise > 0.0:
            hub_id = len(self.hubs)
            members = []
            for t, w in integrated.items():
                edge_w = w * surprise
                if edge_w <= 0.0:
                    continue
                members.append((t, edge_w))
                self.turn_to_hubs.setdefault(t, []).append((hub_id, edge_w))
            self.hubs.append(members)

        # 存当前 turn（user / assistant 分开，照原版）
        self.turns.append(
            {
                "user_text": user_text,
                "assistant_text": assistant_text or "",
                "user_emb": user_emb,
                "assistant_emb": assistant_emb,
                "terms": terms,
            }
        )
        self._document_frequency.update(terms.keys())
        if self._db_path is not None:
            self._save()
        return hub_id

    @staticmethod
    def _turn_dense(query_emb: np.ndarray, turn: dict) -> float:
        """query 跟一个 turn 的 dense 相似度：user/assistant 分别比，取 max（照原版）。"""
        u = _cosine(query_emb, turn["user_emb"]) if turn["user_emb"] is not None else -1.0
        a = _cosine(query_emb, turn["assistant_emb"]) if turn["assistant_emb"] is not None else -1.0
        return max(u, a)

    # ── 召回 ──────────────────────────────────────────────────

    def recall(self, query_text: str, query_emb: np.ndarray, limit: int = 5) -> list[tuple[int, float]]:
        """正式 recall：evidence → seed → 扩散 → 按 reserve 排序取 top-k。"""
        if not self.turns:
            return []
        terms = _tokenize(query_text)
        seed = self._evidence(terms, query_emb)
        reserve = self._diffuse(seed)
        if not reserve:
            return []
        ranked = sorted(reserve.items(), key=lambda x: -x[1])
        return ranked[:limit]

    # ── evidence：dense + BM25 + _tail_surprisal → sparsemax seed ──

    def _evidence(self, terms: Counter[str], emb: np.ndarray) -> dict[int, float]:
        n = len(self.turns)
        if n == 0:
            return {}

        dense = [self._turn_dense(emb, t) for t in self.turns]
        bm25 = [
            _bm25(terms, t["terms"], self._document_frequency, n) for t in self.turns
        ]

        # _tail_surprisal 跨通道校准（同 [0, log n] 标尺，可加）
        dense_tail = _tail_surprisal(dense)
        bm25_tail = _tail_surprisal(bm25)
        combined = [d + b for d, b in zip(dense_tail, bm25_tail)]

        seed_pairs = _sparsemax(combined)
        return {turn_id: w for turn_id, w in seed_pairs}

    # ── 扩散：重启随机游走（幂迭代）──────────────────────────

    def _diffuse(self, seed: dict[int, float]) -> dict[int, float]:
        if not seed:
            return {}
        p: dict[int, float] = dict(seed)
        for _ in range(self.diffuse_steps):
            new: dict[int, float] = {}
            for t, w in seed.items():
                new[t] = new.get(t, 0.0) + self.restart * w
            for turn_id, value in p.items():
                propagated = (1.0 - self.restart) * value
                for target, prob in self._out_edges(turn_id):
                    new[target] = new.get(target, 0.0) + propagated * prob
            p = new
        return p

    def _out_edges(self, turn_id: int) -> list[tuple[int, float]]:
        """turn 的归一化出边：经 hub 到同 hub 的其他 turn（turn→hub→turn 两步合并）。"""
        edges: dict[int, float] = {}
        for hub_id, _ in self.turn_to_hubs.get(turn_id, []):
            for other, w in self.hubs[hub_id]:
                if other != turn_id and w > 0.0:
                    edges[other] = edges.get(other, 0.0) + w
        items = list(edges.items())
        total = sum(w for _, w in items)
        if total <= 0.0:
            return []
        return [(t, w / total) for t, w in items]
