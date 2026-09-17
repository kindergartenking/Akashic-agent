"""QQ 适配器测试：mock OneBot WS 服务端 + stub handler，验证入站/出站闭环。

模拟 NapCat：发一个私聊消息事件，收 QQAdapter 发回的 send_private_msg API，
验证「OneBot 事件 → BusMessage → handler → emit message.final → OneBot API」全链路。

运行（项目根目录）：
    E:/Anaconda/python.exe backend/demo_qq_adapter.py
"""

from __future__ import annotations

import asyncio
import json

from backend.channels.qq_adapter import QQAdapter
from backend.message_bus import MessageBus

PORT = 39001


async def main() -> None:
    import websockets

    received_api: list[dict] = []

    async def mock_napcat(ws) -> None:
        # NapCat 收到客户端连接后，推一个私聊消息事件
        await ws.send(json.dumps({
            "post_type": "message",
            "message_type": "private",
            "user_id": 987654321,
            "raw_message": "你好，测试",
        }))
        # 收 QQAdapter 发来的 API 请求（send_private_msg）
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=3)
            received_api.append(json.loads(raw))
        except Exception:
            pass

    server = await websockets.serve(mock_napcat, "127.0.0.1", PORT)

    # stub MessageBus + handler（模拟 handle_message）
    bus = MessageBus()

    async def handler(message) -> None:
        print(f"[handler] 收到消息: {message.text!r} (chat_id={message.session_id})")
        await message.emit({
            "type": "message.final",
            "session_id": message.session_id,
            "turn_id": message.turn_id,
            "content": "你好，我是回复",
        })

    await bus.start(handler)

    # QQAdapter 连 mock NapCat
    adapter = QQAdapter(f"ws://127.0.0.1:{PORT}", bus)
    await adapter.start()

    await asyncio.sleep(1)

    print(f"\n[mock NapCat] 收到的 API: {received_api}")
    assert received_api, "应收到 QQ 发回的 API 请求"
    api = received_api[0]
    assert api["action"] == "send_private_msg", api
    assert api["params"]["user_id"] == 987654321, api
    assert api["params"]["message"] == "你好，我是回复", api
    print("✅ QQ 适配器入站/出站闭环验证通过")

    await adapter.stop()
    await bus.stop()
    server.close()


if __name__ == "__main__":
    asyncio.run(main())
