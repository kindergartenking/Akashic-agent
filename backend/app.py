"""Akashic MVP backend: WebSocket -> MessageBus -> streaming LLM."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

# The copied model-runtime package follows the original PYTHONPATH=backend layout.
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .llm_client import LLMClient
from .agent_orchestrator import AgentOrchestrator
from .message_bus import BusMessage, MessageBus
from .model_config import ModelConfigResolver
from .settings_api import create_settings_router, settings_security_middleware
from .session_store import SessionStore
from .context_manager import SessionContextManager
from .memory import AkashaMemoryRuntime
from .tools import (
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    FetchMessagesTool,
    SearchMessagesTool,
    LoadSkillTool,
    ToolSearchTool,
    ShellTaskStopTool,
    ShellTool,
    ShellWriteStdinTool,
    ToolRegistry,
    WebFetchTool,
    WebSearchTool,
    current_shell_owner,
    SpawnManager,
    SpawnManageTool,
)


WORKSPACE = Path(os.getenv("AKASHIC_WORKSPACE", str(BACKEND_DIR.parent)))
STATIC_ROOT = WORKSPACE / "static"
CHAT_STATIC_ROOT = STATIC_ROOT / "chat"
DASHBOARD_STATIC_ROOT = STATIC_ROOT / "dashboard"
logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self) -> None:
        self.http: httpx.AsyncClient | None = None
        self.llm: LLMClient | None = None
        self.agent: AgentOrchestrator | None = None
        self.tools: ToolRegistry | None = None
        self.shell: ShellTool | None = None
        self.spawn_manager = SpawnManager()
        self.bus = MessageBus()
        self.turns: dict[tuple[int, str], asyncio.Task[None]] = {}
        self.sessions = SessionStore(WORKSPACE / "sessions.db")
        self.context_manager: SessionContextManager | None = None
        self.memory: AkashaMemoryRuntime | None = None

    async def start(self) -> None:
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self.llm = LLMClient(self.http)
        self.context_manager = SessionContextManager(self.sessions, self.llm)
        # Akasha is owned directly by Runtime for now.  This replaces the
        # original project's Plugin -> EventBus wiring without changing its
        # canonical-source/read-before-write memory contract.
        self.memory = AkashaMemoryRuntime(self.sessions, WORKSPACE, self.http)
        await self.memory.start_or_rebuild()
        self.tools = ToolRegistry(execution_timeout=30.0)
        self.tools.register(ReadFileTool(WORKSPACE))
        self.tools.register(ListDirTool(WORKSPACE))
        self.tools.register(WriteFileTool(WORKSPACE))
        self.tools.register(EditFileTool(WORKSPACE))
        self.tools.register(FetchMessagesTool(self.sessions))
        self.tools.register(SearchMessagesTool(self.sessions))
        self.tools.register(LoadSkillTool(WORKSPACE))
        self.tools.register(ToolSearchTool(self.tools))
        self.tools.register(SpawnManageTool(self.spawn_manager))
        self.tools.register(WebSearchTool(self.http))
        self.tools.register(WebFetchTool(self.http))
        self.shell = ShellTool(working_dir=WORKSPACE, restricted_dir=WORKSPACE)
        self.tools.register(self.shell)
        self.tools.register(ShellWriteStdinTool(self.shell.manager))
        self.tools.register(ShellTaskStopTool(self.shell.manager))
        self.agent = AgentOrchestrator(self.llm, self.tools, self.spawn_manager)
        await self.bus.start(self.handle_message)

    async def stop(self) -> None:
        for task in self.turns.values():
            task.cancel()
        await self.bus.stop()
        if self.shell is not None:
            await self.shell.shutdown()
        await self.spawn_manager.shutdown()
        if self.memory is not None:
            await self.memory.aclose()
        if self.http is not None:
            await self.http.aclose()

    async def handle_message(self, message: BusMessage) -> None:
        task_key = (message.owner_id, message.session_id)
        task = asyncio.current_task()
        if task is not None:
            self.turns[task_key] = task
        started = time.perf_counter()
        try:
            owner_token = current_shell_owner.set(f"{message.owner_id}:{message.session_id}")
            config = ModelConfigResolver(WORKSPACE).resolve(message.runtime_id)
            if config is None:
                raise RuntimeError("未找到模型配置。请设置 AKASHIC_API_KEY、AKASHIC_BASE_URL、AKASHIC_MODEL")
            await asyncio.to_thread(self.sessions.mark_turn_started, message.turn_id)
            await message.emit({"type": "turn.started", "session_id": message.session_id, "turn_id": message.turn_id, "content": ""})
            assert self.llm is not None
            assert self.agent is not None
            history = []
            if self.context_manager is not None:
                history = await self.context_manager.build(
                    config, message.session_id, exclude_turn_id=message.turn_id
                )
            recall = None
            if self.memory is not None:
                try:
                    recall = await self.memory.recall(
                        session_id=message.session_id,
                        turn_id=message.turn_id,
                        text=message.text,
                    )
                    if recall.context_block:
                        # Long-term Akasha recall is distinct from the current
                        # session window/summary assembled above.  It comes
                        # first as a system-scoped evidence block.
                        history = [{"role": "system", "content": recall.context_block}, *history]
                except Exception:
                    # The sidecar is derived state.  A recall outage must not
                    # prevent the canonical chat turn from completing.
                    logger.exception("Akasha recall failed; continuing without memory")
            result = await self.agent.run(
                config,
                message.text,
                session_id=message.session_id,
                turn_id=message.turn_id,
                emit=message.emit,
                initial_messages=history,
            )
            content = result.reply
            duration_ms = int((time.perf_counter() - started) * 1000)
            assistant_message_id = await asyncio.to_thread(
                self.sessions.complete_turn,
                message.session_id,
                message.turn_id,
                content,
                duration_ms=duration_ms,
                tool_chain=list(result.tool_chain),
            )
            if self.memory is not None:
                try:
                    committed = await self.memory.commit_turn(
                        session_id=message.session_id,
                        turn_id=message.turn_id,
                        user_message_id=message.user_message_id,
                        assistant_message_id=assistant_message_id,
                        ticket=recall.ticket if recall is not None else None,
                    )
                    # Do not emit an internal-only event here: the copied
                    # frontend validates a closed WebSocket event union and
                    # would treat an unknown ``memory.committed`` frame as a
                    # transport error.  The commit remains observable through
                    # the derived sidecar and normal turn completion frame.
                    _ = committed
                except Exception:
                    # The source turn has already committed.  On the next
                    # startup Akasha rebuilds its sidecar from sessions.db.
                    logger.exception("Akasha commit failed; source turn remains recoverable")
            await message.emit({"type": "turn.output.completed", "session_id": message.session_id, "turn_id": message.turn_id})
            await message.emit({
                "type": "message.final", "session_id": message.session_id, "turn_id": message.turn_id,
                "content": content, "duration_ms": duration_ms,
            })
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.sessions.fail_turn,
                message.turn_id,
                RuntimeError("生成已停止"),
                status="interrupted",
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(self.sessions.fail_turn, message.turn_id, exc)
            await message.on_error(exc)
        finally:
            try:
                current_shell_owner.reset(owner_token)
            except UnboundLocalError:
                pass
            if self.turns.get(task_key) is task:
                self.turns.pop(task_key, None)

    def cancel(self, websocket: WebSocket, session_id: str) -> bool:
        task = self.turns.get((id(websocket), session_id))
        if task is None or task.done():
            return False
        task.cancel()
        return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime = Runtime()
    await runtime.start()
    _app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="Akashic MVP", lifespan=lifespan)
settings_router, settings_service = create_settings_router(WORKSPACE)
app.include_router(settings_router)
app.middleware("http")(settings_security_middleware)

# The original shell exposes the built frontend and its hashed assets from
# the same origin as the API.  Keep this serving boundary in the MVP so
# browser routes such as /chat and /settings work outside the Vite dev server.
if CHAT_STATIC_ROOT.is_dir():
    app.mount("/assets", StaticFiles(directory=CHAT_STATIC_ROOT, html=True), name="chat-assets")
if DASHBOARD_STATIC_ROOT.is_dir():
    app.mount("/dashboard/assets", StaticFiles(directory=DASHBOARD_STATIC_ROOT, html=True), name="dashboard-assets")


def _frontend_page(root: Path) -> FileResponse:
    index = root / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=503, detail=f"前端构建产物不存在: {index}")
    return FileResponse(index)


@app.get("/", include_in_schema=False)
async def root_page() -> FileResponse:
    """Open the MVP chat UI at the origin root.

    The dashboard bundle is retained at ``/dashboard`` for compatibility,
    while the primary entry point matches the chat frontend users expect.
    """
    return _frontend_page(CHAT_STATIC_ROOT)


@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/", include_in_schema=False)
async def dashboard_route() -> FileResponse:
    return _frontend_page(DASHBOARD_STATIC_ROOT)


@app.get("/chat", include_in_schema=False)
@app.get("/chat/", include_in_schema=False)
async def chat_page() -> FileResponse:
    return _frontend_page(CHAT_STATIC_ROOT)


@app.get("/settings", include_in_schema=False)
@app.get("/settings/", include_in_schema=False)
async def settings_page() -> FileResponse:
    return _frontend_page(CHAT_STATIC_ROOT)


@app.get("/api/shell/state")
async def shell_state() -> dict[str, object]:
    config = ModelConfigResolver(WORKSPACE).resolve()
    configured = config is not None and config.configured
    return {
        "status": "ready" if configured else "needs_setup",
        "configured": configured,
        "chatReady": configured,
        "settingsPath": "",
    }


@app.get("/api/chat/sessions")
async def sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=80, ge=1, le=200),
) -> dict[str, object]:
    runtime: Runtime = app.state.runtime
    return await asyncio.to_thread(runtime.sessions.list_sessions, page=page, page_size=page_size)


@app.get("/api/chat/sessions/{session_id}/messages")
async def messages(
    session_id: str,
    page_size: int = Query(default=50, ge=1, le=200),
    before_seq: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    runtime: Runtime = app.state.runtime
    return await asyncio.to_thread(
        runtime.sessions.list_messages,
        session_id,
        page_size=page_size,
        before_seq=before_seq,
    )


@app.get("/api/chat/models")
async def models() -> dict[str, object]:
    resolver = ModelConfigResolver(WORKSPACE)
    config = resolver.resolve()
    runtimes = list(resolver.runtimes_for_api())
    default = config.runtime_id if config else ""
    return {
        "generationId": 0,
        "defaultRuntime": default,
        "sessionOverride": "",
        "sessionSelection": {"modelRef": default, "reasoningEffort": config.reasoning_effort if config else ""},
        "runtimes": runtimes,
    }


@app.get("/api/chat/plugin-ui/catalog")
async def plugin_ui_catalog() -> dict[str, object]:
    """Return the empty web-plugin catalog used by the MVP shell.

    The copied chat frontend asks for this catalog during startup even when
    no plugin backend is enabled.  Returning a valid empty catalog keeps the
    chat page in a clean state instead of surfacing a misleading 404 error;
    plugin execution remains intentionally outside this MVP.
    """
    return {
        "catalog_revision": "0" * 64,
        "items": [],
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    runtime: Runtime = websocket.app.state.runtime
    emitter = _make_emitter(websocket)
    try:
        while True:
            frame = await websocket.receive_json()
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                await websocket.send_json({"type": "error", "request_id": "", "message": "无效 WebSocket 消息"})
                continue
            kind = frame["type"]
            request_id = str(frame.get("request_id") or "")
            session_id = str(frame.get("session_id") or "")
            if kind in {"session.attach", "message.send"}:
                if not session_id:
                    await websocket.send_json({"type": "error", "request_id": request_id, "message": "缺少 session_id"})
                    continue
                await websocket.send_json({"type": "session.created", "request_id": request_id, "session_id": session_id})
            if kind == "session.attach":
                continue
            if kind == "turn.stop":
                cancelled = runtime.cancel(websocket, session_id)
                await websocket.send_json({"type": "turn.interrupted", "request_id": request_id, "session_id": session_id, "status": "cancelled" if cancelled else "idle", "message": "生成已停止" if cancelled else "当前没有正在生成的回复"})
                continue
            if kind != "message.send":
                if kind not in {"session.attach", "turn.stop"}:
                    await websocket.send_json({"type": "error", "request_id": request_id, "message": f"不支持的消息类型: {kind}"})
                continue
            text = str(frame.get("text") or "").strip()
            if not text:
                await websocket.send_json({"type": "error", "request_id": request_id, "message": "消息内容不能为空"})
                continue
            turn_id = str(frame.get("turn_id") or f"turn-{uuid4().hex}").strip()
            if not turn_id:
                await websocket.send_json({"type": "error", "request_id": request_id, "message": "turn_id 不能为空"})
                continue
            try:
                user_message_id = await asyncio.to_thread(
                    runtime.sessions.record_user_message,
                    session_id,
                    turn_id,
                    text,
                    user_id=str(frame.get("user_id") or "local"),
                    metadata={"request_id": request_id},
                )
            except Exception as exc:
                await _send_error(websocket, request_id, exc)
                continue
            await runtime.bus.publish(BusMessage(
                owner_id=id(websocket),
                session_id=session_id, turn_id=turn_id, text=text,
                user_message_id=user_message_id,
                runtime_id=str(frame.get("model_runtime_id") or "") or None,
                emit=emitter,
                on_error=lambda exc, rid=request_id: _send_error(websocket, rid, exc),
            ))
    except WebSocketDisconnect:
        return


def _make_emitter(websocket: WebSocket):
    send_lock = asyncio.Lock()

    async def emit(frame: dict[str, object]) -> None:
        # A connection can have more than one queued turn. Serialize frames so
        # two streaming workers never interleave bytes on the same socket.
        async with send_lock:
            await websocket.send_json(frame)
    return emit


async def _send_error(websocket: WebSocket, request_id: str, exc: Exception) -> None:
    try:
        await websocket.send_json({"type": "error", "request_id": request_id, "message": str(exc)})
    except Exception:
        pass
