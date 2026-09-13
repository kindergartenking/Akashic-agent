"""Asynchronous process lifecycle management used by the shell tools.

The important distinction in this module is that an ``asyncio.Task`` only
waits for a process; the process itself is owned by ``ShellProcessManager``
until it exits or is explicitly stopped.  This lets a short tool call return
an execution id and a later ``write_stdin`` call continue the same process.
"""

from __future__ import annotations

import asyncio
import os
import random
import signal
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


MIN_YIELD_TIME_MS = 250
MIN_EMPTY_YIELD_TIME_MS = 5_000
MAX_YIELD_TIME_MS = 30_000
MAX_WRITE_STDIN_YIELD_TIME_MS = 300_000
DEFAULT_INITIAL_YIELD_TIME_MS = 10_000
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
OUTPUT_MAX_BYTES = 1024 * 1024
MAX_EXECUTIONS = 64
DEFAULT_HARD_TIMEOUT_S = 4 * 3600
MAX_HARD_TIMEOUT_S = 4 * 3600
POST_EXIT_DRAIN_GRACE_S = 0.2
TERMINATION_CONFIRM_TIMEOUT_S = 5.0
INTERRUPT = "\x03"


class UnknownExecutionError(RuntimeError):
    pass


@dataclass
class ExecutionResult:
    output: bytes
    wall_time_ms: int
    original_token_count: int
    output_omitted_bytes: int
    execution_id: int | None
    exit_code: int | None
    output_path: str | None
    finish_reason: str


@dataclass(frozen=True)
class ExecutionCleanupFailure:
    execution_id: int
    error_type: str
    message: str


@dataclass(frozen=True)
class ExecutionCleanupReport:
    attempted_execution_ids: tuple[int, ...]
    cleaned_execution_ids: tuple[int, ...]
    failures: tuple[ExecutionCleanupFailure, ...]


class HeadTailBuffer:
    """Keep a stable head and the newest tail while accounting for omissions."""

    def __init__(self, max_bytes: int = OUTPUT_MAX_BYTES) -> None:
        self.max_bytes = max(0, max_bytes)
        self.head_budget = self.max_bytes // 2
        self.tail_budget = self.max_bytes - self.head_budget
        self.head = bytearray()
        self.tail: deque[int] = deque()
        self.omitted_bytes = 0

    def push_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.max_bytes == 0:
            self.omitted_bytes += len(chunk)
            return
        head_len = min(max(self.head_budget - len(self.head), 0), len(chunk))
        if head_len:
            self.head.extend(chunk[:head_len])
        rest = chunk[head_len:]
        if self.tail_budget == 0:
            self.omitted_bytes += len(rest)
            return
        if len(rest) >= self.tail_budget:
            self.omitted_bytes += len(self.tail) + len(rest) - self.tail_budget
            self.tail.clear()
            self.tail.extend(rest[-self.tail_budget :])
            return
        self.tail.extend(rest)
        excess = max(0, len(self.tail) - self.tail_budget)
        for _ in range(excess):
            self.tail.popleft()
        self.omitted_bytes += excess

    def drain(self) -> "HeadTailBuffer":
        result = HeadTailBuffer(self.max_bytes)
        result.head = self.head
        result.tail = self.tail
        result.omitted_bytes = self.omitted_bytes
        self.head = bytearray()
        self.tail = deque()
        self.omitted_bytes = 0
        return result

    def to_bytes(self) -> bytes:
        return bytes(self.head) + bytes(self.tail)


@dataclass
class _Execution:
    execution_id: int
    owner_session_key: str
    command: str
    process: asyncio.subprocess.Process
    tty: bool
    output_path: str
    log_file: BinaryIO
    started_at: float
    output_buffer: HeadTailBuffer = field(default_factory=HeadTailBuffer)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_event: asyncio.Event = field(default_factory=asyncio.Event)
    exit_event: asyncio.Event = field(default_factory=asyncio.Event)
    pump_task: asyncio.Task[None] | None = None
    hard_timeout_task: asyncio.Task[None] | None = None
    finish_reason: str = "natural"
    master_fd: int | None = None
    failure_message: str | None = None


def clamp_initial_yield_time(value: int) -> int:
    return max(MIN_YIELD_TIME_MS, min(MAX_YIELD_TIME_MS, int(value)))


def clamp_write_stdin_yield_time(value: int, *, has_input: bool, max_empty_ms: int) -> int:
    lower = MIN_YIELD_TIME_MS if has_input else MIN_EMPTY_YIELD_TIME_MS
    upper = MAX_YIELD_TIME_MS if has_input else max_empty_ms
    return max(lower, min(upper, int(value)))


class ShellProcessManager:
    """Create, continue, and clean up shell processes."""

    def __init__(
        self,
        *,
        max_executions: int = MAX_EXECUTIONS,
        max_write_stdin_yield_time_ms: int = MAX_WRITE_STDIN_YIELD_TIME_MS,
        output_dir: Path | None = None,
    ) -> None:
        if max_executions < 1:
            raise ValueError("max_executions 必须大于零")
        self._max_executions = max_executions
        self._max_write_stdin_yield_time_ms = max(
            max_write_stdin_yield_time_ms, MIN_EMPTY_YIELD_TIME_MS
        )
        self._output_dir = output_dir
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
        self._executions: dict[int, _Execution] = {}
        self._lock = asyncio.Lock()
        self._spawn_lock = asyncio.Lock()
        self._rng = random.SystemRandom()

    async def exec_command(
        self,
        *,
        command: str,
        argv: list[str],
        cwd: Path | None,
        env: dict[str, str],
        tty: bool,
        yield_time_ms: int,
        max_output_tokens: int,
        hard_timeout_s: int,
        owner_session_key: str,
    ) -> ExecutionResult:
        async with self._spawn_lock:
            await self._prune_if_needed()
            execution_id = await self._allocate_execution_id()
            execution = await self._spawn(
                execution_id=execution_id,
                owner_session_key=owner_session_key,
                command=command,
                argv=argv,
                cwd=cwd,
                env=env,
                tty=tty,
            )
            async with self._lock:
                self._executions[execution_id] = execution
            execution.pump_task = asyncio.create_task(
                self._pump_execution(execution), name=f"shell-pump:{execution_id}"
            )
            execution.hard_timeout_task = asyncio.create_task(
                self._enforce_hard_timeout(execution, hard_timeout_s),
                name=f"shell-timeout:{execution_id}",
            )
        started = time.monotonic()
        collected = await self._collect_until_deadline(
            execution,
            started + clamp_initial_yield_time(yield_time_ms) / 1000,
        )
        return await self._build_result(execution, collected, started, max_output_tokens)

    async def write_stdin(
        self,
        *,
        execution_id: int,
        chars: str,
        yield_time_ms: int,
        max_output_tokens: int,
        owner_session_key: str,
    ) -> ExecutionResult:
        execution = await self._get_owned_execution(execution_id, owner_session_key)
        if chars:
            if execution.tty:
                if execution.master_fd is None:
                    raise RuntimeError("tty 输入通道不可用")
                if chars == INTERRUPT:
                    self._interrupt_process_group(execution.process)
                else:
                    os.write(execution.master_fd, chars.encode())
            elif chars == INTERRUPT:
                self._interrupt_process_group(execution.process)
            else:
                raise RuntimeError(f"execution_id={execution_id} 未启用 tty，stdin 已关闭")
        wait_ms = clamp_write_stdin_yield_time(
            yield_time_ms,
            has_input=bool(chars),
            max_empty_ms=self._max_write_stdin_yield_time_ms,
        )
        started = time.monotonic()
        collected = await self._collect_until_deadline(execution, started + wait_ms / 1000)
        return await self._build_result(execution, collected, started, max_output_tokens)

    async def terminate_execution(self, execution_id: int, *, owner_session_key: str) -> bool:
        try:
            execution = await self._get_owned_execution(execution_id, owner_session_key)
        except UnknownExecutionError:
            return False
        execution.finish_reason = "stopped"
        await self._terminate_confirmed(execution)
        await self._remove_execution(execution)
        return True

    async def terminate_owner(self, owner_session_key: str) -> ExecutionCleanupReport:
        async with self._lock:
            executions = [e for e in self._executions.values() if e.owner_session_key == owner_session_key]
        return await self._terminate_many(executions)

    async def shutdown(self) -> ExecutionCleanupReport:
        async with self._lock:
            executions = list(self._executions.values())
        return await self._terminate_many(executions)

    async def active_execution_ids(self) -> list[int]:
        async with self._lock:
            return sorted(self._executions)

    async def _spawn(self, *, execution_id: int, owner_session_key: str, command: str,
                     argv: list[str], cwd: Path | None, env: dict[str, str], tty: bool) -> _Execution:
        fd, output_path = tempfile.mkstemp(
            prefix=f"akashic-exec-{execution_id}-", suffix=".log", dir=self._output_dir
        )
        log_file = os.fdopen(fd, "wb", buffering=0)
        master_fd: int | None = None
        slave_fd: int | None = None
        options: dict[str, object] = {"cwd": str(cwd) if cwd else None, "env": env}
        if tty:
            if os.name == "nt":
                log_file.close()
                Path(output_path).unlink(missing_ok=True)
                raise RuntimeError("当前平台不支持 tty=true；需要 ConPTY 实现")
            import pty

            master_fd, slave_fd = pty.openpty()
            os.set_blocking(master_fd, True)
            options.update(stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, start_new_session=True)
        else:
            options.update(
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(*argv, **options)
        except BaseException:
            log_file.close()
            Path(output_path).unlink(missing_ok=True)
            if master_fd is not None:
                os.close(master_fd)
            if slave_fd is not None:
                os.close(slave_fd)
            raise
        finally:
            if slave_fd is not None:
                os.close(slave_fd)
        return _Execution(
            execution_id=execution_id,
            owner_session_key=owner_session_key,
            command=command,
            process=process,
            tty=tty,
            output_path=output_path,
            log_file=log_file,
            started_at=time.monotonic(),
            master_fd=master_fd,
        )

    async def _pump_execution(self, execution: _Execution) -> None:
        async def read_output() -> None:
            if execution.tty:
                assert execution.master_fd is not None
                while True:
                    try:
                        chunk = await asyncio.to_thread(os.read, execution.master_fd, 4096)
                    except OSError:
                        return
                    if not chunk:
                        return
                    await self._append_output(execution, chunk)
            else:
                assert execution.process.stdout is not None
                while True:
                    chunk = await execution.process.stdout.read(4096)
                    if not chunk:
                        return
                    await self._append_output(execution, chunk)

        reader = asyncio.create_task(read_output(), name=f"shell-reader:{execution.execution_id}")
        try:
            await execution.process.wait()
            execution.exit_event.set()
            execution.output_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(reader), POST_EXIT_DRAIN_GRACE_S)
            except asyncio.TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        except asyncio.CancelledError:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
        except Exception as exc:
            execution.failure_message = str(exc)
        finally:
            execution.exit_event.set()
            execution.output_event.set()
            if execution.master_fd is not None:
                try:
                    os.close(execution.master_fd)
                except OSError:
                    pass
                execution.master_fd = None

    async def _append_output(self, execution: _Execution, chunk: bytes) -> None:
        execution.log_file.write(chunk)
        async with execution.output_lock:
            execution.output_buffer.push_chunk(chunk)
            execution.output_event.set()

    async def _collect_until_deadline(self, execution: _Execution, deadline: float) -> HeadTailBuffer:
        collected = HeadTailBuffer()
        while True:
            async with execution.output_lock:
                chunk = execution.output_buffer.drain()
            if chunk.to_bytes() or chunk.omitted_bytes:
                collected.push_chunk(chunk.to_bytes())
                collected.omitted_bytes += chunk.omitted_bytes
                continue
            if execution.exit_event.is_set():
                return collected
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return collected
            execution.output_event.clear()
            try:
                await asyncio.wait_for(execution.output_event.wait(), remaining)
            except asyncio.TimeoutError:
                return collected

    async def _build_result(self, execution: _Execution, collected: HeadTailBuffer,
                            started: float, max_output_tokens: int) -> ExecutionResult:
        output = collected.to_bytes()
        omitted = collected.omitted_bytes
        # A token is approximately four UTF-8 bytes. Keep the manager bounded;
        # format_execution_result performs the final textual representation.
        byte_budget = max(0, int(max_output_tokens)) * 4
        if byte_budget and len(output) > byte_budget:
            omitted += len(output) - byte_budget
            output = output[:byte_budget]
        running = execution.process.returncode is None and not execution.exit_event.is_set()
        if running:
            execution_id: int | None = execution.execution_id
            exit_code = None
            reason = "yield"
        else:
            execution_id = None
            exit_code = execution.process.returncode
            reason = execution.finish_reason
            if reason == "natural":
                reason = "natural"
        if not running:
            await self._remove_execution(execution)
        return ExecutionResult(
            output=output,
            wall_time_ms=int((time.monotonic() - started) * 1000),
            original_token_count=max(0, (len(output) + omitted) // 4),
            output_omitted_bytes=omitted,
            execution_id=execution_id,
            exit_code=exit_code,
            output_path=execution.output_path,
            finish_reason=reason,
        )

    async def _enforce_hard_timeout(self, execution: _Execution, timeout_s: int) -> None:
        try:
            await asyncio.sleep(max(1, timeout_s))
            if execution.process.returncode is None:
                execution.finish_reason = "timeout"
                await self._terminate_confirmed(execution)
        except asyncio.CancelledError:
            return

    async def _get_owned_execution(self, execution_id: int, owner_session_key: str) -> _Execution:
        async with self._lock:
            execution = self._executions.get(execution_id)
        if execution is None or execution.owner_session_key != owner_session_key:
            raise UnknownExecutionError(f"未知或无权访问 execution_id={execution_id}")
        return execution

    async def _terminate_confirmed(self, execution: _Execution) -> None:
        if execution.process.returncode is not None:
            return
        try:
            self._interrupt_process_group(execution.process)
            await asyncio.wait_for(execution.process.wait(), TERMINATION_CONFIRM_TIMEOUT_S)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            if execution.process.returncode is None:
                execution.process.kill()
                await execution.process.wait()

    def _interrupt_process_group(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                process.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    async def _remove_execution(self, execution: _Execution) -> None:
        async with self._lock:
            current = self._executions.get(execution.execution_id)
            if current is not execution:
                return
            self._executions.pop(execution.execution_id, None)
        for task in (execution.pump_task, execution.hard_timeout_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(
            *(task for task in (execution.pump_task, execution.hard_timeout_task) if task is not None),
            return_exceptions=True,
        )
        execution.log_file.close()

    async def _terminate_many(self, executions: list[_Execution]) -> ExecutionCleanupReport:
        attempted = tuple(e.execution_id for e in executions)
        cleaned: list[int] = []
        failures: list[ExecutionCleanupFailure] = []
        for execution in executions:
            try:
                execution.finish_reason = "stopped"
                await self._terminate_confirmed(execution)
                await self._remove_execution(execution)
                cleaned.append(execution.execution_id)
            except Exception as exc:
                failures.append(ExecutionCleanupFailure(execution.execution_id, type(exc).__name__, str(exc)))
        return ExecutionCleanupReport(attempted, tuple(cleaned), tuple(failures))

    async def _prune_if_needed(self) -> None:
        async with self._lock:
            active = list(self._executions.values())
        if len(active) < self._max_executions:
            return
        finished = [e for e in active if e.process.returncode is not None]
        for execution in finished:
            await self._remove_execution(execution)
        async with self._lock:
            if len(self._executions) >= self._max_executions:
                raise RuntimeError("Shell execution 数量已达到上限")

    async def _allocate_execution_id(self) -> int:
        async with self._lock:
            for _ in range(100):
                candidate = self._rng.randint(1_000, 99_999)
                if candidate not in self._executions:
                    return candidate
        raise RuntimeError("无法分配 execution_id")


def format_execution_result(result: ExecutionResult, *, command: str | None = None) -> str:
    text = result.output.decode("utf-8", errors="replace")
    payload: dict[str, object] = {
        "output": text,
        "execution_id": result.execution_id,
        "exit_code": result.exit_code,
        "finish_reason": result.finish_reason,
        "wall_time_ms": result.wall_time_ms,
        "output_omitted_bytes": result.output_omitted_bytes,
    }
    if result.output_path:
        payload["output_path"] = result.output_path
    if command is not None:
        payload["command"] = command
    return __import__("json").dumps(payload, ensure_ascii=False)
