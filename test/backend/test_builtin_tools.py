from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from backend.tools import ListDirTool, ReadFileTool, ToolRegistry, WebFetchTool


def test_read_file_supports_paging_and_line_numbers(tmp_path: Path) -> None:
    async def run() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        source = workspace / "example.txt"
        source.write_text("first\nsecond\nthird\n", encoding="utf-8")

        output = await ReadFileTool(workspace).execute(
            path="example.txt", offset=1, limit=1
        )

        assert "2-2 / 3" in output
        assert "     2\u2192second" in output
        assert "offset=2" in output

    asyncio.run(run())


def test_file_tools_reject_paths_outside_workspace(tmp_path: Path) -> None:
    async def run() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace))

        result = await registry.execute("read_file", {"path": str(outside)})

        assert result.status == "error"
        assert "超出允许目录" in result.output

    asyncio.run(run())


def test_list_dir_is_sorted_and_marks_directories(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / "z-file.txt").write_text("z", encoding="utf-8")
        (tmp_path / "A-dir").mkdir()

        output = await ListDirTool(tmp_path).execute(path=".")

        assert output.index("A-dir/") < output.index("z-file.txt")

    asyncio.run(run())


def test_web_fetch_converts_html_and_removes_scripts() -> None:
    async def run() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == "https://example.test/article"
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=(
                    "<html><body><h1>标题</h1><p>Hello "
                    "<a href='https://example.test/source'>world</a></p>"
                    "<script>do_not_include()</script></body></html>"
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            raw = await WebFetchTool(client).execute(
                url="https://example.test/article", format="markdown"
            )
        result = json.loads(raw)

        assert "# 标题" in result["text"]
        assert "world (https://example.test/source)" in result["text"]
        assert "do_not_include" not in result["text"]

    asyncio.run(run())


def test_web_fetch_rejects_non_http_url() -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            registry = ToolRegistry()
            registry.register(WebFetchTool(client))
            result = await registry.execute("web_fetch", {"url": "file:///secret"})

        assert result.status == "error"
        assert "http://" in result.output

    asyncio.run(run())
