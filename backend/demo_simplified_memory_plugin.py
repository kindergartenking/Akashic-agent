"""简化记忆架构插件 demo：挂载到 runtime，跑多轮 turn 验证召回 + 落库。

验证：
1. before_reasoning 召回：第二轮起，能召回之前的同话题记忆；
2. after_turn 落库：每轮把 turn 写入记忆图、建 hub；
3. 跨 turn 持久化：记忆图随 turn 累积，hub 聚类形成。

运行（项目根目录）：
    PYTHONPATH=backend E:/Anaconda/python.exe backend/demo_simplified_memory_plugin.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

from agent.core.reasoner import LLMResponse
from agent.core.wiring import build_wiring
from agent.plugins.examples.simplified_memory_plugin import SimplifiedMemoryPlugin
from backend.memory.akasha import WorkspaceEmbeddingProvider
from bus.events import InboundMessage

WORKSPACE = Path("E:/Project/akashic-agent-mine")


def _stub_llm(messages, schemas):
    """桩 LLM：直接回一句，不调工具（演示记忆插件的召回/落库，不测推理）。"""
    return LLMResponse(content="好的，我来回答你的问题。")


async def _stub_llm_async(messages, schemas):
    return _stub_llm(messages, schemas)


def _msg(text: str) -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender="user",
        chat_id="c1",
        content=text,
        timestamp=datetime.now(timezone.utc),
    )


DIALOG = [
    "python 装饰器的原理是什么",
    "python 列表推导式怎么用",
    "红烧肉怎么做才好吃",
    "清蒸鱼的详细步骤",
    "python 的装饰器和列表推导式有什么区别",
    "减脂期怎么安排饮食",
]


async def main() -> None:
    async with httpx.AsyncClient(timeout=60) as http:
        provider = WorkspaceEmbeddingProvider(WORKSPACE, http)

        async def embed_fn(text: str) -> np.ndarray | None:
            vecs = await provider.embed_many([text])
            return np.array(vecs[0]) if vecs else None

        plugin = SimplifiedMemoryPlugin(embed_fn=embed_fn)
        wiring = build_wiring(
            _stub_llm_async,
            before_reasoning_plugin_modules=plugin.before_reasoning_modules(),
            after_turn_plugin_modules=plugin.after_turn_modules(),
        )

        print("=" * 64)
        print("简化记忆架构插件 demo（多轮 turn）")
        print("=" * 64)
        for i, text in enumerate(DIALOG, 1):
            print(f"\n── turn {i}：{text} ──")
            await wiring.pipeline.run(_msg(text), f"cli:c1:{i}")

        print("\n" + "=" * 64)
        print("最终记忆图")
        print("=" * 64)
        mem = plugin.memory
        print(f"  {len(mem.turns)} 个 turn，{len(mem.hubs)} 个 hub")
        for h, members in enumerate(mem.hubs):
            items = []
            for t, w in members:
                items.append(f"t{t}({w:.3f})「{mem.turns[t]['text'][:20]}」")
            print(f"  hub{h}: " + ", ".join(items))


if __name__ == "__main__":
    asyncio.run(main())
