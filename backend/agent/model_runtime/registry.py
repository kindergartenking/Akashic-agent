"""模型绑定注册表（M1b 桩）。

原版 `current_model_binding()` 从 ContextVar 读取当前模型绑定，供 after_turn
阶段的 `_BuildTurnWorkModule` 记录模型信息。M1b 恒返回 None。
"""

from __future__ import annotations

from typing import Any


def current_model_binding() -> Any | None:
    return None
