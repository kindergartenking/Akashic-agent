"""图记忆（砍时间边+砍学习版）测试：验证 hub 聚类 + surprise 去重 + 质量守恒 + 召回。

相比静态图，重点多验证三样：
1. surprise 去重：重复 turn 的 membership 边权 ≈ 0（弱写）；
2. spread/unspread 质量守恒：PPR 扩散后总质量 ≈ seed 总质量（不丢失）；
3. 召回正确（同静态图）。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_graph_memory.py
"""

from __future__ import annotations

import numpy as np

from backend.memory.graph_memory import GraphMemory


VOCAB = ["装修", "预算", "瓷砖", "水电", "理财", "股票", "基金", "旅行", "机票", "酒店"]


def embed(text: str) -> np.ndarray:
    vec = np.zeros(len(VOCAB), dtype=np.float64)
    for i, word in enumerate(VOCAB):
        if word in text:
            vec[i] += 1.0
    return vec


TURNS = [
    (1, "装修预算"), (2, "装修瓷砖"), (3, "装修水电"),
    (4, "理财股票"), (5, "理财基金"),
    (6, "旅行机票"), (7, "旅行酒店"),
]

TOPIC = {
    1: "装修", 2: "装修", 3: "装修",
    4: "理财", 5: "理财",
    6: "旅行", 7: "旅行",
    8: "装修",   # 重复的"装修预算"，仍属装修话题
}


def main() -> None:
    graph = GraphMemory(threshold=0.3)

    print("=" * 60)
    print("建图")
    print("=" * 60)
    for turn_id, text in TURNS:
        hub = graph.add_turn(turn_id, embed(text))
        w = graph.membership_weight_of(turn_id)
        print(f"  落库 turn {turn_id}（{text}）→ hub {hub}，边权 {w:.3f}")

    print(f"\n建图完成：共 {graph.hub_count} 个 hub\n")

    # ── 验证 1：hub 聚类正确 ──────────────────────────────────
    print("=" * 60)
    print("验证 1：hub 聚类")
    print("=" * 60)
    hubs = graph.hubs()
    for hub_id, members in hubs.items():
        topics = {TOPIC[t] for t in members}
        print(f"  hub {hub_id}: {len(members)} 个 turn, 话题 = {sorted(topics)}")
    assert graph.hub_count == 3, f"期望 3 hub，实际 {graph.hub_count}"
    for members in hubs.values():
        assert len({TOPIC[t] for t in members}) == 1, "hub 混入多话题"
    print("  ✅ hub 聚类正确\n")

    # ── 验证 2：surprise 去重 ─────────────────────────────────
    print("=" * 60)
    print("验证 2：surprise 去重（重复 turn 边权应 ≈ 0）")
    print("=" * 60)
    w_first = graph.membership_weight_of(1)          # 装修预算 首次，边权较大
    graph.add_turn(8, embed("装修预算"))              # 重复的"装修预算"
    w_dup = graph.membership_weight_of(8)
    print(f"  首次「装修预算」边权 = {w_first:.3f}")
    print(f"  重复「装修预算」边权 = {w_dup:.4f}")
    assert w_dup < w_first * 0.3, f"重复 turn 边权应远小于首次：{w_dup} vs {w_first}"
    print("  ✅ surprise 去重生效：重复 turn 边权几乎为 0\n")

    # ── 验证 3：质量守恒（spread/unspread）────────────────────
    print("=" * 60)
    print("验证 3：PPR 质量守恒")
    print("=" * 60)
    seed = {1: 0.7, 4: 0.3}
    reserve = graph._ppr(seed)
    total_in = sum(seed.values())
    total_out = sum(reserve.values())
    print(f"  seed 总质量 = {total_in:.4f}")
    print(f"  reserve 总质量 = {total_out:.4f}")
    assert abs(total_in - total_out) < 1e-6, f"质量不守恒：{total_in} vs {total_out}"
    print("  ✅ 质量守恒：扩散后总质量 ≈ seed 总质量\n")

    # ── 验证 4：召回 ──────────────────────────────────────────
    print("=" * 60)
    print("验证 4：召回")
    print("=" * 60)
    for query, topic in [("装修预算", "装修"), ("理财股票", "理财"), ("旅行机票", "旅行")]:
        results = graph.recall(embed(query), limit=4)
        topics = {TOPIC[t] for t, _ in results}
        print(f"  query「{query}」→ 召回 {len(results)} 条，话题 {sorted(topics)}")
        assert topic in topics, f"query「{query}」应召回 {topic}"
    print("  ✅ 召回正确\n")

    print("=" * 60)
    print("砍时间边+砍学习版测试全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
