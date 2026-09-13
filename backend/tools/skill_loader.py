from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import Tool


class LoadSkillTool(Tool):
    name = "load_skill"
    description = "按名称加载工作区 skills/<name>/SKILL.md 的完整技能指令；不存在时列出当前可用技能。"
    parameters = {
        "type": "object",
        "properties": {"skill": {"type": "string", "description": "技能名称，例如 memory"}},
        "required": ["skill"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path) -> None:
        self._root = (workspace / "skills").resolve()
        self.description = (
            "按名称加载工作区 skills/<name>/SKILL.md 的完整技能指令；"
            "用户的问题匹配某个技能时必须先调用本工具。"
        )
        available = [name for name, _ in self._records()]
        if available:
            self.description += f" 当前可用 Skill：{', '.join(available)}。"

    def _records(self) -> list[tuple[str, Path]]:
        if not self._root.is_dir():
            return []
        return sorted(
            [(item.name, item / "SKILL.md") for item in self._root.iterdir() if item.is_dir() and (item / "SKILL.md").is_file()],
            key=lambda pair: pair[0].lower(),
        )

    async def execute(self, **arguments: Any) -> str:
        name = str(arguments.get("skill", "")).strip()
        if not name:
            return "错误：缺少 skill 名称。"
        if Path(name).name != name or name in {".", ".."}:
            return "错误：skill 名称只能是单个目录名。"
        skill_file = self._root / name / "SKILL.md"
        if not skill_file.is_file():
            available = [item[0] for item in self._records()]
            return f"错误：未找到 skill：{name}。可用技能：{', '.join(available) if available else '无'}"
        content = skill_file.read_text(encoding="utf-8")
        return json.dumps({"name": name, "source": "workspace", "base_directory": str(skill_file.parent), "content": content}, ensure_ascii=False)
