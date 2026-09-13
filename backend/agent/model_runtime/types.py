from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    max_output_tokens: int
    max_context_window: int | None = None
    supported_reasoning_efforts: tuple[str, ...] = ()
    default_reasoning_effort: str | None = None
    input_modalities: tuple[str, ...] = ("text",)
    supports_parallel_tool_calls: bool = True
    supports_reasoning_summaries: bool = False
    use_responses_lite: bool = False
