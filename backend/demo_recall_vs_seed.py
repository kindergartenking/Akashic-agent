"""多 query 对比测试：验证「图扩散过滤撞词」是普遍能力，而非单 query 偶然。

遍历多个 query（不同话题 × 不同子话题），每个 query 都对比：
- seed（直接相似度）：会混入"共享子话题词、但不同话题"的撞词 turn；
- 召回（完整图链路）：应通过情景一致性，把撞词全部过滤掉。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_recall_vs_seed.py
"""

from __future__ import annotations

import numpy as np

from backend.memory.graph_memory import GraphMemory
from backend.memory.static_graph import _cosine, _sparsemax


TOPICS = ["装修", "理财", "旅行", "健康", "教育", "科技", "美食", "运动", "音乐", "摄影"]
SUBTOPICS = ["预算", "计划", "推荐", "经验", "入门", "工具", "方法", "案例", "对比", "总结"]
VOCAB = TOPICS + SUBTOPICS


def embed(topic: str, subtopic: str) -> np.ndarray:
    vec = np.zeros(len(VOCAB), dtype=np.float64)
    vec[VOCAB.index(topic)] = 1.0
    vec[VOCAB.index(subtopic)] = 1.0
    return vec


def main() -> None:
    # ── 构建 100 个 turn（10 话题 × 10 子话题）───────────────
    turns: dict[int, tuple[str, str]] = {}
    embeddings: dict[int, np.ndarray] = {}
    tid = 0
    for topic in TOPICS:
        for subtopic in SUBTOPICS:
            tid += 1
            turns[tid] = (topic, subtopic)
            embeddings[tid] = embed(topic, subtopic)

    graph = GraphMemory(threshold=0.3)
    for turn_id, emb in embeddings.items():
        graph.add_turn(turn_id, emb)
    print(f"建图完成：{len(turns)} 个 turn，{graph.hub_count} 个 hub\n")

    # ── 遍历多个 query 对比 ───────────────────────────────────
    queries = [
        ("装修", "预算"),
        ("理财", "计划"),
        ("旅行", "工具"),
        ("健康", "经验"),
        ("科技", "方法"),
    ]

    print("=" * 78)
    print(f"{'query':<12} {'seed命中':>8} {'seed撞词':>8} {'召回命中':>8} {'召回撞词':>8}  {'召回话题是否纯净'}")
    print("=" * 78)

    all_pure = True
    for topic, subtopic in queries:
        query = embed(topic, subtopic)

        # seed：直接相似度 → Sparsemax
        ids = sorted(turns)
        sims = np.array([_cosine(query, embeddings[t]) for t in ids])
        sims = np.maximum(sims, 0.0)
        seed = {ids[i]: w for i, w in _sparsemax(sims)}
        seed_collision = sum(1 for t in seed if turns[t][0] != topic)

        # 召回：完整链路
        recall = graph.recall(query, limit=10)
        recall_ids = {t for t, _ in recall}
        recall_collision = sum(1 for t in recall_ids if turns[t][0] != topic)
        pure = recall_collision == 0 and len(recall_ids) > 0

        all_pure = all_pure and pure
        flag = "✅" if pure else "❌ 混入"
        print(
            f"{topic+subtopic:<12} {len(seed):>8} {seed_collision:>8} "
            f"{len(recall_ids):>8} {recall_collision:>8}  {flag}"
        )

    print("=" * 78)
    if all_pure:
        print("✅ 全部 query 召回都纯净：图扩散稳定地过滤掉了撞词")
    else:
        print("❌ 存在召回混入撞词的情况")
    print("\n多 query 对比测试完成")


if __name__ == "__main__":
    main()
