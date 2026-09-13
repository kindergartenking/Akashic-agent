"""Minimal model configuration boundary for the WebSocket MVP.

The full project keeps model definitions and credentials in the workspace
SQLite registry.  This module only resolves one runtime for one request; it
does not cache conversations or expose credentials to the browser.
"""

from __future__ import annotations

import os
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The copied runtime modules retain the original top-level ``agent`` import
# layout. Make that layout available both when launched as ``backend.app`` and
# when this module is imported directly in a test or utility.
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent.model_runtime.auth.store import CredentialStore
from agent.model_runtime.store import ModelRegistryStore, StoredModelRuntime


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    runtime_id: str
    source_name: str
    reasoning_effort: str = ""
    auth_id: str = ""
    registry_path: Path | None = None
    use_responses_lite: bool = False
    supports_parallel_tool_calls: bool = True
    reasoning_summary: str = "none"
    context_window: int = 8192

    @property
    def configured(self) -> bool:
        credential = self.auth_id if self.provider == "codex" else self.api_key
        return bool(self.model and self.base_url and credential)


class ModelConfigResolver:
    """Resolve the selected runtime from SQLite, with env vars as fallback."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.registry_path = Path(
            os.getenv("AKASHIC_MODEL_REGISTRY", str(workspace / "model-registry.sqlite3"))
        )

    def resolve(self, runtime_id: str | None = None) -> ModelConfig | None:
        snapshot = self._read_snapshot()
        if snapshot is not None:
            selected_id = runtime_id or snapshot.roles["default"].runtime_id
            runtime = snapshot.runtimes.get(selected_id)
            if runtime is None:
                runtime = snapshot.runtimes[snapshot.roles["default"].runtime_id]
            key = self._database_key(runtime)
            # An explicitly supplied key is useful for local development and
            # takes precedence when a registry row has no credential payload.
            key = key or os.getenv("AKASHIC_API_KEY", "").strip()
            return ModelConfig(
                provider=runtime.provider,
                model=runtime.model,
                base_url=(runtime.base_url or os.getenv("AKASHIC_BASE_URL", "")).strip(),
                api_key=key,
                runtime_id=runtime.runtime_id,
                source_name=runtime.source_name,
                reasoning_effort=runtime.reasoning_effort,
                auth_id=runtime.auth_id,
                registry_path=self.registry_path,
                use_responses_lite=runtime.use_responses_lite,
                supports_parallel_tool_calls=runtime.supports_parallel_tool_calls,
                reasoning_summary=runtime.reasoning_summary,
                context_window=runtime.context_window or 8192,
            )

        model = os.getenv("AKASHIC_MODEL", "").strip()
        base_url = os.getenv("AKASHIC_BASE_URL", "https://api.openai.com/v1").strip()
        api_key = os.getenv("AKASHIC_API_KEY", "").strip()
        if not model and not api_key and not os.getenv("AKASHIC_BASE_URL"):
            return None
        return ModelConfig(
            provider=os.getenv("AKASHIC_PROVIDER", "openai").strip() or "openai",
            model=model or "gpt-4o-mini",
            base_url=base_url,
            api_key=api_key,
            runtime_id="env",
            source_name="Environment",
            reasoning_effort="",
            auth_id="env",
            context_window=int(os.getenv("AKASHIC_CONTEXT_WINDOW", "8192") or 8192),
        )

    def runtimes_for_api(self) -> tuple[dict[str, Any], ...]:
        snapshot = self._read_snapshot()
        if snapshot is not None:
            roles: dict[str, list[str]] = {}
            for role, binding in snapshot.roles.items():
                roles.setdefault(binding.runtime_id, []).append(role)
            return tuple(
                {
                    "id": runtime.runtime_id,
                    "provider": runtime.provider,
                    "model": runtime.model,
                    "sourceId": runtime.source_id,
                    "sourceName": runtime.source_name,
                    "reasoningEffort": runtime.reasoning_effort,
                    "supportedReasoningEfforts": list(runtime.supported_reasoning_efforts),
                    "roles": roles.get(runtime.runtime_id, []),
                }
                for runtime in snapshot.runtimes.values()
            )
        config = self.resolve()
        if config is None:
            return ()
        return ({
            "id": config.runtime_id,
            "provider": config.provider,
            "model": config.model,
            "sourceId": config.runtime_id,
            "sourceName": config.source_name,
            "reasoningEffort": config.reasoning_effort,
            "supportedReasoningEfforts": ([config.reasoning_effort] if config.reasoning_effort else []),
            "roles": ["default"],
        },)

    def _read_snapshot(self):
        try:
            return ModelRegistryStore(self.registry_path).read_snapshot()
        except Exception:
            # A partially provisioned registry should not prevent env fallback.
            return None

    def _database_key(self, runtime: StoredModelRuntime) -> str:
        try:
            return CredentialStore(self.registry_path).api_key(runtime.auth_id)
        except Exception:
            # Windows does not reliably apply POSIX mode bits to SQLite files;
            # read the same auth_payload directly as a compatibility fallback.
            # The token remains server-side and is never included in API data.
            try:
                with sqlite3.connect(self.registry_path) as connection:
                    row = connection.execute(
                        "SELECT auth_kind, auth_payload FROM model_connections WHERE auth_id = ?",
                        (runtime.auth_id,),
                    ).fetchone()
                if not row or str(row[0]) != "api_key":
                    return ""
                payload = json.loads(str(row[1]))
                token = payload.get("access_token") if isinstance(payload, dict) else ""
                return str(token or "").strip()
            except Exception:
                return ""
