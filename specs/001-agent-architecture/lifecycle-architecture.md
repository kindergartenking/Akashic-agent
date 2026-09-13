# Akashic Agent 多阶段生命周期架构设计

> 本文是 `001-agent-architecture` 的生命周期专项设计。目标是复刻原版 Akashic Agent 的阶段语义，同时适配本项目的约束：Web 与 QQ 共用一个主 Agent、单 session 单未完成 turn、禁止派生 Agent、手机端不在范围内。

## 1. 目标与边界

### 目标

- 保留原版 7 个 turn 阶段及其执行顺序。
- 保留 `PhaseFrame`、`slot/requires/produces`、拓扑排序和插件扩展能力。
- 明确 GATE（可改变后续控制流）与 TAP（只观察）的边界。
- 将工具调用保留在 Reasoner 的 ReAct 循环中；工具 Hook 不伪装成生命周期 Phase。
- 支持运行时 snapshot/generation 切换插件集合，但不允许改变核心阶段顺序。
- 让 Web、QQ 进入同一 `TurnState`，并共享持久化、工具策略、事件与出站流程。

## 1.1 去除记忆与工具后的最小 Runtime 骨架

本节定义复刻原版 runtime 时应先实现的“骨架层”。它保留生命周期编排、单 session admission、LLM provider、取消、提交和出站端口；记忆召回、工具执行、技能系统、主动任务和具体渠道协议都不属于骨架的必需能力。

### 骨架的职责边界

```text
Web/QQ Adapter
    -> InboundPort
    -> SessionAdmission
    -> RuntimeSnapshot
    -> TurnRuntime
         BeforeTurn
         -> BeforeReasoning
         -> Reasoner
              PromptRender
              -> BeforeStep
              -> Provider.chat/stream
              -> AfterStep
         -> AfterReasoning
         -> AfterTurn
    -> OutboundPort
```

骨架只回答四个问题：

1. 一条输入如何获得唯一的 `turn_id` 并进入 session。
2. 一个 turn 如何按固定顺序经过阶段，并在 abort、取消或异常时结束。
3. provider 的最终输出如何变成 canonical assistant reply。
4. reply 如何在提交后投影到 Web 或 QQ。

以下能力在骨架中只保留端口，不实现业务逻辑：

| 能力 | 骨架中的位置 | 默认行为 |
|---|---|---|
| MemoryPort | `BeforeTurn` 的可选输入 | 返回空 history augmentation |
| ToolPort | `Reasoner` 的可选依赖 | 返回空 schema；provider 不产生 tool call |
| ChannelAdapter | runtime 外部 | 只负责 `InboundMessage`/`OutboundMessage` 转换 |
| PluginRegistry | `RuntimeSnapshot` | 仅允许阶段 TAP/GATE 注册，不改变阶段顺序 |
| PersistencePort | `AfterReasoning` | 写入 user/assistant；可先使用内存实现 |

### 最小运行时接口

```python
class Provider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelDelta]: ...

class SessionPort(Protocol):
    async def get_or_create(self, key: str) -> Session: ...
    async def append_user(self, turn: TurnInput) -> None: ...
    async def append_assistant(self, reply: AssistantReply) -> None: ...

class OutboundPort(Protocol):
    async def dispatch(self, message: OutboundMessage) -> None: ...

class Runtime:
    async def run(self, request: TurnRequest) -> TurnResult: ...
```

`Runtime.run` 是唯一的主入口。Web 与 QQ 不直接调用 Reasoner，也不直接写 session；它们都把请求转换为 `TurnRequest` 后调用该入口。

### 最小数据流

```python
@dataclass(frozen=True)
class TurnRequest:
    request_id: str
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

@dataclass
class RuntimeTurn:
    request: TurnRequest
    turn_id: str
    session: Session | None = None
    prompt: list[dict[str, Any]] = field(default_factory=list)
    provider_response: ModelResponse | None = None
    reply: AssistantReply | None = None
    abort: bool = False
    cancel_scope: CancelScope | None = None

@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    disposition: str                 # committed/aborted/cancelled/failed
    outbound: OutboundMessage | None
    error: str | None = None
```

### 最小阶段骨架

```python
class TurnRuntime:
    async def run(self, request: TurnRequest) -> TurnResult:
        async with self.admission.acquire(request.session_key) as lease:
            turn = RuntimeTurn(request=request, turn_id=new_turn_id())
            snapshot = await self.snapshots.current()
            try:
                await self.before_turn.run(turn, snapshot)
                if turn.abort:
                    return await self.finish_short_circuit(turn)

                await self.before_reasoning.run(turn, snapshot)
                if turn.abort:
                    return await self.finish_short_circuit(turn)

                turn.provider_response = await self.reasoner.run(turn, snapshot)
                await self.after_reasoning.run(turn, snapshot)
                result = await self.after_turn.run(turn, snapshot)
                return result
            except CancelledError:
                await self.cancel.run(turn)
                return TurnResult(turn.turn_id, "cancelled", None)
            except Exception as exc:
                await self.failure.run(turn, exc)
                return TurnResult(turn.turn_id, "failed", None, str(exc))
```

该骨架中 `Reasoner` 只有一次 provider 请求时，内部仍建议保留 `PromptRender -> BeforeStep -> provider -> AfterStep` 的调用边界。未来加入工具时，只需在 provider 与 AfterStep 之间增加 ReAct iteration，不需要改变 `TurnRuntime` 的外层协议。

### 剥离版 Reasoner

```python
class MinimalReasoner:
    async def run(self, turn: RuntimeTurn, snapshot: RuntimeSnapshot) -> ModelResponse:
        render = await self.prompt_render.run(
            PromptRenderInput.from_turn(turn, history=turn.session.history())
        )
        step = await self.before_step.run(BeforeStepInput.from_render(turn, render))
        if step.early_stop:
            return ModelResponse(content=step.early_stop_reply)
        response = await self.provider.complete(
            ModelRequest(messages=render.messages, tools=())
        )
        await self.after_step.run(AfterStepCtx.from_response(turn, response))
        return response
```

这里 `tools=()` 是显式空集合，而不是 `None`。这样 provider、日志和测试都能区分“未配置工具”和“运行时漏传工具配置”。

### 骨架必须保持的顺序

```text
admission acquire
 -> snapshot capture
 -> BeforeTurn
 -> BeforeReasoning
 -> PromptRender
 -> BeforeStep
 -> Provider
 -> AfterStep
 -> AfterReasoning
      persist user/assistant
      build canonical outbound
 -> AfterTurn
      commit event
      dispatch outbound
 -> admission release
```

`TurnCommitted` 是提交边界；provider 输出完成不代表 turn 已提交。任何记忆写回、工具审计、渠道重试等后续能力都应挂在提交边界之后，不能反向改变该骨架的顺序。

### 与完整实现的映射

| 最小骨架 | 完整实现替换点 |
|---|---|
| `BeforeTurn` | 增加 memory exclusion、双路召回、history augmentation |
| `BeforeReasoning` | 增加工具上下文、技能与策略提示 |
| `PromptRender` | 增加 memory block、tool protocol、system sections |
| `Provider` | 增加流式事件、重试、模型路由和 usage |
| `AfterStep` | 增加 tool call 观察和多轮迭代 |
| `AfterReasoning` | 增加 tool chain、control turn、媒体和 metadata |
| `AfterTurn` | 增加 TurnIngested、异步 memory worker、渠道投影 |

因此，复刻顺序应先让最小骨架在无记忆、无工具条件下完成一个完整 turn，再逐项接入扩展端口。不要让 memory 或 tool 的数据结构成为 runtime 主状态，否则剥离功能时会破坏阶段协议。

### 非目标

- 不实现手机端适配。
- 不实现 `spawn`、`spawn_manage` 或任何子 Agent 执行上下文。
- 不把 `after_step` 的观察事件改造成可重排的控制阶段。
- 不在主 turn 链路同步等待 post-response memory worker。

## 2. 总体执行图

```text
ChannelAdapter(Web/QQ)
        │  ChannelEnvelope → InboundMessage
        ▼
SessionAdmission（幂等 + 单 session 未完成 turn 锁）
        ▼
┌──────────────────────────────────────────────────────────────┐
│ PassiveTurnPipeline                                          │
│                                                              │
│  1 BeforeTurn (GATE)                                         │
│        ├─ session / history / memory recall                 │
│        └─ abort ⇒ short-circuit                              │
│  2 BeforeReasoning (GATE)                                    │
│        ├─ tool context / hints                               │
│        └─ abort ⇒ short-circuit                              │
│  3 Reasoner.run_turn                                         │
│        ├─ PromptRender (GATE, each logical provider call)    │
│        └─ iteration loop                                     │
│             ├─ BeforeStep (GATE)                             │
│             ├─ provider call                                 │
│             ├─ ToolExecutor + ToolHook（非 Phase）            │
│             └─ AfterStep (TAP, has_more true/false)           │
│  4 AfterReasoning (GATE)                                     │
│        ├─ parse / persist user+assistant / build outbound    │
│        └─ produce TurnSnapshot                               │
│  5 AfterTurn (TAP + commit boundary)                         │
│        ├─ TurnCommitted fanout                               │
│        ├─ AfterTurnCtx fanout                                │
│        ├─ outbound dispatch                                  │
│        └─ return OutboundMessage                             │
└──────────────────────────────────────────────────────────────┘
        ▼
Web/QQ OutboundPort；TurnCommitted → 异步记忆写回
```

核心阶段顺序是代码定义的稳定顺序，runtime snapshot 只能替换模块集合，不能重排阶段。

## 3. 生命周期抽象

### 3.1 PhaseFrame 与 PhaseModule

```python
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

I = TypeVar("I")
O = TypeVar("O")
F = TypeVar("F", bound="PhaseFrame[Any, Any]")

@dataclass
class PhaseFrame(Generic[I, O]):
    input: I
    slots: dict[str, Any] = field(default_factory=dict)
    output: O | None = None

class PhaseModule(Protocol[F]):
    slot: str
    requires: tuple[str, ...]
    produces: tuple[str, ...]

    async def run(self, frame: F) -> F: ...
```

`slots` 是阶段内部的数据流，不是跨 turn 的全局状态。模块只能通过声明的 slot 读写协作；禁止依赖隐式模块执行顺序。

### 3.2 Phase 执行器

```python
class Phase(Generic[I, O, F]):
    def __init__(
        self,
        modules: Sequence[PhaseModule[F]],
        *,
        frame_factory: Callable[[I], F],
        phase_name: str,
    ) -> None: ...

    async def run(self, input: I) -> O:
        frame = self._frame_factory(input)
        for module in self._modules:       # 已完成拓扑排序
            frame = await module.run(frame)
        if frame.output is None:
            raise PhaseContractError(self.phase_name, "missing_output")
        return frame.output
```

初始化时必须：

1. 校验 slot 唯一性。
2. 禁用缺失插件依赖；内建模块缺依赖直接失败。
3. 校验 module dependency、slot dependency、cycle。
4. 用稳定拓扑排序：无依赖的模块按注册顺序执行，内建模块优先于插件模块。
5. 记录可诊断的 dependency tree 和 generation。

### 3.3 GATE 与 TAP

```python
GateHandler = Callable[[Ctx], Awaitable[Ctx | None] | Ctx | None]
TapHandler = Callable[[FrozenCtx], Awaitable[None] | None]
```

- **GATE**：输入 context 可变；handler 返回新 context 或 `None`（保持当前 context）。`abort`/`early_stop` 会改变控制流。
- **TAP**：接收 frozen snapshot；只做观测、指标、审计和异步触发，不得改变主链路结果。
- `AfterReasoning` 虽处于 after 阶段，但仍是 GATE，因为插件可以修改最终 reply/media/outbound metadata。
- `AfterStep`、`AfterTurn` 的 bus fanout 是 TAP；需要补 metadata 时必须通过 phase module 在 fanout 前后显式收集，不能依赖 handler 修改对象。

## 4. 核心数据结构

以下定义与 `backend/agent/lifecycle/types.py` 兼容；迁移期间不得删除已有字段或改变可写范围。

### 4.1 TurnState 与持久化策略

```python
@dataclass
class TurnPersistencePolicy:
    persist_user: bool = True
    persist_assistant: bool = True

@dataclass
class TurnState:
    msg: InboundMessage
    session_key: str
    dispatch_outbound: bool
    session: SessionLike | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)
    persistence: TurnPersistencePolicy = field(default_factory=TurnPersistencePolicy)
```

`TurnState` 是本轮唯一可变生命周期状态。`session` 在 BeforeTurn 获取；`dispatch_outbound=False` 用于内部/控制回合，不能因此跳过 canonical 持久化规则。

### 4.2 BeforeTurnCtx

```python
@dataclass
class BeforeTurnCtx:
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime
    retrieved_memory_block: str
    retrieval_trace_raw: object | None
    history_messages: tuple[Any, ...]
    skill_names: list[str] = field(default_factory=list)
    abort: bool = False
    abort_reply: str = ""
    extra_hints: list[str] = field(default_factory=list)
    extra_metadata: dict[str, Any] = field(default_factory=dict)
```

职责：获取/创建 session、应用 memory exclusion、准备 history 和 memory recall、收集技能与提示。`abort=True` 时由 orchestrator 生成短路结果，不进入 Reasoner。

### 4.3 BeforeReasoningCtx

```python
@dataclass
class BeforeReasoningCtx:
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime
    skill_names: list[str]
    retrieved_memory_block: str
    extra_hints: list[str] = field(default_factory=list)
    abort: bool = False
    abort_reply: str = ""
```

职责：同步 ToolRegistry 上下文（session/channel/chat/turn）、执行 prompt warmup、允许策略插件在推理前拒绝或补充提示。

### 4.4 PromptRenderCtx / Result

```python
@dataclass
class PromptRenderCtx:
    session_key: str; channel: str; chat_id: str; content: str
    media: list[str] | None; timestamp: datetime
    history: list[dict[str, Any]]
    skill_names: list[str] | None
    retrieved_memory_block: str
    disabled_sections: set[str]
    turn_injection_prompt: str
    extra_hints: list[str] = field(default_factory=list)
    system_sections_top: list[PromptSectionRender] = field(default_factory=list)
    system_sections_bottom: list[PromptSectionRender] = field(default_factory=list)

@dataclass(frozen=True)
class PromptRenderResult:
    messages: list[dict[str, Any]]
```

渲染顺序必须是：固定 system/developer → 有界 history → memory block → 当前 user → tool protocol。所有 section/hint 注入必须经过 token budget 检查。

### 4.5 BeforeStepCtx / AfterStepCtx

```python
@dataclass
class BeforeStepCtx:
    session_key: str; channel: str; chat_id: str
    iteration: int
    input_tokens_estimate: int
    visible_tool_names: frozenset[str] | None
    extra_hints: list[str] = field(default_factory=list)
    early_stop: bool = False
    early_stop_reply: str = ""

@dataclass(frozen=True)
class AfterStepCtx:
    session_key: str; channel: str; chat_id: str
    iteration: int
    context_tokens_estimate: int
    tools_called: tuple[str, ...]
    partial_reply: str
    tools_used_so_far: tuple[str, ...]
    tool_chain_partial: tuple[dict[str, Any], ...]
    partial_thinking: str | None
    has_more: bool
    early_stop: bool = False
    early_stop_reason: str = ""
    extra_metadata: dict[str, Any] = field(default_factory=dict)
```

每个 Reasoner iteration 都执行 BeforeStep。工具分支的 AfterStep 使用 `has_more=True`，最终回复分支使用 `has_more=False`。`early_stop` 只终止当前 Reasoner loop，不等同于 BeforeTurn 的整轮 abort。

### 4.6 AfterReasoningCtx / TurnSnapshot

```python
@dataclass
class AfterReasoningCtx:
    session_key: str; channel: str; chat_id: str
    tools_used: tuple[str, ...]
    thinking: str | None
    response_metadata: ResponseMetadata
    streamed: bool
    tool_chain: tuple[dict[str, Any], ...]
    context_retry: dict[str, object]
    reply: str
    media: list[str] = field(default_factory=list)
    meme_tag: str | None = None
    outbound_metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class TurnSnapshot:
    state: TurnState
    outbound: OutboundMessage
    ctx: AfterReasoningCtx
```

AfterReasoning 的内建顺序：parse response → emit GATE → persist user → persist assistant → update session metadata → append canonical messages → build outbound → return snapshot。持久化失败必须抛出并记录，不得伪造成功出站。

### 4.7 AfterTurnCtx 与工具上下文

```python
@dataclass(frozen=True)
class AfterTurnCtx:
    session_key: str; channel: str; chat_id: str
    reply: str
    tools_used: tuple[str, ...]
    thinking: str | None
    will_dispatch: bool
    extra_metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class BeforeToolCallCtx:
    session_key: str; channel: str; chat_id: str
    tool_name: str; arguments: dict[str, Any]

@dataclass(frozen=True)
class AfterToolResultCtx:
    session_key: str; channel: str; chat_id: str
    tool_name: str; arguments: dict[str, Any]
    result: str; status: str

@dataclass
class PreToolCtx:
    session_key: str; channel: str; chat_id: str
    tool_name: str; arguments: dict[str, Any]
    call_id: str = ""
    source: str = ""
    request_text: str = ""
    tool_batch: tuple[dict[str, Any], ...] = ()
    tool_batch_index: int = 0
```

`PreToolCtx` 是唯一允许 Hook 改写 arguments 或 deny 的上下文；工具 registry 仍是唯一真实执行入口。`BeforeToolCallCtx`/`AfterToolResultCtx` 是生命周期观测事件，不替代 ToolHook。

## 5. 各阶段模块图与基类建议

每个阶段使用独立的 frame 类型，避免把不同阶段的 slot 混用：

```python
@dataclass
class BeforeTurnFrame(PhaseFrame[TurnState, BeforeTurnCtx]): ...
@dataclass
class BeforeReasoningFrame(PhaseFrame[BeforeReasoningInput, BeforeReasoningCtx]): ...
@dataclass
class PromptRenderFrame(PhaseFrame[PromptRenderInput, PromptRenderResult]): ...
@dataclass
class BeforeStepFrame(PhaseFrame[BeforeStepInput, BeforeStepCtx]): ...
@dataclass
class AfterStepFrame(PhaseFrame[AfterStepCtx, AfterStepCtx]): ...
@dataclass
class AfterReasoningFrame(PhaseFrame[AfterReasoningInput, TurnSnapshot]): ...
@dataclass
class AfterTurnFrame(PhaseFrame[TurnSnapshot, OutboundMessage]): ...
```

推荐的内建 slot 顺序如下（插件只能插入或依赖，不得删除内建闭环）：

| 阶段 | 内建 slot 链 | 关键职责 |
|---|---|---|
| BeforeTurn | `acquire_session → memory_exclusion → prepare_context → build_ctx → emit → collect_exports → return` | session、history、memory、abort |
| BeforeReasoning | `sync_tools → build_ctx → emit → collect_exports → warmup → return` | 工具上下文、推理前 gate |
| PromptRender | `build_ctx → emit → collect_exports → render → return` | prompt section/hint 与 token 预算 |
| BeforeStep | `build_ctx → emit → collect_exports → inject_hints → return` | 每轮输入估算、early stop |
| AfterStep | `copy_input → collect_pre → fanout → collect_post → return` | frozen telemetry、补充 metadata |
| AfterReasoning | `build_ctx → emit → persist_user → persist_asst → update_meta → append_messages → build_outbound → return` | canonical 写入与 outbound 构造 |
| AfterTurn | `build_work → collect_extras → build_committed → fanout_committed → log_budget → build_ctx → collect_telemetry → fanout_ctx → dispatch → return` | 提交事件、观测、出站 |

插件模块示例：

```python
class AddSafetyHint:
    slot = "before_step.safety_hint"
    requires = ("before_step.emit", "step:ctx")
    produces = ("step:extra_hint:safety",)

    async def run(self, frame: BeforeStepFrame) -> BeforeStepFrame:
        frame.slots["step:extra_hint:safety"] = "仅使用允许的工具。"
        return frame
```

插件缺失自身依赖时应被禁用并记录 warning；插件依赖内建 slot 但内建闭环不满足时，阶段构建直接失败。

## 6. Reasoner 与工具执行边界

Reasoner 的单轮循环固定为：

```text
for iteration in range(max_iterations):
    before_step = await before_step_phase.run(input)
    if before_step.early_stop: summarize_and_return()
    response = await provider.chat(...)
    if response.tool_calls:
        for call in response.tool_calls:
            pre hooks: matches → validate/改参/deny
            registry invoke: timeout + allowlist + output limit
            post hooks: success/error observation
        after_step(has_more=True)
    else:
        emit TurnOutputCompleted
        after_step(has_more=False)
        return TurnRunResult
```

`spawn` 和 `spawn_manage` 在 ToolPolicy 层始终返回 `denied/unsupported`，不得创建新的 `TurnState`、session lock 或 Agent runtime。

## 7. Runtime Snapshot 与 generation

```python
@dataclass(frozen=True)
class LifecycleSnapshot:
    generation: int
    phase_modules: Mapping[str, tuple[object, ...]]
    tool_hook_generation: int
    created_at: datetime

class LifecycleRuntime:
    async def snapshot(self) -> LifecycleSnapshot: ...
    async def rebuild(self, registry: PluginRegistry) -> LifecycleSnapshot: ...
```

- 每个 turn 开始时捕获一个 snapshot；该 turn 全程使用同一 generation。
- 插件变更只影响后续 turn；不得中途替换当前 phase bundle。
- `PassiveTurnPipeline` 与 `DefaultReasoner` 缓存按 generation 构建的 phase bundle。
- generation、阶段名、slot、耗时、异常类型必须写入结构化诊断事件。

## 8. 错误、取消与提交顺序

统一错误模型：

```python
class PhaseError(RuntimeError):
    phase: str
    slot: str | None
    generation: int
    turn_id: str | None
    retryable: bool
```

规则：

1. BeforeTurn/BeforeReasoning abort 是合法控制结果，不是异常。
2. phase module 异常必须包装为 `PhaseError`，保留原异常和 slot。
3. provider/tool 错误由 Reasoner 形成可识别失败结果；不得写入伪造 assistant 成功消息。
4. AfterReasoning 持久化 user/assistant 或 append 失败，停止 dispatch 并释放 session lock。
5. AfterTurn 的 `TurnCommitted` fanout 在 outbound dispatch **之前**；fanout 异常默认阻止 dispatch，除非项目明确配置 fail-open。
6. QQ/Web 出站失败不回滚 canonical 数据；写入 `delivery_failed` 事件并允许重试，重试不得再次执行工具。
7. 取消必须可安全释放锁；断线不能触发第二次 Reasoner。

## 9. Web/QQ 与并发约束

两个渠道都只做：协议解析、身份映射、mention 过滤、去重和出站投影。核心只接受标准 `InboundMessage`。

```python
async with session_admission.lock(session_key) as lease:
    if lease.busy:
        return BusyTurn(session_key=session_key, request_id=request_id)
    return await passive_turn.run(state, snapshot=runtime.snapshot())
```

QQ 群聊未 @ 机器人时在 adapter 层丢弃；私聊和 @ 消息映射到同一 `session_key` 规则。Web 与 QQ 共享 ToolRegistry、MemoryRuntime、Phase bundle 和 canonical session。

## 10. 可观测事件顺序

最小事件集：`TurnStarted`、`StreamDeltaReady`、`ToolCallStarted`、`ToolCallCompleted`、`TurnOutputCompleted`、`TurnCommitted`、`ProactiveFinished`、`DriftFinished`。

每个事件必须包含 `session_id/session_key`、`turn_id`、`channel`、`sequence`、`timestamp`、`phase`（适用时）、`generation`（适用时）。工具结果和错误 preview 必须脱敏、截断。

权威顺序：

```text
assistant canonical append
 → TurnCommitted fanout
 → AfterTurnCtx fanout
 → outbound dispatch
 → async TurnIngested / post_response memory worker
```

## 11. 测试策略与验收

所有测试放在根目录 `test/`：

- `test_lifecycle_phase.py`：重复 slot、缺依赖、缺 slot、环依赖、稳定拓扑序、generation。
- `test_lifecycle_gate_tap.py`：GATE 替换 context、`None` 语义、TAP 不改变控制流。
- `test_reasoner_lifecycle.py`：无工具、单工具、多轮工具、BeforeStep early stop、max iteration。
- `test_persistence_order.py`：AfterReasoning 写入顺序、TurnCommitted 先于 outbound、出站失败不重复工具。
- `test_channels_lifecycle.py`：Web/QQ 映射同一 turn；QQ 群聊 mention 过滤；重复 request 幂等。
- `test_tool_policy.py`：spawn 拒绝、参数校验、超时、输出裁剪和 secret redaction。
- `test_snapshot_hot_reload.py`：旧 turn 固定旧 generation，新 turn 使用新 bundle。

最小验收命令：

```powershell
pytest -q test/test_lifecycle_phase.py test/test_lifecycle_gate_tap.py
pytest -q test/test_reasoner_lifecycle.py test/test_persistence_order.py
pytest -q test/test_channels_lifecycle.py test/test_tool_policy.py
```

## 12. 迁移顺序

1. 保持当前 `backend/agent/lifecycle/{phase,types,facade}.py` API 不变，补齐 7 个 phase implementation。
2. 将 `PassiveTurnPipeline` 的 before/after 调用接入统一 `PhaseBundle`，并让 Reasoner 使用 PromptRender/BeforeStep/AfterStep。
3. 接入 ToolExecutor 的 `PreToolCtx` 与生命周期 ToolCall 事件，确保 spawn policy 在 registry 层拒绝。
4. 增加 runtime snapshot/generation 和插件重建缓存。
5. 接入 Web/QQ 的共同 `SessionAdmission` 与 OutboundPort，补齐事件顺序测试。
6. 最后接入 TurnCommitted → TurnIngested 的异步记忆写回；memory worker 不阻塞主回复。

## 13. 与原版能力差异记录

| 能力 | 本项目策略 |
|---|---|
| 7 个 turn phase | 复制：保留阶段、上下文和 slot 依赖语义 |
| Phase 拓扑排序 | 复制并保留稳定顺序、缺依赖禁用/失败规则 |
| ToolHook | 复制边界，但独立于 Phase |
| Runtime snapshot | 复制 generation 思路，首期只允许 turn 边界切换 |
| TurnCommitted / memory worker | 复制提交先于异步记忆写回 |
| Web/QQ | 适配器统一进入同一 pipeline |
| Mobile | 明确不实现 |
| spawn/spawn_manage | 明确拒绝，不创建子 Agent |
| 原版全部插件/主动任务 | 延后；必须通过 unsupported 或 feature flag 暴露，不伪装完整复刻 |
