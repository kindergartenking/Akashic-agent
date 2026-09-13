# Akashic MVP 后端

当前后端实现一条最小 Agent 编排链路：

```text
网页端 → WebSocket /ws → 内存 MessageBus → 主 Agent 路由 →（直接回复或单轮子 Agent）→ LLM → 流式 WebSocket 回复
```

主 Agent 自身运行多轮 ReAct，同时看到 `web_search` 和 `spawn`：它可以直接回复、自己连续
调用工具，或通过结构化 `spawn` 工具派生一个同步子 Agent。子 Agent 也运行多轮 ReAct 并可
使用 `web_search`，但看不到 `spawn`，因此不会递归派生。主 Agent 最终会读取 `spawn` 返回的
子任务结果，再生成面向用户的正式回复。

轮数参考原项目当前同步路径：主 Agent 和同步子 Agent 都最多进行 10 次 LLM 迭代。达到上限
后会保留已有 observation，并额外进行一次禁用工具的强制总结。完全相同的工具名+参数最多
执行两次，第三次会被循环保护阻止并进入收尾。

会将 session、turn、user/assistant 消息保存到工作区 `sessions.db`。当前 Runtime 还直接拥有
一个解耦 Plugin 的 Akasha 记忆模块：已完成回合从 `sessions.db` 派生到
`memory/akasha.db`，下一次输入先做只读召回，再把结果作为 system evidence 注入 Agent；
assistant 消息成功落库后才提交对应的记忆回合。主 Agent 的路由决策不会持久化为用户可见
消息；子 Agent 的工具调用事实会写入 assistant message 的 `tool_chain`，因此刷新历史后
工具卡片仍可恢复。短期会话上下文（74% 窗口和最近五轮压缩）与 Akasha 长期召回是两条
独立路径。

## 启动

先安装依赖：

```powershell
pip install -r requirements-mvp.txt
```

使用已配置的 SQLite 模型注册表（工作区根目录的 `model-registry.sqlite3`）：

```powershell
python -m uvicorn backend.app:app --host 127.0.0.1 --port 2236
```

也可以完全使用环境变量启动，无需数据库：

```powershell
$env:AKASHIC_API_KEY = "sk-..."
$env:AKASHIC_BASE_URL = "https://api.openai.com/v1"
$env:AKASHIC_MODEL = "gpt-4o-mini"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 2236
```

支持的环境变量：`AKASHIC_API_KEY`、`AKASHIC_BASE_URL`、`AKASHIC_MODEL`、
`AKASHIC_PROVIDER`、`AKASHIC_MODEL_REGISTRY`、`AKASHIC_WORKSPACE`。

## 接口行为

- `GET /api/shell/state`、`GET /api/chat/models` 为前端启动所需的最小状态接口。
- 会话列表和历史接口从工作区 `sessions.db` 读取已持久化的 session、turn 和消息；
  当前会话上下文会按模型窗口 74% 预算拼接，超限时压缩更早历史并保留最近五轮；
  Akasha 长期召回则作为独立 system evidence 注入。
- `/ws` 接收 `session.attach`、`message.send`、`turn.stop`；回复使用
  `turn.started`、`answer.delta`、`turn.output.completed`、`message.final`。发生派生时还会发送
  `agent.delegation.started` 和 `agent.delegation.completed`，其中包含临时 `agent_id`。
- 工具执行使用 `react.tool.started` 和 `react.tool.completed`；内置 `web_search` 与原项目一样
  调用 Exa 公共 MCP 端点。工具名白名单、基础 JSON Schema 校验、30 秒超时和结果预览已启用。
- `turn.stop` 会取消当前连接对应会话的正在进行的 LLM 请求。
- `memory/akasha.db` 是可删除、可重建的派生库，不是事实源。记忆召回包含 direct 和
  completion 两个 lane，携带 session、turn、消息 ID 和分数；embedding 未配置或不可用
  时自动降级为确定性的词法 BM25 召回，不影响聊天。
- 纯检索回归基准借鉴原项目 LongMemEval 的 `single-session-user`、
  `single-session-preference`、`knowledge-update` 类型，并补充跨会话事实、时间边补全和
  BM25 降级样例。执行 `python -m test.backend.akasha_retrieval_eval` 可查看 Recall@K、Hit@K、
  MRR、nDCG；它使用固定 embedding 边界，测试的是记忆引擎而不是外部模型或最终 LLM 回答。
- 大规模记忆压测使用 `python -m test.backend.akasha_large_memory_eval --reset`，默认在
  `tmp/akasha-large-eval` 创建 100 个主题、每主题 100 个回合（共 10,000 条记忆），
  并通过真实 `sessions.db -> memory/akasha.db` 重建链路建立索引。可用
  `--case knowledge-update|near-topic-collision|temporal-successor|lexical-fallback|
  short-query-noise|cross-session-fact --limit 10` 触发指定 bad case；输出会显示期望
  turn、实际排名、lane、分数和 trace。该 fixture 与聊天数据库隔离，不会污染日常会话。
- 如果存在 `static/chat` 和 `static/dashboard` 构建产物，后端还会提供 `/chat`、
  `/settings`、`/dashboard` 页面，以及 `/assets`、`/dashboard/assets` 静态资源。

设置页面的模型配置 HTTP 接口已经接入：`/api/settings/state`、`/models`、`/apply`、
`/roles`、`/memory`、`/embedding-models` 和 Codex device login。前端可直接打开
Codex、OpenCode Go、DeepSeek 或自定义 API 的配置弹窗；API key 只写入服务端
SQLite，不会在状态接口或保存响应中返回。运行时每个新请求重新读取最新 SQLite
revision，当前正在执行的 turn 不会被中途替换。

## 原项目不是一张 `model/apikey/baseurl` 表

原项目使用 workspace 级 SQLite 文件 `model-registry.sqlite3`，核心是五张表：

| 表 | 作用 |
|---|---|
| `model_registry_meta` | 单例 revision；每次完整配置变更递增，用于乐观并发控制 |
| `model_connections` | Provider 连接：`id/name/provider/catalog_provider_id/auth_id/base_url/auth_kind/auth_payload/enabled/timestamps` |
| `model_definitions` | 一个连接下的模型：模型名、reasoning effort、上下文/输出能力、输入模态、能力来源等；`UNIQUE(connection_id, model)` |
| `model_role_bindings` | `default/fast/agent/vision` 角色到模型的绑定及 reasoning effort |
| `embedding_models` | 记忆系统的 embedding 模型（同样复用 Provider connection） |

API key 不单独放在明文 `api_key` 列，而是保存在 `model_connections.auth_payload` JSON 中，并由 `auth_kind` 标识类型；读取状态时只返回凭据元数据，不返回 token。Codex/OAuth 等凭据也可以复用同一结构。

## 原项目的数据库兜底

- 数据库：Python 标准库 `sqlite3`，不是 PostgreSQL/MySQL，也没有通用数据库连接池。
- 每次读写打开独立连接；连接开启 `PRAGMA foreign_keys = ON`。
- 写事务使用 `BEGIN IMMEDIATE`，revision 检查和 connection/model/role 写入在同一事务中。
- `expected_revision` 不匹配时拒绝写入，避免旧页面覆盖新配置。
- 外键使用 `ON DELETE RESTRICT`，防止仍被角色或 embedding 引用的模型被删除。
- `integrity_check` 与 `foreign_key_check` 用于备份恢复和启动校验。
- 数据库、WAL、SHM 文件权限收紧为 `0600`；凭据更新另有跨进程锁和原子写入路径。
- 读取快照时在一个只读事务中同时读取 revision、模型和角色，避免读到半套配置。
- 运行中的模型配置不是直接替换对象，而是通过 immutable generation + lease：新请求读新 revision，已开始的请求继续使用旧快照。

HTTP 模型请求层使用 `httpx.AsyncClient` 的连接复用和超时控制，但这不是数据库连接池。

## 模型设置与原项目的差异

- 保留：Provider 连接、模型目录发现、能力归一化、Codex OAuth device login、
  write-only 凭据、revision/`BEGIN IMMEDIATE`、备份与失败回滚，以及四类角色绑定。
- 有意简化：未复制原项目 NumPy 动态图的全部 diffusion、plasticity、snapshot/replay
  表结构以及 feedback 工具；当前侧边库用 dense cosine + BM25、session temporal edge
  和轻量 remember/forget 状态表达同一 causal 边界。`memory.changeLocked` 不读取原项目
  `sessions.db` 的历史锁定状态；保存后不触发原项目 gateway reload，而是让下一次
  请求读取新 revision；Codex 仅实现当前文本流，不复制完整工具 replay/历史响应链路。
- OpenCode Go 在本机目录不可用时使用 provider catalog fallback；自定义 API 的模型
  能力优先读取 LiteLLM/genai-prices，无法识别时保留 `unknown` 来源。

## 当前复制内容

- `agent/model_runtime/store.py`
- `agent/model_runtime/auth/store.py`
- `agent/model_runtime/errors.py`
- 对应的 schema migration 文件

为兼容 Windows，凭据锁把原项目 POSIX `fcntl.flock` 增加了 `msvcrt` fallback；SQLite schema 和事务语义保持原样。

示例初始化：

```powershell
$env:PYTHONPATH = "backend"
python -c "from pathlib import Path; from agent.model_runtime.store import ModelRegistryStore; s=ModelRegistryStore(Path('model-registry.sqlite3')); s.initialize(); print(s.revision())"
```
