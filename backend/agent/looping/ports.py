"""looping 端口（M1b 桩）。

原版 `SessionServices` 聚合 session_manager 与 presence，供 after_reasoning
阶段的持久化模块使用。M1b 用最小结构承载这两个依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SessionServices:
    session_manager: Any
    presence: Any = None
