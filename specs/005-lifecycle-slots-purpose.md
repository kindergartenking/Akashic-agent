# 005 — 七阶段 slot 逐项作用梳理

> 定位：`004-lifecycle-slots-reference.md` 是**横向规范**（命名空间怎么分、两类 slot 怎么辨、GATE/TAP 怎么定、有哪些约定）；
> 本文是**纵向手册**——逐个 phase、逐个 slot 讲清「**它为什么存在、解决什么问题、没有它会怎样**」。
>
> 配套：`003-m1b-lifecycle-phases.md` §3（模块链顺序与依赖）｜代码：`backend/agent/lifecycle/phases/*.py`

---

## 0. 先记住这个骨架：七个 phase 高度同构

七个 phase 里，有五个是同一个骨架的变体：

```
build_ctx ──► emit ──► collect_exports ──► [业务模块...] ──► return
  造 ctx      GATE     回收插件外溢          真正干活        提 output
```

对照全部七个 phase：

| phase | 造 ctx 的模块 | 总线动作 | 回收模块 | 业务模块 | 提 output 的模块 |
|---|---|---|---|---|---|
| before_turn | `build_ctx` | `emit` | `collect_exports` | — | `return` |
| before_reasoning | `build_ctx` | `emit` | `collect_exports` | `sync_tools`(前) / `warmup`(后) | `return` |
| prompt_render | `build_ctx` | `emit` | `collect_exports` | `render` | `return` |
| before_step | `build_ctx` | `emit` | `collect_exports` | `inject_hints` | `return` |
| after_step | `copy_input` ⚠️ | `fanout` | `collect_pre` + `collect_post` ⚠️ | — | `return` |
| after_reasoning | `build_ctx` | `emit` | — ⚠️（无独立 collect，靠各业务模块自己收） | `persist_*` / `append_messages` / `build_outbound` | `return` |
| after_turn | `build_ctx`（第二个循环） | `fanout` ×2 ⚠️ | `collect_extras` / `collect_telemetry` | `build_work` / `build_committed` / `dispatch` | `return` |

看懂这张表，剩下每个 phase 的差异就只是「业务模块换了什么」和「槽位叫什么名字」。

**每节的组织**：阶段职责 → 输入/输出 → 模块 slot 表（谁先谁后、每个模块在干嘛）→ 数据 slot 表（每个槽为什么存在）。

---

## 1. `before_turn` — 命名空间 `session:`

### 阶段职责
把一条裸消息变成「一个有会话、有历史、有召回记忆的 `BeforeTurnCtx`」。这是整个 turn 的入口，也是**唯一有权决定「本轮会话从哪来」**的阶段。

### 输入 / 输出
`TurnState`（msg / session_key / dispatch_outbound）→ `BeforeTurnCtx`

### 模块 slot

| 模块 slot | 类 | 作用 |
|---|---|---|
| `before_turn.acquire_session` | `_AcquireSessionModule` | 全链起点（`requires=()`）。按 `msg.metadata["require_existing_session"]` 决定 `get_existing` 还是 `get_or_create`，结果同时挂到 `state.session`（给 pipeline 层）和 slot（给模块链） |
| `before_turn.memory_exclusion` | `_ApplyMemoryExclusionModule` | 在线判定本会话是否排除记忆。命中 `excludes_memory()` 就往 `msg.metadata` 写 `skip_post_memory` / `disable_memory_writes`。**用赋值不用 setdefault**——保证 excluded session 不被 turn 级设置覆盖 |
| `before_turn.prepare_context` | `_PrepareContextModule` | 调 `context_store.prepare(...)` 做检索：历史消息 + 长期记忆召回。带 `if "session:ctx" in frame.slots: return` 守卫（允许插件预填 ctx 短路） |
| `before_turn.build_ctx` | `_BuildBeforeTurnCtxModule` | 把 context bundle 摊平成 `BeforeTurnCtx` 的字段。**本阶段唯一构造 ctx 的点**，同样带 ctx 已存在则跳过的守卫 |
| `before_turn.emit` | `_EmitBeforeTurnCtxModule` | GATE 点。`bus.emit(ctx)` 让插件改写，把返回的新 ctx 写回 slot |
| `before_turn.collect_exports` | `_CollectBeforeTurnExportSlotsModule` | 回收插件外溢：`session:extra_hint:*` → `ctx.extra_hints`；`session:abort_reply` → `ctx.abort` |
| `before_turn.return` | `_ReturnBeforeTurnCtxModule` | 把 slot 里的 ctx 提成 `frame.output`。**缺了它 `Phase.run` 会抛「Phase 模块链未产生 output」** |

### 数据 slot

| 数据 slot | 类 | 谁写 → 谁读 | 作用（为什么需要它） |
|---|---|---|---|
| `session:session` | 中间 | `acquire_session` → `memory_exclusion` / `prepare_context` | 承载「本轮会话对象」。**为什么不直接用 `state.session`？** 因为 `state.session` 是给 pipeline 层读的，而这是模块链的显式信道——两者同时存在，后者让「插件插在 acquire 和 prepare 之间」也能拿到会话 |
| `session:context_bundle` | 中间 | `prepare_context` → `build_ctx` | 承载 `ContextStore.prepare()` 的**原始产物**。刻意不直接把检索结果塞进 ctx，而是先落槽，这样插件就能插在 prepare 和 build 之间改写检索结果 |
| `session:ctx` | **ctx** | `build_ctx` → `emit` / `collect_exports` / `return` | `BeforeTurnCtx` 本体。全程走 GATE 链，插件可通过 `emit` 直接替换 |
| `session:extra_hint:*` | export | 插件 → `collect_exports` | 插件追加提示词的唯一通道，收集后进 `ctx.extra_hints` |
| `session:abort_reply` | control | 插件 → `collect_exports` | 写非空字符串即 `ctx.abort=True`，**短路整个 turn** 并直接以该串回复 |

---

## 2. `before_reasoning` — 命名空间 `reasoning:`

### 阶段职责
reasoning 的「进」：把工具层需要的身份信息同步好，并基于 `before_turn` 的产物构造 `BeforeReasoningCtx`。

### 输入 / 输出
`BeforeReasoningInput(state, before_turn)` → `BeforeReasoningCtx`

### 模块 slot

| 模块 slot | 类 | 作用 |
|---|---|---|
| `before_reasoning.sync_tools` | `_SyncToolContextModule` | 把 channel / chat_id / session_key / turn_id / timestamp / current_user_source_ref 推给 `ToolRegistry.set_context()`。工具执行时要读这份上下文，所以**必须在 reasoning 之前**同步。**全表唯一的零 slot 模块**（纯副作用） |
| `before_reasoning.build_ctx` | `_BuildBeforeReasoningCtxModule` | 从 `frame.input.before_turn` **复制**字段构造 ctx。注意它是**继承**上一阶段的成果（含插件已加过的 `extra_hints` / `skill_names`）——这就是「阶段间靠 ctx 对象传递」的体现 |
| `before_reasoning.emit` | `_EmitBeforeReasoningCtxModule` | GATE 点 |
| `before_reasoning.collect_exports` | `_CollectBeforeReasoningExportSlotsModule` | 收 hints / abort。源码注释点明：插件也可以在 `before_emit` 阶段**直接改 `ctx.abort`**，`after_emit` 才用 slot export |
| `before_reasoning.warmup` | `_PromptWarmupModule` | 用空 history、空 content **试跑一次** `context.render()`，把 ContextBuilder 的模板缓存预热。带 `if ctx.abort: return` 守卫，abort 后不做无用功 |
| `before_reasoning.return` | `_ReturnBeforeReasoningCtxModule` | 提 output（`requires` 指向 `warmup`，故顺序是 collect_exports → warmup → return） |

### 数据 slot

| 数据 slot | 类 | 谁写 → 谁读 | 作用 |
|---|---|---|---|
| `reasoning:ctx` | **ctx** | `build_ctx` → `emit` / `collect_exports` / `warmup` / `return` | `BeforeReasoningCtx` 本体。名字与 `after_reasoning` 的 `_CTX_SLOT` 相同，但作用域是各自的 frame，不冲突 |
| `reasoning:extra_hint:*` | export | 插件 → `collect_exports` | 追加提示，进 `ctx.extra_hints` |
| `reasoning:abort_reply` | control | 插件 → `collect_exports` | 非空 → `ctx.abort=True`，**短路整个 turn** |

---

## 3. `prompt_render` — 命名空间 `prompt:`

### 阶段职责
渲染出**最终要发给 LLM 的 messages 列表**。每个 turn 只跑一次（在 tool loop 之外）。

### 输入 / 输出
`PromptRenderInput`（history / media / disabled_sections / turn_injection_prompt …）→ `PromptRenderResult(messages=[...])`

### 模块 slot

| 模块 slot | 类 | 作用 |
|---|---|---|
| `prompt_render.build_ctx` | `_BuildPromptRenderCtxModule` | 构造 `PromptRenderCtx`。`disabled_sections` 支持**按需关掉某些提示词段落** |
| `prompt_render.emit` | `_EmitPromptRenderCtxModule` | GATE 点 |
| `prompt_render.collect_exports` | `_CollectPromptExportSlotsModule` | **一次收三段**：`section_top:` → `system_sections_top`；`section_bottom:` → `system_sections_bottom`；`extra_hint:` → `extra_hints` |
| `prompt_render.render` | `_RenderPromptModule` | 调 `context.render(ContextRequest(...), system_sections_top=..., system_sections_bottom=...)`；若 `extra_hints` 非空，再追加一条 `plugin_hints` user 消息 |
| `prompt_render.return` | `_ReturnPromptRenderResultModule` | 从 `prompt:result` 提 output（**注意：`requires` 指向结果槽而不是 ctx**） |

### 数据 slot

| 数据 slot | 类 | 谁写 → 谁读 | 作用 |
|---|---|---|---|
| `prompt:ctx` | **ctx** | `build_ctx` → `emit` / `collect_exports` / `render` | `PromptRenderCtx` 本体 |
| `prompt:result` | **结果** | `render` → `return` | `PromptRenderResult`。**为什么单独开一个槽而不直接 `frame.output=`？** 让「渲染」和「产出」解耦——插件若插在 render 之后、return 之前，还能改写最终 messages |
| `prompt:section_top:*` | export | 插件 → `collect_exports` | 插件片段插到系统提示词**顶部**（`PromptSectionRender` 或纯字符串，后者会被包成 `is_static=False` 的 section） |
| `prompt:section_bottom:*` | export | 插件 → `collect_exports` | 同上，插到底部 |
| `prompt:extra_hint:*` | export | 插件 → `collect_exports` | 作为**独立的 `plugin_hints` 消息**追加（不进 system prompt，而是当作 user 侧上下文提示） |

---

## 4. `before_step` — 命名空间 `step:`

### 阶段职责
tool loop 中**每一轮**开始前：算 token 预算、冻结可见工具名、构造 `BeforeStepCtx`、把插件 hints 注入 messages。

### 输入 / 输出
`BeforeStepInput(iteration, messages, visible_names)` → `BeforeStepCtx`

### 模块 slot

| 模块 slot | 类 | 作用 |
|---|---|---|
| `before_step.build_ctx` | `_BuildBeforeStepCtxModule` | 算 `input_tokens_estimate`（`estimate_messages_tokens`），把 `visible_names` 冻成 `frozenset` 防篡改 |
| `before_step.emit` | `_EmitBeforeStepCtxModule` | GATE 点。**每轮循环都会跑一次**，是插件「按轮次介入」的唯一机会 |
| `before_step.collect_exports` | `_CollectBeforeStepExportSlotsModule` | 收 hints / abort_reply。这里把 `abort_reply` **重映射为 `early_stop`** |
| `before_step.inject_hints` | `_InjectHintsModule` | 把 `ctx.extra_hints` 拼成一条 `plugin_hints` 消息 **直接 append 到 `frame.input.messages`**。⚠️ 注意它改的是 **input**（不是 ctx）——因为 `messages` 是可变的 list，直接改最省事，也是全表唯一这么做的模块 |
| `before_step.return` | `_ReturnBeforeStepCtxModule` | 提 output |

### 数据 slot

| 数据 slot | 类 | 谁写 → 谁读 | 作用 |
|---|---|---|---|
| `step:ctx` | **ctx** | `build_ctx` → `emit` / `collect_exports` / `inject_hints` | `BeforeStepCtx` 本体。与 `after_step` 的 `_CTX_SLOT` 同名，作用域各自独立 |
| `step:extra_hint:*` | export | 插件 → `collect_exports` | 追加提示，进 `ctx.extra_hints`，随后被 `inject_hints` 追加进 messages |
| `step:abort_reply` | control | 插件 → `collect_exports` | 非空 → `ctx.early_stop=True`。**只终止当前 tool loop，不终止整个 turn** |

> ⚠️ **同名不同语义**：`step:abort_reply` 与 `session:abort_reply` / `reasoning:abort_reply` 名字一样，但前者的落点是 `early_stop`（停本轮循环，`reasoner.py:134` 判定），后两者是 `abort`（停整个 turn，`turn_pipeline.py:100,115` 判定）。

---

## 5. `after_step` — 命名空间 `step:`（与 4 共用）

### 阶段职责
tool loop 中每一轮结束后：把快照 **fanout 给观察者**，并回收观察者补充的遥测。

### 输入 / 输出
`AfterStepCtx` → `AfterStepCtx`（**输入输出同型**）

### 模块 slot

| 模块 slot | 类 | 作用 |
|---|---|---|
| `after_step.copy_input` | `_CopyInputToCtxModule` | 把 `frame.input` 放进 slot。**为什么需要这一步？** 因为 input 已经是 `AfterStepCtx` 了（输入输出同型），需要一个适配动作让后续模块能统一从 `frame.slots` 读 |
| `after_step.collect_pre` | `_CollectAfterStepExportSlotsModule`（实例 1） | fanout **前**收 telemetry。目的是让观察者一进 handler 就能读到插件注入的元数据 |
| `after_step.fanout` | `_FanoutAfterStepCtxModule` | **TAP 点**。`bus.fanout(ctx)`——只广播，**返回值被丢弃**，观察者无法改 ctx |
| `after_step.collect_post` | `_CollectAfterStepExportSlotsModule`（实例 2） | fanout **后**再收一次，把 `after_fanout` 阶段的**补充**带回返回 ctx；靠 `step:telemetry_collected` 去重，**不覆盖** handler 已看到的同名值 |
| `after_step.return` | `_ReturnAfterStepCtxModule` | 从 `step:ctx` 提 output |

### 数据 slot

| 数据 slot | 类 | 谁写 → 谁读 | 作用 |
|---|---|---|---|
| `step:ctx` | **ctx** | `copy_input` → `collect_pre` / `fanout` / `collect_post` / `return` | `AfterStepCtx`（frozen）。走 TAP 链，只能读；要补数据靠 `replace()` 造新实例 |
| `step:telemetry:*` | export | 插件 → `collect_pre` / `collect_post` | 收进 `ctx.extra_metadata` |
| `step:telemetry_collected` | 内部 | 模块自写自读 | **去重哨兵**。没有它，`collect_post` 就会把 `collect_pre` 已合并的值再覆盖一遍，抹掉观察者在 fanout 期间的写入 |
| `step:early_stop_reason` | control | 插件 → `collect_post` | 非空 → `ctx.early_stop=True` + `early_stop_reason`（通过 `dataclasses.replace()` 造新实例，因为 ctx 是 frozen） |

> `after_step` 是唯一**同一个类实例化两次**的 phase（`collect_pre` / `collect_post`），也是 `slot`/`requires` 由 `__init__` 注入而非类属性的地方（`after_step.py:57`）。

---

## 6. `after_reasoning` — 命名空间 `reasoning:` + `persist:` + `outbound:`

### 阶段职责
reasoning 的「出」：解析原始回复 → **持久化消息** → 构造出站 `OutboundMessage`。

### 输入 / 输出
`AfterReasoningInput(state, turn_result)` → `TurnSnapshot(state, outbound, ctx)`

### 模块 slot

| 模块 slot | 类 | 作用 |
|---|---|---|
| `after_reasoning.build_ctx` | `_BuildAfterReasoningCtxModule` | `parse_response(raw_reply, tool_chain)` 抽出 `clean_text` + metadata；回复为 `None` 时用兜底串。合并 inbound metadata 前**先 pop 掉 5 个 `_control_*` 内部键**，防止内部状态泄漏到出站 |
| `after_reasoning.emit` | `_EmitAfterReasoningCtxModule` | GATE 点。插件可在此改写 `reply` / `media` / `outbound_metadata` |
| `after_reasoning.persist_user` | `_PersistUserMessageModule` | 构造 user 消息 dict（**只造不提交**）。支持 control turn 的多输入（读 `InputLock.used_inputs()`）。受 `state.persistence.persist_user` 开关控制 |
| `after_reasoning.persist_asst` | `_PersistAssistantMessageModule` | 构造 assistant 消息 dict（**只造不提交**），附 tools_used / tool_chain / reasoning_content / model_state / turn_duration_ms |
| `after_reasoning.update_meta` | `_UpdateSessionMetadataModule` | 把 tools_used / tool_chain 写进 session 运行时元数据（`update_session_runtime_metadata`） |
| `after_reasoning.append_messages` | `_AppendMessagesModule` | **全链唯一的提交点**：`session_manager.append_messages(session, messages)`。带 milestone 日志（start / done / error / cancelled），失败会抛而不是吞 |
| `after_reasoning.build_outbound` | `_BuildOutboundMessageModule` | 组装 `OutboundMessage`：合并 `outbound:metadata:*`、回填 persisted message ids、追加 `outbound:media:*` |
| `after_reasoning.return` | `_BuildTurnSnapshotModule` | 组装 `TurnSnapshot`（把 state / outbound / ctx 三者打包，**同时需要 `reasoning:ctx` 和 `reasoning:outbound` 两个槽**） |

### 数据 slot

| 数据 slot | 类 | 谁写 → 谁读 | 作用 |
|---|---|---|---|
| `reasoning:ctx` | **ctx** | `build_ctx` → `emit` 及后续全部模块 | `AfterReasoningCtx`。**插件改 reply/media 的唯一入口** |
| `reasoning:persisted_user` | 中间 | `persist_user` → `append_messages` / `build_outbound` | 本轮 user 消息 dict 列表。**为什么不直接提交？** 让「造消息」和「提交」分离，使提交成为可观测、可失败、可重试的单点 |
| `reasoning:persisted_assistant` | 中间 | `persist_asst` → `append_messages` / `build_outbound` | 同上，assistant 侧。`build_outbound` 还要从它里面取 `id` 作为 `session_message_id` |
| `reasoning:outbound` | **结果** | `build_outbound` → `return` | `OutboundMessage`。**它是本阶段的最终产物，也是 after_turn 阶段 `frame.input.outbound` 的来源** |
| `persist:user:*` | export | 插件 → `persist_user` | 追加 user 消息的额外字段。受 reserved 集保护（`media` / `timestamp` / `client_message_id` / `reply_to_message_id` / `reply_role` / `reply_preview` 不可覆盖） |
| `persist:assistant:*` | export | 插件 → `persist_asst` | 追加 assistant 消息的额外字段。受 reserved 集保护（`tools_used` / `tool_chain` / `reasoning_content` / `model_state`），且 `react_compaction` 已退役，写入直接 `raise` |
| `outbound:metadata:*` | export | 插件 → `build_outbound` | 合并进 `OutboundMessage.metadata` |
| `outbound:media:*` | export | 插件 → `persist_asst` **和** `build_outbound` | 追加出站媒体。**注意它被读两次**——消息持久化和出站构造都要带上媒体 |

> 这是唯一**跨三个命名空间**的 phase。把「改消息字段」放 `persist:`、「改出站字段」放 `outbound:`，是为了让扩展点一眼可辨。

---

## 7. `after_turn` — 命名空间 `turn:`

### 阶段职责
收尾：构造并广播 `TurnCommitted` 事件 → 统计遥测 → 广播 `AfterTurnCtx` → **派发出站消息**。

### 输入 / 输出
`TurnSnapshot` → `OutboundMessage`（**直接从 input 透传，不造新对象**）

### 模块 slot

| 模块 slot | 类 | 作用 |
|---|---|---|
| `after_turn.build_work` | `_BuildTurnWorkModule` | **一次产出 5 个槽**（budget / react_stats / tool_chain / persistence / extra）。是全表单模块产出最多的一个 |
| `after_turn.collect_extras` | `_CollectAfterTurnExtraSlotsModule` | 合并 `turn:extra:*` 进 `turn:extra`，并置 `turn:extra_collected=True` |
| `after_turn.build_committed` | `_BuildTurnCommittedModule` | 构造 `TurnCommitted` 事件（全表最大的一个对象）：含 tool_call_groups、post_reply_budget、react_stats、model_usage、model_binding |
| `after_turn.fanout_committed` | `_FanoutTurnCommittedModule` | **TAP 点 1**：广播 `TurnCommitted` 给事件订阅者。带完整 milestone 日志（start / returned / error / cancelled） |
| `after_turn.log_budget` | `_LogBudgetModule` | 纯日志模块，只读 budget / react_stats 打点，**不写任何 slot** |
| `after_turn.build_ctx` | `_BuildAfterTurnCtxModule` | 构造 `AfterTurnCtx`。含 `will_dispatch` **意图标记**——因为 TAP handler 运行时 dispatch 还没发生，要显式告诉它「即将派发」 |
| `after_turn.collect_telemetry` | `_CollectAfterTurnTelemetrySlotsModule` | 合并 `turn:telemetry:*` 进 `ctx.extra_metadata`（`replace` 造新实例） |
| `after_turn.fanout_ctx` | `_FanoutAfterTurnCtxModule` | **TAP 点 2**：广播 `AfterTurnCtx` 快照 |
| `after_turn.dispatch` | `_DispatchOutboundModule` | **副作用模块**：真正调用 `outbound.dispatch(...)`。受 `snap.state.dispatch_outbound` 开关控制（内部 turn 可只提交不派发） |
| `after_turn.return` | `_ReturnOutboundMessageModule` | `frame.output = frame.input.outbound`——**不造新对象，原样透传**。这是全表唯一的「零加工 return」 |

### 数据 slot

| 数据 slot | 类 | 谁写 → 谁读 | 作用 |
|---|---|---|---|
| `turn:budget` | 中间 | `build_work` → `build_committed` / `log_budget` | 回复后上下文预算统计 |
| `turn:react_stats` | 中间 | `build_work` → `build_committed` / `log_budget` | React 循环统计（含 model_usage 的来源） |
| `turn:tool_chain` | 中间 | `build_work` → `build_committed` | 工具调用链原始数据，供构造 `TurnCommitted` 用 |
| `turn:persistence` | 中间 | `build_work` → `build_committed` | `TurnPersistencePolicy` 快照 |
| `turn:extra` | 中间 | `build_work` → `collect_extras` → `build_committed` | 附加字段袋。由 `build_work` 初始化（含 skip_post_memory / model_binding），再被 `collect_extras` 用 `turn:extra:*` 补充 |
| `turn:extra:*` | export | 插件 → `collect_extras` | 合并进 `turn:extra` → 最终进 `TurnCommitted.extra` |
| `turn:extra_collected` | 内部 | `collect_extras` → （只用于排序） | **顺序哨兵**：让 `build_committed` 的 `requires` 能显式表达「必须等 extra 收完」，避免插件补充的 extra 漏掉 |
| `turn:committed` | **事件** | `build_committed` → `fanout_committed` | `TurnCommitted` 事件载荷。**这个槽的唯一价值就是把对象喂给 fanout** |
| `turn:ctx` | **ctx** | `build_ctx` → `collect_telemetry` / `fanout_ctx` / `dispatch` | `AfterTurnCtx`（frozen，TAP）。`will_dispatch` 字段告诉观察者派发意图 |
| `turn:telemetry:*` | export | 插件 → `collect_telemetry` | 收进 `ctx.extra_metadata` |

> `after_turn` 是全表最复杂的 phase：**两个 fanout**（事件 + 快照）、**有副作用**（dispatch）、**10 个模块**、**有零加工 return**。

---

## 8. 横向速查：每个 phase 的槽位骨架

| phase | ctx slot | 结果/事件 slot | export 前缀 | control slot | 特殊之处 |
|---|---|---|---|---|---|
| before_turn | `session:ctx` | — | `session:extra_hint:` | `session:abort_reply` | 有两个中间槽（session / context_bundle） |
| before_reasoning | `reasoning:ctx` | — | `reasoning:extra_hint:` | `reasoning:abort_reply` | 有零 slot 模块 `sync_tools` |
| prompt_render | `prompt:ctx` | `prompt:result` | `prompt:section_top:` / `section_bottom:` / `extra_hint:` | — | 唯一有 top/bottom 双分段；turn 内只跑一次 |
| before_step | `step:ctx` | — | `step:extra_hint:` | `step:abort_reply`（→ early_stop） | 唯一改 input 而不改 ctx 的模块 `inject_hints` |
| after_step | `step:ctx` | — | `step:telemetry:` | `step:early_stop_reason` | TAP；同类实例化两次；有去重哨兵 |
| after_reasoning | `reasoning:ctx` | `reasoning:outbound` | `persist:user:` / `persist:assistant:` / `outbound:metadata:` / `outbound:media:` | — | 跨 3 个命名空间；唯一提交点 `append_messages` |
| after_turn | `turn:ctx` | `turn:committed` | `turn:extra:` / `turn:telemetry:` | — | 两个 fanout；有副作用 dispatch；return 零加工 |

---

## 9. 三条从「作用」反推出来的设计原则

**① 每个 phase 都有一对「造—收」槽位，是为了给插件留出确定性的介入窗口。**
`build_ctx` 造 slot → `emit` 广播 → `collect_exports` 回收。插件不需要知道模块顺序，只要往固定前缀写 key，就一定会在 `collect` 那一刻被收走。

**② 「造对象」和「用对象」总是分开的。**
`prompt:result` 与 `return` 分开、`reasoning:persisted_*` 与 `append_messages` 分开、`turn:committed` 与 `fanout_committed` 分开——都是为了在两者之间留一个可插入的位置，或者让副作用的时机可控（如持久化提交成为单点）。

**③ 顺序依赖用「哨兵槽」显式化。**
`step:telemetry_collected` 和 `turn:extra_collected` 都是纯排序用途的布尔槽——它们不承载任何业务数据，只是把「A 必须在 B 之前」这条隐含约束**变成代码里可校验的 `requires`**。没有它们，顺序就只能靠「碰巧写在列表前面」来保证。
