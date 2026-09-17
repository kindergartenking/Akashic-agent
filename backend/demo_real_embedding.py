"""真实 embedding 测试：用智谱 embedding-3（2048 维）测图记忆的真实语义效果。

测试集：3 个话题（装修/理财/旅行）× 5 个真实中文句子。
验证：
1. embedding 有效性：同话题相似度应明显高于跨话题；
2. hub 聚类：同话题聚成同 hub；
3. 情景召回：query 正确召回同话题 turn。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_real_embedding.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import numpy as np

from backend.memory.akasha import WorkspaceEmbeddingProvider
from backend.memory.graph_memory import GraphMemory
from backend.memory.static_graph import _cosine


# (turn_id, 文本, 话题)
TURNS = [
    (1, "装修预算怎么规划比较合理", "装修"),
    (2, "瓷砖选什么牌子性价比高", "装修"),
    (3, "水电改造有哪些注意事项", "装修"),
    (4, "客厅设计风格怎么选", "装修"),
    (5, "装修验收的标准是什么", "装修"),
    (6, "股票开户的流程是什么", "理财"),
    (7, "基金定投有什么好策略", "理财"),
    (8, "个人理财规划应该怎么做", "理财"),
    (9, "定投多久比较合适", "理财"),
    (10, "怎样分散投资降低风险", "理财"),
    (11, "机票怎么订更便宜", "旅行"),
    (12, "酒店预订有什么技巧", "旅行"),
    (13, "旅行行程怎么安排才合理", "旅行"),
    (14, "有哪些值得去的旅游景点", "旅行"),
    (15, "出国旅行需要准备什么证件", "旅行"),
]

TOPIC = {tid: topic for tid, _, topic in TURNS}
TEXT = {tid: text for tid, text, _ in TURNS}

WORKSPACE = Path("E:/Project/akashic-agent-mine")


async def main() -> None:
    async with httpx.AsyncClient(timeout=60) as http:
        provider = WorkspaceEmbeddingProvider(WORKSPACE, http)
        texts = [text for _, text, _ in TURNS]
        vecs = await provider.embed_many(texts)
        if vecs is None:
            print("embedding 获取失败（可能 API 不可用）")
            return

        embeddings = {tid: np.array(vec) for (tid, _, _), vec in zip(TURNS, vecs)}
        print(f"获取到 {len(embeddings)} 个 embedding，维度 {len(vecs[0])}\n")

        # ── 验证 1：embedding 有效性（同话题 vs 跨话题相似度）────
        same = []
        cross = []
        for i, (t1, _, topic1) in enumerate(TURNS):
            for t2, _, topic2 in TURNS[i + 1:]:
                sim = _cosine(embeddings[t1], embeddings[t2])
                (same if topic1 == topic2 else cross).append(sim)
        print("=" * 60)
        print("验证 1：embedding 有效性（相似度分布）")
        print("=" * 60)
        print(f"  同话题相似度：均值 {np.mean(same):.3f}，最小 {np.min(same):.3f}")
        print(f"  跨话题相似度：均值 {np.mean(cross):.3f}，最大 {np.max(cross):.3f}")
        assert np.mean(same) > np.mean(cross), "同话题相似度应高于跨话题"
        print("  ✅ embedding 有效：同话题明显比跨话题像\n")

        # ── 建图（阈值取同话题最小和跨话题最大之间）────────────
        threshold = round((np.min(same) + np.max(cross)) / 2, 2)
        print(f"自动选阈值 θ = {threshold}（同话题最小 vs 跨话题最大的中点）\n")
        graph = GraphMemory(threshold=threshold)
        for tid, vec in embeddings.items():
            graph.add_turn(tid, vec)

        print("=" * 60)
        print("验证 2：hub 聚类")
        print("=" * 60)
        hubs = graph.hubs()
        for hub_id, members in hubs.items():
            topics = {TOPIC[t] for t in members}
            print(f"  hub {hub_id}: {len(members)} 个 turn, 话题 = {sorted(topics)}")
        pure = all(len({TOPIC[t] for t in ms}) == 1 for ms in hubs.values())
        print(f"  聚类纯度：{'✅ 每个 hub 单一话题' if pure else '⚠️ 有 hub 混入多话题'}\n")

        # ── 验证 3：召回 ──────────────────────────────────────
        print("=" * 60)
        print("验证 3：情景召回")
        print("=" * 60)
        for qid, query_text in [(1, "装修预算"), (6, "股票开户"), (11, "机票")]:
            qvec = await provider.embed_many([query_text])
            if qvec is None:
                continue
            results = graph.recall(np.array(qvec[0]), limit=5)
            print(f"\n  query「{query_text}」召回：")
            for tid, score in results:
                print(f"    - [{score:.3f}] {TEXT[tid]}（{TOPIC[tid]}）")
        print("\n真实 embedding 测试完成")


if __name__ == "__main__":
    asyncio.run(main())
