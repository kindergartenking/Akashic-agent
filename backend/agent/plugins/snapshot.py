"""M0 快照桩（shim）。

完整版 `agent/plugins/snapshot.py` 依赖 generation/jobs/specs/mcp/skills 等 M2 模块
（`RuntimeSnapshotCompiler` + `PluginGeneration` 装配）。M0 只提供 `EventBus` 所需的
lease + ContextVar + stable/latest 双指针语义，语义与完整版逐条对齐，区别仅在：

- 本桩不含 `RuntimeSnapshotCompiler`（M2 补齐）
- `RuntimeSnapshot.generations` 恒为空（M2 填 `PluginGeneration`）
- generation 级 lease 记账循环因 generations 为空而 no-op（M2 自动生效）

M2 替换本文件时不得破坏以下不变式：
1. bind/reset/get_current 用 ContextVar，且校验 `owner_task` 与 `lease.active`
2. store 维护 stable(`_current`)/latest 双指针；`lease()`/`acquire()` 只租 committed
   且 `accepting_leases` 的快照
3. 版本切换 = 换 `_current` 指针（handler 集合随之切换）；生命周期顺序由 Core 写死，
   不在本层
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass
class RuntimeSnapshot:
    snapshot_id: str
    # M0：恒为空；完整版为 Mapping[str, PluginGeneration]
    generations: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    event_handlers: Mapping = field(default_factory=lambda: MappingProxyType({}))
    workspace_mcp_generation: object | None = None
    state: str = "compiled"
    lease_count: int = 0
    accepting_leases: bool = True
    _store_token: object | None = field(default=None, repr=False)

    def claim(self, store_token: object) -> None:
        if self.state != "compiled" or self.lease_count or self._store_token is not None:
            raise RuntimeError("RuntimeSnapshot 不是可发布的全新 compiled 快照")
        self._store_token = store_token


@dataclass(frozen=True)
class SnapshotTransaction:
    previous: RuntimeSnapshot | None
    candidate: RuntimeSnapshot


class RuntimeSnapshotLease:
    def __init__(
        self,
        store: RuntimeSnapshotStore,
        snapshot: RuntimeSnapshot,
        validation_candidate_plugin_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._store = store
        self.snapshot = snapshot
        self.validation_candidate_plugin_ids = validation_candidate_plugin_ids
        self._released = False

    @property
    def active(self) -> bool:
        return not self._released

    def fork(self) -> RuntimeSnapshotLease:
        return self._store.fork_lease(self)

    async def __aenter__(self) -> RuntimeSnapshot:
        return self.snapshot

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._store.release_lease(self.snapshot)


@dataclass(frozen=True)
class _RuntimeSnapshotBinding:
    lease: RuntimeSnapshotLease
    owner_task: asyncio.Task[object] | None


_current_runtime_binding: ContextVar[_RuntimeSnapshotBinding | None] = ContextVar(
    "current_runtime_binding",
    default=None,
)


def bind_runtime_snapshot(
    lease: RuntimeSnapshotLease,
) -> Token[_RuntimeSnapshotBinding | None]:
    return _current_runtime_binding.set(
        _RuntimeSnapshotBinding(
            lease=lease,
            owner_task=asyncio.current_task(),
        )
    )


def reset_runtime_snapshot(token: Token[_RuntimeSnapshotBinding | None]) -> None:
    _current_runtime_binding.reset(token)


def get_current_runtime_snapshot() -> RuntimeSnapshot | None:
    binding = _current_runtime_binding.get()
    if (
        binding is None
        or not binding.lease.active
        or binding.owner_task is not asyncio.current_task()
    ):
        return None
    return binding.lease.snapshot


def lease_current_runtime_snapshot() -> RuntimeSnapshotLease | None:
    lease = get_current_runtime_lease()
    return lease.fork() if lease is not None else None


def get_current_runtime_lease() -> RuntimeSnapshotLease | None:
    binding = _current_runtime_binding.get()
    if (
        binding is None
        or not binding.lease.active
        or binding.owner_task is not asyncio.current_task()
    ):
        return None
    return binding.lease


class RuntimeSnapshotStore:
    def __init__(
        self,
        on_drained: Callable[[RuntimeSnapshot], Awaitable[None]] | None = None,
    ) -> None:
        self._current: RuntimeSnapshot | None = None
        self._latest: RuntimeSnapshot | None = None
        self._snapshots: dict[str, RuntimeSnapshot] = {}
        self._pending: SnapshotTransaction | None = None
        self._on_drained = on_drained
        self._token = object()
        self._condition = asyncio.Condition()
        self._drain_tasks: dict[str, asyncio.Task[None]] = {}
        self._drain_failures: dict[str, BaseException] = {}

    @property
    def current(self) -> RuntimeSnapshot | None:
        return self._current

    @property
    def stable(self) -> RuntimeSnapshot | None:
        return self._current

    @property
    def latest(self) -> RuntimeSnapshot | None:
        return self._latest or self._current

    @property
    def unpromoted_candidate(self) -> RuntimeSnapshot | None:
        latest = self.latest
        return latest if latest is not self._current else None

    @property
    def pending_candidate(self) -> RuntimeSnapshot | None:
        if self._pending is None:
            return None
        return self._pending.candidate

    @property
    def retained_snapshot_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._snapshots))

    def install(self, snapshot: RuntimeSnapshot) -> None:
        if self._current is not None or self._pending is not None:
            raise RuntimeError("RuntimeSnapshotStore 已安装初始快照")
        self._adopt(snapshot)
        snapshot.state = "committed"
        self._current = snapshot
        self._latest = snapshot
        self._snapshots[snapshot.snapshot_id] = snapshot

    def begin_publish(
        self,
        candidate: RuntimeSnapshot,
        *,
        admission_gated: bool = False,
    ) -> SnapshotTransaction:
        if self._pending is not None:
            raise RuntimeError("已有 RuntimeSnapshot 发布事务")
        if self.unpromoted_candidate is not None:
            raise RuntimeError("已有 RuntimeSnapshot 候选等待 promote/discard")
        if candidate.snapshot_id in self._snapshots:
            raise RuntimeError(f"RuntimeSnapshot 已存在: {candidate.snapshot_id}")
        self._adopt(candidate)
        transaction = SnapshotTransaction(previous=self._current, candidate=candidate)
        candidate.state = "validating"
        candidate.accepting_leases = False
        self._snapshots[candidate.snapshot_id] = candidate
        self._pending = transaction
        return transaction

    async def commit(
        self,
        transaction: SnapshotTransaction,
        *,
        before_open: Callable[[], None] | None = None,
        after_open: Callable[[], None] | None = None,
    ) -> None:
        self._require_pending(transaction)
        if before_open is not None:
            before_open()
        transaction.candidate.state = "committed"
        transaction.candidate.accepting_leases = True
        self._current = transaction.candidate
        self._latest = transaction.candidate
        self._pending = None
        previous = transaction.previous
        if previous is not None:
            previous.state = "retired"
            if after_open is not None:
                after_open()
            self._schedule_drain(previous)
        async with self._condition:
            self._condition.notify_all()

    async def commit_latest(
        self,
        transaction: SnapshotTransaction,
        *,
        before_open: Callable[[], None] | None = None,
    ) -> None:
        """Publish a validation candidate without changing the stable pointer."""

        # 1. Open only the explicitly selected candidate.
        self._require_pending(transaction)
        if before_open is not None:
            before_open()
        transaction.candidate.state = "committed"
        transaction.candidate.accepting_leases = True
        self._latest = transaction.candidate
        self._pending = None

        # 2. Wake latest waiters while stable readers stay on the previous snapshot.
        async with self._condition:
            self._condition.notify_all()

    async def promote_latest(
        self,
        *,
        before_open: Callable[[], None] | None = None,
        after_open: Callable[[], None] | None = None,
    ) -> SnapshotTransaction:
        """Atomically make the ready latest snapshot stable and retire the old stable."""

        # 1. Switch the public pointer without rebuilding the validated snapshot.
        candidate = self.unpromoted_candidate
        if candidate is None:
            raise RuntimeError("没有等待 promote 的 RuntimeSnapshot 候选")
        if candidate.accepting_leases:
            raise RuntimeError("promote 前必须先暂停 candidate lease admission")
        if before_open is not None:
            before_open()
        previous = self._current
        self._current = candidate
        self._latest = candidate
        candidate.accepting_leases = True

        # 2. manager owner 切换完成后，旧 stable 才能开始 drain。
        if previous is not None:
            previous.state = "retired"
            previous.accepting_leases = False
        try:
            if after_open is not None:
                after_open()
        except BaseException:
            self._current = previous
            self._latest = candidate
            candidate.accepting_leases = True
            if previous is not None:
                previous.state = "committed"
                previous.accepting_leases = True
            async with self._condition:
                self._condition.notify_all()
            raise
        if previous is not None:
            self._schedule_drain(previous)
        async with self._condition:
            self._condition.notify_all()
        return SnapshotTransaction(previous=previous, candidate=candidate)

    async def discard_latest(
        self,
        expected: RuntimeSnapshot | None = None,
    ) -> RuntimeSnapshot:
        """Discard the ready latest snapshot without changing stable."""

        # 1. Remove candidate admission once; retries resume its failed drain.
        candidate = self.unpromoted_candidate
        if candidate is None:
            if expected is None or expected.state != "aborted":
                raise RuntimeError("没有等待 discard 的 RuntimeSnapshot 候选")
            candidate = expected
            if candidate.snapshot_id not in self._snapshots:
                return candidate
        elif expected is not None and candidate is not expected:
            raise RuntimeError("等待 discard 的 RuntimeSnapshot 候选不一致")
        if candidate.state != "aborted":
            candidate.state = "aborted"
            candidate.accepting_leases = False
            self._latest = self._current
        await self.wait_for_no_leases(candidate)
        self._schedule_drain(candidate)

        # 2. Wait for validation leases and candidate-owned resources to drain.
        await self._await_drain_tasks((candidate.snapshot_id,))
        self._raise_drain_failures((candidate.snapshot_id,))
        async with self._condition:
            self._condition.notify_all()
        return candidate

    async def abort(self, transaction: SnapshotTransaction) -> None:
        self._require_pending(transaction)
        transaction.candidate.state = "aborted"
        transaction.candidate.accepting_leases = False
        if self._current is transaction.previous and transaction.previous is not None:
            transaction.previous.accepting_leases = True
        self._pending = None
        self._schedule_drain(transaction.candidate)
        await self._await_drain_tasks((transaction.candidate.snapshot_id,))
        self._raise_drain_failures((transaction.candidate.snapshot_id,))
        async with self._condition:
            self._condition.notify_all()

    async def quiesce_current(self) -> RuntimeSnapshot | None:
        snapshot = self.pause_admission()
        if snapshot is None:
            return None
        try:
            await self.wait_for_no_leases(snapshot)
        except BaseException:
            await self.resume(snapshot)
            raise
        return snapshot

    def pause_admission(self) -> RuntimeSnapshot | None:
        snapshot = self._current
        if snapshot is not None:
            snapshot.accepting_leases = False
        return snapshot

    def pause_candidate_admission(
        self,
        expected: RuntimeSnapshot,
    ) -> RuntimeSnapshot:
        """Atomically seal the exact unpromoted candidate against new leases."""

        candidate = self.unpromoted_candidate
        if candidate is None or candidate is not expected:
            raise RuntimeError("等待 promote 的 RuntimeSnapshot 候选不一致")
        candidate.accepting_leases = False
        return candidate

    async def wait_for_no_leases(self, snapshot: RuntimeSnapshot) -> None:
        async with self._condition:
            while snapshot.lease_count:
                await self._condition.wait()

    async def resume(self, snapshot: RuntimeSnapshot | None) -> None:
        if snapshot is None:
            return
        if (
            snapshot.state == "committed"
            and (self._current is snapshot or self.unpromoted_candidate is snapshot)
        ):
            snapshot.accepting_leases = True
        async with self._condition:
            self._condition.notify_all()

    async def acquire(
        self,
        snapshot_id: str | None = None,
        *,
        selector: str = "stable",
    ) -> RuntimeSnapshotLease:
        async with self._condition:
            while True:
                snapshot = (
                    self._selected(selector)
                    if snapshot_id is None
                    else self._snapshots.get(snapshot_id)
                )
                if snapshot is None:
                    raise RuntimeError("RuntimeSnapshot 不可用")
                if snapshot.state != "committed":
                    raise RuntimeError(f"RuntimeSnapshot 不可租用: {snapshot.state}")
                if snapshot.accepting_leases:
                    return self._claim_lease(snapshot)
                await self._condition.wait()

    async def close(self) -> None:
        if self._pending is not None:
            raise RuntimeError("RuntimeSnapshot 发布事务尚未结束")
        leased = [
            snapshot.snapshot_id
            for snapshot in self._snapshots.values()
            if snapshot.lease_count
        ]
        if leased:
            raise RuntimeError(f"RuntimeSnapshot 仍有 lease: {', '.join(sorted(leased))}")
        await self.retry_drains()
        latest = self.unpromoted_candidate
        self._latest = self._current
        if latest is not None:
            latest.state = "aborted"
            latest.accepting_leases = False
            self._schedule_drain(latest)
        current = self._current
        self._current = None
        self._latest = None
        if current is not None:
            current.state = "retired"
            self._schedule_drain(current)
            await self.retry_drains()

    def lease(
        self,
        snapshot_id: str | None = None,
        *,
        selector: str = "stable",
    ) -> RuntimeSnapshotLease:
        snapshot = (
            self._selected(selector)
            if snapshot_id is None
            else self._snapshots.get(snapshot_id)
        )
        if snapshot is None:
            raise RuntimeError("RuntimeSnapshot 不可用")
        if snapshot.state != "committed":
            raise RuntimeError(f"RuntimeSnapshot 不可租用: {snapshot.state}")
        if not snapshot.accepting_leases:
            raise RuntimeError("RuntimeSnapshot 暂停接收新 lease")
        return self._claim_lease(snapshot)

    def _claim_lease(self, snapshot: RuntimeSnapshot) -> RuntimeSnapshotLease:
        snapshot.lease_count += 1
        for generation in snapshot.generations.values():
            generation.lease_count += 1
        if snapshot.workspace_mcp_generation is not None:
            snapshot.workspace_mcp_generation.lease_count += 1
        return RuntimeSnapshotLease(
            self,
            snapshot,
            self._validation_candidate_plugin_ids(snapshot),
        )

    def fork_lease(self, source: RuntimeSnapshotLease) -> RuntimeSnapshotLease:
        snapshot = source.snapshot
        if not source.active or self._snapshots.get(snapshot.snapshot_id) is not snapshot:
            raise RuntimeError("RuntimeSnapshot lease 不可复制")
        snapshot.lease_count += 1
        for generation in snapshot.generations.values():
            generation.lease_count += 1
        if snapshot.workspace_mcp_generation is not None:
            snapshot.workspace_mcp_generation.lease_count += 1
        return RuntimeSnapshotLease(
            self,
            snapshot,
            source.validation_candidate_plugin_ids,
        )

    def _validation_candidate_plugin_ids(
        self,
        snapshot: RuntimeSnapshot,
    ) -> frozenset[str]:
        stable = self._current
        if stable is None or snapshot is not self.unpromoted_candidate:
            return frozenset()
        return frozenset(
            plugin_id
            for plugin_id, generation in snapshot.generations.items()
            if stable.generations.get(plugin_id) is not generation
        )

    async def release_lease(self, snapshot: RuntimeSnapshot) -> None:
        if snapshot.lease_count <= 0:
            raise RuntimeError(f"RuntimeSnapshot lease 计数失衡: {snapshot.snapshot_id}")
        snapshot.lease_count -= 1
        for generation in snapshot.generations.values():
            generation.lease_count -= 1
        if snapshot.workspace_mcp_generation is not None:
            snapshot.workspace_mcp_generation.lease_count -= 1
        self._schedule_drain(snapshot)
        async with self._condition:
            self._condition.notify_all()

    def _schedule_drain(self, snapshot: RuntimeSnapshot) -> None:
        if (
            self._snapshots.get(snapshot.snapshot_id) is not snapshot
            or snapshot.state not in {"retired", "aborted"}
            or snapshot.lease_count
        ):
            return
        existing = self._drain_tasks.get(snapshot.snapshot_id)
        if existing is not None and not existing.done():
            return
        _ = self._drain_failures.pop(snapshot.snapshot_id, None)
        self._drain_tasks[snapshot.snapshot_id] = asyncio.create_task(
            self._run_drain(snapshot),
            name=f"runtime_snapshot_drain:{snapshot.snapshot_id}",
        )

    async def _run_drain(self, snapshot: RuntimeSnapshot) -> None:
        try:
            if self._on_drained is not None:
                await self._on_drained(snapshot)
        except (asyncio.CancelledError, Exception) as error:
            self._drain_failures[snapshot.snapshot_id] = error
        else:
            _ = self._snapshots.pop(snapshot.snapshot_id, None)
        finally:
            _ = self._drain_tasks.pop(snapshot.snapshot_id, None)
            async with self._condition:
                self._condition.notify_all()

    async def retry_drains(self) -> None:
        await self._await_drain_tasks(tuple(self._drain_tasks))
        for snapshot in tuple(self._snapshots.values()):
            self._schedule_drain(snapshot)
        attempted = tuple(self._drain_tasks)
        await self._await_drain_tasks(attempted)
        self._raise_drain_failures(attempted)

    async def _await_drain_tasks(self, snapshot_ids: tuple[str, ...]) -> None:
        tasks = [
            task
            for snapshot_id in snapshot_ids
            if (task := self._drain_tasks.get(snapshot_id)) is not None
        ]
        if tasks:
            await asyncio.gather(*tasks)

    def _raise_drain_failures(self, snapshot_ids: tuple[str, ...]) -> None:
        failures = [
            (snapshot_id, self._drain_failures[snapshot_id])
            for snapshot_id in snapshot_ids
            if snapshot_id in self._drain_failures
        ]
        if not failures:
            return
        snapshot_id, error = failures[0]
        raise RuntimeError(f"RuntimeSnapshot drain 失败: {snapshot_id}") from error

    def _require_pending(self, transaction: SnapshotTransaction) -> None:
        if self._pending is not transaction:
            raise RuntimeError("RuntimeSnapshot 发布事务已失效")

    def _adopt(self, snapshot: RuntimeSnapshot) -> None:
        snapshot.claim(self._token)

    def _selected(self, selector: str) -> RuntimeSnapshot | None:
        if selector == "stable":
            return self._current
        if selector == "latest":
            return self.latest
        raise ValueError(f"未知 RuntimeSnapshot selector: {selector}")
