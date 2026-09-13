<!--
Sync Impact Report
- Version change: template → 1.0.0
- Modified principles: template placeholders → API 兼容优先；最小可行实现；测试先行；安全与数据边界；可观测与可回滚
- Added sections: 项目约束；开发工作流
- Removed sections: 无
- Follow-up TODOs: 无
-->

# Akashic Agent Constitution

## Core Principles

### I. API 兼容优先
前端现有行为和后端接口契约是本项目的首要兼容边界。任何后端改动 MUST 保持
`backend-contract.md`、WebSocket `/ws` 事件名称、字段语义和错误行为兼容；确需破坏兼容时，
MUST 先更新契约、提供迁移说明并增加对应回归测试。前后端联调测试 MUST 覆盖真实消息链路。

### II. 最小可行实现与清晰边界
每项功能 MUST 先定义明确的范围、非目标和验收条件，并优先采用能够独立测试的最小实现。
后端重写 MUST 与保留的前端结构解耦；Runtime、MessageBus、Agent、工具和记忆模块 MUST
通过明确接口协作，不得把暂未实现的原项目子系统伪装成已实现能力。引入额外复杂度时 MUST
说明其解决的问题、替代方案和回滚方式。

### III. 测试先行且可重复
新增或修改的行为 MUST 有自动化测试；共享数据结构、WebSocket 事件、数据库事务和 Agent
编排 MUST 至少包含单元测试或集成测试。所有测试代码 MUST 位于根目录 `test/`，不得放入
`backend/` 或前端源码目录。测试 MUST 使用隔离数据、固定输入和可重复命令；功能完成前 MUST
运行受影响的测试以及必要的类型检查或构建检查。

### IV. 安全与数据边界
API Key、OAuth 凭据和其他机密 MUST 只保存在服务端受控存储中，状态接口和日志 MUST 不返回
明文凭据。文件、Shell、网络和模型工具 MUST 使用显式白名单、参数校验、超时和错误处理。
Session 数据 MUST 按 session_id 隔离；数据库写入 MUST 使用事务和一致性检查。任何扩大权限或
访问范围的改动 MUST 在规格和测试中明确记录。

### V. 可观测、可回滚与诚实降级
异步消息、Agent 编排、工具调用、LLM 请求和记忆召回 MUST 发出可关联的结构化事件或日志，
并携带 session_id、turn_id 等必要标识。外部服务不可用时 MUST 返回可识别的错误或降级结果，
不得伪造成功。数据库迁移、模型配置和记忆索引更新 MUST 保留备份或可重建路径；发布前 MUST
验证启动、核心链路和失败恢复行为。

## Project Constraints

- 前端构造和路由优先保留原 Akashic Agent 的行为；后端可以逐步重写，但不得无记录地删除前端所依赖的接口。
- 当前后端以 FastAPI、SQLite、异步 MessageBus 和 OpenAI-compatible LLM 接口为基础；新的基础设施替换 MUST 提供等价契约和迁移方案。
- 会话、回合和消息是事实记录；Akasha 记忆库属于可删除、可重建的派生数据，不得成为唯一事实源。
- 生产构建产物、开发服务器和静态资源路径 MUST 与 README 中声明的 `/chat`、`/settings`、`/dashboard` 和 `/assets` 约定保持一致。
- 测试、评测 fixture 和临时数据库 MUST 与日常运行数据隔离，避免测试污染用户会话或模型配置。

## Development Workflow

1. 每个中等以上功能 MUST 先建立 `specs/<id>-<slug>/spec.md`，写明目标、用户场景、范围、非目标、边界情况和验收标准。
2. 在实现前 MUST 通过 `$speckit-plan` 记录架构、接口、数据模型、失败处理和测试策略；必要时先运行 `$speckit-clarify`。
3. MUST 通过 `$speckit-tasks` 生成依赖有序的任务清单，并按任务实现；每个任务完成后更新其状态和验证结果。
4. 实现阶段 MUST 遵守测试先行原则，至少运行受影响的 Python/Node 测试、类型检查或构建检查，并记录失败原因和后续处理。
5. 合并或交付前 MUST 运行 `$speckit-converge`，确认代码、规格、计划和任务清单一致；未完成项 MUST 明确列出，不得宣称已完成。
6. 对原项目行为的复刻 MUST 在规格或文档中说明“已复制、已简化、未实现”的差异，避免把 MVP 能力误认为完整等价实现。

## Governance
<!-- Example: Constitution supersedes all other practices; Amendments require documentation, approval, migration plan -->

本章程优先于未明确记录的个人习惯和临时实现决定。所有新增功能、接口变更、数据迁移和
权限扩展 MUST 检查本章程的适用条款。章程修订 MUST：

1. 在本文件顶部更新 Sync Impact Report，说明版本变化、修改内容、删除内容和待办事项。
2. 按语义化版本递增：新增或实质扩展原则使用 MINOR；删除或重新定义既有原则使用 MAJOR；文字澄清和非语义修订使用 PATCH。
3. 同步更新受影响的规格、计划、任务、测试和开发文档；破坏性变更 MUST 提供迁移或回滚方案。
4. 在交付前进行合规检查：至少验证 API 契约、凭据安全、数据隔离、测试目录和核心链路测试。

**Version**: 1.0.0 | **Ratified**: 2026-09-05 | **Last Amended**: 2026-09-05
