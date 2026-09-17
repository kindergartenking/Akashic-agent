"""往 simplified_memory.db 注入构造的 turn，便于测试召回。

用 4 个语义距离远的话题（编程/美食/健身/法律）× 4 个 turn，共 16 条。
文本刻意避开「怎么做/好吃」这类通用词（避免万金油污染）。

注入后用真实 embedding 建图，并模拟几次 recall 验证召回可用。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_seed_memory.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import numpy as np

from backend.memory.akasha import WorkspaceEmbeddingProvider
from backend.memory.simplified_memory import SimplifiedMemory

WORKSPACE = Path("E:/Project/akashic-agent-mine")
DB_PATH = WORKSPACE / "memory" / "simplified_memory.db"

# (话题, 文本)
SEED_TURNS = [
    ("编程", "python 装饰器的原理"),
    ("编程", "python 闭包怎么理解"),
    ("编程", "python 生成器 yield 用法"),
    ("编程", "python 多线程和协程区别"),
    ("美食", "红烧肉的做法步骤"),
    ("美食", "清蒸鱼的火候"),
    ("美食", "麻婆豆腐的配料"),
    ("美食", "糖醋排骨的糖醋比例"),
    ("健身", "深蹲的正确姿势"),
    ("健身", "卧推练胸肌的方法"),
    ("健身", "硬拉练背的动作要领"),
    ("健身", "减脂期的训练安排"),
    ("法律", "合同违约怎么处理"),
    ("法律", "劳动仲裁的流程"),
    ("法律", "离婚财产怎么分割"),
    ("法律", "遗产继承的顺序"),
]


async def main() -> None:
    # 清空旧 db，全新注入
    if DB_PATH.exists():
        DB_PATH.unlink()

    async with httpx.AsyncClient(timeout=60) as http:
        provider = WorkspaceEmbeddingProvider(WORKSPACE, http)
        texts = [text for _, text in SEED_TURNS]
        vecs = await provider.embed_many(texts)
        if vecs is None:
            print("embedding 获取失败")
            return

        mem = SimplifiedMemory(db_path=str(DB_PATH))
        for (topic, text), vec in zip(SEED_TURNS, vecs):
            mem.add_turn(text, np.array(vec))

        print(f"注入完成：{len(mem.turns)} 个 turn，{len(mem.hubs)} 个 hub\n")
        print("=== hub 分布 ===")
        for h, members in enumerate(mem.hubs):
            topics = sorted({SEED_TURNS[t][0] for t, _ in members})
            print(f"  hub{h}: {len(members)} 成员, 话题={topics}")

        print("\n=== 召回验证（建议你测的 query）===")
        for q in ["python 装饰器", "红烧肉", "深蹲", "合同违约"]:
            qv = await provider.embed_many([q])
            if qv is None:
                continue
            results = mem.recall(q, np.array(qv[0]), limit=4)
            print(f"\n  query「{q}」召回：")
            for tid, score in results:
                print(f"    - [{score:.3f}] {mem.turns[tid]['user_text']}")


if __name__ == "__main__":
    asyncio.run(main())
