from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .base import Tool

SpawnCallback = Callable[[str, str], Awaitable[str]]

@dataclass
class _BackgroundJob:
    job_id: str
    task: str
    label: str
    started_at: str
    future: asyncio.Task[str]

class SpawnManager:
    """In-process registry for background child-agent jobs."""
    def __init__(self) -> None:
        self._jobs: dict[str, _BackgroundJob] = {}

    def create(self, task: str, label: str, callback: SpawnCallback) -> str:
        job_id = f"job-{uuid4().hex[:12]}"
        future = asyncio.create_task(callback(task, label), name=job_id)
        self._jobs[job_id] = _BackgroundJob(job_id, task, label, datetime.now(timezone.utc).isoformat(), future)
        return job_id

    def get_running_count(self) -> int:
        return sum(not job.future.done() for job in self._jobs.values())

    def list_running_jobs(self) -> list[dict[str, object]]:
        return [{"job_id": j.job_id, "label": j.label, "task": j.task, "started_at": j.started_at, "status": "running"} for j in self._jobs.values() if not j.future.done()]

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.future.done():
            return False
        job.future.cancel()
        return True

    async def shutdown(self) -> None:
        active = [j.future for j in self._jobs.values() if not j.future.done()]
        for future in active:
            future.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

class SpawnTool(Tool):
    name = "spawn"
    execution_timeout: float | None = None
    description = "把一个独立、边界清晰、需要多步处理的任务交给子 Agent；可选择后台运行。"
    parameters = {"type": "object", "properties": {
        "task": {"type": "string", "description": "交给子 Agent 的完整任务"},
        "label": {"type": "string", "description": "简短任务标签"},
        "run_in_background": {"type": "boolean", "description": "是否后台运行，可用 spawn_manage 管理", "default": False},
    }, "required": ["task"], "additionalProperties": False}

    def __init__(self, callback: SpawnCallback, manager: SpawnManager | None = None) -> None:
        self._callback = callback
        self._manager = manager

    async def execute(self, **arguments: Any) -> str:
        task = str(arguments["task"]).strip()
        if not task:
            raise ValueError("子 Agent 任务不能为空")
        label = str(arguments.get("label") or "").strip()
        if bool(arguments.get("run_in_background", False)):
            if self._manager is None:
                return "错误：当前运行时未启用后台子 Agent。"
            job_id = self._manager.create(task, label, self._callback)
            return json.dumps({"job_id": job_id, "status": "started", "task": task}, ensure_ascii=False)
        return await self._callback(task, label)

class SpawnManageTool(Tool):
    name = "spawn_manage"
    description = "查询或取消当前 Runtime 中运行的后台子 Agent。"
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "cancel"], "description": "list 查询，cancel 取消"},
        "job_id": {"type": "string", "description": "cancel 时的后台 job_id"},
    }, "required": ["action"], "additionalProperties": False}

    def __init__(self, manager: SpawnManager) -> None:
        self._manager = manager

    async def execute(self, **arguments: Any) -> str:
        action = str(arguments.get("action", ""))
        if action == "list":
            return json.dumps({"running_count": self._manager.get_running_count(), "jobs": self._manager.list_running_jobs()}, ensure_ascii=False)
        if action == "cancel":
            job_id = str(arguments.get("job_id", "")).strip()
            if not job_id:
                return json.dumps({"error": "缺少 job_id"}, ensure_ascii=False)
            cancelled = await self._manager.cancel(job_id)
            return json.dumps({"job_id": job_id, "status": "cancel_requested" if cancelled else "not_found"}, ensure_ascii=False)
        return json.dumps({"error": f"未知 action: {action}"}, ensure_ascii=False)
