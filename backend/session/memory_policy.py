"""记忆排除策略（M1b 桩）。

原版 `session.memory_policy.excludes_memory` 根据 session 元数据判断是否跳过
post-memory 写入。M1b 阶段恒返回 False，真实谓词 M4 落地。
"""

from __future__ import annotations

from typing import Any


def excludes_memory(session_key: str, metadata: dict[str, Any]) -> bool:
    """M1b 桩：恒 False，表示不排除记忆写入。"""
    return False
