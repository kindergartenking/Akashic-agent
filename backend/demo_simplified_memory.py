"""简化记忆（dense+BM25 完整版）demo：真实 embedding + 语义距离远的话题。

验证：
1. embedding 有效性：同话题相似度明显高于跨话题；
2. 建图：同话题聚成 hub；
3. recall：query 正确召回同话题 turn。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_simplified_memory.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import numpy as np

from backend.memory.akasha import WorkspaceEmbeddingProvider
from backend.memory.simplified_memory import SimplifiedMemory


# (turn_id, 文本, 话题) —— 语义距离远的话题，保证 embedding 能分开
# 注意：SimplifiedMemory 内部 turn_id 是 0-based（按 add_turn 顺序），这里统一 0-based。
TURNS = [
    (0, "python 列表推导式怎么用", "编程"),
    (1, "python 函数默认参数有什么坑", "编程"),
    (2, "python 装饰器的原理是什么", "编程"),
    (3, "红烧肉怎么做才好吃", "美食"),
    (4, "清蒸鱼的详细步骤", "美食"),
    (5, "番茄炒蛋的做法", "美食"),
    (6, "深蹲的正确姿势是什么", "健身"),
    (7, "卧推怎么练胸肌", "健身"),
    (8, "减脂期怎么安排训练", "健身"),
    (9, "合同违约怎么起诉", "法律"),
    (10, "劳动仲裁需要什么材料", "法律"),
    (11, "打官司请律师要多少钱", "法律"),
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
        print(f"获取 {len(embeddings)} 个 embedding，维度 {len(vecs[0])}\n")

        # ── 验证 1：embedding 有效性 ──────────────────────────
        from backend.memory.simplified_memory import _cosine
        same, cross = [], []
        for i, (t1, _, topic1) in enumerate(TURNS):
            for t2, _, topic2 in TURNS[i + 1:]:
                sim = _cosine(embeddings[t1], embeddings[t2])
                (same if topic1 == topic2 else cross).append(sim)
        print("=" * 60)
        print("验证 1：embedding 有效性")
        print("=" * 60)
        print(f"  同话题相似度：均值 {np.mean(same):.3f}，最小 {np.min(same):.3f}")
        print(f"  跨话题相似度：均值 {np.mean(cross):.3f}，最大 {np.max(cross):.3f}")
        print(f"  可分离：{'✅' if np.min(same) > np.max(cross) else '⚠️ 有重叠'}\n")

        # ── 建图 ──────────────────────────────────────────────
        mem = SimplifiedMemory()
        for tid, text, _ in TURNS:
            mem.add_turn(text, embeddings[tid])
        print("=" * 60)
        print("验证 2：建图（hub 分布）")
        print("=" * 60)
        for h, members in enumerate(mem.hubs):
            topics = {TOPIC[t] for t, _ in members}
            print(f"  hub{h}: {len(members)} 成员, 话题={sorted(topics)}, "
                  f"成员={[(t, round(w, 3)) for t, w in members]}")
        print(f"  共 {len(mem.turns)} turn, {len(mem.hubs)} hub\n")

        # ── 验证 3：recall ────────────────────────────────────
        print("=" * 60)
        print("验证 3：recall（dense + BM25 双 lane）")
        print("=" * 60)
        queries = [("python 装饰器", "编程"), ("红烧肉怎么做", "美食"), ("深蹲练腿", "健身")]
        for qtext, qtopic in queries:
            qvec = await provider.embed_many([qtext])
            if qvec is None:
                continue
            results = mem.recall(qtext, np.array(qvec[0]), limit=4)
            print(f"\n  query「{qtext}」召回：")
            for tid, score in results:
                print(f"    - [{score:.3f}] {TEXT[tid]}（{TOPIC[tid]}）")
            topics = {TOPIC[t] for t, _ in results}
            assert qtopic in topics, f"query「{qtext}」应召回 {qtopic}，实际 {topics}"
        print("\n✅ 简化记忆（dense+BM25 完整版）测试通过")


if __name__ == "__main__":
    asyncio.run(main())
