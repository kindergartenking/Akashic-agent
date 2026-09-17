"""真实 embedding 测试 GraphMemoryFull（多归属 + dense+BM25 完整闭环）。

对比之前的 GraphMemory（单归属简化版），重点验证：
1. 多归属是否生效（一个 turn 连多个 hub）；
2. 完整闭环建图（检索→激活场→整合权重→建hub）；
3. dense+BM25 双 lane 召回在真实语义下的效果。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_graph_memory_full.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import numpy as np

from backend.memory.akasha import WorkspaceEmbeddingProvider
from backend.memory.graph_memory_full import GraphMemoryFull


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
        vecs = await provider.embed_many([text for _, text, _ in TURNS])
        if vecs is None:
            print("embedding 获取失败")
            return
        embeddings = {tid: np.array(vec) for (tid, _, _), vec in zip(TURNS, vecs)}

        graph = GraphMemoryFull(threshold=0.3)
        for tid, text, _ in TURNS:
            graph.add_turn(tid, embeddings[tid], text)

        print(f"建图完成：{len(TURNS)} 个 turn，{graph.hub_count} 个 hub\n")

        # ── 验证 1：多归属 ─────────────────────────────────────
        print("=" * 60)
        print("验证 1：多归属（一个 turn 连几个 hub）")
        print("=" * 60)
        multi = 0
        for tid in TEXT:
            hubs = graph.hubs_of(tid)
            if len(hubs) > 1:
                multi += 1
            if len(hubs) > 1:
                print(f"  turn {tid}（{TEXT[tid]}）连了 {len(hubs)} 个 hub: {hubs}")
        print(f"  多归属的 turn 数：{multi}/{len(TEXT)}")
        print()

        # ── 验证 2：hub 话题分布 ───────────────────────────────
        print("=" * 60)
        print("验证 2：hub 话题分布")
        print("=" * 60)
        for hub_id, members in graph.hubs().items():
            topics = {TOPIC[t] for t in members}
            print(f"  hub {hub_id}: {len(members)} 个 turn, 话题 = {sorted(topics)}")
        print()

        # ── 验证 3：召回 ───────────────────────────────────────
        print("=" * 60)
        print("验证 3：召回（dense + BM25 双 lane）")
        print("=" * 60)
        for qid, query_text in [(1, "装修预算怎么规划"), (6, "股票开户流程"), (11, "机票怎么订")]:
            qvec = await provider.embed_many([query_text])
            if qvec is None:
                continue
            results = graph.recall(np.array(qvec[0]), query_text, limit=5)
            print(f"\n  query「{query_text}」召回：")
            for tid, score in results:
                print(f"    - [{score:.3f}] {TEXT[tid]}（{TOPIC[tid]}）")
        print("\n测试完成")


if __name__ == "__main__":
    asyncio.run(main())
