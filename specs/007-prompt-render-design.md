# 007 — prompt_render 阶段设计

> 状态：设计稿（实现同步进行）
> 前置：`before_turn`（✅）、`before_reasoning`（✅）已闭环
> 关联：`LIFECYCLE_ARCHITECTURE.md`（全局架构权威来源）、`003`（7 阶段总览）、`006`（before_reasoning 设计）

---

## 1. 定位与职责

prompt_render 是 7 阶段生命周期的**第三个阶段**，也是 **loop 层（reasoner 内部）的第一个阶段**。
它位于 `before_reasoning` 之后、`before_step` 之前，并且**只跑一次**（在推理循环开始前渲染一次，
之后 before_step / after_step 才循环）。

一句话职责：**把前面攒好的所有信息（历史 + 当前消息 + 技能 + 记忆 + 提示）渲染成一条条 messages，
同时允许插件在渲染前后改 prompt 片段。**

它是 GATE 阶段（ctx 可变），但和前面两个 GATE 阶段有一个**本质区别**：它的 output **不是 ctx**，
而是渲染好的消息列表 `PromptRenderResult`。

---

## 2. 输入 / 输出

```
输入：PromptRenderInput（frozen dataclass）
输出：PromptRenderResult（frozen dataclass，只装 messages）
```

### 输入 `PromptRenderInput`（`types.py:88`）

```python
@dataclass(frozen=True)
class PromptRenderInput:
    session_key: str
    channel: str
    chat_id: str
    content: str
    media: list[str] | None
    timestamp: datetime
    history: list[dict[str, Any]]        # ← 历史消息（推理层第一次真正消费历史）
    skill_names: list[str] | None
    retrieved_memory_block: str
    disabled_sections: set[str]          # ← 哪些 prompt 片段被禁用
    turn_injection_prompt: str
    extra_hints: list[str] | None = None
```

**关键点**：`history` 在这里进场。之前一直说「推理层不直接消费历史」，到 prompt_render 才是历史消息
真正被渲染成 messages 原料的地方。

### 输出 `PromptRenderResult`（`types.py:129`）

```python
@dataclass(frozen=True)
class PromptRenderResult:
    messages: list[dict[str, Any]]       # ← 渲染好的消息，直接给 LLM
```

frozen：渲染结果一旦产出即定型，下游（before_step / LLM）只读不写。

### 中间 `PromptRenderCtx`（`types.py:104`，GATE 可写）

```python
@dataclass
class PromptRenderCtx:
    # 只读事实（从 input 搬来）
    session_key / channel / chat_id / content / media / timestamp
    history / skill_names / retrieved_memory_block / disabled_sections / turn_injection_prompt
    extra_hints: list[str] = []
    # 可写（插件介入点）
    system_sections_top: list[PromptSectionRender] = []      # 插到 system prompt 顶部
    system_sections_bottom: list[PromptSectionRender] = []   # 插到底部
```

插件「改 prompt 片段」就落在这两个可写列表上。

---

## 3. 与 before_reasoning 的承接关系

```
before_reasoning.run() → BeforeReasoningCtx
                              │ 其中的决策字段（skill_names / retrieved_memory_block / extra_hints）
                              ▼
PromptRenderInput(...)  →  prompt_render.run()  →  PromptRenderResult(messages)
```

关键点：**数据槽不跨 phase 存活**，before_reasoning 的 `reasoning:ctx` 槽不会出现在这里。跨阶段靠
before_reasoning 的 output（`BeforeReasoningCtx`）→ reasoner 把它拆开，重组成 `PromptRenderInput`。
这个「拆开重组」发生在 `reasoner.run_turn()` 里（`reasoner.py:100-115`），不属于 prompt_render 阶段本身。

---

## 4. 模块链设计（5 模块）

```
build_ctx → emit → collect_exports → render → return
 (打包 ctx)  (门控)   (回收插件外溢)   (真正渲染)  (产出 result)
```

| # | 模块 slot | requires | produces | 职责 |
|---|---|---|---|---|
| 1 | `prompt_render.build_ctx` | — | `prompt:ctx` | 从 input 组装 PromptRenderCtx |
| 2 | `prompt_render.emit` | `prompt_render.build_ctx`, `prompt:ctx` | `prompt:ctx` | `bus.emit(ctx)` 门控 |
| 3 | `prompt_render.collect_exports` | `prompt_render.emit`, `prompt:ctx` | `prompt:ctx` | 回收 `prompt:section_top:` / `prompt:section_bottom:` / `prompt:extra_hint:` |
| 4 | `prompt_render.render` | `prompt_render.collect_exports`, `prompt:ctx` | `prompt:result` | 调 `ContextBuilder.render` 产出 messages |
| 5 | `prompt_render.return` | `prompt_render.render`, `prompt:result` | — | 设 `frame.output = PromptRenderResult` |

命名空间是 `prompt:`（不是 `prompt_render:`），理由同前——命名空间表达语义域，不表达生产阶段。

---

## 5. 与前面 GATE 阶段的关键差异

### 5.1 多了一个「真正产出」的 `render` 模块

before_turn / before_reasoning 的 output 就是 ctx 本身，所以它们的链是：

```
... → emit → collect_exports → return   （return 直接把 ctx 设为 output）
```

而 prompt_render 的 output 是 **render 出来的 messages**，所以多了一个 `render` 模块，且**排在
collect_exports 之后、return 之前**：

```
build_ctx → emit → collect_exports → render → return
                                      └──── 用「收好的完整 ctx」渲染
```

`render` 必须在 collect_exports 之后，因为插件外溢的 `system_sections_top/bottom`、`extra_hints`
要**先被收回 ctx**，render 才能用完整信息渲染。

### 5.2 插件的两个介入点（比前两个阶段多一个）

| 介入点 | 通道 | 落在哪 |
|---|---|---|
| emit 改字段 | `bus.emit` 的 handler 直接改 `ctx.system_sections_top/bottom` | 渲染前 |
| 外溢回收 | 插件往 slot 写 `prompt:section_top:` / `prompt:section_bottom:` / `prompt:extra_hint:` | collect_exports 收回 |

`prompt:section_top:` / `prompt:section_bottom:` 是 prompt_render 独有的前缀（前两个阶段没有），
因为「改 prompt 片段」是这个阶段的专属职责。

### 5.3 GATE 骨架同构性

`emit → collect_exports → return` 骨架仍与前两个阶段一致，只是：
- 命名空间 `session:`/`reasoning:` → `prompt:`；
- ctx 类型 → `PromptRenderCtx`；
- 回收前缀多了 `section_top` / `section_bottom`；
- `return` 产出的是 `PromptRenderResult`（而非 ctx）。

---

## 6. 砍掉的能力

原版 prompt_render 依赖完整的 `ContextBuilder.render`（多 section 组装 + 检索注入 + 缓存），
M1b 把 `ContextBuilder.render` 降为桩：只拼 `history` + `current_message`，忽略 system sections。
这是「基本功能」判据下的合理裁剪——真正的 prompt 组装逻辑（section 缓存、检索注入、token 估算）
属于 M3，不属于「让一次 turn 端到端跑通」的最小集合。

保留的「基本功能」：渲染 messages 的结构 + 插件可改 prompt 片段（`system_sections_top/bottom`
字段和 section 前缀外溢通道都在，只是桩 render 暂时不消费它们）。

---

## 7. 关键设计决策

### 决策 A：为什么 output 用 frozen 的 `PromptRenderResult` 而非 mutable ctx？

渲染结果是一次性产物，下游（before_step / LLM）只读。frozen 物理约束「不许改」，避免下游误改
渲染结果。而 ctx（`PromptRenderCtx`）是 GATE 门控对象，保持 mutable 供插件改写。

### 决策 B：为什么 `render` 排在 `collect_exports` 之后？

render 需要「门控后 + 回收后」的完整 ctx（system_sections + extra_hints 都齐了）才能渲染。这是
prompt_render 与 before_turn/before_reasoning 唯一的链序差异。

### 决策 C：为什么 section 回收要区分 `PromptSectionRender` 和 `str`？

插件外溢 section 时，可能是结构化对象（`PromptSectionRender`）也可能是裸字符串。`_append_sections`
统一处理：对象直接用，字符串包装成 `PromptSectionRender(name=..., content=..., is_static=False)`。
这样 collect_exports 对插件友好（两种写法都收）。

### 决策 D：extra_hints 为什么不直接塞 messages，而要包成 hint message？

`build_context_hint_message` 把 hints 拼成一个 `{"role": "user", "content": "[plugin_hints]\n..."}`
的独立消息追加到末尾。这样 hints 有明确边界标记（`[plugin_hints]`），LLM 能区分「这是系统提示」
而非「用户正文」。

---

## 8. 实现现状与待办

- `prompt_render.py` 已存在 5 模块轻量版（M1b），逻辑完整、结构正确。
- 本次「重建」的工作是：**补模块 docstring（统一风格）→ 补独立测试 → 更新架构文档状态**，
  逻辑保持不变（现有实现已符合「基本功能」判据，无需裁剪模块）。

---

## 9. 测试策略

| 测试文件 | 验证点 |
|---|---|
| `test_prompt_render_return.py` | 5 模块链端到端：build_ctx 搬运 + emit 替换 + collect 回收 section/hint + render 产出 messages + return 产出 result |
