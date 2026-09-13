"""会话管理器（M1b 桩）。

原版 `Session` / `SessionManager` 负责持久化消息历史与元数据。语义对齐：
- `Session.add_message` = build 消息 dict（含稳定 id），**不提交**；
- `SessionManager.append_messages` = 把消息提交进 session 历史。

M1b 用内存实现，不落 DB，真实持久化 M3/M4 落地。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.core.runtime_support import HistoryUnit


@dataclass
class Session:
    key: str
    metadata: dict[str, Any] = field(default_factory=dict)
    _messages: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = field(default=0, init=False, repr=False)

    def add_message(self, role: str, content: str, **kwargs: Any) -> dict[str, Any]:
        message: dict[str, Any] = {
            "id": f"msg_{self.key}:{self._seq}",
            "role": role,
            "content": content,
        }
        message.update(kwargs)
        self._seq += 1
        return message

    def history_units(self) -> list[HistoryUnit]:
        if not self._messages:
            return []
        return [HistoryUnit(messages=list(self._messages))]


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get_existing(self, key: str) -> Session | None:
        return self._sessions.get(key)

    def get_or_create(self, key: str) -> Session:
        session = self._sessions.get(key)
        if session is None:
            session = Session(key=key)
            self._sessions[key] = session
        return session

    async def append_messages(
        self,
        session: Session,
        messages: list[dict[str, Any]],
    ) -> None:
        session._messages.extend(messages)
