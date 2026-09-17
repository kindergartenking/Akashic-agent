# QQ / Telegram 接入技术文档

> 基于对原版 `akashic-agent-main` 渠道层源码的梳理，以及当前项目
> `akashic-agent-mine` 的接入方案设计。

---

## 1. 原版渠道架构总览

原版把「外部消息来源」抽象成统一的 **Channel（渠道）契约**，让 Web / QQ / Telegram
等不同入口都收敛到同一条消息总线。

### 1.1 核心契约 `infra/channels/contract.py`

```python
class Channel(Protocol):
    name: str                              # 渠道名，如 "qq" / "telegram" / "web"

    # 返回只表示入口 ownership 已取得且 channel ready；失败必须抛错。
    async def start(self, ctx: ChannelContext) -> None: ...

    # 返回只表示新 ingress 已停止、在途工作已收束、ownership 已释放。
    async def stop(self) -> None: ...
```

`ChannelContext` 是渠道启动时注入的依赖容器：

```python
@dataclass
class ChannelContext:
    bus: MessageBus                    # 消息总线（入站 publish_inbound / 出站订阅）
    session_manager: SessionManager    # 会话管理
    event_bus: EventBus                # 生命周期事件（工具调用等）
    push_tool: MessagePushTool         # 主动推送工具
    attachment_store: AttachmentStore  # 附件存储（图片/文件落盘）
    http_resources: SharedHttpResources
    interrupt_controller: InterruptController | None   # /stop 中断
    mobile_bot_commands: list[tuple[str, str]]
    log: logging.Logger
```

### 1.2 三个内置渠道

| 渠道 | 文件 | 技术栈 |
|---|---|---|
| Web 聊天 | `infra/channels/web_chat_channel.py` | WebSocket |
| Telegram | `infra/channels/telegram_channel.py` | `python-telegram-bot`（Application + Handler） |
| **QQ** | `infra/channels/qq_channel.py` | **NcatBot SDK → NapCat（OneBot WebSocket）** |

### 1.3 装配 `bootstrap/channels.py`

```python
async def start_channels(config, *, bus, session_manager, push_tool, ...):
    host = ChannelHost(_ctx_factory)

    if config.channels.telegram and config.channels.telegram.token:
        host.add(TelegramChannel(token=..., ...))

    if config.channels.qq and config.channels.qq.bot_uin:
        host.add(QQChannel(bot_uin=..., ...))

    for channel in plugin_channels or []:
        host.add(channel)     # 插件贡献的渠道
    return host
```

**关键点**：渠道由 config 里的凭据（`telegram.token` / `qq.bot_uin`）决定是否启用；
插件也能通过 `Plugin.channels()` 贡献自定义渠道。

---

## 2. 统一消息流（渠道无关）

所有渠道都收敛到同一条「入站 → 总线 → 出站」的数据流：

```
外部消息（QQ/Telegram/Web）
  → 渠道把外部消息转成 InboundMessage
  → MessageBus.publish_inbound(InboundMessage)          # 入站
  → AgentLoop（统一的 agent 处理，跟来源无关）
  → MessageBus 广播 OutboundMessage                     # 出站
  → 渠道订阅出站（subscribe_outbound），把 OutboundMessage 发回外部
```

`InboundMessage` 的核心字段：

```python
InboundMessage(
    channel="qq",            # 渠道名
    sender=user_id,          # 发送者
    chat_id=...,             # 会话标识（见下文 chat_id 约定）
    content=text,            # 文本
    media=[...],             # 图片/文件的本地路径
    metadata={...},          # 渠道特有元数据
)
```

`OutboundMessage` 由 agent 产出，渠道 `_on_response` 收到后翻译成渠道原生消息发送。

---

## 3. QQ 接入详解（重点）

### 3.1 技术栈与消息流

```
QQ 客户端
  └─ NapCat（QQ 机器人框架，OneBot 协议 WebSocket 服务端）
       └─ NcatBot（Python SDK，正向 WS 客户端）
            └─ QQChannel（把 OneBot 事件翻译成 InboundMessage / OutboundMessage）
```

**消息流向**：`QQ → NcatBot → MessageBus → AgentLoop → MessageBus → QQ`

### 3.2 chat_id 约定

| 会话类型 | chat_id 格式 | 示例 |
|---|---|---|
| 私聊 | `"{user_id}"` | `"987654321"` |
| 群聊 | `"gqq:{group_id}"` | `"gqq:111222333"` |

session_key 统一为 `"{channel}:{chat_id}"`，如 `"qq:987654321"`、`"qq:gqq:111222333"`。

### 3.3 入站（QQ → Agent）

`QQChannel` 绑定 NcatBot 事件回调：

```python
@self._bot.on_private_message()      # 私聊
async def _(event):
    user_id = str(event.user_id)
    if not self._is_allowed(user_id):  # allow_from 白名单
        return
    text, img_urls = _extract_cq_images(event.raw_message)  # 解析 [CQ:image] 码
    ...
    self._submit_to_main_loop(self._handle_private(user_id, text, img_urls))

@self._bot.on_group_message()        # 群聊
async def _(event):
    group_id = str(event.group_id)
    group_cfg = self._groups.get(group_id)   # 只处理已配置的群
    ...
    # 群过滤（如 @机器人 才响应）
    future = run_coroutine_threadsafe(self._group_filter.should_process(event, group_cfg), main_loop)
    if not future.result(timeout=5):
        return
    ...
```

入站最终统一调用 `publish_inbound`：

```python
# 私聊
await self._bus.publish_inbound(InboundMessage(
    channel="qq", sender=user_id, chat_id=user_id, content=text,
    media=media, metadata={"chat_type": "private"},
))

# 群聊
chat_id = f"gqq:{group_id}"
await self._bus.publish_inbound(InboundMessage(
    channel="qq", sender=user_id, chat_id=chat_id, content=text,
    media=media, metadata={"chat_type": "group", "group_id": group_id, "sender_id": user_id},
))
```

### 3.4 出站（Agent → QQ）

`QQChannel` 订阅出站：

```python
self._bus.subscribe_outbound(_CHANNEL, self._on_response)

async def _on_response(self, msg: OutboundMessage):
    receipt = await self._deliver_message(channel_message_from_outbound(msg))
    if not receipt.succeeded:
        raise RuntimeError(receipt.detail or "QQ 消息提交失败")
```

发送通道（自动区分私聊/群聊）：

```python
async def send(self, chat_id, message):
    if chat_id.startswith("gqq:"):
        await api.send_group_text(int(group_id), message)
    else:
        await api.send_private_text(int(chat_id), message)
```

同时支持 `send_file`（base64:// 或本地文件）、`send_image`。

### 3.5 三个关键摩擦点（跨 loop 桥接）

原版注释明确标注了 NcatBot 接入的三个难点：

1. **`run_backend()` 是同步阻塞调用** → 用 `run_in_executor` 包裹：
   ```python
   self._api = await self._main_loop.run_in_executor(None, self._bot.run_backend)
   ```

2. **NcatBot 事件回调跑在独立线程/loop** → 用 `run_coroutine_threadsafe` 桥接到主 loop：
   ```python
   def _submit_to_main_loop(self, coro):
       asyncio.run_coroutine_threadsafe(coro, self._require_main_loop())
   ```

3. **出站消息需跨 loop 调用 API** → `run_coroutine_threadsafe` 投递回 NcatBot loop：
   ```python
   async def _run_on_bot_loop(self, coro):
       future = asyncio.run_coroutine_threadsafe(coro, self._bot_loop)
       return await asyncio.wrap_future(future)
   ```

### 3.6 其他细节

- **图片**：QQ 图片以 CQ 码 `[CQ:image url=...]` 传入，正则提取 URL → 下载到临时文件 → 交给 agent；
- **群过滤**：`GroupMessageFilter`（默认 `DefaultGroupFilter`），决定群消息是否响应（如 @机器人）；
- **/stop 中断**：识别 `/stop` 命令，调 `interrupt_controller.request_interrupt`；
- **过程记录**：监听 `TurnStarted`/`ToolCallStarted`/`ToolCallCompleted`，私聊时把「模型思路 + 工具链」用 ForwardConstructor 合并转发；
- **白名单**：`allow_from` 限制可交互用户。

---

## 4. Telegram 接入（对比）

技术栈为 `python-telegram-bot`：

```python
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# 入站
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
app.add_handler(CommandHandler("stop", on_stop))

# 出站
await send_markdown(...)        # 一次性发送
await send_stream_markdown(...)  # 流式编辑（打字机）
```

与 QQ 的差异主要在 SDK 与协议，但**入站/出站都翻译成相同的 InboundMessage/OutboundMessage**，
消息总线之后的处理完全一致。

---

## 5. 当前项目（akashic-agent-mine）的接入方案

### 5.1 现状差异

当前项目是 MVP 后端（`backend/app.py`），消息总线是简化的 `backend/message_bus.py`：

```python
@dataclass(frozen=True)
class BusMessage:
    owner_id: int
    session_id: str
    turn_id: str
    text: str
    runtime_id: str | None
    emit: Callable[[dict], Awaitable[None]]          # 发事件（answer.delta / message.final）
    on_error: Callable[[Exception], Awaitable[None]]
    user_message_id: str = ""

class MessageBus:
    async def publish(self, message: BusMessage): ...   # 入队
    async def start(self, handler): ...                 # 起 worker
```

**关键差异**：当前项目没有原版的 `InboundMessage`/`OutboundMessage`/`Channel` 契约，
也没有 `publish_inbound`/`subscribe_outbound`。WebSocket 入口直接构造 `BusMessage`，
`emit` 回调把事件推回前端。

### 5.2 QQ 接入方案（对齐原版思想，适配当前总线）

在原版思想基础上，适配到当前 `MessageBus`：

```
QQ 消息（OneBot WebSocket，来自 NapCat）
  → QQAdapter 收到 OneBot 事件
  → 构造 BusMessage(emit=发回QQ的回调)
  → MessageBus.publish(BusMessage)
  → handle_message 处理（emit 发 answer.delta / message.final）
  → QQAdapter 的 emit 把 message.final 的 content 发回 QQ
```

核心：**QQ 渠道只需实现「入站：OneBot 事件 → BusMessage」和「出站：emit 事件 → QQ 消息」两段，
中间的 agent 处理完全复用现有 handle_message。**

### 5.3 依赖选择

| 方案 | 依赖 | 说明 |
|---|---|---|
| A（原版） | NcatBot SDK | 封装扫码登录/事件回调/API，但依赖 NapCat 特定版本、重 |
| B（本次实现） | `websockets`（OneBot 正向 WS） | 直接实现 OneBot 11 协议，轻、可控，仍需 NapCat 服务端 |

本次采用 **方案 B**：直接实现 OneBot 正向 WebSocket 客户端，连接 NapCat 的
`ws://127.0.0.1:<port>`，收发 OneBot JSON（事件 + API），不依赖 NcatBot SDK。

OneBot 11 协议要点：

- **事件**：服务端推送 `{"post_type":"message", "message_type":"private"/"group", "user_id":..., "group_id":..., "raw_message":...}`；
- **API**：客户端发 `{"action":"send_private_msg"/"send_group_msg", "params":{"user_id"/"group_id":..., "message":...}}`，服务端回 `{"status":"ok"}`。

---

## 6. 实现清单（QQ 接入）

1. `backend/channels/qq_adapter.py`：`QQAdapter`，OneBot 正向 WS 客户端：
   - 连接 NapCat WS；
   - 收 OneBot 消息事件 → 构造 `BusMessage`（emit 回调发 QQ）→ `MessageBus.publish`；
   - emit 回调：收到 `message.final` 发 `content` 到 QQ（私聊/群聊自动区分）；
   - `answer.delta` 可选流式（本次先忽略，只发 message.final）。
2. `backend/app.py`：`start()` 里按 config 启动 `QQAdapter`；`stop()` 里停止。
3. 配置：`config.toml` 增加 `[qq]`（`ws_url`、`allow_from`、`groups`）。

---

## 7. 关键结论

- 原版渠道层的核心是 **Channel 契约 + 统一消息总线**：任何渠道都只做两件事——
  入站把外部消息翻译成 InboundMessage、出站把 OutboundMessage 翻译回外部；
- QQ 用 **NcatBot → NapCat（OneBot WebSocket）**，Telegram 用 **python-telegram-bot**，
  两者差异只在 SDK/协议，总线之后完全一致；
- 当前项目可复用现有 `MessageBus` + `handle_message`，只需补一个 **QQ 适配器**
  （OneBot WS 客户端），把 QQ 消息桥接进总线，emit 事件桥接回 QQ。
