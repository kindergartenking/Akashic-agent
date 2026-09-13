# 006 — before_reasoning 阶段设计

> 状态：设计稿（待逐 module 实现）
> 前置：`before_turn` 5 模块链已闭环（见 `backend/agent/lifecycle/phases/before_turn.py`）
> 关联：`003`（7 阶段落地总览）、`004`（slot 参考）、`005`（slot 逐项作用）

---

## 1. 定位与职责

before_reasoning 是 7 阶段生命周期的**第二个阶段**，位于 `before_turn` 之后、`reasoner`（内部含 prompt_render / before_step / after_step）之前。它是「推理真正开始前，最后一次**准备 + 门控**的机会」。

一句话职责：**把 before_turn 阶段的决策「转交」给推理层，并为工具调用铺设正确的会话上下文。**

拆成两层职责：

| 层 | 做什么 | 对应模块 |
|---|---|---|
| 准备工具上下文 | 把本 turn 的 channel / chat_id / session_key / turn_id / 时间戳 / user_source_ref 灌进 `ToolRegistry`，让后续工具调用在正确的会话上下文里执行 | `sync_tools` |
| 准备 reasoning ctx | 把 before_turn 的决策（skill_names / 记忆块 / 提示）搬进 `BeforeReasoningCtx`，作为本阶段的交接载体 | `build_ctx` |

后面的 `emit / collect_exports / return` 是 GATE 骨架，与 before_turn 完全同构（见 §5）。

---

## 2. 输入 / 输出

```
输入：BeforeReasoningInput（frozen dataclass）
输出：BeforeReasoningCtx（GATE mutable dataclass）
```

### 输入 `BeforeReasoningInput`（`types.py:65`）

```python
@dataclass(frozen=True)
class BeforeReasoningInput:
    state: TurnState          # 贯穿全程的可变状态锚点（session 已由 before_turn 填好）
    before_turn: BeforeTurnCtx  # 上一阶段的产物
```

**为什么是 frozen + 包装？** 输入是「已经发生的事实」，不该被模块原地改（输入稳定性）。它包装两个对象，因为本阶段需要**同时**访问：
- `state`：拿 `session`（acquire_session 在 before_turn 填的），以及 `state.session` 给 sync_tools 用；
- `before_turn`：拿上一阶段打包好的身份 + 决策字段。

### 输出 `BeforeReasoningCtx`（`types.py:71`）

```python
@dataclass
class BeforeReasoningCtx:
    # 按约定只读（事实）
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime
    # 可写（决策）
    skill_names: list[str]
    retrieved_memory_block: str
    extra_hints: list[str] = field(default_factory=list)
    abort: bool = False
    abort_reply: str = ""
```

9 个字段。注意和 `BeforeTurnCtx` 的差异：**没有 `history_messages`**（历史在 before_turn 打包过，reasoning 阶段不直接消费历史，历史走 prompt_render 的 `PromptRenderInput.history`）；**`skill_names` / `retrieved_memory_block` 从「有默认值」变成「必填」**（因为它们是本阶段要往下传的核心决策，必须显式从 before_turn 搬来）。

---

## 3. 与 before_turn 的承接关系

```
before_turn.run(state)  ──►  BeforeTurnCtx（output）
                                   │
                                   │ 作为 before_turn 字段，塞进
                                   ▼
BeforeReasoningInput(state, before_turn)  ──►  before_reasoning.run()
```

关键点：**数据槽不跨 phase 存活**（每个 phase 新建 frame + 空 slots），所以 before_turn 的 `session:ctx` 槽**不会**出现在 before_reasoning 里。跨阶段传数据靠的是 **before_turn 的 output（`BeforeTurnCtx` 对象）**，它被塞进 `BeforeReasoningInput.before_turn` 字段，作为本阶段的输入。

这决定了 before_reasoning 的 `build_ctx` 必须「把 before_turn 的字段**搬**过来」，而不是直接复用 `BeforeTurnCtx`——因为两个 ctx 类型不同、字段集合不同、插件作用域也不同（见 §7）。

---

## 4. 模块链设计（5 模块）

```
sync_tools ──► build_ctx ──► emit ──► collect_exports ──► return
 (准备工具)    (准备 ctx)    (GATE 门控)  (回收插件外溢)     (产出 output)
```

| # | 模块 slot | requires | produces | 职责 |
|---|---|---|---|---|
| 1 | `before_reasoning.sync_tools` | —（零依赖） | —（零 slot） | 把会话身份灌进 ToolRegistry |
| 2 | `before_reasoning.build_ctx` | `before_reasoning.sync_tools` | `reasoning:ctx` | 组装 BeforeReasoningCtx |
| 3 | `before_reasoning.emit` | `before_reasoning.build_ctx`, `reasoning:ctx` | `reasoning:ctx` | `bus.emit(ctx)` 门控 |
| 4 | `before_reasoning.collect_exports` | `before_reasoning.emit`, `reasoning:ctx` | `reasoning:ctx` | 回收 `reasoning:extra_hint:` / `reasoning:abort_reply` |
| 5 | `before_reasoning.return` | `before_reasoning.collect_exports`, `reasoning:ctx` | — | 设 `frame.output = ctx` |

命名空间是 `reasoning:`（不是 `before_reasoning:`），理由同 before_turn 用 `session:`——**命名空间表达「数据语义域」，不表达「生产阶段」**。`reasoning:` 会被 before_reasoning 和 after_reasoning **两个阶段共享**（它们都在同一条推理链上）。

### 模块 1：`sync_tools`（唯一的「零 slot 模块」）

```python
slot = "before_reasoning.sync_tools"
requires: tuple[str, ...] = ()   # 无依赖，无 produces

async def run(self, frame):
    state = frame.input.state
    before_turn = frame.input.before_turn
    if state.session is None:
        raise RuntimeError("BeforeReasoning requires TurnState.session")
    self._tools.set_context(
        channel=before_turn.channel,
        chat_id=before_turn.chat_id,
        session_key=before_turn.session_key,
        turn_id=running_turn_id.get(),
        current_timestamp=before_turn.timestamp.isoformat(),
        current_user_source_ref=predict_current_user_source_ref(
            session_manager=self._session_manager,
            session=state.session,
        ),
    )
    return frame
```

**特点**：只调 `tools.set_context(...)` 这个副作用，不读不写任何 slot。它是 7 个阶段里唯一的「零 slot 模块」。依赖注入 `ToolRegistry` + `SessionManager`。

### 模块 2：`build_ctx`

```python
slot = "before_reasoning.build_ctx"
requires = ("before_reasoning.sync_tools",)   # 排在 sync_tools 之后
produces = (_CTX_SLOT,)                        # reasoning:ctx

async def run(self, frame):
    before_turn = frame.input.before_turn
    frame.slots[_CTX_SLOT] = BeforeReasoningCtx(
        session_key=before_turn.session_key,
        channel=before_turn.channel,
        chat_id=before_turn.chat_id,
        content=before_turn.content,
        timestamp=before_turn.timestamp,
        skill_names=list(before_turn.skill_names),          # 决策：搬
        retrieved_memory_block=before_turn.retrieved_memory_block,  # 决策：搬
        extra_hints=list(before_turn.extra_hints),          # 决策：搬
    )
    return frame
```

**核心动作**：把 before_turn 的「身份事实」（5 个）原样搬，把「决策字段」（skill_names / retrieved_memory_block / extra_hints，这 3 个是 before_turn 阶段插件可能改过的）也搬过来。`list(...)` 是**浅拷贝**，避免后续阶段改 before_reasoning 的字段时反噬 before_turn 的 ctx。

### 模块 3~5：`emit` / `collect_exports` / `return`（骨架，与 before_turn 完全同构）

这三个是 GATE 骨架，逻辑和 before_turn 一模一样，只是：
- 命名空间 `session:` → `reasoning:`；
- ctx 类型 `BeforeTurnCtx` → `BeforeReasoningCtx`；
- export 前缀 `session:extra_hint:` → `reasoning:extra_hint:`；
- abort 槽 `session:abort_reply` → `reasoning:abort_reply`。

`return` 是唯一设 `frame.output` 的模块，其余都不碰 output。

---

## 5. 与 before_turn 的同构性（核心结论）

```
before_turn:    acquire_session → build_ctx → emit → collect_exports → return
before_reasoning: sync_tools    → build_ctx → emit → collect_exports → return
                        └──────────────┬──────────────┘
                            业务/准备模块（各阶段不同）

                       └──────────────────┬──────────────────┘
                             GATE 骨架（完全一致，可复用）
```

**规律**：GATE 阶段的骨架（`emit → collect_exports → return`）是**稳定的**，变的只是前两个「准备模块」：
- before_turn 的准备 = 取 session + 打包身份历史；
- before_reasoning 的准备 = 同步工具上下文 + 把 before_turn 决策搬进新 ctx。

后续 `prompt_render`、`before_step` 也按这个二分法处理（准备模块不同，骨架一致）。

---

## 6. 砍掉的能力

原版 before_reasoning 是 **6 模块**，多一个：

| 模块 | 原版职责 | 砍掉理由 |
|---|---|---|
| `warmup` | 用 `context.render` 做一次丢弃结果的 prompt 预热（提前校验 + 暖缓存） | 纯优化；真正的渲染在 reasoner 内部的 prompt_render 才发生，预热只是提前触发，不影响正确性 |

其余 5 模块都是「基本功能」，保留。

---

## 7. 关键设计决策（为什么这么设计）

### 决策 A：为什么 sync_tools 是「零 slot」模块？

因为它的产物不是「数据」，而是「副作用」——把上下文写进 `ToolRegistry` 这个**外部可变对象**。它不需要通过 slot 传递任何东西给下游，所以 `requires=()`、无 `produces`。这是「副作用型模块」和「数据型模块」的区分：数据型走 slot，副作用型直接改外部对象。

### 决策 B：为什么 build_ctx 要「搬字段」而不是复用 `BeforeTurnCtx`？

因为每个阶段有自己的 ctx 类型，这是**刻意**的（详见 `004` §5.0.3）：

1. **字段集合不同**：`BeforeTurnCtx` 有 `history_messages`（before_turn 打包历史用），`BeforeReasoningCtx` 没有（推理阶段不直接消费历史）。
2. **插件作用域不同**：before_turn 插件能改 `BeforeTurnCtx` 的字段，before_reasoning 插件能改 `BeforeReasoningCtx` 的字段。如果复用同一个对象，两个阶段的插件作用域就混在一起了。
3. **阶段边界清晰**：每个 ctx 是「一次阶段性交接的载体」，字段明确表达「这个阶段交接什么」。

所以 build_ctx 的「搬」是**有选择的搬**：只搬本阶段需要的字段，丢掉 before_turn 专属的（如 history_messages）。

### 决策 C：为什么 `BeforeReasoningInput` 用 frozen？

输入是「已经发生的事实」，模块只读不该改。frozen 用物理约束强制这一点（Python dataclass 的 `frozen=True` 让字段赋值抛错）。而输出 `BeforeReasoningCtx` 是 mutable，因为它是 GATE 阶段的「可改写对象」，插件要能改它。

### 决策 D：为什么 skill_names / retrieved_memory_block 在 BeforeReasoningCtx 里是「必填」而非默认值？

因为它们是「上一阶段已经决定好的决策」，必须在交接时**显式**传下来，不能靠默认值掩盖「忘了搬」的 bug。而 extra_hints / abort / abort_reply 是「本阶段插件还可能新增的」，给默认值即可。

---

## 8. 逐 module 实现顺序（按方法论）

沿用 before_turn 的「逐 module 重建 + 每模块一次 git commit + 简单测试」节奏：

1. **`sync_tools`**：先实现「同步工具上下文」。验证 `tools.set_context` 被正确调用（channel/chat_id/session_key/turn_id/时间戳都灌进去）。
2. **`build_ctx`**：实现「打包 BeforeReasoningCtx」。验证字段正确从 before_turn 搬过来。
3. **`emit`**：实现 GATE 门控。验证 handler 能改/替换 ctx。
4. **`collect_exports`**：实现回收。验证插件模块外溢的 extra_hints / abort_reply 被收回。
5. **`return`**：实现产出。验证 output 是链尾最终 ctx。

每个模块的联动改动要点：
- 实现 `emit` 时，`default_before_reasoning_modules` 要加 `bus` 参数；
- 实现 `return` 时，前序模块的 output 职责顺延给 return（和 before_turn 一样）。

---

## 9. 测试策略

沿用 before_turn 的「每模块一个独立测试文件」：

| 模块 | 测试文件 | 验证点 |
|---|---|---|
| sync_tools | `test_before_reasoning_sync_tools.py` | `tools.set_context` 被调、参数正确 |
| build_ctx | `test_before_reasoning_build_ctx.py` | output 类型 + 字段正确搬运 |
| emit | `test_before_reasoning_emit.py` | handler 改字段 / 整体替换生效 |
| collect_exports | `test_before_reasoning_collect_exports.py` | extra_hints / abort_reply 回收 |
| return | `test_before_reasoning_return.py` | 5 模块链端到端，output 是最终 ctx |

---

## 附：当前实现状态

`backend/agent/lifecycle/phases/before_reasoning.py` 已存在一个「旧策略」下的 5 模块轻量版（`sync_tools → build_ctx → emit → collect_exports → return`）。本次设计稿是「按新方法论重新审视」的产物——**结论是现有 5 模块结构保持不变**，但实现时按 §8 的顺序逐个 module 走一遍，补齐每个模块的独立测试与 git commit（当前缺这些）。
