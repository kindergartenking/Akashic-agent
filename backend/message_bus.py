"""In-memory MessageBus for one-shot chat turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class BusMessage:
    owner_id: int
    session_id: str
    turn_id: str
    text: str
    runtime_id: str | None
    emit: Callable[[dict[str, object]], Awaitable[None]]
    on_error: Callable[[Exception], Awaitable[None]]
    # Kept optional for callers that construct BusMessage directly; the
    # websocket adapter always supplies the persisted source ID.
    user_message_id: str = ""


class MessageBus:
    """A bounded async queue; messages disappear after their worker finishes."""

    def __init__(self, worker_count: int = 1) -> None:
        self._queue: asyncio.Queue[BusMessage] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._handler: Callable[[BusMessage], Awaitable[None]] | None = None
        self._worker_count = max(1, worker_count)

    async def start(self, handler: Callable[[BusMessage], Awaitable[None]]) -> None:
        self._handler = handler
        self._workers = [asyncio.create_task(self._run(), name=f"message-bus-{i}") for i in range(self._worker_count)]

    async def stop(self) -> None:
        for task in self._dispatch_tasks:
            task.cancel()
        if self._dispatch_tasks:
            await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
        self._dispatch_tasks.clear()
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def publish(self, message: BusMessage) -> None:
        await self._queue.put(message)

    async def _run(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                if self._handler is not None:
                    task = asyncio.create_task(self._dispatch(message))
                    self._dispatch_tasks.add(task)
                    task.add_done_callback(self._dispatch_tasks.discard)
            finally:
                self._queue.task_done()

    async def _dispatch(self, message: BusMessage) -> None:
        try:
            if self._handler is not None:
                await self._handler(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await message.on_error(exc)
