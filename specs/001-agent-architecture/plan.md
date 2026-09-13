# Implementation Plan: Akashic Agent Core Architecture

**Branch**: `001-agent-architecture` | **Date**: 2026-09-06 | **Spec**: [spec.md](spec.md)

## Summary

建立一个以 canonical session/turn/message 为事实源、以单主 Agent 为执行核心、以渠道适配器隔离 Web 与 QQ 的服务端架构。保留现有 Web API 与 `/ws` 契约，新增 QQ 入站/出站适配器；工具由一个集中注册表统一执行，`spawn` 与 `spawn_manage` 永久拒绝。记忆服务作为可重建派生层，在硬 token 预算内执行 Akasha 召回链路，并准确复制 `akashic-agent-main` 的 `_tail_surprisal` 定义。

## Technical Context

**Language/Version**: Python 3.13 runtime（现有后端）；TypeScript 5.8 前端

**Primary Dependencies**: FastAPI/uvicorn、httpx、SQLite、LiteLLM-compatible model client、现有 React/Vite Web UI；QQ 适配器先实现 OneBot 11 风格事件边界与测试替身

**Storage**: `sessions.db` 为 canonical SQLite；`model-registry.sqlite3` 保存模型/凭据元数据；`memory/akasha.db` 为可删除可重建的派生索引

**Testing**: 根目录 `test/` 中的 pytest 后端单元/集成测试、Node test 与 TypeScript typecheck；固定 fixture，隔离临时数据库

**Target Platform**: 单台服务端运行时；Web 浏览器和 QQ Bot 网关为入口；不包含移动端

**Project Type**: 多入口 Web 服务 + Agent runtime + 可插拔渠道适配层

**Performance Goals**: 常规 Web 回合在模型响应满足约定时 30 秒内完成；召回和 prompt 组装必须在单回合内确定性完成，不得等待无限队列

**Constraints**: 保持 `backend-contract.md`；同一 session 同时仅一个未完成 turn；所有秘密服务端保存；工具显式白名单/参数校验/超时/输出限制；记忆注入不得超过剩余上下文预算

**Scale/Scope**: 首期单实例、SQLite、Web + QQ 两渠道；支持现有 MVP 会话量，暂不承诺水平扩展和移动端

## Constitution Check

*GATE: Must pass before Phase 0 research and re-check after Phase 1 design.*

- **I. API 兼容优先**：通过保留现有 HTTP/WebSocket 帧，QQ 只新增适配器；契约变更需单独迁移。
- **II. 最小可行实现与清晰边界**：只实现 Web/QQ、单主 Agent、统一工具和 Akasha 核心召回；移动端、递归派生和未验证高级插件明确列为非目标。
- **III. 测试先行且可重复**：所有新行为映射到根目录 `test/` 的固定 fixture 测试和端到端 quickstart。
- **IV. 安全与数据边界**：canonical 数据与派生索引分离；工具策略、凭据脱敏、session 隔离和 SQLite 事务纳入设计。
- **V. 可观测、可回滚与诚实降级**：结构化关联事件、出站失败记录、索引重建、启动校验和无记忆降级均有明确路径。

**Gate result: PASS（设计不引入章程例外）**。

## Research Summary

研究结论详见 [research.md](research.md)。关键结论：

1. 现有 `backend/app.py` 已具备 WebSocket→MessageBus→Agent→SessionStore→Akasha 的最小链路，应在其边界上重构而非另起协议。
2. 原项目 `_tail_surprisal` 必须逐字保持排序、`searchsorted(side="left")`、全零快捷返回和 `-log(count/n)` 行为；RRF 属于另一条 memory2 检索路由。
3. QQ 使用 ChannelEnvelope 进入同一 turn pipeline；群聊 mention 过滤属于适配器职责，核心 Agent 不感知 QQ 特殊 payload。
4. 子 Agent 相关类和工具从运行时能力面移除或替换为显式 unsupported policy，避免继续暴露派生能力。

## Architecture

```text
Web /ws ───────┐
               ├─ ChannelAdapter ──> MessageBus ──> SessionAdmission
QQ OneBot 11 ──┘                                  │
                                                  ▼
                                      Turn Orchestrator
                                      ├─ Context Manager
                                      ├─ Memory Recall (derived)
                                      ├─ Main Agent / ReAct
                                      ├─ Tool Policy + Registry
                                      └─ Event/Outbound Projection
                                                  │
                           ┌──────────────────────┴─────────────────────┐
                           ▼                                            ▼
                    sessions.db (事实源)                       Web/QQ outbound
                           │
                           ▼
                    memory/akasha.db (可重建派生索引)
```

### Runtime boundaries

- `ChannelAdapter` 只负责协议解析、身份/会话映射、mention 过滤、入站去重和出站投影；不得直接调用模型或工具。
- `MessageBus` 传递标准化 `ChannelEnvelope`，并将 `session_id`、`turn_id`、`request_id` 固定在事件上下文中。
- `SessionAdmission` 对每个 session 执行幂等检查和单未完成 turn 锁；busy 请求返回可识别错误，不排队执行。
- `TurnOrchestrator` 按“读取上下文→只读召回→构造 prompt→单主 Agent ReAct→写入 assistant→记忆提交→出站完成”顺序编排。
- `ToolRegistry` 是唯一工具执行入口；工具 schema、白名单、超时、输出裁剪、审计事件在这里集中实现。
- `MainAgent` 只能看到经过策略过滤的工具集合；`spawn`/`spawn_manage` 始终返回 unsupported/denied，不创建子执行上下文。
- `MemoryRuntime` 不修改 canonical 消息；召回失败只产生降级事件并返回空 recall，成功回合提交后再写派生索引。

### Memory pipeline contract

1. 从 user/assistant 历史 turn 计算 dense 与 BM25 两路分数。
2. 每一路独立执行源项目 `_tail_surprisal`，不得先套 RRF。
3. 按 continuation 规则组合 query/context evidence，应用 Sparsemax 得到 seed。
4. 在残差图上执行 residual push，进行盆地池化和 Sparsemax head 选择。
5. 每个 head 独立扩散，再以 Entmax 稀疏化尾部并执行横向竞争淘汰。
6. 依据剩余 token 预算做确定性裁剪，返回带 provenance 的 `MemoryRecall`。

## Project Structure

```text
backend/
├── app.py                         # FastAPI 路由与生命周期
├── message_bus.py                 # 标准事件分发
├── session_store.py               # canonical session/turn/message
├── agent_orchestrator.py          # 单主 Agent ReAct（移除派生路径）
├── context_manager.py             # 短期上下文与 token 预算
├── memory/akasha.py               # 派生记忆 runtime 与召回接口
├── channels/
│   ├── base.py                    # ChannelEnvelope/Adapter 协议
│   ├── web.py                     # /ws 投影（保留现有帧）
│   └── qq_onebot.py               # QQ 私聊/群聊 mention 适配器
├── tools/
│   ├── registry.py                # 统一 schema/策略/执行
│   ├── policy.py                  # allowlist + spawn deny + redaction
│   └── ...                         # 文件、shell、网络、消息工具
└── observability.py               # 结构化关联事件与脱敏
frontend/
├── chat/                          # 保留 Web 桌面页面；删除/停用移动构建入口
└── dashboard/                     # 现有 dashboard，非核心阻塞项
test/
├── backend/                       # Python 单元/集成/契约测试
└── frontend/                      # Node/TS 前端测试
specs/001-agent-architecture/      # 本设计及后续 tasks
```

**Structure Decision**: 采用现有 `backend/` + `frontend/` 多入口结构，新增 `backend/channels/` 和集中策略/可观测边界；不创建第二套 Agent runtime，不引入移动端目录或协议。

## Data and Failure Design

- canonical 写入使用事务：先记录 user turn，再在 assistant 成功/失败时关闭 turn；派生 memory commit 只能发生在 canonical assistant 已存在之后。
- QQ 出站失败不回滚 canonical turn，写入 `delivery_failed` 事件并允许受控重试；WebSocket 断线不重复执行工具。
- embedding/索引错误采用无记忆降级；模型错误、工具拒绝和超时分别暴露可识别状态。
- 启动时校验 `sessions.db`、模型配置和派生索引；派生库损坏时备份/删除后从 canonical 数据重建。
- 凭据只通过 credential reference 读取，任何状态、事件和 preview 均执行统一脱敏。

## Phase 0/1 Outputs

- [research.md](research.md)：仓库事实、源项目差异和关键决策。
- [data-model.md](data-model.md)：实体、状态、关系、约束和迁移策略。
- [contracts/web-ws.md](contracts/web-ws.md)：Web API/WebSocket 兼容边界。
- [contracts/qq-channel.md](contracts/qq-channel.md)：QQ OneBot 11 适配器输入输出契约。
- [contracts/tool-policy.md](contracts/tool-policy.md)：工具权限、拒绝与审计契约。
- [quickstart.md](quickstart.md)：固定 fixture 下的启动、验证、故障恢复流程。

## Constitution Re-check (post-design)

- API 契约保持：PASS；Web 文件和帧名称未被重新定义。
- 边界清晰：PASS；QQ、单主 Agent、记忆派生和移动端非目标均已列出。
- 测试与恢复：PASS；每个边界都有对应契约或 quickstart 场景。
- 安全与数据：PASS；工具/凭据/session/派生库边界已明确。
- 可观测与回滚：PASS；关联事件、出站失败和索引重建已定义。

## Complexity Tracking

无章程违规，无需例外记录。
