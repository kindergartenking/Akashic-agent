"""after_reasoning 阶段（5 模块已完整）。

after_reasoning 是 7 阶段生命周期的第六个阶段（reasoning 层 · GATE），位于 loop 循环
结束之后、after_turn 之前，且**每次 turn 只跑一次**。完整职责是「解析回复 + 持久化 +
组 outbound + 可改 reply」。

  build_ctx → emit → persist → build_outbound → return

  · build_ctx —— 解析 LLM 原始回复（parse_response）+ 组装 AfterReasoningCtx。
  · emit —— GATE 门控，插件 handler 可改 reply / media / outbound_metadata。
  · persist —— 把 user / assistant 消息落库（合并原 persist_user / persist_asst /
    update_meta / append_messages 四步）。
  · build_outbound —— 组装最终 OutboundMessage（外发消息）。
  · return —— 打包 TurnSnapshot(state, outbound, ctx) 作为阶段 output。

本阶段三个独特之处：
1. 是 7 阶段里**最后一个 GATE**——插件可在回复真正发出去前改 reply / media /
   outbound_metadata，这是「可改 reply」基本功能的落点。
2. output 是 TurnSnapshot 三元组（state + outbound + ctx），不是单个 ctx——它把原始
   state、组装好的 outbound、最终 ctx 一起打包交给 after_turn（TAP 广播）。
3. 与 before_reasoning 共享 reasoning: 命名空间，是一条链的两端（链头组 ctx，链尾解析
   回复 + 持久化 + 组 outbound）。

裁剪说明（相对 M1b 旧版 8 模块 500 行）：
- 砍 context_retry 字段、meme_tag 字段；
- 砍 mobile channel 特判、control turn 多输入回放（InputLock）；
- 砍 milestone 诊断日志、retired 字段保护、persist 字段扩展；
- 四个持久化步骤（persist_user / persist_asst / update_meta / append_messages）合并为
  一个 persist 模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias, cast

from agent.core.response_parser import parse_response
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModule,
    append_string_exports,
    collect_prefixed_slots,
    topo_sort_modules,
)
from agent.lifecycle.types import (
    AfterReasoningCtx,
    AfterReasoningInput,
    TurnSnapshot,
)
from bus.event_bus import EventBus
from bus.events import OutboundMessage


@dataclass
class AfterReasoningFrame(PhaseFrame[AfterReasoningInput, TurnSnapshot]):
    """after_reasoning 的帧：input = AfterReasoningInput，output = TurnSnapshot。"""

    pass


AfterReasoningModules: TypeAlias = list[PhaseModule[AfterReasoningFrame]]


_CTX_SLOT = "reasoning:ctx"
_OUTBOUND_SLOT = "reasoning:outbound"
_PERSISTED_USER_SLOT = "reasoning:persisted_user"
_PERSISTED_ASSISTANT_SLOT = "reasoning:persisted_assistant"
_OUTBOUND_METADATA_PREFIX = "outbound:metadata:"
_OUTBOUND_MEDIA_PREFIX = "outbound:media:"


class _BuildAfterReasoningCtxModule:
    """build_ctx：解析回复 + 组装 AfterReasoningCtx。

    - slot     ：after_reasoning.build_ctx；
    - requires ：无依赖（链上第一个模块）；
    - produces ：reasoning:ctx；
    - run      ：从 input（state + turn_result）解析 LLM 原始回复，组装 AfterReasoningCtx。
      parse_response 把 raw_reply 拆成 clean_text（正文）+ metadata（结构化字段）；
      outbound_metadata 拼 inbound metadata + state.extra_metadata + tools 信息。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_reasoning.build_ctx"
    requires: tuple[str, ...] = ()
    produces = (_CTX_SLOT,)

    async def run(self, frame: AfterReasoningFrame) -> AfterReasoningFrame:
        input = frame.input
        msg = input.state.msg
        turn_result = input.turn_result
        raw_reply = turn_result.reply
        if raw_reply is None:
            raw_reply = "I've completed processing but have no response to give."
        tool_chain = list(turn_result.tool_chain)
        parsed = parse_response(raw_reply, tool_chain=tool_chain)
        frame.slots[_CTX_SLOT] = AfterReasoningCtx(
            session_key=input.state.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            reply=parsed.clean_text,
            response_metadata=parsed.metadata,
            tools_used=tuple(turn_result.tools_used),
            tool_chain=tuple(tool_chain),
            media=list(turn_result.media),
            thinking=turn_result.thinking,
            streamed=turn_result.streamed,
            outbound_metadata={
                **dict(msg.metadata or {}),
                **input.state.extra_metadata,
                "tools_used": list(turn_result.tools_used),
                "tool_chain": list(tool_chain),
                "streamed_reply": turn_result.streamed,
            },
        )
        return frame


class _EmitAfterReasoningCtxModule:
    """GATE 门控：把 ctx 交给 EventBus，插件 handler 可改 reply / media / outbound_metadata。

    - slot     ：after_reasoning.emit；
    - requires ：after_reasoning.build_ctx + reasoning:ctx；
    - produces ：reasoning:ctx（emit 可能替换 ctx，替换后写回槽）；
    - run      ：frame.slots[reasoning:ctx] = await bus.emit(ctx)。

    这是「可改 reply」的落点——插件在回复发出前改最终正文 / 媒体 / 元数据。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_reasoning.emit"
    requires = ("after_reasoning.build_ctx", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: AfterReasoningFrame) -> AfterReasoningFrame:
        ctx = cast(AfterReasoningCtx, frame.slots[_CTX_SLOT])
        frame.slots[_CTX_SLOT] = await self._bus.emit(ctx)
        return frame


class _PersistMessagesModule:
    """persist：把 user / assistant 消息落库（合并原四步持久化）。

    - slot     ：after_reasoning.persist；
    - requires ：after_reasoning.emit + reasoning:ctx；
    - produces ：reasoning:persisted_user + reasoning:persisted_assistant；
    - run      ：按 persistence 策略 add user 消息（state.msg.content）+ assistant 回复
      （ctx.reply，带 tools_used / tool_chain / reasoning_content），再统一 append 进
      session_manager 落库。

    这是「持久化」基本功能的落点。相对旧版：合并了 persist_user / persist_asst /
    update_meta / append_messages 四步，砍掉 control turn 回放、mobile 特判、
    milestone 日志、persist 字段扩展。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_reasoning.persist"
    requires = ("after_reasoning.emit", _CTX_SLOT)
    produces = (_PERSISTED_USER_SLOT, _PERSISTED_ASSISTANT_SLOT)

    def __init__(self, session_services: Any) -> None:
        self._session_services = session_services

    async def run(self, frame: AfterReasoningFrame) -> AfterReasoningFrame:
        ctx = cast(AfterReasoningCtx, frame.slots[_CTX_SLOT])
        state = frame.input.state
        session = state.session
        if session is None:
            raise RuntimeError("AfterReasoning requires TurnState.session")
        messages: list[dict[str, Any]] = []
        if state.persistence.persist_user:
            user_msg = session.add_message("user", state.msg.content)
            frame.slots[_PERSISTED_USER_SLOT] = user_msg
            messages.append(user_msg)
        if state.persistence.persist_assistant:
            kwargs: dict[str, Any] = {}
            if ctx.tools_used:
                kwargs["tools_used"] = list(ctx.tools_used)
            if ctx.tool_chain:
                kwargs["tool_chain"] = list(ctx.tool_chain)
            if ctx.thinking is not None:
                kwargs["reasoning_content"] = ctx.thinking
            asst_msg = session.add_message("assistant", ctx.reply, **kwargs)
            frame.slots[_PERSISTED_ASSISTANT_SLOT] = asst_msg
            messages.append(asst_msg)
        if messages:
            await self._session_services.session_manager.append_messages(session, messages)
        return frame


class _BuildOutboundMessageModule:
    """build_outbound：组装最终 OutboundMessage。

    - slot     ：after_reasoning.build_outbound；
    - requires ：after_reasoning.persist + reasoning:ctx；
    - produces ：reasoning:outbound；
    - run      ：拼 outbound metadata（ctx.outbound_metadata + 插件外溢的 outbound:metadata:）、
      回收 outbound:media:、把 persisted 消息稳定 ID 写回 metadata，组装 OutboundMessage。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "after_reasoning.build_outbound"
    requires = ("after_reasoning.persist", _CTX_SLOT)
    produces = (_OUTBOUND_SLOT,)

    async def run(self, frame: AfterReasoningFrame) -> AfterReasoningFrame:
        ctx = cast(AfterReasoningCtx, frame.slots[_CTX_SLOT])
        metadata = dict(ctx.outbound_metadata)
        metadata.update(collect_prefixed_slots(frame.slots, _OUTBOUND_METADATA_PREFIX))
        state = frame.input.state
        if state.persistence.persist_user:
            persisted_user = frame.slots.get(_PERSISTED_USER_SLOT)
            if isinstance(persisted_user, dict) and isinstance(persisted_user.get("id"), str):
                metadata["persisted_user_message_id"] = persisted_user["id"]
        media = list(ctx.media)
        _append_media(
            media,
            collect_prefixed_slots(frame.slots, _OUTBOUND_MEDIA_PREFIX),
        )
        session_message_id: str | None = None
        if state.persistence.persist_assistant:
            persisted_asst = frame.slots.get(_PERSISTED_ASSISTANT_SLOT)
            if isinstance(persisted_asst, dict) and isinstance(persisted_asst.get("id"), str):
                session_message_id = persisted_asst["id"]
        frame.slots[_OUTBOUND_SLOT] = OutboundMessage(
            channel=ctx.channel,
            chat_id=ctx.chat_id,
            content=ctx.reply,
            thinking=ctx.thinking,
            media=media,
            metadata=metadata,
            session_message_id=session_message_id,
        )
        return frame


class _BuildTurnSnapshotModule:
    """return：打包 TurnSnapshot(state, outbound, ctx) 作为阶段 output。

    - slot     ：after_reasoning.return；
    - requires ：after_reasoning.build_outbound + reasoning:ctx + reasoning:outbound；
    - run      ：frame.output = TurnSnapshot(state, outbound, ctx)。

    本模块是 after_reasoning 的链尾，也是「output 职责」的最终归宿——之前的 build_ctx /
    emit / persist / build_outbound 都不碰 output。产出的是三元组快照，交给 after_turn。
    """

    slot = "after_reasoning.return"
    requires = ("after_reasoning.build_outbound", _CTX_SLOT, _OUTBOUND_SLOT)

    async def run(self, frame: AfterReasoningFrame) -> AfterReasoningFrame:
        frame.output = TurnSnapshot(
            state=frame.input.state,
            outbound=cast(OutboundMessage, frame.slots[_OUTBOUND_SLOT]),
            ctx=cast(AfterReasoningCtx, frame.slots[_CTX_SLOT]),
        )
        return frame


def _append_media(target: list[str], exports: dict[str, object]) -> None:
    append_string_exports(target, exports)


def default_after_reasoning_modules(
    bus: EventBus,
    session_services: Any,
    plugin_modules: AfterReasoningModules | None = None,
) -> AfterReasoningModules:
    """装配 after_reasoning 的内置模块链（build_ctx → emit → persist → build_outbound → return）。"""
    builtins: AfterReasoningModules = [
        _BuildAfterReasoningCtxModule(),
        _EmitAfterReasoningCtxModule(bus),
        _PersistMessagesModule(session_services),
        _BuildOutboundMessageModule(),
        _BuildTurnSnapshotModule(),
    ]
    return cast(
        AfterReasoningModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )
