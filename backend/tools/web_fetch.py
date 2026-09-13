from __future__ import annotations

import html
import json
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import Tool


_MAX_BYTES = 5 * 1024 * 1024
_MAX_TEXT_CHARS = 50_000
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
_SKIPPED_TAGS = {"script", "style", "noscript", "iframe", "object", "embed"}
_BLOCK_TAGS = {"article", "aside", "blockquote", "br", "div", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main", "nav", "p", "section", "table", "tr"}


class _HTMLContentParser(HTMLParser):
    def __init__(self, *, markdown: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._markdown = markdown
        self._skip_depth = 0
        self._parts: list[str] = []
        self._links: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        if self._markdown and tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self._parts.append("#" * int(tag[1]) + " ")
        elif self._markdown and tag == "li":
            self._parts.append("- ")
        elif self._markdown and tag == "a":
            self._links.append(dict(attrs).get("href"))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if self._markdown and tag == "a" and self._links:
            href = self._links.pop()
            if href:
                self._parts.append(f" ({href})")
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def result(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


class WebFetchTool(Tool):
    """Fetch bounded HTTP(S) text content for the model."""

    name = "web_fetch"
    description = (
        "抓取一个 HTTP/HTTPS URL。支持 text、markdown、html 输出；"
        "适合在 web_search 找到链接后读取具体页面，不用于二进制文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "必须以 http:// 或 https:// 开头的完整 URL",
            },
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "html"],
                "description": "输出格式，默认 markdown",
            },
            "timeout": {
                "type": "integer",
                "description": f"请求超时秒数，默认 {_DEFAULT_TIMEOUT}，最大 {_MAX_TIMEOUT}",
                "minimum": 1,
                "maximum": _MAX_TIMEOUT,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def execute(self, **arguments: Any) -> str:
        url = str(arguments["url"]).strip()
        fmt = str(arguments.get("format", "markdown"))
        timeout = int(arguments.get("timeout", _DEFAULT_TIMEOUT))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL 必须是完整的 http:// 或 https:// 地址")

        headers = {
            "User-Agent": "akashic-mvp/1.0",
            "Accept": "text/html, text/markdown, text/plain, application/json;q=0.9, */*;q=0.1",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        async with self._http.stream(
            "GET",
            url,
            headers=headers,
            follow_redirects=True,
            timeout=float(timeout),
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > _MAX_BYTES:
                        raise ValueError("响应超过 5MB 限制")
                except ValueError as exc:
                    if "5MB" in str(exc):
                        raise
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > _MAX_BYTES:
                    raise ValueError("响应超过 5MB 限制")
                chunks.append(chunk)
            body = b"".join(chunks)
            content_type = response.headers.get("content-type", "").lower()
            final_url = str(response.url)
            status = response.status_code
            encoding = response.encoding or "utf-8"

        if any(kind in content_type for kind in ("application/pdf", "application/octet-stream", "image/", "video/", "audio/")):
            raise ValueError(f"不支持二进制内容：{content_type or 'unknown'}")

        raw_text = body.decode(encoding, errors="replace")
        is_html = "text/html" in content_type or "application/xhtml+xml" in content_type
        if fmt == "html" or not is_html:
            text = raw_text
        else:
            parser = _HTMLContentParser(markdown=fmt == "markdown")
            parser.feed(raw_text)
            parser.close()
            text = parser.result()
        text = html.unescape(text)
        truncated = len(text) > _MAX_TEXT_CHARS
        if truncated:
            text = text[:_MAX_TEXT_CHARS]

        result: dict[str, Any] = {
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "format": fmt,
            "length": len(text),
            "text": text,
        }
        if truncated:
            result["truncated"] = True
            result["note"] = f"内容已截断到 {_MAX_TEXT_CHARS} 字符"
        return json.dumps(result, ensure_ascii=False)
