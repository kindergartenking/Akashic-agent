"""模块版探针（AuditModule）：展示「加模块」比「加 handler」多出的能力。

这是理解「模块 vs handler」的范例。handler（bus.on / @on_*）只能在 emit/fanout
那一刻看到单个 ctx；而模块（PhaseModule）是模块链上的正式一环，能看到整个
frame，并做到三件 handler 做不到的事：

1. 读 frame.input —— 阶段的原始输入（before_turn 是 TurnState，含原始消息）；
2. 读/写 frame.slots —— 读 build_ctx 产出的 ctx、写自定义数据槽；
3. 通过 slot/requires 参与拓扑排序 —— 精确插到 build_ctx 之后、emit 之前。

本模块插在 before_turn 的 build_ctx 与 emit 之间：
- slot     : audit.inspect（自定义前缀，不被识别为 builtin，属插件模块）；
- requires : before_turn.build_ctx（模块槽依赖 → 排在 build_ctx 之后；
             又因插件模块排在同依赖的 builtin 之前，故落在 emit 之前）；
- produces : session:audit:started_at（生产一个自定义数据槽，供后续模块消费）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

from agent.lifecycle.phases.before_turn import BeforeTurnFrame
from agent.lifecycle.types import BeforeTurnCtx


class AuditModule:
    slot = "audit.inspect"
    requires = ("before_turn.build_ctx",)
    produces = ("session:audit:started_at",)

    async def run(self, frame: BeforeTurnFrame) -> BeforeTurnFrame:
        # 能力①：读 frame.input（原始输入 TurnState）—— handler 拿不到
        state = frame.input
        print(
            f"  [模块 audit] 读 frame.input → msg={state.msg.content!r} "
            f"session_key={state.session_key} dispatch={state.dispatch_outbound}"
        )

        # 能力②：读 frame.slots 里 build_ctx 已产出的 ctx
        ctx = cast(BeforeTurnCtx, frame.slots["session:ctx"])
        print(
            f"  [模块 audit] 读 frame.slots['session:ctx'] → "
            f"ctx.content={ctx.content!r} history={len(ctx.history_messages)}条"
        )

        # 能力③：生产数据槽（handler 只能改 ctx 字段，不能往 slots 塞新键）
        frame.slots["session:audit:started_at"] = datetime.now(timezone.utc).isoformat()
        print(f"  [模块 audit] 写数据槽 session:audit:started_at={frame.slots['session:audit:started_at']}")

        # 能力④：改 ctx 字段（这条 handler 也能做，放在这里作为对比参照）
        ctx.extra_hints.append("audit_module 注入的 hint")

        return frame
