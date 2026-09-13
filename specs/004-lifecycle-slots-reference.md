# 004 — 七阶段生命周期 slot 参考

> 状态：与代码同步（`backend/agent/lifecycle/phases/*.py`）
> 上游：`003-m1b-lifecycle-phases.md`（模块链与依赖表）；本文只讲 **slot**
> 配套：`005-lifecycle-slots-purpose.md`——逐 phase、逐 slot 的「为什么存在」纵向手册
> 读者：要写生命周期插件、或要读懂 `frame.slots[...]` 存取的人

003 回答的是「每个 phase 有哪些模块、依赖什么」；本文回答的是「**模块之间靠什么交换数据、这些数据叫什么名字、谁写谁读、什么时候失效**」。

---

## 1. slot 是什么

`PhaseFrame` 上有三个字段（`agent/lifecycle/phase.py:64`）：

```python
@dataclass
class PhaseFrame(Generic[I, O]):
    input: I                              # 阶段入参（只读）
    slots: dict[str, Any] = field(default_factory=dict)   # 模块间通信区
    output: O | None = None               # 阶段产物
```

`slots` 是**模块之间唯一的通信通道**。模块彼此不 import、不持有对方引用：

```python
# 模块 A：把结果写进 slots
frame.slots["session:session"] = session

# 模块 B：从 slots 取
session = cast(SessionLike, frame.slots["session:session"])
```

`Phase.run()` 造一个 frame，按序调 `module.run(frame)`，同一个 frame 一路传下去，最后取 `frame.output`（`phase.py:314`）。所以 **slots 的存活期 = 一次 phase 执行**。

---

## 2. 两类 slot —— 这是最容易混淆的地方

代码里 `slot` 这个词有**两种完全不同的含义**，靠分隔符区分（判别函数 `_is_module_slot`，`phase.py:232`）：

| | **模块 slot** | **数据 slot** |
|---|---|---|
| 形态 | `before_turn.acquire_session` | `session:session` |
| 分隔符 | `.` | `:` |
| 声明位置 | 类属性 `slot = "..."` | 类属性 `produces = (...)` / `requires = (...)` |
| 是否进 `frame.slots` | ❌ **不进** | ✅ 进 |
| 谁消费它 | `topo_sort_modules()` 排序器 | 模块的 `run()` |
| 解决了什么 | 「**谁先谁后**」 | 「**谁给谁数据**」 |
| 唯一性范围 | 全链唯一（重名直接 raise） | 每个 phase 内唯一；**可跨 phase 重名** |

一句话：**模块 slot 定义顺序，数据 slot 定义数据**。二者只是共用「slot」这个名字，住在同一个 `requires` 元组里，但走的是两条完全不同的通路。

### 2.1 三个声明属性分别被谁读

模块类上真正起作用的声明只有三个：`slot` / `requires` / `produces`。它们都是普通类属性，用 `getattr(module, attr, ())` 读取，因此缺省就等于空元组。**但它们的消费者各不相同**：

| 声明属性 | 谁读 | 读来干什么 |
|---|---|---|
| `slot` | `topo_sort_modules` / `Phase._validate` | 作为图里的**节点名**；重复即 `RuntimeError` |
| `requires`（**模块槽** `a.b`） | `topo_sort_modules` | 建**入边**，决定谁先谁后 |
| `requires`（**数据槽** `a:b`） | 仅 `Phase._validate` | 校验该数据槽此前是否已被 `produces` 声明过 |
| `produces` | **仅** `Phase._validate` | 把声明的数据槽加进「已提供」集合 |

⚠️ **反直觉但很重要**：`produces` **不参与拓扑排序**。排序只看 `requires` 里的模块槽；`produces` 的唯一实际作用是让下游模块的 `requires` 能通过 `Phase.__init__` 里的「slot 闭合」校验：

```python
# phase.py:322  Phase._validate() —— 按已排序的执行顺序遍历
provided: set[str] = set()
for module in self._modules:
    for raw_slot in module.requires:
        if 是数据槽 and raw_slot not in provided:
            raise RuntimeError("Phase slot 未闭合: ...")   # 前面没人 produce 过它
    provided.add(module.slot)
    provided.update(module.produces)      # ← produces 唯一的落点
```

因此 `produces` 的实际语义是「**给后面的模块发凭据**」：B 想 `requires = (..., "session:session")`，就必须有某个排在它前面的模块 `produces = ("session:session",)`，否则 Phase 构造时直接抛错。

### 2.2 声明与执行是两件事，失败模式不同

`produces` 是**声明**（构建期读），`frame.slots[...] = ...` 是**执行**（运行期发生）。两者写的是同一个字符串字面量，但不会互相校验，于是有四种组合：

| 是否 `produces` 声明 | 是否真的写 slot | 有下游 `requires` 它吗 | 结果 |
|---|---|---|---|
| ✅ | ✅ | ✅ | 正常 |
| ✅ | ❌ | ✅ | `_validate` **通过** → 运行时 `KeyError` ⚠️ **最危险** |
| ❌ | ✅ | ✅ | `_validate` 抛 `Phase slot 未闭合` ❌ 构建期就炸，反而安全 |
| ❌ | ✅ | ❌ | 静默通过（`step:telemetry_collected` 即此类） |

**实践结论**：新增模块时，只要希望别处能 `require` 你的产出，就**必须**在 `produces` 里登记，否则 Phase 构造即失败；反之若只是模块内部临时使用，不登记也无妨。

另外注意：`produces` 里**只放数据槽，从不放模块槽**——模块槽是靠「排在前面」自动进入 `provided` 的，不需要声明。

`requires` 里两种可以混写，语义按分隔符自动分流：

```python
class _ApplyMemoryExclusionModule:
    slot = "before_turn.memory_exclusion"
    requires = ("before_turn.acquire_session", "session:session")
    #           └── 模块 slot：先跑完 acquire_session  └── 数据 slot：且 session:session 已就位
```

---

## 3. 五个命名空间（核心结论）

7 个 phase，但**只有 5 个命名空间**——因为 `reasoning:` 和 `step:` 各自被两个 phase 共用：

| 命名空间 | 归属 phase | 领地 |
|---|---|---|
| `session:` | `before_turn` | 会话获取 + 上下文组装（历史、检索记忆） |
| `reasoning:` | `before_reasoning` **+** `after_reasoning` | reasoning 的**进**与**出** |
| `prompt:` | `prompt_render` | 系统提示词渲染 |
| `step:` | `before_step` **+** `after_step` | 单步 tool loop 的**进**与**出** |
| `turn:` | `after_turn` | 收尾：提交、遥测、派发 |

命名空间的切分依据是**语义分组**，不是「一个 phase 一个空间」。`before_reasoning` 和 `after_reasoning` 是一件事的首尾两半，所以共用一个空间；`before_step` / `after_step` 同理。

> 顺带一个例子：`reasoning:ctx` 同时是 `before_reasoning.py:33` 和 `after_reasoning.py:69` 的 `_CTX_SLOT`。这不是笔误，也不会冲突——见下一节。

---

## 4. 重名 slot 为什么不出事

`reasoning:ctx` 和 `step:ctx` 在两个 phase 里重名，却不冲突，原因是 **slot 的作用域是单个 `PhaseFrame`，不是整个 turn**：

```
before_reasoning.run(input)          after_reasoning.run(input)
  frame_A = BeforeReasoningFrame(...)   frame_B = AfterReasoningFrame(...)
  frame_A.slots = {}   ← 全新空字典      frame_B.slots = {}   ← 又一个新的空字典
  ...写入 reasoning:ctx...              ...也写入 reasoning:ctx...
  return frame_A.output                 return frame_B.output
  frame_A 被丢弃                         frame_B 被丢弃
```

两个 `reasoning:ctx` 分别活在两个不同 dict 里，生命周期不重叠。**数据 slot 不跨 phase 存活**——想在 `before_turn` 写一个 slot、在 `after_reasoning` 才读，是读不到的。

那跨阶段的数据怎么传？靠 **`frame.output` 这个 ctx 对象**，而不是 slots：

```
BeforeTurnCtx ──► BeforeReasoningInput.before_turn ──► ... ──► TurnSnapshot.ctx
     (对象，被下一个 phase 的 input 拎着走)
```

**记住这条分工**：`slots` 管**阶段内部**的模块间通信，`output`/`input` 管**阶段之间**的数据传递。

---

## 5. 七个 phase 的 slot 明细

符号约定：**ctx** = 主上下文对象槽；**中间** = 阶段内部中间产物；**结果** = 供 `return` 模块取用；**export** = 插件注入口；**control** = 控制位。

### 5.0 先明确：`*:ctx` 是什么类

`ctx` **不是某一个类**，而是**一族各自独立的 dataclass**，全部定义在 `agent/lifecycle/types.py`。
七个 phase 七个类，**彼此没有继承关系**，也没有基类或 Protocol：

| phase | ctx 类 | 装饰器 | 可写性 |
|---|---|---|---|
| before_turn | `BeforeTurnCtx` | `@dataclass` | mutable |
| before_reasoning | `BeforeReasoningCtx` | `@dataclass` | mutable |
| prompt_render | `PromptRenderCtx` | `@dataclass` | mutable |
| before_step | `BeforeStepCtx` | `@dataclass` | mutable |
| after_step | `AfterStepCtx` | `@dataclass(frozen=True)` | frozen |
| after_reasoning | `AfterReasoningCtx` | `@dataclass` | mutable |
| after_turn | `AfterTurnCtx` | `@dataclass(frozen=True)` | frozen |

**为什么没有基类？** 因为这些类的角色是「**插件协议**」而非「类型体系」。`types.py:28` 的注释写得很直白：

> 插件阶段接口：现有插件直接构造或读写下列上下文。新核心可以用薄层转换，
> 但迁移插件前**不得删除字段、改变可写范围或调整上下文出现的阶段**。

插件是靠「**字段名 + 出现时机**」跟核心耦合的，不是靠继承。加基类反而会诱导插件写出跨阶段通用逻辑，破坏阶段语义。

**事实上的公共接口只有三个身份字段**：`session_key` / `channel` / `chat_id`——7 个 ctx 类全都有，其余字段各阶段自定。

**可变性严格对应 GATE / TAP**：mutable 的 5 个走 `emit`（插件可直接改字段）；frozen 的 2 个走 `fanout`（插件只能读，补充走 slot export，由模块 `dataclasses.replace()` 造新实例）。

#### 5.0.1 每个 ctx 的「可写字段」= 该阶段插件能影响什么

`types.py` 里用 `# 可写` 注释分隔的字段，就是该阶段插件的作用域：

| ctx 类 | 可写字段 |
|---|---|
| `BeforeTurnCtx` | `skill_names`、`abort`、`abort_reply`、`extra_hints`、`extra_metadata` |
| `BeforeReasoningCtx` | `skill_names`、`retrieved_memory_block`、`extra_hints`、`abort`、`abort_reply` |
| `PromptRenderCtx` | `system_sections_top`、`system_sections_bottom`（+ `extra_hints`） |
| `BeforeStepCtx` | `extra_hints`、`early_stop`、`early_stop_reply` |
| `AfterStepCtx` | **无**（frozen，靠 `replace()`） |
| `AfterReasoningCtx` | `reply`、`media`、`meme_tag`、`outbound_metadata` |
| `AfterTurnCtx` | **无**（frozen，靠 `replace()`） |

⚠️ `# 可写` 注释是**约定，不是强制**——Python dataclass 不拦写入。`PromptRenderCtx.extra_hints` 就落在注释线上方，但功能上确实可写（`render` 会读它、`collect_exports` 会追加）。
**真正的强制只有一条**：`frozen=True`。

#### 5.0.2 别和这几类混淆

`types.py` 里还住着一批名字近似的类，但它们**不是生命周期阶段的 ctx**：

| 类 | 是什么 |
|---|---|
| `TurnState` | 每 turn 的可变状态，是 `before_turn` 的 `frame.input`（不是 ctx） |
| `BeforeReasoningInput` / `PromptRenderInput` / `BeforeStepInput` / `AfterReasoningInput` | 各 phase 的**入参**，基本都 `frozen=True`，只读 |
| `PromptRenderResult` | `prompt_render` 的产物（对应 `prompt:result` 槽） |
| `TurnSnapshot` | `after_reasoning` 的产物 / `after_turn` 的入参 |
| `TurnPersistencePolicy` | 持久化开关，挂在 `TurnState` 上 |
| `BeforeToolCallCtx` / `AfterToolResultCtx` / `PreToolCtx` | **工具 hook 的 ctx**，不属七个生命周期阶段，走另一套（`PreToolCtx` 是唯一 mutable 的，handler 返回 dict 改 arguments） |

#### 5.0.3 为什么是七个 ctx，而不是一个

**核心**：ctx 不是「状态容器」，而是「**一次阶段性交接的载体**」。它的寿命短于 turn，所以只该装这次交接需要的东西。

先看真实的存活跨度：

| 对象 | 存活区间 | 证据 |
|---|---|---|
| `TurnState`（**不是 ctx**） | **整个 turn** | `turn_pipeline.py:91` 建 → `:140` 最后一次用 |
| `BeforeTurnCtx` | Phase1 → Phase2 | `:98` 产出 → `:113` 作为 `BeforeReasoningInput.before_turn`，**之后再没用过** |
| `BeforeReasoningCtx` | Phase2 → 喂给 reasoner | `:112` → `:133-135`（skill_names / retrieved_memory_block / extra_hints） |
| `PromptRenderCtx` | prompt_render 内（**一次**） | `reasoner.py:100`，在循环外 |
| `BeforeStepCtx` | **每轮一次** | `reasoner.py:124`，在 `for iteration` 内 |
| `AfterStepCtx` | **每轮一次** | `reasoner.py:171`，在 `for iteration` 内 |
| `AfterReasoningCtx` | Phase5 → 被 `TurnSnapshot` 带走 | `:139` 产出 → `:144` |
| `AfterTurnCtx` | Phase6 内 | `after_turn.py:305` 造 → `:325` fanout |

注意第一行与其余七行的对比：**真正长命的是 `TurnState`，而它恰恰不是 ctx。**

由此推出四条理由：

1. **生命周期不同 → 合并必然产生"过期字段"。** `input_tokens_estimate` / `iteration` 每轮都变，`prompt:result` 只有渲染那一刻有效。合成一个活满 turn 的对象，就得在每个阶段反复问「这个字段现在还有效吗」。
2. **可变性不兼容——硬约束。** GATE 要 mutable（`emit` 直接改字段、可替换对象），TAP 必须 frozen（广播时观察者不能互相干扰）。一个 dataclass 不可能既 mutable 又 frozen，所以 `AfterStepCtx` / `AfterTurnCtx` **必须**独立成类。
3. **可写字段 = 权限边界，且每阶段不同。** `skill_names` 在 before_turn / before_reasoning 可写、prompt_render 只读（那时已渲染进 prompt）；`retrieved_memory_block` 在 before_reasoning 可写、before_turn 只读。合并后「这个阶段能不能改 reply」就只剩文档约定。
4. **钩子签名要能表达窗口。** 插件写 `async def on_before_reasoning(ctx: BeforeReasoningCtx)`，签名本身就是契约——只在此窗口生效、这些字段可用。单一 `TurnCtx` 会让签名退化成「什么阶段都能收到」。

**反证**：合成一个 `TurnCtx`（60+ 字段），会同时失去权限边界、`frozen` 保证、签名窗口信息和字段有效性保证，换来的只是少写 6 个 dataclass。



### 5.1 `before_turn` — 命名空间 `session:`

模块链：`acquire_session → memory_exclusion → prepare_context → build_ctx → emit → collect_exports → return`

| 数据 slot | 类型 | 写入者 | 读取者 | 作用 |
|---|---|---|---|---|
| `session:session` | 中间 | `_AcquireSessionModule` | `memory_exclusion` / `prepare_context` | 本轮会话对象（`get_or_create` 或 `require_existing`） |
| `session:context_bundle` | 中间 | `_PrepareContextModule` | `_BuildBeforeTurnCtxModule` | 检索记忆 + 历史消息的原始打包 |
| `session:ctx` | **ctx** | `_BuildBeforeTurnCtxModule` | `emit` / `collect_exports` / `return` | `BeforeTurnCtx` 本体，穿过 GATE 链 |
| `session:extra_hint:*` | export | 插件 | `_CollectBeforeTurnExportSlotsModule` | 追加提示，收进 `ctx.extra_hints` |
| `session:abort_reply` | control | 插件 | `_CollectBeforeTurnExportSlotsModule` | 非空字符串 → `ctx.abort=True`，**短路整个 turn** |

### 5.2 `before_reasoning` — 命名空间 `reasoning:`

模块链：`sync_tools → build_ctx → emit → collect_exports → warmup → return`

| 数据 slot | 类型 | 写入者 | 读取者 | 作用 |
|---|---|---|---|---|
| `reasoning:ctx` | **ctx** | `_BuildBeforeReasoningCtxModule` | `emit` / `collect_exports` / `warmup` / `return` | `BeforeReasoningCtx`，GATE |
| `reasoning:extra_hint:*` | export | 插件 | `_CollectBeforeReasoningExportSlotsModule` | 收进 `ctx.extra_hints` |
| `reasoning:abort_reply` | control | 插件 | `_CollectBeforeReasoningExportSlotsModule` | 非空 → `ctx.abort=True`，**短路整个 turn** |

> 注意 `before_reasoning.sync_tools` **不写任何 slot**——它调 `tools.set_context(...)` 把身份信息推给工具层，属于副作用而非数据交换。这是全表唯一的「零 slot 模块」。

### 5.3 `prompt_render` — 命名空间 `prompt:`

模块链：`build_ctx → emit → collect_exports → render → return`

| 数据 slot | 类型 | 写入者 | 读取者 | 作用 |
|---|---|---|---|---|
| `prompt:ctx` | **ctx** | `_BuildPromptRenderCtxModule` | `emit` / `collect_exports` / `render` | `PromptRenderCtx`，GATE |
| `prompt:result` | 结果 | `_RenderPromptModule` | `_ReturnPromptRenderResultModule` | `PromptRenderResult(messages=[...])`，本阶段最终产物 |
| `prompt:section_top:*` | export | 插件 | `_CollectPromptExportSlotsModule` | 插到系统提示词**顶部**的片段（`PromptSectionRender` 或字符串） |
| `prompt:section_bottom:*` | export | 插件 | `_CollectPromptExportSlotsModule` | 插到系统提示词**底部**的片段 |
| `prompt:extra_hint:*` | export | 插件 | `_CollectPromptExportSlotsModule` | 作为独立的 `plugin_hints` user 消息追加 |

> `prompt:` 是唯一有**两个** export 落点（top / bottom 分段）的命名空间。

### 5.4 `before_step` — 命名空间 `step:`

模块链：`build_ctx → emit → collect_exports → inject_hints → return`

| 数据 slot | 类型 | 写入者 | 读取者 | 作用 |
|---|---|---|---|---|
| `step:ctx` | **ctx** | `_BuildBeforeStepCtxModule` | `emit` / `collect_exports` / `inject_hints` | `BeforeStepCtx`（含 token 估算、可见工具名），GATE |
| `step:extra_hint:*` | export | 插件 | `_CollectBeforeStepExportSlotsModule` | 收进 `ctx.extra_hints`，再追加进 `messages` |
| `step:abort_reply` | control | 插件 | `_CollectBeforeStepExportSlotsModule` | 非空 → `ctx.early_stop=True`，**只终止当前 tool loop** |

> ⚠️ **同名不同语义**：`step:abort_reply` 与 `session:abort_reply` / `reasoning:abort_reply` 名字一样，但前者的落点是 `early_stop`（只停本轮循环），后两者是 `abort`（停整个 turn）。`before_step.py:31` 专门写了注释说明这个映射。

### 5.5 `after_step` — 命名空间 `step:`（与 5.4 共用）

模块链：`copy_input → collect_pre → fanout → collect_post → return`

| 数据 slot | 类型 | 写入者 | 读取者 | 作用 |
|---|---|---|---|---|
| `step:ctx` | **ctx** | `_CopyInputToCtxModule` | `collect_pre` / `fanout` / `collect_post` / `return` | `AfterStepCtx`，**TAP（只读快照）** |
| `step:telemetry:*` | export | 插件 | `collect_pre` / `collect_post` | 收进 `ctx.extra_metadata` |
| `step:telemetry_collected` | 内部 | `_CollectAfterStepExportSlotsModule` | 第二个 collect 实例 | 去重哨兵：`after_fanout` 可**补充** telemetry，但不可覆盖 fanout 已看到的同名值 |
| `step:early_stop_reason` | control | 插件 | `_CollectAfterStepExportSlotsModule` | 非空 → `ctx.early_stop=True` + `early_stop_reason`（`replace()` 新实例） |

> `after_step` 是全表唯一**同一个类实例化两次**的 phase：`collect_pre`（fanout 前，给 handler 读）和 `collect_post`（fanout 后，把补充带回返回 ctx）。`step:telemetry_collected` 就是给这一对去重用的。

### 5.6 `after_reasoning` — 命名空间 `reasoning:` + `persist:` + `outbound:`

模块链：`build_ctx → emit → persist_user → persist_asst → update_meta → append_messages → build_outbound → return`

| 数据 slot | 类型 | 写入者 | 读取者 | 作用 |
|---|---|---|---|---|
| `reasoning:ctx` | **ctx** | `_BuildAfterReasoningCtxModule` | `emit` / 后续全部模块 | `AfterReasoningCtx`（解析后的 reply / media / outbound_metadata），**GATE** |
| `reasoning:persisted_user` | 中间 | `_PersistUserMessageModule` | `_AppendMessagesModule` | 本轮 user 消息 dict 列表（未提交） |
| `reasoning:persisted_assistant` | 中间 | `_PersistAssistantMessageModule` | `_AppendMessagesModule` / `_BuildOutboundMessageModule` | 本轮 assistant 消息 dict（未提交） |
| `reasoning:outbound` | 结果 | `_BuildOutboundMessageModule` | `_BuildTurnSnapshotModule` | `OutboundMessage`，本阶段产物 |
| `persist:user:*` | export | 插件 | `_PersistUserMessageModule` | 追加 user 消息的额外字段（受 reserved 保护） |
| `persist:assistant:*` | export | 插件 | `_PersistAssistantMessageModule` | 追加 assistant 消息的额外字段（受 reserved + retired 检查） |
| `outbound:metadata:*` | export | 插件 | `_BuildOutboundMessageModule` | 合并进 outbound metadata |
| `outbound:media:*` | export | 插件 | `persist_asst` / `build_outbound` | 追加出站媒体（字符串列表） |

> 这是唯一**跨了三个命名空间**的 phase：主链在 `reasoning:`，持久化扩字段在 `persist:`，出站扩字段在 `outbound:`。设计上把「插件改消息字段」和「插件改出站元数据」分到不同前缀，是为了让扩展点一眼可辨。

### 5.7 `after_turn` — 命名空间 `turn:`

模块链：`build_work → collect_extras → build_committed → fanout_committed → log_budget → build_ctx → collect_telemetry → fanout_ctx → dispatch → return`

| 数据 slot | 类型 | 写入者 | 读取者 | 作用 |
|---|---|---|---|---|
| `turn:budget` | 中间 | `_BuildTurnWorkModule` | `build_committed` / `log_budget` | 回复后上下文预算统计 |
| `turn:react_stats` | 中间 | `_BuildTurnWorkModule` | `build_committed` / `log_budget` | React 循环统计（含 model_usage） |
| `turn:tool_chain` | 中间 | `_BuildTurnWorkModule` | `build_committed` | 本轮工具调用链原始数据 |
| `turn:persistence` | 中间 | `_BuildTurnWorkModule` | `build_committed` | `TurnPersistencePolicy`（本阶段共 5 个 slot 由同一模块产出） |
| `turn:extra` | 中间 | `_BuildTurnWorkModule` | `collect_extras` / `build_committed` | 附加字段袋，会被 `turn:extra:*` 补充 |
| `turn:extra:*` | export | 插件 | `_CollectAfterTurnExtraSlotsModule` | 合并进 `turn:extra` |
| `turn:extra_collected` | 内部 | `_CollectAfterTurnExtraSlotsModule` | — | 哨兵：声明「extra 已收集完」，保证 `build_committed` 排在收集之后 |
| `turn:committed` | 事件 | `_BuildTurnCommittedModule` | `_FanoutTurnCommittedModule` | `TurnCommitted` 事件载荷，fanout 给所有订阅者 |
| `turn:ctx` | **ctx** | `_BuildAfterTurnCtxModule` | `collect_telemetry` / `fanout_ctx` / `dispatch` | `AfterTurnCtx`，**TAP（只读快照）** |
| `turn:telemetry:*` | export | 插件 | `_CollectAfterTurnTelemetrySlotsModule` | 收进 `ctx.extra_metadata` |

> `after_turn` 是全表**唯一有两个 fanout**（`turn:committed` 事件 + `turn:ctx` 快照）和**唯一有 dispatch 副作用**的 phase。`frame.output = frame.input.outbound`——它不造新产物，只是把 after_reasoning 造好的 `OutboundMessage` 原样透出。

---

## 6. 按作用分类的 slot 类型学

把 40+ 个数据 slot 按用途归成 5 类，理解这套分类比背名字有用：

### ① ctx slot —— 阶段的「公文包」
形态 `*:ctx`，每阶段一个，是该阶段插件唯一能直接改的对象。

| slot | 所属 phase |
|---|---|
| `session:ctx` | before_turn |
| `reasoning:ctx` | before_reasoning / after_reasoning |
| `prompt:ctx` | prompt_render |
| `step:ctx` | before_step / after_step |
| `turn:ctx` | after_turn |

它在链里的位置固定：`build_ctx` 造 → `emit`/`fanout` 过总线 → `collect_exports` 收插件的外溢 → `return` 取走。

### ② 中间产物 slot —— 阶段内部的临时数据
只在同一个 phase 内被写与被读，出不了这个 phase。
`session:session`、`session:context_bundle`、`reasoning:persisted_user`、`reasoning:persisted_assistant`、`turn:budget`、`turn:react_stats`、`turn:tool_chain`、`turn:persistence`、`turn:extra`。

### ③ 结果 slot —— 供 `return` 取用的产物
`prompt:result`、`reasoning:outbound`、`turn:committed`（事件载荷，严格说是「下一个模块的输入」）。

### ④ export slot —— **插件唯一的合法写入面**
这就是**插件怎么影响核心**的全部机制：插件往 `frame.slots` 写一个带特定前缀的 key，phase 里的 `collect` 模块用前缀扫描收集，再合并到目标字段。

| 前缀 | 收集进 | 归属 |
|---|---|---|
| `session:extra_hint:` | `ctx.extra_hints`（list[str]） | before_turn |
| `reasoning:extra_hint:` | `ctx.extra_hints` | before_reasoning |
| `prompt:section_top:` | `ctx.system_sections_top` | prompt_render |
| `prompt:section_bottom:` | `ctx.system_sections_bottom` | prompt_render |
| `prompt:extra_hint:` | 独立的 `plugin_hints` 消息 | prompt_render |
| `step:extra_hint:` | `ctx.extra_hints` → 追加进 `messages` | before_step |
| `step:telemetry:` | `ctx.extra_metadata` | after_step |
| `persist:user:` | `Session.add_message("user", **kw)` | after_reasoning |
| `persist:assistant:` | `Session.add_message("assistant", **kw)` | after_reasoning |
| `outbound:metadata:` | `OutboundMessage.metadata` | after_reasoning |
| `outbound:media:` | `OutboundMessage.media` | after_reasoning |
| `turn:extra:` | `turn:extra` → `TurnCommitted.extra` | after_turn |
| `turn:telemetry:` | `ctx.extra_metadata` | after_turn |

两类合并语义（`phase.py:20` / `phase.py:38`）：
- `collect_prefixed_slots()` —— 扫描前缀，去掉前缀后的剩余部分是「字段名」
- `append_string_exports()` —— 值是 str 或 list[str] 就追加，否则 `logger.warning` 忽略（不抛错，插件写错类型不会炸主链）

### ⑤ control slot —— 一发即中的控制位
写入一个**非空字符串**就触发短路，是插件「叫停」的唯一手段：

| slot | 落点字段 | 影响范围 |
|---|---|---|
| `session:abort_reply` | `ctx.abort` / `ctx.abort_reply` | **整个 turn** 短路，回复该字符串 |
| `reasoning:abort_reply` | `ctx.abort` / `ctx.abort_reply` | **整个 turn** 短路 |
| `step:abort_reply` | `ctx.early_stop` / `ctx.early_stop_reply` | **只终止当前 tool loop** |
| `step:early_stop_reason` | `ctx.early_stop_reason` | **只终止当前 tool loop** |

---

## 7. GATE vs TAP —— 决定 slot 能不能被改写

同一个 `*:ctx` slot，在不同 phase 里的可写性不同，取决于它走 GATE 还是 TAP：

| | **GATE**（emit 路径） | **TAP**（fanout 路径） |
|---|---|---|
| 出现于 | before_turn / before_reasoning / prompt_render / before_step / after_reasoning | after_step / after_turn |
| ctx dataclass | **mutable**（`@dataclass`） | **frozen**（`@dataclass(frozen=True)`） |
| emit / fanout 语义 | `emit()` 可**替换** ctx，后续模块拿到新对象 | `fanout()` 只广播，返回值被丢弃 |
| 插件如何影响 | 直接改字段，或 `return` 一个新 ctx | 只能读；要补充就走 **slot export**，由模块 `replace()` 造新实例 |
| 典型 slot | `session:ctx` `reasoning:ctx` `prompt:ctx` `step:ctx` | `step:ctx` `turn:ctx` |

`after_step` 的 `_CollectAfterStepExportSlotsModule` 用 `dataclasses.replace(ctx, ...)` 而不是原地改，正是因为 `AfterStepCtx` 是 frozen——**TAP 阶段的「写」永远是「造新对象」**。

---

## 8. 一页速查总表

| 命名空间 | phase | ctx slot | 结果 slot | export 前缀 | control slot |
|---|---|---|---|---|---|
| `session:` | before_turn | `session:ctx` | — | `session:extra_hint:` | `session:abort_reply` |
| `reasoning:` | before_reasoning | `reasoning:ctx` | — | `reasoning:extra_hint:` | `reasoning:abort_reply` |
| `prompt:` | prompt_render | `prompt:ctx` | `prompt:result` | `prompt:section_top:` / `prompt:section_bottom:` / `prompt:extra_hint:` | — |
| `step:` | before_step | `step:ctx` | — | `step:extra_hint:` | `step:abort_reply` |
| `step:` | after_step | `step:ctx` | — | `step:telemetry:` | `step:early_stop_reason` |
| `reasoning:` `persist:` `outbound:` | after_reasoning | `reasoning:ctx` | `reasoning:outbound` | `persist:user:` / `persist:assistant:` / `outbound:metadata:` / `outbound:media:` | — |
| `turn:` | after_turn | `turn:ctx` | `turn:committed` | `turn:extra:` / `turn:telemetry:` | — |

---

## 9. 五条约定与陷阱

**① 数据 slot 不跨 phase 存活。**
每个 phase 新建 frame、新建空 `slots`。要在阶段间传数据，放进 `ctx` 对象走 `input`/`output`，不要指望 slot。

**② 模块 slot 和数据 slot 靠分隔符区分，别记错。**
`before_turn.emit`（`.`）= 一个模块；`session:ctx`（`:`）= 一份数据。`requires` 里混写时，`.` 参与排序，`:` 只参与校验。

**③ 固定字段受保护，插件不能乱塞。**
- `persist:assistant:` 的 reserved 集：`tools_used` / `tool_chain` / `reasoning_content` / `model_state`
- `persist:user:` 的 reserved 集：`media` / `timestamp` / `client_message_id` / `reply_to_message_id` / `reply_role` / `reply_preview`
- 已退役字段：`persist:assistant:react_compaction` —— 写入会直接 `raise ValueError`（`after_reasoning.py:473`）

**④ 少数 slot 是「无声明写入」。**
`step:telemetry_collected` 被 `run()` 写入但未列入 `produces`。不影响排序（没有模块 `require` 它），但做静态分析时要注意这类例外。

**⑤ 依赖缺失的模块会被静默禁用，但内置模块不会。**
`phase.py:164` 的 `_active_module_slots()`：插件模块若 `require` 了一个不存在的模块 slot，会被 `logger.warning` 后禁用；**内置 slot（7 个 phase 前缀）不受此保护**，缺依赖会直接 `RuntimeError`。写插件时依赖要指向真实存在的模块。

---

## 附：本文档与代码的对应关系

| 本文内容 | 代码位置 |
|---|---|
| `PhaseFrame.slots` / `PhaseModule` / `topo_sort_modules` | `agent/lifecycle/phase.py` |
| `_is_module_slot` 判别规则 | `phase.py:232` |
| `collect_prefixed_slots` / `append_string_exports` | `phase.py:20` / `phase.py:38` |
| 七个 phase 的 slot 常量 | `agent/lifecycle/phases/*.py` 顶部 `_*_SLOT` / `_*_PREFIX` |
| ctx 数据类与 mutable/frozen 标记 | `agent/lifecycle/types.py` |
| 模块链顺序与依赖 | `specs/003-m1b-lifecycle-phases.md` §3 |
