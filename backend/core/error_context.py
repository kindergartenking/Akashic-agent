"""错误上下文 ContextVar（M1b 桩）。"""

from __future__ import annotations

from contextvars import ContextVar

current_session_key: ContextVar[str] = ContextVar("akashic_current_session_key", default="")
current_client_message_id: ContextVar[str] = ContextVar(
    "akashic_current_client_message_id", default=""
)
