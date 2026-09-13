"""prompt_render 阶段（5 模块已完整）。

prompt_render 是 7 阶段生命周期的第三个阶段（loop 层 · GATE），位于 before_reasoning
之后、before_step 之前，且**只跑一次**（在推理循环开始前渲染一次 prompt）。完整职责是
「把历史 + 当前消息 + 技能 + 记忆 + 提示渲染成 messages，并允许插件改 prompt 片段」。

  build_ctx → emit → collect_exports → render → return

  · build_ctx —— 从 input 组装 PromptRenderCtx。
  · emit —— GATE 门控，插件 handler 可改写 ctx 字段（如 system_sections_top/bottom）。
  · collect_exports —— 回收插件外溢的 section_top / section_bottom / extra_hint。
  · render —— 调 ContextBuilder.render 产出 messages（真正的渲染）。
  · return —— 把 PromptRenderResult 设为阶段 output。

与 before_turn / before_reasoning 的关键差异：本阶段 output 不是 ctx，而是渲染好的
messages（PromptRenderResult），因此多了一个「真正产出」的 render 模块，且排在
collect_exports 之后（render 要用收好的完整 ctx 渲染）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, cast

from agent.core.passive_support import build_context_hint_message
from agent.core.types import ContextRequest
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModule,
    append_string_exports,
    collect_prefixed_slots,
    topo_sort_modules,
)
from agent.lifecycle.types import PromptRenderCtx, PromptRenderInput, PromptRenderResult
from agent.prompting import PromptSectionRender
from bus.event_bus import EventBus

if TYPE_CHECKING:
    from agent.context import ContextBuilder


@dataclass
class PromptRenderFrame(PhaseFrame[PromptRenderInput, PromptRenderResult]):
    """prompt_render 的帧：input = PromptRenderInput，output = PromptRenderResult。"""

    pass


PromptRenderModules: TypeAlias = list[PhaseModule[PromptRenderFrame]]


_CTX_SLOT = "prompt:ctx"
_RESULT_SLOT = "prompt:result"
_SECTION_TOP_PREFIX = "prompt:section_top:"
_SECTION_BOTTOM_PREFIX = "prompt:section_bottom:"
_EXTRA_HINT_PREFIX = "prompt:extra_hint:"


class _BuildPromptRenderCtxModule:
    """build_ctx：从 input 组装 PromptRenderCtx。

    - slot     ：prompt_render.build_ctx；
    - requires ：无依赖（链上第一个模块）；
    - produces ：prompt:ctx；
    - run      ：把 input 的字段搬进 PromptRenderCtx，写入数据槽。

    注意 disabled_sections 用 set() 拷贝、extra_hints 用 list() 拷贝，避免后续阶段
    改 ctx 时反噬 input（input 是 frozen，但其内部的可变容器仍可能被外部持有）。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "prompt_render.build_ctx"
    requires: tuple[str, ...] = ()
    produces = (_CTX_SLOT,)

    async def run(self, frame: PromptRenderFrame) -> PromptRenderFrame:
        input = frame.input
        frame.slots[_CTX_SLOT] = PromptRenderCtx(
            session_key=input.session_key,
            channel=input.channel,
            chat_id=input.chat_id,
            content=input.content,
            media=input.media,
            timestamp=input.timestamp,
            history=input.history,
            skill_names=input.skill_names,
            retrieved_memory_block=input.retrieved_memory_block,
            disabled_sections=set(input.disabled_sections),
            turn_injection_prompt=input.turn_injection_prompt,
            extra_hints=list(input.extra_hints or []),
        )
        return frame


class _EmitPromptRenderCtxModule:
    """GATE 门控：把 ctx 交给 EventBus，插件 handler 可改写字段或整体替换 ctx。

    - slot     ：prompt_render.emit；
    - requires ：prompt_render.build_ctx + prompt:ctx；
    - produces ：prompt:ctx（emit 可能替换 ctx，替换后写回槽）；
    - run      ：frame.slots[prompt:ctx] = await bus.emit(ctx)。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "prompt_render.emit"
    requires = ("prompt_render.build_ctx", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: PromptRenderFrame) -> PromptRenderFrame:
        ctx = cast(PromptRenderCtx, frame.slots[_CTX_SLOT])
        frame.slots[_CTX_SLOT] = await self._bus.emit(ctx)
        return frame


class _CollectPromptExportSlotsModule:
    """回收插件外溢：把 section_top / section_bottom / extra_hint 从 slot 合并回 ctx。

    - slot     ：prompt_render.collect_exports；
    - requires ：prompt_render.emit + prompt:ctx；
    - produces ：prompt:ctx（原地改 ctx 字段，不替换对象）；
    - run      ：收 prompt:section_top: / prompt:section_bottom: 前缀的槽合并进
      ctx.system_sections_top/bottom；收 prompt:extra_hint: 合并进 ctx.extra_hints。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "prompt_render.collect_exports"
    requires = ("prompt_render.emit", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    async def run(self, frame: PromptRenderFrame) -> PromptRenderFrame:
        ctx = cast(PromptRenderCtx, frame.slots[_CTX_SLOT])
        _append_sections(
            ctx.system_sections_top,
            collect_prefixed_slots(frame.slots, _SECTION_TOP_PREFIX),
        )
        _append_sections(
            ctx.system_sections_bottom,
            collect_prefixed_slots(frame.slots, _SECTION_BOTTOM_PREFIX),
        )
        append_string_exports(
            ctx.extra_hints,
            collect_prefixed_slots(frame.slots, _EXTRA_HINT_PREFIX),
        )
        return frame


class _RenderPromptModule:
    """真正渲染：调 ContextBuilder.render 产出 messages。

    - slot     ：prompt_render.render；
    - requires ：prompt_render.collect_exports + prompt:ctx（保证排在 collect 之后）；
    - produces ：prompt:result；
    - run      ：用收好的完整 ctx 调 context.render(ContextRequest(...), sections)，
      再把 extra_hints 包成 hint message 追加，写入 prompt:result。

    本模块不设 frame.output——output 由链尾的 return 模块产出。
    """

    slot = "prompt_render.render"
    requires = ("prompt_render.collect_exports", _CTX_SLOT)
    produces = (_RESULT_SLOT,)

    def __init__(self, context: ContextBuilder) -> None:
        self._context = context

    async def run(self, frame: PromptRenderFrame) -> PromptRenderFrame:
        ctx = cast(PromptRenderCtx, frame.slots[_CTX_SLOT])
        rendered = self._context.render(
            ContextRequest(
                history=ctx.history,
                current_message=ctx.content,
                media=ctx.media,
                skill_names=ctx.skill_names,
                channel=ctx.channel,
                chat_id=ctx.chat_id,
                message_timestamp=ctx.timestamp,
                retrieved_memory_block=ctx.retrieved_memory_block,
                disabled_sections=ctx.disabled_sections,
                turn_injection_prompt=ctx.turn_injection_prompt,
            ),
            system_sections_top=ctx.system_sections_top,
            system_sections_bottom=ctx.system_sections_bottom,
        )
        messages = list(rendered.messages)
        if ctx.extra_hints:
            messages.append(
                build_context_hint_message(
                    "plugin_hints",
                    "\n".join(ctx.extra_hints),
                )
            )
        frame.slots[_RESULT_SLOT] = PromptRenderResult(messages=messages)
        return frame


class _ReturnPromptRenderResultModule:
    """产出 output：把渲染好的 PromptRenderResult 设为阶段 output，交给下一阶段。

    - slot     ：prompt_render.return；
    - requires ：prompt_render.render + prompt:result（保证排在最后）；
    - run      ：frame.output = frame.slots[prompt:result]。

    本模块是 prompt_render 的链尾，也是「output 职责」的最终归宿——之前的
    build_ctx / emit / collect_exports / render 都不碰 output。
    """

    slot = "prompt_render.return"
    requires = ("prompt_render.render", _RESULT_SLOT)

    async def run(self, frame: PromptRenderFrame) -> PromptRenderFrame:
        frame.output = cast(PromptRenderResult, frame.slots[_RESULT_SLOT])
        return frame


def default_prompt_render_modules(
    bus: EventBus,
    context: ContextBuilder,
    plugin_modules: PromptRenderModules | None = None,
) -> PromptRenderModules:
    """装配 prompt_render 的内置模块链（build_ctx → emit → collect_exports → render → return）。"""
    builtins: PromptRenderModules = [
        _BuildPromptRenderCtxModule(),
        _EmitPromptRenderCtxModule(bus),
        _CollectPromptExportSlotsModule(),
        _RenderPromptModule(context),
        _ReturnPromptRenderResultModule(),
    ]
    return cast(
        PromptRenderModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )


def _append_sections(
    target: list[PromptSectionRender],
    exports: dict[str, object],
) -> None:
    """把插件外溢的 section 合并进目标列表：对象直接用，字符串包装成 PromptSectionRender。"""
    for name, value in exports.items():
        if isinstance(value, PromptSectionRender):
            target.append(value)
        elif isinstance(value, str) and value.strip():
            target.append(
                PromptSectionRender(
                    name=name,
                    content=value,
                    is_static=False,
                )
            )
