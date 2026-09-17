"""静态图记忆模块测试：自主构建多话题测试集，验证建图聚类 + 情景召回。

测试集设计：三个话题（装修 / 理财 / 旅行），每个话题若干 turn。
用「关键词词频向量」作为伪 embedding（同话题共享话题词 → 相似度高，
跨话题无共享词 → 相似度 0）。验证：

1. 建图：同话题的 turn 聚成同一个 hub（3 个 hub）；
2. 召回：query 一个话题的线索，能召回同话题的 turn（情景记忆激活）。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_static_graph.py
"""

from __future__ import annotations

import numpy as np

from backend.memory.static_graph import StaticMemoryGraph


# 全局词表（伪 embedding 的维度）
VOCAB = [
    "装修", "预算", "瓷砖", "水电", "设计",   # 装修话题
    "理财", "股票", "基金", "定投",          # 理财话题
    "旅行", "机票", "酒店", "行程",          # 旅行话题
]


def embed(text: str) -> np.ndarray:
    """伪 embedding：关键词词频向量（词表维度上的 one-hot/词频）。"""
    vec = np.zeros(len(VOCAB), dtype=np.float64)
    for i, word in enumerate(VOCAB):
        if word in text:
            vec[i] += 1.0
    return vec


# ── 测试集：三个话题，每个话题若干 turn ────────────────────────

TURNS = [
    # 装修话题（都含「装修」）
    (1, "装修预算怎么规划"),
    (2, "装修预算清单"),
    (3, "装修瓷砖选什么牌子"),
    (4, "装修水电改造注意事项"),
    (5, "装修客厅设计风格"),
    # 理财话题（都含「理财」）
    (6, "理财股票怎么开户"),
    (7, "理财基金定投策略"),
    (8, "理财定投多久合适"),
    # 旅行话题（都含「旅行」）
    (9, "旅行机票怎么订便宜"),
    (10, "旅行酒店推荐"),
    (11, "旅行行程怎么安排"),
]

# 每个 turn 所属的话题（用于断言）
TOPIC = {
    1: "装修", 2: "装修", 3: "装修", 4: "装修", 5: "装修",
    6: "理财", 7: "理财", 8: "理财",
    9: "旅行", 10: "旅行", 11: "旅行",
}

TEXT = {tid: text for tid, text in TURNS}


def main() -> None:
    graph = StaticMemoryGraph(threshold=0.3)

    # ── 建图 ──────────────────────────────────────────────────
    for turn_id, text in TURNS:
        hub = graph.add_turn(turn_id, embed(text))
        print(f"  落库 turn {turn_id:>2}（{text}）→ hub {hub}")

    print(f"\n建图完成：共 {graph.hub_count} 个 hub\n")

    # ── 验证 1：hub 聚类正确 ──────────────────────────────────
    hubs = graph.hubs()
    print("=" * 60)
    print("验证 1：hub 聚类")
    print("=" * 60)
    for hub_id, members in hubs.items():
        topics = {TOPIC[t] for t in members}
        print(f"  hub {hub_id}: {len(members)} 个 turn, 话题 = {sorted(topics)}, "
              f"成员 = {members}")

    # 断言：3 个 hub，每个 hub 内部话题一致
    assert graph.hub_count == 3, f"期望 3 个 hub，实际 {graph.hub_count}"
    for hub_id, members in hubs.items():
        topics = {TOPIC[t] for t in members}
        assert len(topics) == 1, f"hub {hub_id} 混入了多个话题: {topics}"
    print("  ✅ hub 聚类正确：3 个 hub，各对应一个话题\n")

    # ── 验证 2：情景召回 ──────────────────────────────────────
    print("=" * 60)
    print("验证 2：情景召回")
    print("=" * 60)

    queries = [
        ("装修预算", "装修"),
        ("理财股票", "理财"),
        ("旅行机票", "旅行"),
    ]
    for query, expected_topic in queries:
        results = graph.recall(embed(query), limit=4)
        recalled = [(tid, f"{TEXT[tid]}", round(score, 3)) for tid, score in results]
        recalled_topics = {TOPIC[tid] for tid, _ in results}
        print(f"\n  query「{query}」召回 {len(results)} 条：")
        for tid, text, score in recalled:
            print(f"    - [{score:.3f}] {text}（{TOPIC[tid]}）")
        # 断言：召回的话题以期望话题为主
        assert expected_topic in recalled_topics, (
            f"query「{query}」应召回 {expected_topic} 话题，实际 {recalled_topics}"
        )
        print(f"  ✅ query「{query}」正确召回 {expected_topic} 话题")

    print("\n" + "=" * 60)
    print("静态图测试全部通过：建图聚类正确 + 情景召回正确")
    print("=" * 60)


if __name__ == "__main__":
    main()
