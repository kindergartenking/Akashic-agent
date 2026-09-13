from __future__ import annotations

import asyncio
import difflib
import os
import tempfile
from pathlib import Path
from typing import Any

from .base import Tool


_DEFAULT_READ_LINES = 400
_MAX_READ_LINES = 2_000
_MAX_READ_CHARS = 80_000
_BINARY_PROBE_BYTES = 8_192
_MAX_DIRECTORY_ENTRIES = 500

_FILE_LOCKS: dict[str, asyncio.Lock] = {}


def _file_lock(path: Path) -> asyncio.Lock:
    key = str(path)
    return _FILE_LOCKS.setdefault(key, asyncio.Lock())


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _resolve_inside(path: str, allowed_dir: Path) -> Path:
    """Resolve a model-supplied path and keep it inside allowed_dir."""

    root = allowed_dir.expanduser().resolve()
    candidate = Path(path).expanduser()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PermissionError(f"路径 {path} 超出允许目录 {root}")
    return resolved


class ReadFileTool(Tool):
    """Read a bounded page of a text file inside the workspace."""

    name = "read_file"
    description = (
        "读取工作区内的文本文件并返回带行号的内容。支持 offset/limit 分页；"
        "不能读取工作区之外的路径，也不用于读取二进制文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区内的文件路径"},
            "offset": {
                "type": "integer",
                "description": "跳过的行数，0-based，默认 0",
                "minimum": 0,
            },
            "limit": {
                "type": "integer",
                "description": f"最多读取的行数，默认 {_DEFAULT_READ_LINES}，最大 {_MAX_READ_LINES}",
                "minimum": 1,
                "maximum": _MAX_READ_LINES,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, allowed_dir: Path) -> None:
        self._allowed_dir = allowed_dir

    async def execute(self, **arguments: Any) -> str:
        path = str(arguments["path"])
        offset = int(arguments.get("offset", 0))
        limit = int(arguments.get("limit", _DEFAULT_READ_LINES))
        file_path = _resolve_inside(path, self._allowed_dir)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"路径不是文件：{path}")

        raw = await asyncio.to_thread(file_path.read_bytes)
        if b"\x00" in raw[:_BINARY_PROBE_BYTES]:
            raise ValueError(f"{path} 看起来是二进制文件")
        text = raw.decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        page = lines[offset : offset + limit]
        numbered = "\n".join(
            f"{line_number:6}\u2192{line}"
            for line_number, line in enumerate(page, start=offset + 1)
        )
        truncated_by_chars = len(numbered) > _MAX_READ_CHARS
        if truncated_by_chars:
            numbered = numbered[:_MAX_READ_CHARS]

        end = offset + len(page)
        header = f"文件: {file_path}\n行: {offset + 1}-{end} / {len(lines)}"
        notes: list[str] = []
        if end < len(lines):
            notes.append(f"还有 {len(lines) - end} 行，可使用 offset={end} 继续读取")
        if truncated_by_chars:
            notes.append(f"本页内容已截断到 {_MAX_READ_CHARS} 字符")
        if not page:
            numbered = "[没有可返回的行]"
        suffix = "\n提示: " + "；".join(notes) if notes else ""
        return f"{header}\n\n{numbered}{suffix}"


class ListDirTool(Tool):
    """List a bounded number of entries inside a workspace directory."""

    name = "list_dir"
    description = "列举工作区内目录的直接子项；目录以 / 结尾，不会递归读取。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "工作区内的目录路径；工作区根目录使用 .",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, allowed_dir: Path) -> None:
        self._allowed_dir = allowed_dir

    async def execute(self, **arguments: Any) -> str:
        path = str(arguments["path"])
        dir_path = _resolve_inside(path, self._allowed_dir)
        if not dir_path.exists():
            raise FileNotFoundError(f"目录不存在：{path}")
        if not dir_path.is_dir():
            raise NotADirectoryError(f"路径不是目录：{path}")

        entries = await asyncio.to_thread(lambda: sorted(dir_path.iterdir(), key=lambda item: item.name.lower()))
        visible = entries[:_MAX_DIRECTORY_ENTRIES]
        items = [f"{item.name}/" if item.is_dir() else item.name for item in visible]
        if not items:
            return f"目录 {dir_path} 为空"
        if len(entries) > len(visible):
            items.append(f"... 另有 {len(entries) - len(visible)} 项未显示")
        return f"目录: {dir_path}\n\n" + "\n".join(items)


class WriteFileTool(Tool):
    name = "write_file"
    description = "在工作区内完整覆盖写入文本文件，并自动创建父目录。已有文件建议先 read_file，再用 edit_file 精确修改。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区内的文件路径"},
            "content": {"type": "string", "description": "要写入的完整文本内容"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, allowed_dir: Path) -> None:
        self._allowed_dir = allowed_dir

    async def execute(self, **arguments: Any) -> str:
        path = str(arguments["path"])
        content = str(arguments["content"])
        file_path = _resolve_inside(path, self._allowed_dir)
        if file_path.exists() and file_path.is_dir():
            raise IsADirectoryError(f"路径是目录：{path}")
        async with _file_lock(file_path):
            await asyncio.to_thread(_atomic_write, file_path, content)
        return f"已写入 {len(content)} 字节到 {path}"


class EditFileTool(Tool):
    name = "edit_file"
    description = "在工作区内精确替换文件中的 old_text；old_text 必须与原文完全一致，可用 replace_all 替换所有匹配。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区内的文件路径"},
            "old_text": {"type": "string", "description": "要精确查找的原文"},
            "new_text": {"type": "string", "description": "替换后的文本"},
            "replace_all": {"type": "boolean", "description": "是否替换全部匹配，默认 false", "default": False},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def __init__(self, allowed_dir: Path) -> None:
        self._allowed_dir = allowed_dir

    async def execute(self, **arguments: Any) -> str:
        path = str(arguments["path"])
        old_text = str(arguments["old_text"])
        new_text = str(arguments["new_text"])
        replace_all = bool(arguments.get("replace_all", False))
        file_path = _resolve_inside(path, self._allowed_dir)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"路径不是文件：{path}")

        async with _file_lock(file_path):
            raw = await asyncio.to_thread(file_path.read_bytes)
            bom = raw.startswith(b"\xef\xbb\xbf")
            text = raw.decode("utf-8-sig")
            matched = old_text
            replacement = new_text
            if matched not in text and "\r\n" in text and "\r" not in text.replace("\r\n", ""):
                candidate = old_text.replace("\n", "\r\n")
                if candidate in text:
                    matched, replacement = candidate, new_text.replace("\n", "\r\n")
            if matched not in text:
                return "错误：未找到 old_text，请确保与文件内容完全一致。"
            count = text.count(matched)
            if count > 1 and not replace_all:
                return f"警告：old_text 在文件中出现了 {count} 次。如需全部替换，请设 replace_all=true。"
            updated = text.replace(matched, replacement, -1 if replace_all else 1)
            if bom:
                updated = "\ufeff" + updated
            await asyncio.to_thread(_atomic_write, file_path, updated)
        diff = "\n".join(difflib.unified_diff(text.splitlines(), updated.lstrip("\ufeff").splitlines(), fromfile=f"{path} (before)", tofile=f"{path} (after)", lineterm=""))
        suffix = f"\n\n```diff\n{diff}\n```" if diff else ""
        return f"已成功编辑 {path}（替换 {count if replace_all else 1} 处）{suffix}"
