from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .base import Tool
from .shell_command import resolve_shell
from .shell_security import validate_command, validate_network_command
from .unified_exec import (
    DEFAULT_HARD_TIMEOUT_S,
    DEFAULT_INITIAL_YIELD_TIME_MS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_HARD_TIMEOUT_S,
    ExecutionCleanupReport,
    ExecutionResult,
    ShellProcessManager,
    format_execution_result,
)

logger = logging.getLogger(__name__)

current_shell_owner: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_shell_owner", default=None
)

_MAX_OUTPUT = 30_000
_LOCAL_OWNER_PREFIX = "local-shell"
_UNIFIED_EXEC_ENV = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "LANG": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "COLORTERM": "",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
}


def _error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _owner_session_key(manager: ShellProcessManager) -> str:
    return current_shell_owner.get() or f"{_LOCAL_OWNER_PREFIX}:{id(manager)}"


def _execution_outcome(result: ExecutionResult) -> str:
    if result.execution_id is not None:
        return "running"
    if result.finish_reason == "timeout":
        return "timed_out"
    return "succeeded" if result.exit_code == 0 else "failed"


def _shell_env() -> dict[str, str]:
    env = {str(k): str(v) for k, v in os.environ.items()}
    for key, value in _UNIFIED_EXEC_ENV.items():
        env[key] = value
    # User-local binaries are often missing from a service's PATH.
    candidates = [Path.home() / ".local" / "bin"]
    if env.get("NVM_BIN"):
        candidates.append(Path(env["NVM_BIN"]).expanduser())
    nvm_root = Path(env.get("NVM_DIR", str(Path.home() / ".nvm"))).expanduser()
    try:
        versions = sorted((p for p in (nvm_root / "versions" / "node").iterdir() if p.is_dir()), reverse=True)
        candidates.extend(p / "bin" for p in versions)
    except OSError:
        pass
    current = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    seen = set(current)
    prefix = [str(p) for p in candidates if p.is_dir() and str(p) not in seen]
    env["PATH"] = os.pathsep.join(prefix + current)
    return env


class ShellTool(Tool):
    name = "shell"
    execution_timeout = None

    def __init__(self, manager: ShellProcessManager | None = None, *, allow_network: bool = True,
                 working_dir: Path | None = None, restricted_dir: Path | None = None,
                 spawn_hook: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.manager = manager or ShellProcessManager()
        self._allow_network = allow_network
        self._working_dir = working_dir
        self._restricted_dir = restricted_dir.expanduser().resolve() if restricted_dir else None
        self._spawn_hook = spawn_hook

    @property
    def description(self) -> str:
        return (
            "在 shell 中执行命令。短命令直接返回 exit_code；仍在运行时返回 execution_id，"
            "之后用 write_stdin 读取增量输出或向 tty 输入。需要交互输入时设置 tty=true；"
            "放弃运行中的命令前调用 task_stop。网络命令仅允许公网 HTTP(S)，禁止上传或写文件。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "description": {"type": "string", "description": "用 5-10 字描述命令作用"},
                "cwd": {"type": "string", "description": "可选工作目录"},
                "shell": {"type": "string", "description": "要启动的 shell binary"},
                "login": {"type": "boolean", "description": "是否使用 login shell，默认 true"},
                "tty": {"type": "boolean", "description": "是否启用伪终端，默认 false"},
                "yield_time_ms": {"type": "integer", "minimum": 250, "maximum": 30_000, "description": "首次等待毫秒数"},
                "max_output_tokens": {"type": "integer", "minimum": 0, "description": "本次结果的近似输出 token 预算"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": MAX_HARD_TIMEOUT_S, "description": "进程组硬超时秒数"},
            },
            "required": ["command", "description"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        removed = sorted(set(kwargs) & {"run_in_background", "auto_promote"})
        if removed:
            return _error(f"shell 已移除参数: {', '.join(removed)}")
        command = str(kwargs.get("command", "")).strip()
        if not command:
            return _error("命令不能为空")
        description = str(kwargs.get("description", ""))
        yield_time_ms = int(kwargs.get("yield_time_ms", DEFAULT_INITIAL_YIELD_TIME_MS))
        max_output_tokens = int(kwargs.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        hard_timeout_s = int(kwargs.get("timeout", DEFAULT_HARD_TIMEOUT_S))
        if max_output_tokens < 0:
            return _error("max_output_tokens 不能为负数")
        if hard_timeout_s < 1 or hard_timeout_s > MAX_HARD_TIMEOUT_S:
            return _error(f"timeout 必须在 1 到 {MAX_HARD_TIMEOUT_S} 秒之间")
        try:
            selected_shell = resolve_shell(None if kwargs.get("shell") is None else str(kwargs["shell"]))
        except ValueError as exc:
            return _error(str(exc))
        cwd = self._working_dir
        if kwargs.get("cwd") not in (None, ""):
            cwd = Path(str(kwargs["cwd"])).expanduser()
        env = _shell_env()
        if self._spawn_hook is not None:
            hooked = self._spawn_hook({"command": command, "cwd": str(cwd) if cwd else None, "env": env})
            command = str(hooked.get("command", command)).strip()
            cwd = Path(str(hooked["cwd"])) if hooked.get("cwd") else None
            if isinstance(hooked.get("env"), dict):
                env = {str(k): str(v) for k, v in hooked["env"].items()}
        if self._restricted_dir is not None and cwd is None:
            cwd = self._restricted_dir
        validation_error = validate_command(
            command, allow_network=self._allow_network,
            restricted_dir=self._restricted_dir, cwd=cwd,
        )
        if validation_error:
            return _error(validation_error)
        operation_id = uuid4().hex
        command_fp = hashlib.sha256(command.encode()).hexdigest()[:16]
        logger.info("shell execution admitted operation=%s fp=%s bytes=%d cwd=%s", operation_id, command_fp, len(command.encode()), cwd or "")
        result = await self.manager.exec_command(
            command=command,
            argv=selected_shell.derive_argv(command, login=bool(kwargs.get("login", True))),
            cwd=cwd,
            env=env,
            tty=bool(kwargs.get("tty", False)),
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
            hard_timeout_s=hard_timeout_s,
            owner_session_key=_owner_session_key(self.manager),
        )
        return format_execution_result(result, command=command)

    async def shutdown(self) -> ExecutionCleanupReport:
        return await self.manager.shutdown()

    async def terminate_owner(self, owner_session_key: str) -> ExecutionCleanupReport:
        return await self.manager.terminate_owner(owner_session_key)


class ShellWriteStdinTool(Tool):
    name = "write_stdin"
    execution_timeout = None
    description = "等待 shell execution 的新增输出，或向 tty=true 的 execution 写入字符。"
    parameters = {
        "type": "object",
        "properties": {
            "execution_id": {"type": "integer", "description": "shell 返回的 execution_id"},
            "chars": {"type": "string", "description": "写入 PTY 的字符；留空表示等待和读取"},
            "yield_time_ms": {"type": "integer", "minimum": 250, "maximum": 300_000, "description": "本次等待毫秒数"},
            "max_output_tokens": {"type": "integer", "minimum": 0, "description": "本次结果的近似输出 token 预算"},
        },
        "required": ["execution_id"],
        "additionalProperties": False,
    }

    def __init__(self, manager: ShellProcessManager) -> None:
        self.manager = manager

    async def execute(self, **kwargs: Any) -> str:
        chars = str(kwargs.get("chars", ""))
        default_wait = 5_000 if not chars else 250
        max_output_tokens = int(kwargs.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        if max_output_tokens < 0:
            return _error("max_output_tokens 不能为负数")
        result = await self.manager.write_stdin(
            execution_id=int(kwargs.get("execution_id", 0)), chars=chars,
            yield_time_ms=int(kwargs.get("yield_time_ms", default_wait)),
            max_output_tokens=max_output_tokens,
            owner_session_key=_owner_session_key(self.manager),
        )
        return format_execution_result(result)


class ShellTaskStopTool(Tool):
    name = "task_stop"
    execution_timeout = None
    description = "终止 shell execution 的进程组，并释放 execution_id。"
    parameters = {
        "type": "object",
        "properties": {"execution_id": {"type": "integer", "description": "shell 返回的 execution_id"}},
        "required": ["execution_id"],
        "additionalProperties": False,
    }

    def __init__(self, manager: ShellProcessManager) -> None:
        self.manager = manager

    async def execute(self, **kwargs: Any) -> str:
        if "task_id" in kwargs:
            return _error("task_stop 已移除 task_id；请使用 execution_id")
        execution_id = int(kwargs.get("execution_id", 0))
        stopped = await self.manager.terminate_execution(
            execution_id, owner_session_key=_owner_session_key(self.manager)
        )
        return json.dumps({
            "execution_id": execution_id,
            "process_status": "stopped" if stopped else "unknown",
            "status": "stopped" if stopped else "not_found",
        }, ensure_ascii=False)


_validate_command = validate_command
_validate_network_command = validate_network_command

