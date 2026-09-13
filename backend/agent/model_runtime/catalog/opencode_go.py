from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import cast

import httpx

from agent.model_runtime.catalog.litellm_registry import resolve_catalog_capabilities
from agent.model_runtime.errors import AuthenticationError, TransportError
from agent.model_runtime.provider_profiles import OPENCODE_GO_BASE_URL, OPENCODE_GO_PROFILE


@dataclass(frozen=True)
class OpenCodeGoModel:
    slug: str
    supported_reasoning_efforts: tuple[str, ...] = ()


_OPENCODE_PROVIDER_PREFIX = "opencode-go/"


def _parse_opencode_go_reasoning_efforts(output: str) -> dict[str, tuple[str, ...]]:
    decoder = json.JSONDecoder()
    cursor = 0
    efforts: dict[str, tuple[str, ...]] = {}
    while cursor < len(output):
        while cursor < len(output) and output[cursor].isspace():
            cursor += 1
        if cursor >= len(output):
            break
        line_end = output.find("\n", cursor)
        if line_end == -1:
            raise TransportError("OpenCode 模型目录包含不完整的标题行")
        header = output[cursor:line_end].strip()
        if not header.startswith(_OPENCODE_PROVIDER_PREFIX):
            raise TransportError("OpenCode 模型目录包含未知记录")
        model_id = header.removeprefix(_OPENCODE_PROVIDER_PREFIX).strip()
        if not model_id:
            raise TransportError("OpenCode 模型目录包含空模型 ID")
        json_start = line_end + 1
        while json_start < len(output) and output[json_start].isspace():
            json_start += 1
        try:
            decoded, cursor = decoder.raw_decode(output, json_start)
        except json.JSONDecodeError as exc:
            raise TransportError("OpenCode 模型目录返回了无效 JSON") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("variants"), dict):
            raise TransportError(f"OpenCode 模型 {model_id} 的 variants 无效")
        variants = cast(dict[object, object], decoded["variants"])
        if not all(isinstance(name, str) and name for name in variants):
            raise TransportError(f"OpenCode 模型 {model_id} 的 variants 无效")
        efforts[model_id] = tuple(cast(str, name) for name in variants)
    return efforts


async def _load_opencode_go_reasoning_efforts(executable: str) -> dict[str, tuple[str, ...]]:
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "models",
            "opencode-go",
            "--verbose",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise TransportError(f"无法执行 OpenCode 模型探测：{exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TransportError("OpenCode 模型探测超时") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise TransportError(f"OpenCode 模型探测失败{'：' + detail[:500] if detail else ''}")
    try:
        return _parse_opencode_go_reasoning_efforts(stdout.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as exc:
        raise TransportError("OpenCode 模型目录不是有效 UTF-8") from exc


class OpenCodeGoModelCatalog:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = OPENCODE_GO_BASE_URL,
        opencode_executable: str = "opencode",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.opencode_executable = opencode_executable

    async def list_models(self) -> list[OpenCodeGoModel]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise TransportError(f"OpenCode Go 模型目录请求失败：{exc}") from exc
        if response.status_code in {401, 403}:
            raise AuthenticationError("OpenCode Go 模型目录认证失败，请检查 API key")
        if response.status_code >= 400:
            raise TransportError(f"OpenCode Go 模型目录请求失败 (HTTP {response.status_code})")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise TransportError("OpenCode Go 模型目录返回了无效 JSON") from exc
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise TransportError("OpenCode Go 模型目录响应缺少 data 数组")
        try:
            efforts = await _load_opencode_go_reasoning_efforts(self.opencode_executable)
        except TransportError as exc:
            logging.getLogger(__name__).warning(
                "OpenCode variant 目录不可用，思考强度改用本地模型注册表: %s", exc
            )
            efforts = {}
        result: list[OpenCodeGoModel] = []
        for raw in rows:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
                raise TransportError("OpenCode Go 模型目录包含无效模型项")
            model_id = raw["id"]
            if OPENCODE_GO_PROFILE.classify_model(model_id) == "chat_completions":
                capabilities = resolve_catalog_capabilities(
                    "opencode-go", model_id, base_url=self.base_url
                )
                result.append(
                    OpenCodeGoModel(
                        slug=model_id,
                        supported_reasoning_efforts=efforts.get(
                            model_id,
                            capabilities.supported_reasoning_efforts if capabilities else (),
                        ),
                    )
                )
        return result
