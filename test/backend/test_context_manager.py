from __future__ import annotations

import asyncio
from pathlib import Path

from backend.context_manager import SessionContextManager
from backend.llm_client import LLMResponse
from backend.model_config import ModelConfig
from backend.session_store import SessionStore


class _FakeLLM:
    async def chat(self, config, messages, **kwargs):
        return LLMResponse(content="历史摘要：用户之前讨论过项目实现。")


def _config(window: int) -> ModelConfig:
    return ModelConfig("openai", "test", "http://test", "key", "t", "test", context_window=window)


def test_context_uses_all_history_under_74_percent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.record_user_message("s", "t1", "你好")
    store.complete_turn("s", "t1", "你好，有什么可以帮你")
    manager = SessionContextManager(store, _FakeLLM())
    result = asyncio.run(manager.build(_config(4096), "s"))
    assert len(result) == 2


def test_context_compresses_older_history_and_keeps_recent_five(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    for i in range(8):
        turn = f"t{i}"
        store.record_user_message("s", turn, f"用户消息 {i} " + "x" * 100)
        store.complete_turn("s", turn, f"助手消息 {i} " + "y" * 100)
    manager = SessionContextManager(store, _FakeLLM())
    result = asyncio.run(manager.build(_config(512), "s"))
    assert result[0]["role"] == "system"
    assert "历史摘要" in result[0]["content"]
    assert any("用户消息 7" in str(item["content"]) for item in result)
    assert not any("用户消息 0" in str(item["content"]) for item in result)
