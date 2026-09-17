"""QQ 渠道适配器（OneBot 正向 WebSocket 客户端）。

对齐原版 `infra/channels/qq_channel.py` 的接入思想，适配当前项目的 MessageBus：

- 连接 NapCat 的 OneBot WebSocket（正向 WS）；
- 收 OneBot 消息事件（private / group）→ 构造 BusMessage → publish 进 MessageBus；
- emit 回调把 `message.final` 的 content 发回 QQ（私聊/群聊自动区分）。

chat_id 约定（与原版一致）：
  私聊："{user_id}"        （如 "987654321"）
  群聊："gqq:{group_id}"   （如 "gqq:111222333"）

OneBot 11 协议：
  事件：{"post_type":"message", "message_type":"private"/"group", "user_id":..., "group_id":..., "raw_message":...}
  API ：{"action":"send_private_msg"/"send_group_msg", "params":{...}}

依赖：websockets（OneBot 正向 WS）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from ..message_bus import BusMessage, MessageBus

logger = logging.getLogger(__name__)

_GROUP_PREFIX = "gqq:"


class QQAdapter:
    """把 QQ 消息（OneBot）桥接进 MessageBus。"""

    def __init__(
        self,
        ws_url: str,
        bus: MessageBus,
        *,
        sessions: Any = None,
        allow_from: list[str] | None = None,
        groups: list[str] | None = None,
        runtime_id: str | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._bus = bus
        self._sessions = sessions
        self._allow_from = {str(u) for u in (allow_from or [])}
        self._groups = {str(g) for g in (groups or [])}
        self._runtime_id = runtime_id

        self._ws: Any = None
        self._recv_task: asyncio.Task[None] | None = None

    # ── 生命周期 ─────────────────────────────────────────────

    async def start(self) -> None:
        import websockets

        self._ws = await websockets.connect(self._ws_url)
        self._recv_task = asyncio.create_task(self._recv_loop())
        print(f"[qq] 已连接 OneBot WebSocket: {self._ws_url}", flush=True)

    async def stop(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        logger.info("[qq] QQAdapter 已停止")

    # ── 入站（OneBot 事件 → BusMessage）──────────────────────

    async def _recv_loop(self) -> None:
        while True:
            raw = await self._ws.recv()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("post_type") == "message":
                await self._handle_message_event(event)

    async def _handle_message_event(self, event: dict) -> None:
        message_type = str(event.get("message_type") or "")
        user_id = str(event.get("user_id") or "")
        raw_message = self._extract_text(event)

        if not raw_message.strip():
            return
        if not self._is_allowed(user_id):
            logger.warning("[qq] 拒绝未授权用户 user_id=%s", user_id)
            return

        if message_type == "private":
            chat_id = user_id
        elif message_type == "group":
            group_id = str(event.get("group_id") or "")
            if self._groups and group_id not in self._groups:
                logger.debug("[qq] 忽略未配置群 group_id=%s", group_id)
                return
            chat_id = f"{_GROUP_PREFIX}{group_id}"
        else:
            return

        preview = raw_message[:60] + ("..." if len(raw_message) > 60 else "")
        print(f"[qq] 收到消息 chat_id={chat_id} 内容={preview!r}", flush=True)

        turn_id = f"turn-{uuid4().hex}"
        # 先记录用户消息（对齐 WebSocket 入口，保证 complete_turn 的外键成立）
        user_message_id = ""
        if self._sessions is not None:
            user_message_id = await asyncio.to_thread(
                self._sessions.record_user_message,
                chat_id,
                turn_id,
                raw_message,
                user_id=user_id,
                metadata={"channel": "qq", "chat_type": message_type},
            )

        message = BusMessage(
            owner_id=id(self),
            session_id=chat_id,
            turn_id=turn_id,
            text=raw_message,
            runtime_id=self._runtime_id,
            user_message_id=user_message_id,
            emit=self._make_emit(chat_id),
            on_error=self._make_on_error(chat_id),
        )
        await self._bus.publish(message)

    @staticmethod
    def _extract_text(event: dict) -> str:
        """从 OneBot 事件提取纯文本（raw_message 优先，兼容 CQ 码）。"""
        raw = str(event.get("raw_message") or "")
        if raw:
            return raw
        msg = event.get("message")
        if isinstance(msg, str):
            return msg
        if isinstance(msg, list):
            parts = []
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(str(seg.get("data", {}).get("text", "")))
            return "".join(parts)
        return ""

    def _is_allowed(self, user_id: str) -> bool:
        if not self._allow_from:
            return True
        return user_id in self._allow_from

    # ── 出站（emit 事件 → QQ 消息）───────────────────────────

    def _make_emit(self, chat_id: str):
        async def emit(frame: dict) -> None:
            if frame.get("type") == "message.final":
                content = str(frame.get("content") or "").strip()
                if content:
                    await self._send_text(chat_id, content)

        return emit

    def _make_on_error(self, chat_id: str):
        async def on_error(exc: Exception) -> None:
            logger.error("[qq] turn 失败 chat_id=%s: %s", chat_id, exc)
            try:
                await self._send_text(chat_id, f"出错了：{exc}")
            except Exception:
                logger.exception("[qq] 发送错误提示失败")

        return on_error

    async def _send_text(self, chat_id: str, text: str) -> None:
        """发文本到 QQ（OneBot API，自动区分私聊/群聊）。"""
        if chat_id.startswith(_GROUP_PREFIX):
            action = "send_group_msg"
            params = {"group_id": int(chat_id[len(_GROUP_PREFIX):]), "message": text}
        else:
            action = "send_private_msg"
            params = {"user_id": int(chat_id), "message": text}
        await self._call_api(action, params)

    async def _call_api(self, action: str, params: dict) -> None:
        if self._ws is None:
            raise RuntimeError("QQ WebSocket 未连接")
        payload = {"action": action, "params": params}
        await self._ws.send(json.dumps(payload, ensure_ascii=False))
        print(f"[qq] 调用 API action={action}", flush=True)
