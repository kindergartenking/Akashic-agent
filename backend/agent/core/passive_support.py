"""passive_support 辅助函数（M1b 桩）。

原版这些函数做上下文提示拼接、token 估算、预算统计等。M1b 保留签名，
返回合理默认值，真实逻辑 M3 落地。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_context_hint_message(name: str, content: str) -> dict[str, str]:
    return {"role": "user", "content": f"[{name}]\n{content}"}


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        text = message.get("content", "")
        if isinstance(text, str):
            total += max(1, len(text) // 3)
    return total


def predict_current_user_source_ref(
    session_manager: Any,
    session: Any,
) -> str:
    return ""


def update_session_runtime_metadata(
    session: Any,
    *,
    tools_used: list[str],
    tool_chain: list[dict[str, Any]],
) -> None:
    return None


def build_post_reply_context_budget(context: Any, history: list[dict[str, Any]]) -> dict[str, int]:
    return {"history_messages": len(history)}


def extract_react_stats(context_retry: dict[str, Any]) -> dict[str, int]:
    return {}


def log_post_reply_context_budget(session_key: str, budget: dict[str, int]) -> None:
    logger.debug("post_reply budget session=%s budget=%s", session_key, budget)


def log_react_context_budget(session_key: str, react_stats: dict[str, int]) -> None:
    logger.debug("react budget session=%s stats=%s", session_key, react_stats)
