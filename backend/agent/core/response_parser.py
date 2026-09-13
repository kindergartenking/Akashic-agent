"""回复解析（M1b 桩）。

原版 `parse_response` 解析模型输出里的 meme_tag / 工具链等结构。M1b 直接
把原始回复当作 clean_text，metadata 仅携带 raw_text。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResponseMetadata:
    raw_text: str = ""


@dataclass
class ParsedResponse:
    clean_text: str
    metadata: ResponseMetadata


def parse_response(
    raw_reply: str,
    *,
    tool_chain: list[dict[str, Any]] | None = None,
) -> ParsedResponse:
    return ParsedResponse(
        clean_text=raw_reply,
        metadata=ResponseMetadata(raw_text=raw_reply),
    )
