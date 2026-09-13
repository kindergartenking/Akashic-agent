# 002 · 运行时复刻平移映射表

> 目标：把 `akashic-agent-mine`（前端重构 + 最小 MVP 后端）复刻为原版 `akashic-agent-main`
> 的 Agent Runtime 架构。本表回答两个问题：**每种能力走哪条协议**，以及**现有模块怎么映射到原版文件**。

---

## 0. 最关键的包结构决策（先读这条）

MVP 后端的导入约定是 **`PYTHONPATH=backend`**，包名不带 `backend.` 前缀：

```text
backend/agent/model_runtime/...   →   import agent.model_runtime
backend/tools/...                 →   import tools
backend/memory/...                →   import memory
```

原版的绝对导入（`from agent.plugins.registry import ...`、`from bus.event_bus import ...`、
`from agent.tools.base import Tool`）因此可以 **零改写** 直接落地——只要把同名包摆进 `backend/` 即可：

| 原版包 | 落地位置 | 导入名 |
|---|---|---|
| `agent/plugins/` | `backend/agent/plugins/` | `agent.plugins.*` |
| `agent/tools/` | `backend/agent/tools/` | `agent.tools.*` |
| `bus/` | `backend/bus/` | `bus.*` |
| `core/` | `backend/core/` | `core.*` |

**与现有目录的命名关系（不冲突，但要心里有数）：**

- `backend/agent/` 目前是 namespace 包（无 `__init__.py`），只放 `model_runtime`。新增
  `agent/plugins/`、`agent/tools/` 后，`agent` 变成"namespace 父包 + 多个 regular 子包"，与原版一致，**不需要动 `model_runtime`**。
- MVP 的扁平工具包 `backend/tools/`（`import tools`）与原版 `backend/agent/tools/`（`import agent.tools`）
  是**两个不同包**。前者是 MVP 临时产物，最终要收敛到后者。M0 阶段两者并存、互不覆盖。

---

## 1. 能力 → 协议 对照表

原版"一切皆插件"实际是 **两条并列挂载通道 + 一个独立记忆协议**，不是所有东西都继承 `Plugin`：

| 能力 | 挂载协议 | 注册入口 | 走 EventBus? | 版本/快照语义 |
|---|---|---|---|---|
| 模型可调用工具（builtin 必需工具） | `Tool(ABC)` + `ToolRegistry` | `bootstrap/toolsets/*.py` → `ToolRegistry.register(...)` | ❌ 走 ToolExecutor | 快照 `tool_registry` |
| 模型可调用工具（插件提供的扩展工具） | `@tool` 装饰器 → `MetadataKind.TOOL` | `plugin_registry._handlers` | ❌ 走 ToolExecutor | 快照 |
| 生命周期钩子（7 turn 阶段 + tool 前后） | `@on_*` 装饰器 → `MetadataKind.LIFECYCLE` | `plugin_registry._handlers` | ✅ GATE(intercept) / TAP(observe) | 快照 `event_handlers` |
| 工具前置钩子 | `@on_tool_pre` → `MetadataKind.TOOL_HOOK` | `plugin_registry._handlers` | ❌ 走 ToolExecutor pre_hook 链 | 快照 `tool_hooks` |
| Skill 目录 | `Plugin.skill_roots()` | Plugin → SkillIndex | ❌ | 快照 `skill_catalog_generation_id` |
| MCP server | `Plugin.mcp_servers()` | Plugin → McpCatalog | ❌ | 快照 `mcp_catalog_generation_ids` |
| 记忆引擎 | `MemoryPlugin(Protocol).build()` | `core/memory/plugin.py` | ❌ 独立协议 | 独立于 Plugin 体系 |

**一句话：**
- **"必需内置工具"** = 继承 `Tool(ABC)`，发布前由 `ToolsetProvider` 装配进 `ToolRegistry`。
- **"扩展能力（skill / mcp / 生命周期钩子 / 插件工具）"** = 继承 `Plugin(ABC)`，通过 `@tool` / `@on_*` /
  `@on_tool_pre` 装饰器 + 各种 `*_roots()/*_servers()` 贡献方法声明"何时用、在哪用"。
- **"记忆"** = 第三条路，`MemoryPlugin` Protocol（`build()` 返回 `MemoryEngine`），与 `Plugin(ABC)` 无关。

---

## 2. 生命周期事件（10 个 PluginEventType）

`agent/plugins/registry.py` 定义的权威枚举，7 个 turn 级 + 3 个 tool 级：

| 事件 | HandlerType | 语义 |
|---|---|---|
| `before_turn` | GATE | 可拦截（替换事件） |
| `before_reasoning` | GATE | 可拦截 |
| `prompt_render` | GATE | 可拦截（改 prompt） |
| `before_step` | GATE | 可拦截 |
| `after_step` | TAP | 只观察 |
| `after_reasoning` | GATE | 可拦截 |
| `after_turn` | TAP | 只观察 |
| `before_tool_call` | TAP | 只观察 |
| `after_tool_result` | TAP | 只观察 |
| `pre_tool` | —（TOOL_HOOK） | 不走 EventBus，走 ToolExecutor |

> **关键修正（之前校准过的）**：版本/快照切换的是**每个阶段挂载的 handler 集合**，
> 不是生命周期**顺序**；顺序由 Core 写死，`RuntimeSnapshot` 只换"谁来处理"。

---

## 3. 现有模块 → 原版文件 → 动作

### 3.1 工具层（MVP `backend/tools/` 是原版 `agent/tools/` 的阉割同名版）

| MVP 现有 | 原版对应 | 动作 | 里程碑 |
|---|---|---|---|
| `tools/base.py`（7 行极简 Tool） | `agent/tools/base.py`（259 行：`__init_subclass__` 校验 + `validate_params` + `to_schema` + `ToolExecutionContext`） | **改造**（原版替换，落到 `agent/tools/base.py`） | **M0** |
| `tools/registry.py`（简易 name lookup） | `agent/tools/registry.py`（`ToolMeta`/`ToolDocument`/`SearchBackend`/`requires_turn_search`/`_runtime_view` 快照视图） | **改造** | M1 |
| `tools/spawn.py` / `tools/shell.py` / `tools/filesystem.py` / `tools/web_search.py` / `tools/web_fetch.py` / `tools/message_lookup.py` / `tools/skill_loader.py` / `tools/tool_search.py` | `agent/tools/*.py`（同名） | **改造**（逐个用原版替换） | M1 |
| `tools/unified_exec.py` | `agent/tools/unified_exec.py` | **改造** | M1 |

### 3.2 编排/总线/上下文层

| MVP 现有 | 原版对应 | 动作 | 里程碑 |
|---|---|---|---|
| `message_bus.py`（内存 MessageBus） | `bus/queue.py`（MessageBus） | **改造** | M3 |
| `context_manager.py`（74% 窗口 + 压缩） | `agent/context/` 相关模块 | **改造** | M3 |
| `agent_orchestrator.py`（ReactRunner / AgentOrchestrator / SubAgent / SpawnTool 接线） | `agent/` 的 runner / lifecycle 编排 | **改造** | M1→M3 |
| `memory/akasha.py`（40KB 派生记忆） | `core/memory/` + `memory2/` | **改造** | M4 |
| `llm_client.py` / `model_config.py` / `session_store.py` / `settings_api.py` / `app.py` | 原版对应（provider / session / 配置） | **保持**（MVP 自有实现，逐步对齐） | M3+ |

### 3.3 新增（原版有、MVP 没有）

| 新文件 | 原版对应 | 说明 | 里程碑 |
|---|---|---|---|
| `agent/plugins/registry.py` | 同名 | `HandlerType`/`MetadataKind`/`PluginEventType` + `PluginHandlerRegistry` + `PluginRegistry` | **M0** |
| `agent/plugins/base.py` | 同名 | `Plugin(ABC)` + `__init_subclass__` 自动注册 | **M0** |
| `agent/plugins/decorators.py` | 同名 | `@on_*` / `@tool` / `@on_tool_pre` | **M0** |
| `agent/plugins/snapshot.py` | 同名 | M0 先落 lease + ContextVar + stable/latest 双指针桩；M2 替换为含 `RuntimeSnapshotCompiler` 全量 | **M0(桩)→M2(全量)** |
| `bus/event_bus.py` | 同名 | `EventBus`（emit/observe/fanout/enqueue + 快照 lease 绑定） | **M0** |
| `bus/events.py` + `bus/events_lifecycle.py` | 同名 | 事件契约（InboundMessage / TurnCommitted / ToolCallStarted...） | **M0** |
| `bus/queue.py` | 同名 | MessageBus | M3 |
| `bootstrap/toolsets/*.py` | 同名 | `ToolsetProvider` 协议 + meta/spawn 装配 | M1 |
| `core/memory/plugin.py` | 同名 | `MemoryPlugin(Protocol)` | M4 |

---

## 4. M0 落地清单（本次交付）

| 文件 | 性质 | 说明 |
|---|---|---|
| `backend/agent/plugins/registry.py` | **逐行对齐** | 契约枚举 + 双注册表 |
| `backend/agent/plugins/base.py` | **逐行对齐** | Plugin ABC（type-only 依赖，运行时零依赖） |
| `backend/agent/plugins/decorators.py` | **逐行对齐** | 唯一运行时依赖 `docstring_parser` |
| `backend/agent/plugins/snapshot.py` | **M0 桩** | 保留 lease + ContextVar + 双指针语义，剥离 `RuntimeSnapshotCompiler` |
| `backend/agent/tools/base.py` | **逐行对齐** | 原版 Tool ABC（`__init_subclass__` 校验 + `validate_params` + `to_schema`） |
| `backend/bus/event_bus.py` | **逐行对齐** | EventBus + EventSubscription |
| `backend/bus/events.py` | **逐行对齐** | 事件契约（TYPE_CHECKING 依赖为惰性） |
| `backend/bus/events_lifecycle.py` | **逐行对齐** | 生命周期事件契约 |
| `backend/m0_verify.py` | 验证脚本 | 验证插件自动注册、装饰器绑定、EventBus 四语义、Tool 校验、快照版本切换 |

> `agent/plugins/__init__.py` 为 M0 缩减版（只重导出 `Plugin` + 装饰器），
> 原版还重导出 config/context/scope/generation/jobs/specs——这些随 M2 补齐。
> 原版 `bus/` 与 `agent/tools/` 是 namespace 包（无 `__init__.py`），本复刻补了空 `__init__.py`
> （行为等价，仅更明确），已在文件中注明。

---

## 5. 后续里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** | 契约 + EventBus + Plugin/Tool 基类 + 装饰器 + 快照桩 | ✅ 已落地（18/18 验证） |
| **M1a** | 生命周期框架：`TurnLifecycle` + `Phase` 管道 + 7+3 Ctx 契约 + `prompting/assembler` | ✅ 已落地（11/11 验证） |
| **M1b** | phase 实现（`phases/*.py`）+ turn loop（`turn_pipeline`）+ `Reasoner`（改造 ReactRunner）+ `ToolRegistry` + 依赖留桩 | ✅ 已落地（m2_verify 22/22，见 `003`） |
| **M2** | 快照热重载全量（`RuntimeSnapshotCompiler` + generation 装配 + turn-boundary rollout PLG-013） | ⏳ 待办 |
| **M3** | channel + proactive + MCP + MessageBus 收敛 | ⏳ 待办 |
| **M4** | 记忆协议（`MemoryPlugin` + markdown/akasha 分层） | ⏳ 待办 |
