# 生命周期架构（Lifecycle Architecture）

> 本文件是 akashic-agent-mine **生命周期架构的全局唯一权威来源**。
> 任何对 7 阶段生命周期（模块链、ctx 类型、命名空间、GATE/TAP 语义、数据流转）的改动，
> 都**必须同步更新本文件**。它取代零散的 spec 文档，作为「当前架构是什么」的唯一真相。

---

## 1. 复刻策略上下文

- **不做全量复刻**：目标是复现生命周期「基本功能」，不是代码等价。
- **逐个 phase 轻量重建**：先搞清每个 phase 的基本功能，再砍掉原版工程化负担后实现。
- **可砍的「重型负担」**（非基本功能，重建时跳过）：control turn 多输入回放（InputLock）、
  mobile channel 特判、milestone 诊断日志、context_retry、meme_tag、retired 字段保护、
  budget/react_stats 统计、model_binding、persist 字段扩展、snapshot 热重载。

---

## 2. 核心概念

### 2.1 三层嵌套结构

7 阶段不是平铺链，而是三层嵌套：

```
turn 层（最外，一次 turn）
  ├─ before_turn
  ├─ reasoning 层（一次推理）
  │    ├─ before_reasoning
  │    ├─ loop 层（reasoner 内部循环）
  │    │    ├─ prompt_render     ← 只跑一次
  │    │    ├─ before_step ─┐
  │    │    └─ after_step  ─┘   ← 循环，直到推理结束
  │    └─ after_reasoning
  └─ after_turn
```

### 2.2 GATE vs TAP（两类 ctx 语义）

| 类型 | 阶段 | ctx 性质 | 插件能做什么 | 底层走 |
|---|---|---|---|---|
| **GATE**（门控） | before_turn / before_reasoning / prompt_render / before_step / after_reasoning | **mutable**，可改写 | 改字段、整体替换、置 abort 短路 | `bus.emit`（返回值替换 ctx） |
| **TAP**（快照） | after_step / after_turn | **frozen**，只读 | 只能观察，不能改 | `bus.fanout` / `observe`（返回值丢弃） |

规律：**所有 before-\* 和 after_reasoning 都是 GATE，只有 after_step / after_turn 是 TAP**。

### 2.3 slot 双义（`phase.py` 的 `_is_module_slot`）

- **模块槽** `phase.module`：含 `.` 不含 `:`，不在 `frame.slots` 里，仅用于拓扑排序 / 依赖定位。
- **数据槽** `namespace:field`：含 `:`，在 `frame.slots` 里，用于跨模块 / 插件通信。

### 2.4 命名空间 = 语义域，不是生产阶段

| 命名空间 | 使用的阶段 | 语义域 |
|---|---|---|
| `session:` | before_turn | 会话 |
| `reasoning:` | before_reasoning + after_reasoning | 推理（一条链共享） |
| `prompt:` | prompt_render | prompt 渲染 |
| `step:` | before_step + after_step | 单步循环 |
| `turn:` | after_turn | turn 收尾 |

### 2.5 跨阶段数据流转

每个阶段**新建 frame + 空 slots**，数据槽不跨阶段存活。跨阶段交接靠**阶段 output**，
塞进下一阶段的 **input**：

```
before_turn.run(state) → BeforeTurnCtx
    ↓ 作为 before_turn 字段
BeforeReasoningInput(state, before_turn) → before_reasoning.run() → BeforeReasoningCtx
    ↓
PromptRenderInput(...) → prompt_render.run() → PromptRenderResult(messages)
    ↓ messages 进循环
before_step / after_step（每轮）
    ↓ 最终回复
AfterReasoningInput(state, turn_result) → after_reasoning.run() → AfterReasoningCtx
    ↓
TurnSnapshot(state, outbound, ctx) → after_turn（TAP 广播）
```

### 2.6 output 职责约定

每个阶段**只有链尾的 `return` 模块设置 `frame.output`**，其余模块一律不碰 output。
数据型模块走 slot 传递，副作用型模块（如 sync_tools）直接改外部对象。

### 2.7 GATE 骨架模板（可复用）

GATE 阶段的后三段 `emit → collect_exports → return` 是**稳定骨架**，各阶段完全同构，
仅命名空间、ctx 类型、export 前缀不同。变的只有前段「准备模块」（每阶段不同）。

---

## 3. 7 阶段总览

| # | 阶段 | 层级 | 类型 | 命名空间 | 基本功能（最小） |
|---|---|---|---|---|---|
| 1 | before_turn | turn | GATE | `session:` | 拿会话 + 打包身份/历史 + 可 abort |
| 2 | before_reasoning | reasoning | GATE | `reasoning:` | 同步工具上下文 + 打包 ctx + 可 abort |
| 3 | prompt_render | loop(一次) | GATE | `prompt:` | 渲染 messages + 插件可改 prompt 片段 |
| 4 | before_step | loop(循环) | GATE | `step:` | 每轮组 ctx + 注入 hints + 可 early_stop |
| 5 | after_step | loop(循环) | TAP | `step:` | 每轮广播快照 |
| 6 | after_reasoning | reasoning | GATE | `reasoning:` | 解析回复 + 持久化 + 组 outbound + 可改 reply |
| 7 | after_turn | turn | TAP | `turn:` | 提交事件 + 派发 outbound |

### 3.1 各阶段 I/O 类型（`types.py`）

| 阶段 | input | output |
|---|---|---|
| before_turn | `TurnState` | `BeforeTurnCtx` |
| before_reasoning | `BeforeReasoningInput`（frozen） | `BeforeReasoningCtx` |
| prompt_render | `PromptRenderInput`（frozen） | `PromptRenderResult`（frozen） |
| before_step | `BeforeStepInput`（frozen） | `BeforeStepCtx` |
| after_step | `AfterStepCtx`（frozen 快照） | `AfterStepCtx`（frozen，补过 telemetry） |
| after_reasoning | `AfterReasoningInput`（frozen） | `TurnSnapshot`（state + outbound + ctx 三元组） |
| after_turn | `TurnSnapshot` | `AfterTurnCtx`（frozen） |

---

## 4. 各阶段模块链

> 状态标记：✅ 已按新方法论重建（逐 module + 独立测试 + git commit）；⏳ 旧版（M1b 遗留，待重建）。

### 4.1 before_turn ✅

```
acquire_session → build_ctx → emit → collect_exports → return
```

| 模块 | requires | produces | 职责 |
|---|---|---|---|
| acquire_session | — | `session:session` | get_or_create 拿会话，写 state.session |
| build_ctx | `session:session` | `session:ctx` | 打包 BeforeTurnCtx（身份 + history_messages + extra_metadata） |
| emit | `before_turn.build_ctx`, `session:ctx` | `session:ctx` | `bus.emit(ctx)` 门控 |
| collect_exports | `before_turn.emit`, `session:ctx` | `session:ctx` | 回收 `session:extra_hint:` + `session:abort_reply` |
| return | `before_turn.collect_exports`, `session:ctx` | — | 设 frame.output |

### 4.2 before_reasoning ✅

```
sync_tools → build_ctx → emit → collect_exports → return
```

| 模块 | requires | produces | 职责 |
|---|---|---|---|
| sync_tools | —（零依赖） | —（零 slot） | 把会话身份灌进 ToolRegistry（副作用） |
| build_ctx | `before_reasoning.sync_tools`（模块槽） | `reasoning:ctx` | 搬 before_turn 决策 → BeforeReasoningCtx |
| emit | `before_reasoning.build_ctx`, `reasoning:ctx` | `reasoning:ctx` | `bus.emit(ctx)` 门控 |
| collect_exports | `before_reasoning.emit`, `reasoning:ctx` | `reasoning:ctx` | 回收 `reasoning:extra_hint:` + `reasoning:abort_reply` |
| return | `before_reasoning.collect_exports`, `reasoning:ctx` | — | 设 frame.output |

> 注意：build_ctx 的 requires 用「模块槽依赖」（sync_tools 是零 produces，无法用数据槽排序），
> 这与 before_turn 的 build_ctx（靠 `session:session` 数据槽排序）不同。

### 4.3 prompt_render ✅

```
build_ctx → emit → collect_exports → render → return
```

| 模块 | requires | produces | 职责 |
|---|---|---|---|
| build_ctx | — | `prompt:ctx` | 从 input 组装 PromptRenderCtx |
| emit | `prompt_render.build_ctx`, `prompt:ctx` | `prompt:ctx` | `bus.emit(ctx)` 门控 |
| collect_exports | `prompt_render.emit`, `prompt:ctx` | `prompt:ctx` | 回收 `prompt:section_top:` / `prompt:section_bottom:` / `prompt:extra_hint:` |
| render | `prompt_render.collect_exports`, `prompt:ctx` | `prompt:result` | 调 ContextBuilder.render 产出 messages |
| return | `prompt_render.render`, `prompt:result` | — | 设 frame.output = PromptRenderResult |

> 关键差异：output 不是 ctx 而是 `PromptRenderResult`（messages），因此多一个 `render` 模块，
> 且排在 collect_exports 之后（render 要用收好的完整 ctx 渲染）。

### 4.4 before_step ✅

```
build_ctx → emit → collect_exports → inject_hints → return
```

| 模块 | requires | produces | 职责 |
|---|---|---|---|
| build_ctx | — | `step:ctx` | 组装 BeforeStepCtx（iteration + token 估算 + 可见工具转 frozenset） |
| emit | `before_step.build_ctx`, `step:ctx` | `step:ctx` | `bus.emit(ctx)` 门控 |
| collect_exports | `before_step.emit`, `step:ctx` | `step:ctx` | 回收 `step:extra_hint:` + `step:abort_reply`（映射为 early_stop） |
| inject_hints | `before_step.collect_exports`, `step:ctx` | —（副作用） | 把 hints 塞进 `frame.input.messages` |
| return | `before_step.inject_hints`, `step:ctx` | — | 设 frame.output = BeforeStepCtx |

> 独特之处：inject_hints 是「副作用型业务模块」（改 frame.input.messages，不产 slot）；
> early_stop 用 `step:abort_reply` 槽（后缀统一）内部映射，只终止 tool loop 而非整个 turn。

### 4.5 after_step ✅（TAP）

```
copy_input → collect_pre → fanout → collect_post → return
```

| 模块 | requires | produces | 职责 |
|---|---|---|---|
| copy_input | — | `step:ctx` | 把 input 快照（AfterStepCtx）原样放进 step:ctx |
| collect_pre | `after_step.copy_input`, `step:ctx` | `step:ctx` | fanout 前回收 `step:telemetry:` 合并进 extra_metadata |
| fanout | `after_step.collect_pre`, `step:ctx` | —（旁路） | `bus.fanout(ctx)` 并发广播给所有 `@on_after_step` / `@on_any` 观察者 |
| collect_post | `after_step.fanout`, `step:ctx` | `step:ctx` | fanout 后回收补充的 telemetry（不覆盖观察者已见同名） |
| return | `after_step.collect_post`, `step:ctx` | — | 设 frame.output = AfterStepCtx |

> 独特之处：TAP 阶段没有 build_ctx（input 本身就是 AfterStepCtx 快照，copy 即可）、没有 emit（用 fanout
> 并发广播，返回值丢弃、失败只计数）；collect 模块被实例化两次（collect_pre / collect_post）夹住 fanout；
> AfterStepCtx 是 frozen，补 telemetry 用 dataclasses.replace 生成新实例，而非原地改字段。

### 4.6 after_reasoning ✅（GATE）

```
build_ctx → emit → persist → build_outbound → return
```

| 模块 | requires | produces | 职责 |
|---|---|---|---|
| build_ctx | — | `reasoning:ctx` | `parse_response` 解析回复 + 组装 AfterReasoningCtx |
| emit | `after_reasoning.build_ctx`, `reasoning:ctx` | `reasoning:ctx` | `bus.emit(ctx)` 门控，插件可改 reply / media / outbound_metadata |
| persist | `after_reasoning.emit`, `reasoning:ctx` | `reasoning:persisted_user` + `reasoning:persisted_assistant` | user + assistant 消息落库（合并原 persist_user / persist_asst / update_meta / append_messages） |
| build_outbound | `after_reasoning.persist`, `reasoning:ctx` | `reasoning:outbound` | 组装 OutboundMessage，回填 persisted 稳定 ID |
| return | `after_reasoning.build_outbound`, `reasoning:ctx`, `reasoning:outbound` | — | 打包 TurnSnapshot(state, outbound, ctx) |

> 独特之处：是最后一个 GATE（插件可在回复发出前改 reply / media / outbound_metadata）；
> output 是 TurnSnapshot 三元组，不是单个 ctx；与 before_reasoning 共享 reasoning: 命名空间（一条链两端）。
>
> 裁剪说明（相对 M1b 旧版 8 模块 ~500 行）：砍 context_retry / meme_tag 字段、mobile 特判、
> control turn 多输入回放（InputLock）、milestone 诊断日志、retired 字段保护、persist 字段扩展；
> 四个持久化步骤合并为一个 persist。

### 4.7 after_turn ✅（TAP）

```
build_committed → fanout_committed → build_ctx → fanout_ctx → dispatch → return
```

| 模块 | requires | produces | 职责 |
|---|---|---|---|
| build_committed | — | `turn:committed` | 组装 TurnCommitted（提交事件，核心字段） |
| fanout_committed | `after_turn.build_committed`, `turn:committed` | —（旁路） | `bus.fanout(committed)` 广播提交事件 |
| build_ctx | `after_turn.fanout_committed` | `turn:ctx` | 组装 AfterTurnCtx（插件 TAP 快照） |
| fanout_ctx | `after_turn.build_ctx`, `turn:ctx` | —（旁路） | `bus.fanout(ctx)` 广播快照 |
| dispatch | `after_turn.fanout_ctx` | —（副作用） | `OutboundPort.dispatch` 派发 outbound |
| return | `after_turn.dispatch` | — | 设 frame.output = OutboundMessage |

> 独特之处：TAP 阶段但有 build_ctx（input 是 TurnSnapshot 三元组，需组装出 AfterTurnCtx，
> 不同于 after_step 的 copy_input）；**双层广播**（TurnCommitted 内部权威事件 + AfterTurnCtx 插件快照）；
> output 是 OutboundMessage（整个 turn pipeline 的最终产物）。
>
> 裁剪说明（相对 M1b 旧版 10 模块 ~400 行）：砍 build_work（budget/react_stats/model_binding）、
> collect_extras（turn:extra:）、log_budget（日志）、collect_telemetry（turn:telemetry:）；
> TurnCommitted 事件类型砍到 8 核心字段（session_key/channel/chat_id/input_message/
> assistant_response/tools_used/assistant_message_id/timestamp）。

---

## 5. 实现状态（重建进度）

| 阶段 | 状态 | 测试 |
|---|---|---|
| before_turn | ✅ 已重建（5 模块） | 5 个独立测试全绿 |
| before_reasoning | ✅ 已重建（5 模块） | sync_tools + return 端到端全绿 |
| prompt_render | ✅ 已重建（5 模块） | return 端到端全绿（6 断言） |
| before_step | ✅ 已重建（5 模块） | return 端到端全绿（8 断言） |
| after_step | ✅ 已重建（5 实例） | return 端到端全绿（9 断言） |
| after_reasoning | ✅ 已重建（5 模块） | return 端到端全绿（11 断言） |
| after_turn | ✅ 已重建（6 模块） | return 端到端全绿（14 断言） |

> `turn_pipeline.run()` 已接回完整 7 阶段链路（before_turn → before_reasoning → reasoner →
> after_reasoning → after_turn），一次 turn 端到端跑通；m2_verify 22/22 全绿（顺序、step 循环、
> GATE 改写、abort/early_stop 短路、TurnCommitted fanout、dispatch 全部验证通过）。

---

## 6. 关键设计决策（为什么这么设计）

1. **每个阶段有独立 ctx 类型**：字段集合不同、插件作用域不同、阶段边界清晰，故不跨阶段复用 ctx。
2. **GATE 输入用 frozen、输出用 mutable**：输入是「已发生的事实」只读；输出是门控对象可改写。
3. **「必填 vs 默认值」表达语义**：上一阶段已拍板的决策字段（如 skill_names / retrieved_memory_block）设为必填，
   逼显式传递；本阶段插件可新增的字段（extra_hints / abort）给默认值。
4. **副作用型 vs 数据型模块**：数据型走 slot，副作用型（sync_tools）直接改外部对象，零 slot。
5. **跨阶段交接靠 output，不靠 slot**：slot 不跨 phase 存活，output 是唯一跨阶段通道。

---

## 7. 维护约定

- **任何生命周期架构改动**（增删模块、改 ctx 字段、改命名空间、改 GATE/TAP 语义、改数据流转）
  → 同步更新本文件对应章节，并更新第 5 节的实现状态。
- 每个 phase 重建完成后，把其状态从 ⏳ 改为 ✅，并更新模块链表格。
- 本文件是「当前真相」，specs/ 里的历史设计文档（002~006）仅作过程记录，冲突时以本文件为准。
