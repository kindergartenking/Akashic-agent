# 003 — M1b：七阶段生命周期落地设计（先接线、依赖留桩）

> 状态：✅ 已落地（m2_verify 22/22；M0 18/18、M1a 11/11 回归通过）
> 上游：`002-runtime-migration-map.md`（M1a 已落地：types / facade / phase / prompting）
> 原版参照：`akashic-agent-main/agent/core/passive_turn.py` + `agent/lifecycle/phases/*.py`
>
> **落地偏差（相对本设计）**：§8.3 原计划「重写 `agent_orchestrator.py`」调整为
> 「新增 `agent/core/wiring.py` 接线 + `agent_orchestrator.py` 保持不动」。原因：MVP
> 的 `agent_orchestrator.py` 依赖自己的 `tools.ToolRegistry`（`schemas()/execute()/
> fork()/document()`）与 `llm_client.LLMClient`（`.chat(config, ...)`），API 与 akashic
> 接口不一致，破坏性重写会打断 `app.py` 正在使用的 MVP 聊天路径。收敛留到 M3 用适配层完成。

---

## 1. 目标与范围

把 M1a 落地的「骨架」（`TurnLifecycle` facade + `Phase` 管道 + 7+3 Ctx 类型）接上「肉」：

1. **7 个 Phase 实现**逐行对齐原版 `agent/lifecycle/phases/*.py`。
2. 新增 **turn-loop driver**（缩减版 `PassiveTurnPipeline`），硬编码 7 阶段顺序。
3. 新增 **Reasoner**，把 `ReactRunner` 的内层循环改造成 `prompt_render` + `before_step/after_step`。
4. 把 MVP 的 `AgentOrchestrator` 重接到 pipeline。

**留桩边界（本次的关键决策）**：`session / context / memory / outbound / control` 这些叶子依赖一律用**最小桩**，只保证 7 阶段顺序能端到端跑通、可验证；真实实现（SessionManager 持久化、ContextBuilder、memory policy 等）留到 M3/M4。

**不做的**：snapshot 热重载（M2）、channels/proactive/MCP（M3）、memory protocol（M4）。

---

## 2. 架构决策（本次新增）

| # | 决策 | 理由 |
|---|---|---|
| D1 | 7 个 phase 文件**逐行对齐**原版，只替换叶子依赖 import | 延续 002 的零改写平移策略；`PYTHONPATH=backend` 使 `agent.lifecycle.phases.*`、`bus.event_bus` 等绝对导入直接命中 |
| D2 | **7 阶段顺序硬编码**在 driver 里，不用 EventBus 编排 | 原版即如此；`TurnLifecycle`/`bus.emit` 只是插件视角的 hook，是「链上的一环」而非顺序来源 |
| D3 | 7 阶段分布两层：pipeline（5 顺序点）+ reasoner（3 阶段） | 原版 `run()` 调 5 个 Phase，`before_step/after_step/prompt_render` 藏在 `DefaultReasoner.run_turn()` 内 |
| D4 | 依赖留桩用「协议 + 内存实现」，不碰真实 DB / LLM | 桩只满足 phase 模块链的类型契约；验证脚本证明顺序正确即可 |
| D5 | `ReactRunner` 重命名为 `Reasoner`，保留原 `AgentOrchestrator` 对外入口 | 最小侵入；`run()` 循环体移入 reasoner，调度交给 pipeline |

---

## 3. 七个 Phase 的模块链与接口

每个 phase = 一个 `PhaseFrame`（dataclass 子类）+ 一个 `default_*_modules(...)` 工厂 + N 个模块。`slot` 形如 `phase.module`（builtin 前缀），数据 slot 形如 `命名空间:字段`。

### 3.1 before_turn — `BeforeTurnFrame[TurnState, BeforeTurnCtx]`

`default_before_turn_modules(bus, session_manager, context_store, plugin_modules=None)`

| 顺序 | slot | requires | produces | 职责 | 依赖 |
|---|---|---|---|---|---|
| 1 | `before_turn.acquire_session` | — | `session:session` | get_or_create session | SessionManager |
| 2 | `before_turn.memory_exclusion` | acquire_session | — | 命中谓词则注 skip_post_memory | memory_policy |
| 3 | `before_turn.prepare_context` | acquire_session | `session:context_bundle` | 组装检索记忆/历史 | ContextStore |
| 4 | `before_turn.build_ctx` | prepare_context | `session:ctx` | 构造 BeforeTurnCtx | — |
| 5 | `before_turn.emit` | build_ctx | `session:ctx` | `bus.emit(ctx)` | EventBus |
| 6 | `before_turn.collect_exports` | emit | `session:ctx` | 收 extra_hints / abort_reply | — |
| 7 | `before_turn.return` | collect_exports | — | 产出 BeforeTurnCtx | — |

### 3.2 before_reasoning — `BeforeReasoningFrame[BeforeReasoningInput, BeforeReasoningCtx]`

`default_before_reasoning_modules(bus, tools, session_manager, context, plugin_modules=None)`

| 顺序 | slot | requires | produces | 职责 | 依赖 |
|---|---|---|---|---|---|
| 1 | `before_reasoning.sync_tools` | — | — | `tools.set_context(...)` | ToolRegistry |
| 2 | `before_reasoning.build_ctx` | sync_tools | `reasoning:ctx` | 构造 BeforeReasoningCtx | — |
| 3 | `before_reasoning.emit` | build_ctx | `reasoning:ctx` | `bus.emit(ctx)` | EventBus |
| 4 | `before_reasoning.collect_exports` | emit | `reasoning:ctx` | 收 hints / abort | — |
| 5 | `before_reasoning.warmup` | collect_exports | — | 预热 context render | ContextBuilder |
| 6 | `before_reasoning.return` | warmup | — | 产出 BeforeReasoningCtx | — |

### 3.3 prompt_render — `PromptRenderFrame[PromptRenderInput, PromptRenderResult]`

`default_prompt_render_modules(bus, context, plugin_modules=None)`

| 顺序 | slot | requires | produces | 职责 | 依赖 |
|---|---|---|---|---|---|
| 1 | `prompt_render.build_ctx` | — | `prompt:ctx` | 构造 PromptRenderCtx | — |
| 2 | `prompt_render.emit` | build_ctx | `prompt:ctx` | `bus.emit(ctx)` | EventBus |
| 3 | `prompt_render.collect_exports` | emit | `prompt:ctx` | 收 section_top/bottom + hints | — |
| 4 | `prompt_render.render` | collect_exports | `prompt:result` | `context.render(...)` | ContextBuilder |
| 5 | `prompt_render.return` | render | — | 产出 PromptRenderResult | — |

### 3.4 before_step — `BeforeStepFrame[BeforeStepInput, BeforeStepCtx]`

`default_before_step_modules(bus, plugin_modules=None)`

| 顺序 | slot | requires | produces | 职责 | 依赖 |
|---|---|---|---|---|---|
| 1 | `before_step.build_ctx` | — | `step:ctx` | 构造 BeforeStepCtx（含 token 估算） | passive_support |
| 2 | `before_step.emit` | build_ctx | `step:ctx` | `bus.emit(ctx)` | EventBus |
| 3 | `before_step.collect_exports` | emit | `step:ctx` | 收 hints / early_stop | — |
| 4 | `before_step.inject_hints` | collect_exports | — | 把 hints 追加进 messages | — |
| 5 | `before_step.return` | inject_hints | — | 产出 BeforeStepCtx | — |

### 3.5 after_step — `AfterStepFrame[AfterStepCtx, AfterStepCtx]`

`default_after_step_modules(bus, plugin_modules=None)`

| 顺序 | slot | requires | produces | 职责 | 依赖 |
|---|---|---|---|---|---|
| 1 | `after_step.copy_input` | — | `step:ctx` | 复制输入到 ctx | — |
| 2 | `after_step.collect_pre`（实例1） | copy_input | `step:ctx` | fanout 前收 telemetry | — |
| 3 | `after_step.fanout` | collect_pre | — | `bus.fanout(ctx)` | EventBus |
| 4 | `after_step.collect_post`（实例2） | fanout | `step:ctx` | fanout 后补 telemetry / early_stop | — |
| 5 | `after_step.return` | collect_post | — | 产出 AfterStepCtx | — |

> 注意：`_CollectAfterStepExportSlotsModule` 同一类实例化两次（slot/requires 由实例注入），这是原版的既有写法，平移时保留。

### 3.6 after_reasoning — `AfterReasoningFrame[AfterReasoningInput, TurnSnapshot]`

`default_after_reasoning_modules(bus, session_services, plugin_modules=None)`

| 顺序 | slot | requires | produces | 职责 | 依赖 |
|---|---|---|---|---|---|
| 1 | `after_reasoning.build_ctx` | — | `reasoning:ctx` | parse 回复 + 构造 AfterReasoningCtx | response_parser |
| 2 | `after_reasoning.emit` | build_ctx | `reasoning:ctx` | `bus.emit(ctx)` | EventBus |
| 3 | `after_reasoning.persist_user` | emit | `reasoning:persisted_user` | 存 user 消息 | SessionServices |
| 4 | `after_reasoning.persist_asst` | persist_user | `reasoning:persisted_assistant` | 存 assistant 消息 | Session |
| 5 | `after_reasoning.update_meta` | persist_asst | — | 更新 session 运行时元数据 | passive_support |
| 6 | `after_reasoning.append_messages` | update_meta | — | 追加消息 | SessionManager |
| 7 | `after_reasoning.build_outbound` | append_messages | `reasoning:outbound` | 构造 OutboundMessage | — |
| 8 | `after_reasoning.return` | build_outbound | — | 产出 TurnSnapshot | — |

### 3.7 after_turn — `AfterTurnFrame[TurnSnapshot, OutboundMessage]`

`default_after_turn_modules(bus, outbound, context, plugin_modules=None)`

| 顺序 | slot | requires | produces | 职责 | 依赖 |
|---|---|---|---|---|---|
| 1 | `after_turn.build_work` | — | 5 个 slot | 组 budget / react_stats / persistence | ContextBuilder |
| 2 | `after_turn.collect_extras` | build_work | `turn:extra` | 收 extra | — |
| 3 | `after_turn.build_committed` | collect_extras | `turn:committed` | 构造 TurnCommitted | — |
| 4 | `after_turn.fanout_committed` | build_committed | — | `bus.fanout(TurnCommitted)` | EventBus |
| 5 | `after_turn.log_budget` | build_work | — | 打日志 | passive_support |
| 6 | `after_turn.build_ctx` | fanout_committed | `turn:ctx` | 构造 AfterTurnCtx | — |
| 7 | `after_turn.collect_telemetry` | build_ctx | `turn:ctx` | 收 telemetry | — |
| 8 | `after_turn.fanout_ctx` | collect_telemetry | — | `bus.fanout(AfterTurnCtx)` | EventBus |
| 9 | `after_turn.dispatch` | fanout_ctx | — | `outbound.dispatch(...)` | OutboundPort |
| 10 | `after_turn.return` | dispatch | — | 产出 OutboundMessage | — |

---

## 4. 依赖留桩清单

分两类：**真实现（本次 port / 已落地）** 与 **留桩（最小接口）**。

### 4.1 真实现（无需留桩）

| 模块 | 状态 | 说明 |
|---|---|---|
| `bus.event_bus` / `bus.events` / `bus.events_lifecycle` | ✅ 已落地（M0） | `EventBus.emit/fanout/observe`、`TurnCommitted` 等都在 |
| `agent.lifecycle.phase` | ✅ 已落地（M1a） | `Phase` / `PhaseFrame` / `PhaseModule` / `topo_sort_modules` |
| `agent.lifecycle.types` | ✅ 已落地（M1a） | 7+3 全部 Ctx + 所有 Input/Result/Snapshot |
| `agent.lifecycle.facade` | ✅ 已落地（M1a） | `TurnLifecycle` 7 个 `on_*` |
| `agent.plugins.*` / `agent.tools.base` | ✅ 已落地（M0） | `Plugin` / `Tool` 基类 |
| `agent.tools.registry` | 🔨 本次 port | 原版 `ToolRegistry`（已读原文，逐行对齐） |

### 4.2 留桩（最小接口，M3/M4 再补真实实现）

| 桩模块 | 需要提供的最小接口 |
|---|---|
| `session.manager` | `Session`（`key` / `metadata` / `add_message(role, content, **kw) -> dict` / `history_units()`）；`SessionManager`（`get_existing` / `get_or_create` / `append_messages`）——**内存字典实现，不落 DB** |
| `session.memory_policy` | `excludes_memory(session_key, metadata) -> bool`（恒 `False`） |
| `agent.context` | `ContextRequest`（dataclass）、`ContextBundle`、`ContextBuilder.render(...) -> RenderedContext(.messages)`——`render` 直接拼接 system/user 消息，不调 LLM |
| `agent.core.response_parser` | `parse_response(raw_reply, tool_chain=...) -> Parsed(.clean_text/.metadata.raw_text)` |
| `agent.core.runtime_support` | `SessionLike`（Protocol）、`TurnRunResult`（dataclass：reply/tools_used/tool_chain/media/thinking/streamed/context_retry/model_state/mobile_attention） |
| `agent.core.passive_support` | `build_context_hint_message` / `estimate_messages_tokens` / `predict_current_user_source_ref` / `update_session_runtime_metadata` / `build_post_reply_context_budget` / `extract_react_stats` / `log_*`（能返回合理默认值即可） |
| `agent.core.types` | `ContextRequest`（或并入 context）、`to_tool_call_groups(tool_chain)` |
| `agent.turns.outbound` | `OutboundDispatch`（dataclass）、`OutboundPort`（Protocol：`dispatch(...)` → 记录到内存队列） |
| `agent.control.context` | `running_turn_id`（ContextVar[str]） |
| `agent.control.ports` | `InputLock` / `TurnUserInput`（control 相关，本次仅需类型存在） |
| `agent.looping.ports` | `SessionServices`（`session_manager` / `presence`） |
| `core.common.diagnostic_log` | `turn_milestone(logger, event, ...)`（no-op + 结构化日志） |
| `core.error_context` | `current_client_message_id` / `current_session_key`（ContextVar） |

> 桩的唯一目的：满足 phase 模块链的**类型契约**，让 `m2_verify.py` 能证明「7 阶段按固定顺序跑通 + 每阶段 emit/fanout 正确 + abort/early_stop 短路正确」。

---

## 5. turn pipeline driver（缩减版 PassiveTurnPipeline）

新文件 `backend/agent/core/turn_pipeline.py`，只保留 5 顺序点 + abort/异常兜底，砍掉诊断日志、control replay、context-retry 等重逻辑：

```python
class TurnPipeline:
    def __init__(self, *, bus, session_manager, context_store, tools,
                 context, outbound, session_services):
        # 惰性构建 5 个 Phase（对应原版 _runtime_phases()）
        ...

    async def run(self, msg: InboundMessage, key: str, *, dispatch_outbound=True) -> OutboundMessage:
        state = TurnState(msg=msg, session_key=key, dispatch_outbound=dispatch_outbound)

        # Phase 1
        before_turn = await self._before_turn.run(state)
        state.extra_metadata.update(before_turn.extra_metadata)
        if before_turn.abort:
            return await self._control_outbound(state, OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=before_turn.abort_reply,
                turn_disposition=TurnDisposition.SHORT_CIRCUITED))

        # Phase 2
        before_reasoning = await self._before_reasoning.run(
            BeforeReasoningInput(state=state, before_turn=before_turn))
        if before_reasoning.abort:
            return await self._control_outbound(...)

        # Phase 3-4（reasoner 内部：prompt_render + before_step/after_step 循环）
        turn_result = await self._reasoner.run_turn(
            msg=msg, skill_names=..., session=state.session,
            retrieved_memory_block=..., extra_hints=...)

        # Phase 5
        after_reasoning = await self._after_reasoning.run(
            AfterReasoningInput(state=state, turn_result=turn_result))

        # Phase 6
        outbound = await self._after_turn.run(after_reasoning)
        return outbound
```

---

## 6. Reasoner（改造自 ReactRunner）

新文件 `backend/agent/core/reasoner.py`。核心变化：把 `ReactRunner` 的 `while` 循环体，拆成「一次 prompt_render + 每次迭代一对 before_step/after_step」：

```python
class Reasoner:
    async def run_turn(self, *, msg, session, skill_names,
                       retrieved_memory_block, extra_hints) -> TurnRunResult:
        # 1. 一次 prompt_render（原版 run_turn 的第 1 步）
        prompt = await self._prompt_render.run(PromptRenderInput(
            session_key=..., channel=msg.channel, chat_id=msg.chat_id,
            content=msg.content, history=[...], skill_names=skill_names,
            retrieved_memory_block=..., ...))
        messages = prompt.messages

        # 2. 循环（对应原版 ReactRunner 的 iteration loop）
        for iteration in range(self._max_iterations):
            before_step = await self._before_step.run(BeforeStepInput(
                session_key=..., iteration=iteration, messages=messages, ...))
            if before_step.early_stop:
                break  # 或返回 early_stop_reply

            response = await self._llm(messages)          # 桩：返回固定/可注入响应
            if response.has_tool_calls:
                # 执行工具（本次用桩工具，M3 接真实 tool executor）
                ...
                after_step = await self._after_step.run(AfterStepCtx(...))
                if after_step.early_stop:
                    break
                continue
            return self._to_turn_result(response)          # 终态

        return self._to_turn_result(...)                   # 预算耗尽 force_final
```

**ReactRunner → Reasoner 字段映射**：

| 原 ReactRunner（MVP） | 新 Reasoner | 说明 |
|---|---|---|
| `_MAX_TOOL_RESULT_CHARS` / `_REPEAT_CALL_LIMIT` / `_MAIN_MAX_ITERATIONS` | 保留，改为构造参数 | 迭代预算语义不变 |
| `run()` 里的 `while` 循环体 | `run_turn()` 的 for 循环 | 包进 `before_step/after_step` |
| `llm_client` 调用 | `self._llm`（本次留桩，可注入） | 验证脚本注入固定响应 |
| `tool.execute(...)` | 工具执行 + `bus.emit(BeforeToolCallCtx/AfterToolResultCtx)` | 3 个 tool 事件在 M1b 一并接线 |
| — | 新增 `prompt_render` / `before_step` / `after_step` 三个 Phase | 这是本次「改架构」的实质 |

---

## 7. 数据流（7 阶段之间如何传递）

```
InboundMessage
   │
   ▼ before_turn(TurnState) ──────────────► BeforeTurnCtx（含 skill_names/retrieved_memory/hints/abort）
   │                                              │
   ▼ before_reasoning(BeforeReasoningInput) ◄─────┘（打包 state + before_turn）
   │        └─► BeforeReasoningCtx（继承 before_turn 的可写字段）
   │                                              │
   ▼ reasoner.run_turn(msg, skill_names, retrieved_memory, hints)
   │        ├─ prompt_render(PromptRenderInput) ─► messages
   │        └─ for 循环: before_step ─► after_step（每步）  ─► TurnRunResult
   │                                              │
   ▼ after_reasoning(AfterReasoningInput) ◄──────┘（state + turn_result）
   │        └─► TurnSnapshot(state, outbound, ctx)   ← 关键：outbound 在此时定型
   │                                              │
   ▼ after_turn(TurnSnapshot) ────────────────► OutboundMessage（fanout TurnCommitted → dispatch → 返回）
```

**关键不变量**：`before_*` 是 GATE（可改写 ctx 影响后续），`after_*` 是 TAP（观察快照）。abort 只发生在 `before_turn`/`before_reasoning`（整 turn 短路），early_stop 只发生在 `before_step`/`after_step`（只终止当前 tool loop）。

---

## 8. 落地文件清单

### 8.1 新增（phase 实现 + core）

```
backend/agent/lifecycle/phases/__init__.py      # re-export 7 frame + 7 default_*_modules
backend/agent/lifecycle/phases/before_turn.py
backend/agent/lifecycle/phases/before_reasoning.py
backend/agent/lifecycle/phases/prompt_render.py
backend/agent/lifecycle/phases/before_step.py
backend/agent/lifecycle/phases/after_step.py
backend/agent/lifecycle/phases/after_reasoning.py
backend/agent/lifecycle/phases/after_turn.py
backend/agent/core/__init__.py
backend/agent/core/turn_pipeline.py             # 缩减版 PassiveTurnPipeline
backend/agent/core/reasoner.py                  # 改造自 ReactRunner
backend/agent/tools/registry.py                 # 原版 ToolRegistry（逐行对齐）
```

### 8.2 新增（留桩）

```
backend/session/__init__.py
backend/session/manager.py                      # 内存 Session / SessionManager
backend/session/memory_policy.py                # excludes_memory 恒 False
backend/agent/context.py                        # ContextRequest / ContextBundle / ContextBuilder
backend/agent/core/response_parser.py           # parse_response
backend/agent/core/runtime_support.py           # SessionLike / TurnRunResult
backend/agent/core/passive_support.py           # helper 函数
backend/agent/core/types.py                     # to_tool_call_groups 等
backend/agent/turns/__init__.py
backend/agent/turns/outbound.py                 # OutboundPort / OutboundDispatch
backend/agent/control/__init__.py
backend/agent/control/context.py                # running_turn_id
backend/agent/control/ports.py                  # InputLock / TurnUserInput
backend/agent/looping/__init__.py
backend/agent/looping/ports.py                  # SessionServices
backend/core/__init__.py
backend/core/common/__init__.py
backend/core/common/diagnostic_log.py           # turn_milestone
backend/core/error_context.py                   # current_* ContextVar
```

### 8.3 修改

```
backend/agent_orchestrator.py                   # AgentOrchestrator 改为调 TurnPipeline；ReactRunner 逻辑移入 Reasoner
backend/m2_verify.py                            # 新增验证脚本
```

### 8.4 更新

```
specs/002-runtime-migration-map.md              # 里程碑表：M1b 打勾
.workbuddy/memory/2026-09-08.md                 # 追加 M1b 记录
```

---

## 9. 验证脚本设计（m2_verify.py）

目标：**证明 7 阶段顺序正确 + emit/fanout 正确 + 短路正确**，不依赖真实 LLM/DB。

| # | 检查项 | 断言 |
|---|---|---|
| 1 | 7 phase 均可构建 | `default_*_modules(...)` 返回非空，topo 排序无循环异常 |
| 2 | 全链路顺序 | 注入探针插件，记录每个 Ctx 出现顺序 = `[before_turn, before_reasoning, prompt_render, before_step, after_step, after_reasoning, after_turn]` |
| 3 | before_step/after_step 循环 | 工具调用 N 次 → 这对阶段各出现 N 次 |
| 4 | GATE 可改写 | `on_before_turn` 改 `skill_names` → `before_reasoning.skill_names` 可见 |
| 5 | abort 短路 | `on_before_turn` 设 `abort=True` → 直接返回、不触发 reasoning |
| 6 | early_stop 短路 | `on_before_step` 设 `early_stop=True` → 只终止 tool loop、仍走 after_reasoning/after_turn |
| 7 | TurnCommitted fanout | after_turn 里 `TurnCommitted` 被 fanout 一次 |
| 8 | 出站 dispatch | `OutboundPort.dispatch` 被调用，`content` = reply |
| 9 | 回归 M0 / M1a | 复用 `m0_verify.py`（18 项）+ `m1_verify.py`（11 项），全绿 |

> 探针插件通过 `TurnLifecycle` 注册到同一个 `EventBus`，用 `handler` 内 `record.append(ctx_type)` 采集顺序——这是对「顺序来自 pipeline 而非 EventBus」的正面验证。

---

## 10. 风险与边界

1. **桩的保真度**：桩 `ContextBuilder.render` 不产真实 prompt，但结构上 `messages` 必须走完整 7 阶段，否则 M2 接入真实 context 时会返工。约定：桩字段名/类型与 `agent.lifecycle.types` 严格一致。
2. **`after_step` 双实例模块**：`topo_sort_modules` 靠 slot 去重，同一类两个 slot（`collect_pre`/`collect_post`）必须实例注入，平移时不得合并。
3. **`TurnDisposition`**：`_control_outbound` 用到，需确认 `bus.events` 已有（M0 已落地，若缺则补）。
4. **暂不接真实 LLM / 工具执行**：Reasoner 的 `_llm` 与工具执行本次留可注入桩，验证脚本注入固定响应。

---

## 11. 待你确认的三处范围

1. **留桩清单（4.2 节 14 个模块）**是否照此切边界？还是你想把某个（如 `session.manager`）这次就做真？
2. **Reasoner 的 LLM 调用**：本次留「可注入桩」，还是顺手接 MVP 已有的 `llm_client.py`？
3. **`agent/tools/registry.py`**：是逐行对齐原版（含 tool_search 相关），还是只 port 本次 phase 需要的 `set_context`/`get_source_tool_names` 两个方法？
