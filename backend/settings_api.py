"""Model settings boundary copied from the original Akashic onboarding design."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import httpx
import tomlkit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agent.model_runtime.auth.codex import CODEX_API_BASE, CodexAuthDriver
from agent.model_runtime.auth.store import Credential, CredentialStore
from agent.model_runtime.catalog.codex import CodexModel, CodexModelCatalog
from agent.model_runtime.catalog.litellm_registry import (
    CatalogCapabilities,
    resolve_catalog_capabilities,
    resolve_catalog_provider_id,
)
from agent.model_runtime.catalog.opencode_go import OpenCodeGoModelCatalog
from agent.model_runtime.errors import AuthenticationError, ModelRuntimeError, TransportError
from agent.model_runtime.provider_profiles import OPENCODE_GO_BASE_URL, validate_profile_runtime
from agent.model_runtime.store import ModelRegistryStore, StoredModelRuntime


class ModelQuery(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(default="", max_length=200)
    api_key: str = ""
    credential_id: str = ""
    use_local_opencode: bool = False
    base_url: str = ""


class ApplyPayload(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(default="", max_length=200)
    source_id: str = Field(default="", max_length=96)
    source_name: str = Field(default="", max_length=80)
    api_key: str = ""
    credential_id: str = ""
    use_local_opencode: bool = False
    base_url: str = Field(default="", max_length=2048)
    context_window: int = Field(default=0, ge=0)
    max_output_tokens: int = Field(default=0, ge=0)
    input_modalities: list[Literal["text", "image"]] | None = None
    reasoning_effort: str = Field(default="", max_length=32)
    expected_config_revision: str = Field(default="", max_length=64)
    defer_restart: bool = False


class RoleBindingPayload(BaseModel):
    role: Literal["default", "fast", "agent", "vision"]
    model_id: str = Field(min_length=1, max_length=128)
    reasoning_effort: str = Field(default="", max_length=32)
    expected_revision: int | None = Field(default=None, ge=0)


class EmbeddingModelPayload(BaseModel):
    model_id: str = Field(default="", max_length=128)
    source_id: str = Field(default="", max_length=96)
    source_name: str = Field(default="向量服务", max_length=80)
    provider: str = Field(default="openai", min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = ""
    credential_id: str = Field(default="", max_length=128)
    base_url: str = Field(min_length=1, max_length=2048)
    expected_revision: int | None = Field(default=None, ge=0)


class MemorySettingsPayload(BaseModel):
    enabled: bool
    engine: Literal["akasha", "default"] = "akasha"
    embedding_model_id: str = Field(default="", max_length=128)
    expected_revision: str = Field(default="", max_length=64)


@dataclass
class RuntimeCandidate:
    runtime_id: str
    source_id: str
    source_name: str
    provider: str
    catalog_provider_id: str
    auth_id: str
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str
    supported_reasoning_efforts: tuple[str, ...]
    context_window: int
    max_output_tokens: int
    input_modalities: tuple[str, ...]
    capability_source: str
    context_window_source: str
    max_output_tokens_source: str
    input_modalities_source: str
    use_responses_lite: bool = False
    supports_parallel_tool_calls: bool = True
    reasoning_summary: str = "none"

    def as_config(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "catalog_provider_id": self.catalog_provider_id,
            "auth": self.auth_id,
            "base_url": self.base_url,
            "reasoning_effort": self.reasoning_effort,
            "supported_reasoning_efforts": list(self.supported_reasoning_efforts),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input_modalities": list(self.input_modalities),
            "capability_source": self.capability_source,
            "context_window_source": self.context_window_source,
            "max_output_tokens_source": self.max_output_tokens_source,
            "input_modalities_source": self.input_modalities_source,
            "use_responses_lite": self.use_responses_lite,
            "supports_parallel_tool_calls": self.supports_parallel_tool_calls,
            "reasoning_summary": self.reasoning_summary,
        }


class CodexLoginSession:
    def __init__(self, login_id: str, driver: CodexAuthDriver) -> None:
        self.login_id = login_id
        self.driver = driver
        self.code = driver.begin_device_login()
        self.status = "waiting"
        self.error = ""


class SettingsService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.config_path = workspace / "config.toml"
        self.registry = ModelRegistryStore.for_workspace(workspace)
        self.credentials = CredentialStore.for_workspace(workspace)
        self.apply_lock = threading.Lock()
        self.login_lock = threading.Lock()
        self.logins: dict[str, CodexLoginSession] = {}

    def state(self) -> dict[str, object]:
        snapshot = self.registry.read_snapshot()
        credential_meta = self._credential_metadata()
        raw: dict[str, object] = {}
        if self.config_path.is_file():
            try:
                raw = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                return self._repair_state("config.toml 无法解析，请先修复或移走该文件")
        runtimes = []
        role_bindings: dict[str, dict[str, str]] = {}
        active: str | None = None
        if snapshot is not None:
            active = snapshot.roles["default"].runtime_id
            runtimes = [
                _runtime_summary(runtime, credential_meta)
                for runtime in snapshot.runtimes.values()
            ]
            role_bindings = {
                role: {
                    "modelId": binding.runtime_id,
                    "reasoningEffort": binding.reasoning_effort,
                }
                for role, binding in snapshot.roles.items()
            }
        return {
            "mode": "ready" if snapshot is not None else "needs_setup",
            "workspace": str(self.workspace),
            "activeRuntime": active,
            "runtimes": runtimes,
            "roleBindings": role_bindings,
            "modelRevision": snapshot.revision if snapshot else 0,
            "codexConfigured": "codex_default" in credential_meta,
            "localOpenCodeConfigured": _local_opencode_key(required=False) is not None,
            "configRevision": self.settings_revision(),
            "memory": self.memory_state(raw, credential_meta),
        }

    def _repair_state(self, error: str) -> dict[str, object]:
        return {
            "mode": "needs_repair",
            "workspace": str(self.workspace),
            "error": error,
            "activeRuntime": None,
            "runtimes": [],
            "roleBindings": {},
            "modelRevision": self.registry.revision(),
            "codexConfigured": False,
            "localOpenCodeConfigured": _local_opencode_key(required=False) is not None,
            "configRevision": "",
            "memory": self.memory_state({}, {}),
        }

    def _credential_metadata(self) -> dict[str, dict[str, str]]:
        try:
            return self.credentials.metadata()
        except Exception:
            if not self.registry.path.is_file():
                return {}
            result: dict[str, dict[str, str]] = {}
            with sqlite3.connect(self.registry.path) as connection:
                rows = connection.execute(
                    "SELECT auth_id, auth_kind, auth_payload FROM model_connections "
                    "WHERE auth_id != '' AND auth_kind != '' AND auth_payload != ''"
                ).fetchall()
            for auth_id, auth_kind, payload in rows:
                raw = json.loads(str(payload))
                result[str(auth_id)] = {
                    "driver": str(auth_kind),
                    "updated_at": str(raw.get("updated_at") or "") if isinstance(raw, dict) else "",
                }
            return result

    def settings_revision(self) -> str:
        revision = self.registry.revision()
        if revision:
            return f"models:{revision}"
        if not self.config_path.is_file():
            return ""
        stat = self.config_path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
        return hashlib.sha256(identity.encode("ascii")).hexdigest()

    def memory_state(
        self,
        raw: dict[str, object],
        credential_meta: dict[str, dict[str, str]],
    ) -> dict[str, object]:
        memory = raw.get("memory") if isinstance(raw.get("memory"), dict) else None
        embedding = memory.get("embedding") if isinstance(memory, dict) and isinstance(memory.get("embedding"), dict) else {}
        configured = memory is not None
        enabled = bool(memory.get("enabled", False)) if memory is not None else False
        engine = str(memory.get("engine") or "") if memory is not None else ""
        return {
            "configured": configured,
            "enabled": enabled,
            "engine": engine or ("default" if configured and enabled else "akasha"),
            "embeddingModelId": str(embedding.get("model_ref") or ""),
            "embeddingModels": [
                {
                    "id": item.model_id,
                    "sourceId": item.source_id,
                    "sourceName": item.source_name,
                    "provider": item.provider,
                    "baseUrl": item.base_url,
                    "model": item.model,
                    "dimensions": item.dimensions,
                    "credential": {
                        "id": item.auth_id,
                        "configured": item.auth_id in credential_meta,
                    },
                }
                for item in self.registry.list_embedding_models()
            ],
            "changeLocked": False,
            "revision": self.memory_revision(),
        }

    def memory_revision(self) -> str:
        payload = self.config_path.read_bytes() if self.config_path.is_file() else b""
        return hashlib.sha256(payload + b"\0" + str(self.registry.revision()).encode("ascii")).hexdigest()

    def write_memory(self, payload: MemorySettingsPayload) -> None:
        if payload.expected_revision and payload.expected_revision != self.memory_revision():
            raise HTTPException(status_code=409, detail="记忆设置已经变化，请刷新后重试")
        if payload.enabled and not payload.embedding_model_id.strip():
            raise HTTPException(status_code=422, detail="启用语义记忆需要选择向量模型")
        if payload.enabled and self.registry.get_embedding_model(payload.embedding_model_id.strip()) is None:
            raise HTTPException(status_code=422, detail="选择的向量模型不存在")
        document = self._config_document()
        memory = tomlkit.table()
        memory["enabled"] = payload.enabled
        memory["engine"] = payload.engine
        embedding = tomlkit.table()
        if payload.embedding_model_id.strip():
            embedding["model_ref"] = payload.embedding_model_id.strip()
        memory["embedding"] = embedding
        document["memory"] = memory
        _atomic_write(self.config_path, tomlkit.dumps(document))

    def _config_document(self):
        if self.config_path.is_file():
            return tomlkit.parse(self.config_path.read_text(encoding="utf-8"))
        document = tomlkit.document()
        runtime = tomlkit.table()
        runtime["workspace"] = str(self.workspace)
        document["runtime"] = runtime
        llm = tomlkit.table()
        llm["registry"] = "workspace"
        document["llm"] = llm
        return document

    def ensure_config_marker(self) -> None:
        document = self._config_document()
        llm = document.get("llm")
        if not isinstance(llm, dict):
            llm = tomlkit.table()
            document["llm"] = llm
        llm["registry"] = "workspace"
        _atomic_write(self.config_path, tomlkit.dumps(document))


def create_settings_router(workspace: Path) -> tuple[APIRouter, SettingsService]:
    service = SettingsService(workspace)
    router = APIRouter()

    @router.get("/api/settings/state")
    async def state() -> dict[str, object]:
        return await asyncio.to_thread(service.state)

    @router.post("/api/settings/models")
    async def models(payload: ModelQuery) -> dict[str, object]:
        try:
            return {"models": await _discover_models(service, payload)}
        except (ModelRuntimeError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/settings/apply")
    async def apply(payload: ApplyPayload) -> dict[str, object]:
        if payload.max_output_tokens and payload.context_window and payload.max_output_tokens >= payload.context_window:
            raise HTTPException(status_code=422, detail="最大输出必须小于上下文窗口")
        if payload.input_modalities is not None and "text" not in payload.input_modalities:
            raise HTTPException(status_code=422, detail="输入模态必须包含 text")
        if not service.apply_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有设置操作正在执行")
        try:
            if payload.expected_config_revision and payload.expected_config_revision != service.settings_revision():
                raise HTTPException(status_code=409, detail="配置已经变化，请刷新后重试")
            operation_id = f"settings-{uuid4().hex}"
            candidates = await _connection_candidates(service, payload)
            revision = await asyncio.to_thread(
                _publish_candidates, service, candidates, operation_id
            )
            return {
                "operationId": operation_id,
                "status": "applied",
                "modelCount": len(candidates),
                "revision": revision,
            }
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ModelRuntimeError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            service.apply_lock.release()

    @router.post("/api/settings/roles")
    async def set_role(payload: RoleBindingPayload) -> dict[str, object]:
        try:
            revision = await asyncio.to_thread(
                service.registry.set_role,
                payload.role,
                payload.model_id.strip(),
                reasoning_effort=payload.reasoning_effort,
                expected_revision=payload.expected_revision,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "applied", "revision": revision}

    @router.post("/api/settings/embedding-models")
    async def save_embedding(payload: EmbeddingModelPayload) -> dict[str, object]:
        source_id = payload.source_id.strip() or f"embedding-{uuid4().hex}"
        model_id = payload.model_id.strip() or f"{source_id}__{_model_hash(payload.model)}"
        auth_id = payload.credential_id.strip() or f"embedding_{source_id}"
        key = payload.api_key.strip()
        if not key:
            try:
                key = service.credentials.api_key(auth_id)
            except AuthenticationError as exc:
                raise HTTPException(status_code=422, detail="新向量连接必须填写 API Key") from exc
        try:
            dimensions = await _probe_embedding(payload.base_url, key, payload.model)
            revision = await asyncio.to_thread(
                service.registry.upsert_embedding_model,
                model_id=model_id,
                source_id=source_id,
                source_name=payload.source_name,
                provider=payload.provider,
                auth_id=auth_id,
                base_url=payload.base_url,
                model=payload.model,
                dimensions=dimensions,
                credential=(
                    Credential(
                        driver="api_key",
                        access_token=key,
                        updated_at=datetime.now(timezone.utc).isoformat(),
                    )
                    if payload.api_key.strip()
                    else None
                ),
                expected_revision=payload.expected_revision,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "applied",
            "revision": revision,
            "model": {
                "id": model_id,
                "sourceId": source_id,
                "sourceName": payload.source_name.strip(),
                "provider": payload.provider.strip().lower(),
                "baseUrl": payload.base_url.strip(),
                "model": payload.model.strip(),
                "dimensions": dimensions,
                "credential": {"id": auth_id, "configured": True},
            },
        }

    @router.post("/api/settings/memory")
    async def save_memory(payload: MemorySettingsPayload) -> dict[str, object]:
        if not service.apply_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有设置操作正在执行")
        try:
            await asyncio.to_thread(service.write_memory, payload)
        finally:
            service.apply_lock.release()
        return {"status": "applied", "operationId": f"memory-settings-{uuid4().hex}"}

    @router.post("/api/settings/codex-login")
    async def begin_codex_login() -> dict[str, object]:
        login_id = f"codex-{uuid4().hex}"
        try:
            await asyncio.to_thread(
                service.credentials.provision_connection,
                "codex_default",
                name="Codex",
                provider="codex",
                base_url=CODEX_API_BASE,
            )
            session = await asyncio.to_thread(
                CodexLoginSession,
                login_id,
                CodexAuthDriver(service.credentials, "codex_default"),
            )
        except ModelRuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with service.login_lock:
            service.logins[login_id] = session
        threading.Thread(
            target=_complete_codex_login,
            args=(session, service.login_lock),
            name=f"settings-{login_id}",
            daemon=True,
        ).start()
        return _codex_login_state(session)

    @router.get("/api/settings/codex-login/{login_id}")
    async def codex_login_status(login_id: str) -> dict[str, object]:
        with service.login_lock:
            session = service.logins.get(login_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Codex 登录会话不存在")
            return _codex_login_state(session)

    return router, service


async def settings_security_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/settings/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin", "")
        # The settings page is served by the Vite dev origin during local
        # development and by this backend in the built shell.  A dev proxy
        # preserves the browser Origin header, so accepting only the backend
        # port would reject every legitimate save from http://127.0.0.1:5173.
        expected = f"{request.url.scheme}://{request.url.netloc}"
        allowed_origins = {
            expected,
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        }
        if origin not in allowed_origins or request.headers.get("x-akasic-csrf") != "1":
            return JSONResponse(
                status_code=403,
                content={"code": "csrf_rejected", "message": "请求来源无效"},
            )
    response = await call_next(request)
    if request.url.path.startswith("/api/settings/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def _discover_models(service: SettingsService, payload: ModelQuery) -> list[dict[str, object]]:
    provider = payload.provider.strip().lower()
    if provider == "codex":
        entries = await CodexModelCatalog(
            CodexAuthDriver(service.credentials, payload.credential_id or "codex_default")
        ).list_models()
        return [_codex_model_option(item) for item in entries]
    if provider == "opencode-go":
        key = _candidate_api_key(service, payload.api_key, payload.credential_id, payload.use_local_opencode)
        entries = await OpenCodeGoModelCatalog(
            key, base_url=payload.base_url or OPENCODE_GO_BASE_URL
        ).list_models()
        return [
            {
                "id": item.slug,
                "supportedReasoningEfforts": list(item.supported_reasoning_efforts),
            }
            for item in entries
        ]
    capabilities = resolve_catalog_capabilities(provider, payload.model, base_url=payload.base_url)
    if capabilities is not None and payload.model.strip():
        return [_capability_option(payload.model.strip(), capabilities)]
    if payload.model.strip():
        return []
    key = _candidate_api_key(service, payload.api_key, payload.credential_id, payload.use_local_opencode)
    base_url = _validate_base_url(payload.base_url)
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{base_url}/models", headers={"Authorization": f"Bearer {key}"}
        )
        response.raise_for_status()
    body = response.json()
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise TransportError("模型目录响应缺少 data 数组")
    result = []
    for model_id in sorted(
        {
            str(row.get("id") or "").strip()
            for row in rows
            if isinstance(row, dict) and str(row.get("id") or "").strip()
        }
    ):
        item: dict[str, object] = {"id": model_id}
        caps = resolve_catalog_capabilities(provider, model_id, base_url=payload.base_url)
        if caps is not None:
            item.update(_capability_option(model_id, caps))
        result.append(item)
    return result


async def _connection_candidates(service: SettingsService, payload: ApplyPayload) -> list[RuntimeCandidate]:
    provider = payload.provider.strip().lower()
    if payload.model.strip():
        candidate = await _candidate(service, payload)
        await _validate_live_candidate(service, candidate)
        return [candidate]
    if provider == "codex":
        auth_id = payload.credential_id.strip() or "codex_default"
        entries = await CodexModelCatalog(CodexAuthDriver(service.credentials, auth_id)).list_models()
        if not entries:
            raise TransportError("Codex 账号没有可用模型")
        result = []
        for entry in entries:
            item = payload.model_copy(update={"model": entry.slug, "source_id": auth_id})
            result.append(await _candidate(service, item, provider_capabilities=_codex_capabilities(entry)))
        return result
    if provider == "opencode-go":
        key = _candidate_api_key(service, payload.api_key, payload.credential_id, payload.use_local_opencode)
        entries = await OpenCodeGoModelCatalog(
            key, base_url=payload.base_url or OPENCODE_GO_BASE_URL
        ).list_models()
        if not entries:
            raise TransportError("OpenCode Go 账号没有可用模型")
        source_id = payload.source_id.strip() or "opencode_go_default"
        result = []
        for entry in entries:
            item = payload.model_copy(
                update={"model": entry.slug, "source_id": source_id, "input_modalities": ["text"]}
            )
            candidate = await _candidate(service, item)
            candidate.supported_reasoning_efforts = entry.supported_reasoning_efforts
            result.append(candidate)
        return result
    raise ValueError("自定义 API 连接需要填写或检测一个模型")


async def _candidate(
    service: SettingsService,
    payload: ApplyPayload,
    *,
    provider_capabilities: CatalogCapabilities | None = None,
) -> RuntimeCandidate:
    requested_provider = payload.provider.strip().lower()
    requested_source_id = payload.source_id.strip()
    _validate_identifier("source_id", requested_source_id, allow_colon=True)
    catalog_provider = resolve_catalog_provider_id(
        requested_provider, model=payload.model, base_url=payload.base_url
    )
    provider = (
        catalog_provider
        if requested_provider not in {"codex", "opencode-go"} and catalog_provider
        else requested_provider
    )
    if not provider:
        provider = requested_provider
    legacy_runtime_id = f"{provider.replace('-', '_')}_main"
    runtime_id = (
        f"{requested_source_id}__{_model_hash(payload.model)}"
        if requested_source_id
        else legacy_runtime_id
    )
    source_id = requested_source_id or f"source:{legacy_runtime_id}"
    source_name = payload.source_name.strip() or provider
    auth_id = payload.credential_id.strip()
    key = ""
    if provider == "codex":
        auth_id = auth_id or "codex_default"
        service.credentials.get(auth_id)
        source_id = auth_id
        runtime_id = f"{auth_id}__{_model_hash(payload.model)}" if requested_source_id else legacy_runtime_id
        base_url = payload.base_url.strip() or CODEX_API_BASE
    else:
        auth_id = auth_id or f"model_{requested_source_id or legacy_runtime_id}"
        key = _candidate_api_key(service, payload.api_key, auth_id, payload.use_local_opencode)
        base_url = _validate_base_url(payload.base_url)
    capabilities = provider_capabilities or resolve_catalog_capabilities(
        provider, payload.model, base_url=base_url
    )
    context_window = payload.context_window or (capabilities.context_window if capabilities else 0)
    max_output_tokens = (
        payload.max_output_tokens
        if "max_output_tokens" in payload.model_fields_set
        else capabilities.max_output_tokens if capabilities else 0
    )
    modalities = tuple(payload.input_modalities or (capabilities.input_modalities if capabilities else ("text",)))
    validate_profile_runtime(provider=provider, model=payload.model, input_modalities=modalities)
    catalog_source = "provider_catalog" if provider_capabilities else "litellm" if capabilities else "unknown"
    context_source = "explicit" if payload.context_window else catalog_source if context_window else "unknown"
    output_source = "explicit" if "max_output_tokens" in payload.model_fields_set else catalog_source if max_output_tokens else "unknown"
    modalities_source = "explicit" if payload.input_modalities is not None else catalog_source if capabilities and capabilities.input_modalities_known else "unknown"
    sources = {context_source, output_source, modalities_source}
    return RuntimeCandidate(
        runtime_id=runtime_id,
        source_id=source_id,
        source_name=source_name,
        provider=provider,
        catalog_provider_id=catalog_provider or provider,
        auth_id=auth_id,
        api_key=key,
        base_url=base_url,
        model=payload.model.strip(),
        reasoning_effort=payload.reasoning_effort.strip(),
        supported_reasoning_efforts=capabilities.supported_reasoning_efforts if capabilities else (),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        input_modalities=modalities,
        capability_source=next(iter(sources)) if len(sources) == 1 else "mixed",
        context_window_source=context_source,
        max_output_tokens_source=output_source,
        input_modalities_source=modalities_source,
        use_responses_lite=(provider_capabilities.source == "provider_catalog" and False) if provider_capabilities else False,
        supports_parallel_tool_calls=capabilities.supports_parallel_tool_calls if capabilities else True,
    )


async def _validate_live_candidate(service: SettingsService, candidate: RuntimeCandidate) -> None:
    if candidate.provider == "codex":
        entries = await CodexModelCatalog(CodexAuthDriver(service.credentials, candidate.auth_id)).list_models()
        if candidate.model not in {entry.slug for entry in entries}:
            raise TransportError(f"Codex 模型目录中不存在 {candidate.model}")
        return
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{candidate.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {candidate.api_key}"},
            json={
                "model": candidate.model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
                "stream": False,
            },
        )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise TransportError(f"模型验证失败 (HTTP {response.status_code}): {detail}")


def _publish_candidates(
    service: SettingsService,
    candidates: list[RuntimeCandidate],
    operation_id: str,
) -> int:
    if not candidates:
        raise ValueError("模型连接没有可发布的模型")
    snapshot = service.registry.read_snapshot()
    runtimes: dict[str, dict[str, object]] = {}
    if snapshot is not None:
        runtimes.update(
            {runtime_id: runtime.as_config_table() for runtime_id, runtime in snapshot.runtimes.items()}
        )
    for item in candidates:
        runtimes[item.runtime_id] = item.as_config()
    main = candidates[0].runtime_id
    llm: dict[str, object] = {"main": main, "runtimes": runtimes}
    if snapshot is not None:
        llm["fast"] = snapshot.roles.get("fast", snapshot.roles["default"]).runtime_id
        llm["agent"] = snapshot.roles.get("agent", snapshot.roles["default"]).runtime_id
        llm["vl"] = snapshot.roles.get("vision", snapshot.roles["default"]).runtime_id
    credentials: dict[str, Credential] = {}
    for item in candidates:
        if item.provider == "codex":
            credentials[item.auth_id] = service.credentials.get(item.auth_id)
        elif item.api_key:
            credentials[item.auth_id] = Credential(
                driver="api_key",
                access_token=item.api_key,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
    config_snapshot = service.config_path.read_bytes() if service.config_path.is_file() else None
    registry_existed = service.registry.path.is_file()
    backup_dir = service.workspace / "backups" / "model-settings" / operation_id
    backup_dir.mkdir(parents=True, exist_ok=False)
    if config_snapshot is not None:
        config_backup = backup_dir / "config.before"
        config_backup.write_bytes(config_snapshot)
        os.chmod(config_backup, 0o600)
    registry_backup = backup_dir / "model-registry.before.sqlite3"
    if registry_existed:
        service.registry.backup_to(registry_backup)
    manifest = backup_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "config": config_snapshot is not None,
                "model_registry": registry_existed,
                "credentials": "model-registry.sqlite3",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        revision = service.registry.replace_from_llm_config(llm, credentials=credentials)
        service.ensure_config_marker()
        service.registry.integrity_check()
        return revision
    except BaseException:
        if config_snapshot is None:
            if service.config_path.exists():
                service.config_path.unlink()
        else:
            _atomic_write_bytes(service.config_path, config_snapshot)
        if registry_existed:
            service.registry.restore_from(registry_backup)
        else:
            _remove_sqlite_database(service.registry.path)
        raise


def _runtime_summary(
    runtime: StoredModelRuntime, credential_meta: dict[str, dict[str, str]]
) -> dict[str, object]:
    return {
        "id": runtime.runtime_id,
        "provider": runtime.provider,
        "model": runtime.model,
        "sourceId": runtime.source_id,
        "sourceName": runtime.source_name,
        "catalogProvider": runtime.catalog_provider_id,
        "baseUrl": runtime.base_url,
        "contextWindow": runtime.context_window,
        "maxOutputTokens": runtime.max_output_tokens,
        "inputModalities": list(runtime.input_modalities),
        "reasoningEffort": runtime.reasoning_effort,
        "supportedReasoningEfforts": list(runtime.supported_reasoning_efforts),
        "credential": {
            "id": runtime.auth_id,
            "configured": runtime.auth_id in credential_meta,
            "source": "credential_store" if runtime.auth_id else "none",
        },
    }


def _candidate_api_key(
    service: SettingsService,
    value: str,
    credential_id: str,
    use_local_opencode: bool,
) -> str:
    if value.strip():
        return value.strip()
    if use_local_opencode:
        key = _local_opencode_key(required=True)
        assert key is not None
        return key
    if credential_id:
        try:
            return service.credentials.api_key(credential_id)
        except AuthenticationError:
            pass
    raise AuthenticationError("API Key 不能为空")


def _codex_capabilities(entry: CodexModel) -> CatalogCapabilities:
    caps = entry.capabilities
    return CatalogCapabilities(
        context_window=caps.context_window,
        max_output_tokens=caps.max_output_tokens,
        input_modalities=caps.input_modalities,
        input_modalities_known=entry.input_modalities_known,
        reasoning=bool(caps.supported_reasoning_efforts),
        tool_call=True,
        supported_reasoning_efforts=caps.supported_reasoning_efforts,
        supports_parallel_tool_calls=caps.supports_parallel_tool_calls,
        source="provider_catalog",
    )


def _codex_model_option(entry: CodexModel) -> dict[str, object]:
    caps = entry.capabilities
    return {
        "id": entry.slug,
        "contextWindow": caps.context_window,
        "maxOutputTokens": caps.max_output_tokens,
        "inputModalities": list(caps.input_modalities),
        "supportedReasoningEfforts": list(caps.supported_reasoning_efforts),
        "defaultReasoningEffort": caps.default_reasoning_effort or "",
    }


def _capability_option(model: str, caps: CatalogCapabilities) -> dict[str, object]:
    return {
        "id": model,
        "contextWindow": caps.context_window,
        "maxOutputTokens": caps.max_output_tokens,
        "inputModalities": list(caps.input_modalities),
        "supportedReasoningEfforts": list(caps.supported_reasoning_efforts),
    }


async def _probe_embedding(base_url: str, api_key: str, model: str) -> int:
    normalized = _validate_base_url(base_url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{normalized}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model.strip(), "input": ["Akashic memory connection test"]},
        )
        response.raise_for_status()
    body = response.json()
    rows = body.get("data") if isinstance(body, dict) else None
    vector = rows[0].get("embedding") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if not isinstance(vector, list) or not vector:
        raise ValueError("向量服务响应缺少非空 data[0].embedding")
    if any(isinstance(value, bool) or not isinstance(value, int | float) for value in vector):
        raise ValueError("向量服务返回了无效向量")
    return len(vector)


def _complete_codex_login(session: CodexLoginSession, lock: threading.Lock) -> None:
    try:
        session.driver.complete_device_login(session.code)
    except ModelRuntimeError as exc:
        with lock:
            session.status = "failed"
            session.error = str(exc)
        return
    with lock:
        session.status = "completed"


def _codex_login_state(session: CodexLoginSession) -> dict[str, object]:
    return {
        "loginId": session.login_id,
        "status": session.status,
        "userCode": session.code.user_code,
        "verificationUri": session.code.verification_uri,
        "interval": session.code.interval,
        "error": session.error,
    }


def _local_opencode_key(*, required: bool) -> str | None:
    path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if not path.is_file():
        if required:
            raise AuthenticationError("未找到本机 OpenCode Go 登录")
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthenticationError("OpenCode auth.json 无法读取") from exc
    entry = document.get("opencode-go") if isinstance(document, dict) else None
    key = str(entry.get("key") or "") if isinstance(entry, dict) else ""
    if not key and required:
        raise AuthenticationError("OpenCode Go 登录缺少 API key")
    return key or None


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("https://", "http://")):
        raise ValueError("Base URL 必须是 http(s) 地址")
    return normalized


def _validate_identifier(name: str, value: str, *, allow_colon: bool = False) -> None:
    if not value:
        return
    normalized = value.replace("-", "").replace("_", "")
    if allow_colon:
        normalized = normalized.replace(":", "")
    if not normalized.isalnum() or value.startswith("__"):
        suffix = "、冒号" if allow_colon else ""
        raise ValueError(f"{name} 只能包含字母、数字{suffix}、连字符和下划线")


def _model_hash(model: str) -> str:
    return hashlib.sha256(model.strip().encode("utf-8")).hexdigest()[:10]


def _atomic_write(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_sqlite_database(path: Path) -> None:
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        if candidate.exists():
            candidate.unlink()


__all__ = ["SettingsService", "create_settings_router", "settings_security_middleware"]
