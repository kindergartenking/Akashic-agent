from __future__ import annotations

import asyncio
import json
from pathlib import Path

from backend.session_store import SessionStore
from backend.tools import EditFileTool, FetchMessagesTool, LoadSkillTool, SearchMessagesTool, ToolRegistry, ToolSearchTool, WriteFileTool


def test_file_mutation_tools_and_message_tools(tmp_path: Path) -> None:
    async def run() -> None:
        write = WriteFileTool(tmp_path)
        edit = EditFileTool(tmp_path)
        assert "已写入" in await write.execute(path="a.txt", content="hello\nhello")
        assert "出现了 2 次" in await edit.execute(path="a.txt", old_text="hello", new_text="hi")
        assert "替换 2 处" in await edit.execute(path="a.txt", old_text="hello", new_text="hi", replace_all=True)

        store = SessionStore(tmp_path / "sessions.db")
        store.record_user_message("s1", "t1", "find this")
        store.complete_turn("s1", "t1", "answer")
        found = json.loads(await SearchMessagesTool(store).execute(query="find"))
        assert found["count"] == 1
        fetched = json.loads(await FetchMessagesTool(store).execute(source_ref=found["messages"][0]["source_ref"]))
        assert fetched["messages"][0]["content"] == "find this"

    asyncio.run(run())


def test_skill_and_tool_search(tmp_path: Path) -> None:
    async def run() -> None:
        skill_dir = tmp_path / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Demo", encoding="utf-8")
        loaded = json.loads(await LoadSkillTool(tmp_path).execute(skill="demo"))
        assert loaded["content"] == "# Demo"
        registry = ToolRegistry()
        registry.register(WriteFileTool(tmp_path))
        result = json.loads(await ToolSearchTool(registry).execute(query="write"))
        assert "write_file" in result["unlocked"]

    asyncio.run(run())
