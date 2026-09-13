# Akashic Agent 前后端接口契约

前端只依赖以下 HTTP API 和 `/ws` WebSocket。所有 JSON 错误响应建议使用 `{ "detail": string | object }`；设置接口同时发送 `X-Akasic-CSRF: 1`。

## Shell 与 Chat

| 方法 | 路径 | 前端用途 |
|---|---|---|
| GET | `/api/shell/state` | 返回 `{status: "needs_setup" | "starting" | "ready", configured: boolean, chatReady: boolean, settingsPath: string}` |
| GET | `/api/chat/sessions?page=1&page_size=80` | 返回 `{items: SessionRow[]}`；行至少含 `key`，可含 `updated_at`、`message_count`、`first_message_content` |
| GET | `/api/chat/sessions/{session}/messages?page_size=50[&before_seq=N]` | 返回 `{items: MessageRow[], total, has_more, before_seq}` |
| GET | `/api/chat/models[?session_key=...]` | 返回模型注册表，结构见 `frontend/chat/src/web-chat-data.ts` 的 `ChatModelState` |
| POST | `/api/chat/uploads?filename=...` | 请求体为文件二进制；返回 `{filename, upload_path, upload_url?}` |
| GET | `/api/chat/media?path=...` | 返回媒体文件 |
| GET | `/api/chat/runtime/documents` | 返回 `{items: RuntimeDocument[]}` |
| GET | `/api/chat/runtime/jobs` | 返回 `{items: RuntimeJob[]}` |
| GET | `/api/chat/runtime/capabilities` | 返回 `RuntimeCapabilities` |
| GET | `/api/chat/runtime/documents/{id}` | 返回含 `title`, `relative_path`, `markdown` 的详情 |
| GET | `/api/chat/runtime/jobs/{id}` | 返回含 `id`, `name`, `timezone`, `markdown` 的详情 |
| GET | `/api/chat/runtime/mcp?owner_id=...&name=...` | 返回含 `owner_id`, `name`, `markdown` 的详情 |
| GET | `/api/chat/plugin-ui/catalog` | 返回移动插件 UI catalog |
| GET | `/api/chat/plugin-ui/asset?...` | 返回插件静态资源 |
| POST | `/api/chat/plugin-ui/query` | 插件 UI 查询接口 |

### WebSocket `/ws`

客户端发送：

- `session.attach`: `{type, request_id, session_id}`
- `message.send`: `{type, request_id, session_id, text, media, reply_to_message_id?, model_runtime_id?, model_reasoning_effort?}`
- `turn.stop`: `{type, request_id, session_id}`

服务端 frame 类型及必需字段：

- `session.created`: `request_id`, `session_id`
- `turn.started`: `session_id`, `turn_id`, `content`
- `react.thinking.delta`: `session_id`, `turn_id`, `delta`
- `react.tool.started`: `session_id`, `turn_id`, `call_id`, `tool_name`, `arguments`
- `react.tool.completed`: `session_id`, `turn_id`, `call_id`, `tool_name`, `status`, `result_preview`
- `answer.delta`: `session_id`, `turn_id`, `delta`
- `message.final`: `session_id`, `turn_id`, `content`, 可选 `thinking`, `media`, `duration_ms`, `metadata`
- `turn.output.completed`: `session_id`, `turn_id`, 可选 `client_message_id`
- `turn.interrupted`: `request_id`, `session_id`, `status`, `message`
- `error`: `request_id`, `message`
- `pong`: `request_id`

## Settings

| 方法 | 路径 | 请求/响应 |
|---|---|---|
| GET | `/api/settings/state` | 返回 `SettingsState` |
| POST | `/api/settings/models` | 请求 provider/model/api_key/base_url/credential_id；返回 `{models: ModelOption[]}` |
| POST | `/api/settings/apply` | 提交连接、模型、凭据、reasoning 和 `expected_config_revision` |
| POST | `/api/settings/roles` | `{role, model_id, reasoning_effort, expected_revision}` |
| POST | `/api/settings/memory` | `{enabled, engine, embedding_model_id, expected_revision}` |
| GET | `/api/settings/embedding-models` | 返回 embedding model 列表（由 `SettingsState.memory` 使用） |
| POST | `/api/settings/embedding-models` | 提交 embedding 连接；返回 `{model: EmbeddingModelSummary}` |
| POST | `/api/settings/codex-login` | 返回 `CodexLoginState` |
| GET | `/api/settings/codex-login/{login_id}` | 返回 `CodexLoginState` |

## Dashboard

| 方法 | 路径 | 前端用途 |
|---|---|---|
| GET | `/api/dashboard/sessions?...` | 分页 `PageResult<SessionRow>` |
| GET | `/api/dashboard/messages?...` | 分页 `PageResult<MessageRow>` |
| POST | `/api/dashboard/messages/batch-delete` | `{ids: string[]}` |
| DELETE | `/api/dashboard/interactions/{control_turn_id}` | 返回 `{message_ids: string[]}` |
| GET | `/api/dashboard/sessions/{session}/compaction` | 返回 `CompactionDetail` |
| GET | `/api/dashboard/proactive/overview` | 返回 `ProactiveOverview` |
| GET | `/api/dashboard/proactive/tick_logs?...` | 分页 `PageResult<ProactiveTick>` |
| GET | `/api/dashboard/proactive/tick_logs/{tick_id}` | 返回 `ProactiveTick` |
| GET | `/api/dashboard/proactive/tick_logs/{tick_id}/steps` | 分页 `PageResult<ProactiveStep>` |
| GET | `/api/dashboard/plugins` | 返回插件注册信息；插件面板随后从 `/plugins/{id}/...` 加载 JS/CSS |

## Mobile pairing

- `POST /api/chat/mobile-pairing` → `MobilePairingOffer`
- `GET /api/chat/mobile-pairing/{pairing_id}` → `PendingClaim | {pairing_id, status: "waiting_for_phone"}`
- `POST /api/chat/mobile-pairing/{pairing_id}/approve`，body `{confirmation_code}` → `PairedDevice`

完整字段类型以前端源码中的接口定义和解析函数为准；解析函数会严格校验字段，后端应避免返回额外包装层。
