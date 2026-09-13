# Feature Specification: Akashic Agent Core Architecture

**Feature Branch**: `001-agent-architecture`

**Created**: 2026-09-06

**Status**: Draft

**Input**: User description: "重新设计 Akashic Agent 复刻项目架构：保留 Web 与 QQ 端，移除手机端适配，单主 Agent 不允许派生子 Agent，复用统一工具能力与会话记忆召回，并兼容现有前端接口契约。"

## Goal and Scope

本规格定义 Akashic Agent 复刻项目的首个可交付架构边界。系统为同一个主 Agent 提供 Web 和 QQ 两种入口，使用统一的会话、回合、工具、模型和记忆能力。手机端适配、子 Agent 派生和原项目未验证的高级子系统不属于本阶段交付。

## User Scenarios & Testing

### User Story 1 - Web 对话回合 (Priority: P1)

作为 Web 用户，我希望在已有聊天页面中发送消息并获得流式回答，同时看到工具调用状态和最终结果，从而完成一次可追踪的 Agent 任务。

**Why this priority**: Web 是现有可运行入口，也是验证主 Agent、工具和持久化链路的最小闭环。

**Independent Test**: 启动服务后通过 Web 页面创建或打开 session，发送一条文本消息，验证收到开始、工具轨迹（如有）、增量回答和最终消息，并刷新页面后仍能看到该回合。

**Acceptance Scenarios**:

1. **Given** 一个可用 session，**When** 用户发送文本消息，**Then** 系统创建唯一 turn，返回流式状态并最终持久化 user/assistant 消息。
2. **Given** Agent 需要调用工具，**When** 工具执行开始和结束，**Then** Web 客户端能按既有事件契约显示工具名称、状态和结果摘要，但不显示原始内部思考文本。
3. **Given** 模型或工具失败，**When** 回合结束，**Then** 用户看到明确的失败状态，源消息和失败原因可追踪，系统不伪造成功回答。

### User Story 2 - QQ 私聊与群聊入口 (Priority: P1)

作为 QQ 用户，我希望在私聊中直接对话、在群聊中 @机器人 后获得回答，并与 Web 使用同一份 session 记忆和工具能力。

**Why this priority**: QQ 是必须复现的主流通信入口，且要求验证跨渠道的统一 Agent 行为。

**Independent Test**: 使用 QQ 渠道适配器注入私聊和群聊事件，验证私聊直接触发、群聊仅在 @机器人 时触发；检查回复、session 归属、错误和去重行为。

**Acceptance Scenarios**:

1. **Given** 一条合法 QQ 私聊消息，**When** 渠道接收消息，**Then** 它被映射为标准 turn 并向原会话发送回答。
2. **Given** 一条群聊消息，**When** 消息未 @机器人，**Then** 系统忽略它且不创建 turn；当消息 @机器人 时，系统去除 mention 后处理正文。
3. **Given** QQ 渠道暂时不可用，**When** Agent 已完成或失败，**Then** 核心回合状态仍被持久化，并记录可重试的出站失败，不影响其他渠道。

### User Story 3 - 受上下文预算约束的记忆召回 (Priority: P1)

作为用户，我希望 Agent 在每轮回答前召回相关历史 turn，并能知道召回内容来自哪里，同时不会因为记忆注入超过模型上下文限制。

**Why this priority**: 记忆是 Akashic Agent 的核心差异化能力，且必须在有限上下文中稳定工作。

**Independent Test**: 准备包含 dense 与 BM25 可检索历史的固定 fixture，执行一次召回，验证排序、稀疏化、扩散、截断、溯源字段和 token 预算；再用空库、全零分数和超预算数据验证降级。

**Acceptance Scenarios**:

1. **Given** 历史 turn 同时存在 dense 与 BM25 分数，**When** 执行召回，**Then** 系统按规定的两路检索、`_tail_surprisal`、Sparsemax、continuation、图扩散、盆地池化、head 独立扩散、Entmax 尾部竞争顺序生成结果。
2. **Given** 当前上下文剩余 token 预算不足，**When** 组装 prompt，**Then** 系统按确定性优先级裁剪召回项并保留最小可用 prompt，不超过预算。
3. **Given** 任一召回项，**When** 将其注入 prompt 或展示诊断信息，**Then** 结果包含 turn/session、源消息、入口 lane、各阶段分数、图路径、continuation、选择阶段和 token 成本等可溯源信息。
4. **Given** 记忆索引损坏或 embedding 服务不可用，**When** 处理普通回合，**Then** 回合仍可在无记忆模式下完成，并发出结构化降级事件。

### User Story 4 - 单主 Agent 与受控工具执行 (Priority: P1)

作为系统维护者，我希望所有渠道调用同一个主 Agent，工具权限集中可控且禁止派生子 Agent，从而保证行为一致、权限边界清晰和资源可预测。

**Why this priority**: 统一执行面是 Web/QQ 复用和安全控制的基础。

**Independent Test**: 对同一 session 分别从 Web 和 QQ 发送任务，验证使用同一工具注册表；让模型请求 `spawn`/`spawn_manage`，确认调用被拒绝并记录审计事件；让 shell、文件、网络工具执行允许和拒绝案例。

**Acceptance Scenarios**:

1. **Given** 任一入口提交回合，**When** Agent 选择工具，**Then** 工具经过统一注册、参数校验、超时和结果裁剪后执行。
2. **Given** Agent 请求派生或管理子 Agent，**When** 权限策略评估，**Then** 请求被明确拒绝，主 Agent 可继续给出解释或最终答案。
3. **Given** 两个请求针对同一 session，**When** 前一个 turn 尚未结束，**Then** 后一个 turn 不被并发执行，返回可识别的 busy/idempotency 结果。
4. **Given** shell、文件或网络工具，**When** 参数超出白名单、超时或失败，**Then** 工具返回受控错误且不泄露凭据或任意文件内容。

### User Story 5 - 事实源、配置和可观测性 (Priority: P2)

作为维护者，我希望会话事实、配置变更、工具轨迹和记忆派生数据可审计、可恢复，从而能诊断失败并安全升级架构。

**Why this priority**: 持久化和可观测性决定了系统能否在多渠道长期运行。

**Independent Test**: 完成一轮成功、一轮失败和一次配置更新，检查关联的 session/turn/event 标识、事务一致性、敏感字段脱敏、重启后的恢复和记忆索引重建。

**Acceptance Scenarios**:

1. **Given** 回合包含多个异步阶段，**When** 阶段状态变化，**Then** 每个事件带有 session_id、turn_id 和可关联的时间顺序。
2. **Given** 服务重启或派生记忆库删除，**When** 系统再次启动，**Then** 会话事实仍可读取，记忆索引可从事实记录重建。
3. **Given** API Key 或 OAuth 凭据，**When** 查询状态、日志或错误，**Then** 返回值中不出现明文凭据。

### Edge Cases

- 空文本、超长文本、重复 `request_id` 或重复 `turn_id` 不得产生重复持久化回合。
- WebSocket 断线发生在流式输出中时，回合状态必须可查询；重连后不得自动重复执行工具。
- QQ 群聊中机器人被多次 @、消息包含图片或不可解析内容时，必须有明确的过滤或降级结果。
- 历史库为空、所有检索分数为零、分数含重复值或负值时，召回应返回空集合或确定性结果而不抛出未处理异常。
- 记忆召回结果超过 token 预算时，裁剪不得破坏系统提示、当前用户消息和工具协议。
- 同一消息从 Web 与 QQ 重复投递时，去重键必须阻止重复 turn。

## Requirements

### Functional Requirements

- **FR-001**: System MUST support Web and QQ as user-facing channels while treating channel-specific payloads as adapters to one canonical turn model.
- **FR-002**: System MUST preserve the existing Web HTTP and `/ws` event names, required fields, error semantics, and paths documented in `backend-contract.md`.
- **FR-003**: System MUST support QQ private chat and group chat, with group processing gated by an explicit @robot mention.
- **FR-004**: System MUST maintain one canonical session/turn/message record per accepted user message and provide deterministic idempotency for retries or duplicate deliveries.
- **FR-005**: System MUST serialize turns within a session; a second turn cannot execute concurrently with an unfinished turn in that session.
- **FR-006**: System MUST run a single main Agent execution path for all channels and MUST reject `spawn` and `spawn_manage` operations.
- **FR-007**: System MUST expose one centrally governed tool registry shared by Web and QQ, including explicit allowlists, argument validation, timeouts, output limits, and audit events.
- **FR-008**: System MUST keep shell execution on the server-side runtime and apply the same security policy regardless of originating channel.
- **FR-009**: System MUST assemble each model request in this order: fixed system/developer prompts, bounded conversation context, bounded memory recall, current user input, and tool protocol data.
- **FR-010**: System MUST enforce a hard remaining-context token budget before memory items are injected and MUST use deterministic truncation when the budget is insufficient.
- **FR-011**: System MUST implement the Akasha recall pipeline as: dense and BM25 lanes → per-lane `_tail_surprisal` normalization copied from `akashic-agent-main` → lane combination and Sparsemax seed → continuation decision → residual graph push → basin pooling → Sparsemax head selection → independent head diffusion → Entmax sparse tail and lateral competition.
- **FR-012**: System MUST define `_tail_surprisal` exactly as the source implementation: for non-empty, non-all-zero scores, sort stably, compute `counts = n - searchsorted(sorted_scores, scores, side="left")`, and return `-log(counts / n)` elementwise; empty or all-zero arrays return zeros of the same shape.
- **FR-013**: System MUST keep RRF as a separate retrieval route where it already exists and MUST NOT silently replace the Akasha per-lane `_tail_surprisal` pipeline with RRF.
- **FR-014**: System MUST return recall provenance for every selected turn, including `turn_id`, `session_id`, source message IDs, entry lane, seed/head/tail scores, graph path, continuation flag, selection stage, and token cost.
- **FR-015**: System MUST treat session/turn/message data as the canonical fact source and memory indexes, embeddings, and graph state as rebuildable derived data.
- **FR-016**: System MUST support automatic recall and successful-turn memory writeback by default, plus explicit user controls to disable, remember, or forget memory without corrupting canonical messages.
- **FR-017**: System MUST emit structured, correlated events for inbound messages, turn lifecycle, model requests, tool calls, outbound delivery, memory recall, and degradation/failure.
- **FR-018**: System MUST redact secrets from persisted settings responses, logs, tool previews, and error messages.
- **FR-019**: System MUST provide an explicit mobile-out-of-scope boundary: no mobile UI, mobile realtime gateway, pairing flow, or mobile-only API is required for this architecture milestone.
- **FR-020**: System MUST document which source capabilities are copied, simplified, or intentionally unimplemented, and MUST expose unsupported capability errors rather than pretending parity.

### Key Entities

- **Session**: A channel-independent conversation identity with ownership/scope and ordered turns.
- **Turn**: One accepted user request and its assistant outcome, lifecycle state, idempotency key, and correlated events.
- **Message**: Canonical user, assistant, tool, or system content attached to a turn, with optional media and source metadata.
- **ChannelEnvelope**: Normalized inbound/outbound representation carrying channel, chat identity, sender, mention state, and delivery metadata.
- **ToolInvocation**: A validated execution request with tool name, arguments, policy decision, timing, status, and redacted result preview.
- **MemoryRecall**: A derived, non-authoritative set of selected historical turns with scoring stages, provenance, graph paths, continuation state, and token costs.
- **ModelConfiguration**: Server-controlled provider, model, reasoning, embedding, and revision data with secret references rather than plaintext credentials.
- **StructuredEvent**: Correlated audit/observability record keyed by session, turn, channel, and event sequence.

## Success Criteria

### Measurable Outcomes

- **SC-001**: In a clean deployment, at least 95% of Web smoke-test messages complete with all required lifecycle frames and a persisted final message within 30 seconds when the configured model responds within that period.
- **SC-002**: 100% of valid QQ private messages and @robot group messages in the integration fixture map to exactly one canonical turn; non-mentioned group messages map to zero turns.
- **SC-003**: 100% of duplicate delivery fixtures produce no additional turn, tool execution, or assistant message.
- **SC-004**: 100% of recall fixture results obey the configured token budget and contain all required provenance fields; no selected item exceeds the remaining budget.
- **SC-005**: 100% of attempted `spawn` and `spawn_manage` calls are rejected and auditable, while ordinary allowed tools continue to work.
- **SC-006**: After deleting the derived memory index and restarting, 100% of canonical sessions and messages remain readable and the index can be rebuilt from them.
- **SC-007**: Automated tests cover the Web contract, QQ adapter, session serialization/idempotency, tool policy, `_tail_surprisal` numerical behavior, recall provenance, secret redaction, and failure recovery.
- **SC-008**: No mobile-only module or endpoint is required for the milestone's build, test, or deployment path.

## Assumptions

- Existing Web frontend structure and `backend-contract.md` are the compatibility baseline.
- QQ transport credentials, bot account, and gateway availability are deployment concerns; the architecture defines an adapter contract and test doubles before production connectivity.
- A configured OpenAI-compatible model and optional embedding provider are available, but model vendor selection is not part of this feature.
- The initial deployment targets one server-side runtime and SQLite-compatible durable storage; horizontal scaling is deferred until the canonical contracts are stable.
- The user accepts no backend session queue beyond serialization/idempotency needed to enforce one unfinished turn per session.
- Mobile support is explicitly out of scope for this milestone and may be designed separately later.

## Out of Scope

- Mobile web/native UI, mobile realtime protocol, pairing, push notifications, or mobile-specific plugin UI.
- Recursive or parallel sub-Agent orchestration.
- Reproducing every original Akashic plugin, dashboard, proactive subsystem, or provider integration before the core Web/QQ loop is validated.
- Changing the existing Web API or WebSocket contract without a separately approved migration specification.
