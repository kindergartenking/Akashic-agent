from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, cast

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"


@dataclass(frozen=True)
class CatalogCapabilities:
    context_window: int
    max_output_tokens: int
    input_modalities: tuple[str, ...]
    reasoning: bool
    tool_call: bool
    input_modalities_known: bool = True
    supported_reasoning_efforts: tuple[str, ...] = ()
    supports_parallel_tool_calls: bool = True
    source: str = "litellm"


_PROVIDER_ALIASES = {"dashscope": "dashscope", "opencode_go": "opencode-go", "xai": "x-ai"}
_LITELLM_PROVIDER_ALIASES = {"x-ai": "xai", "z-ai": "zai"}
_EFFORT_FLAGS = (
    ("none", "supports_none_reasoning_effort"),
    ("minimal", "supports_minimal_reasoning_effort"),
    ("xhigh", "supports_xhigh_reasoning_effort"),
    ("max", "supports_max_reasoning_effort"),
)
_BASE_URL_PROVIDERS = {
    "api.openai.com": "openai",
    "api.deepseek.com": "deepseek",
    "openrouter.ai": "openrouter",
    "api.x.ai": "x-ai",
    "dashscope.aliyuncs.com": "dashscope",
}


def resolve_catalog_capabilities(
    provider: str, model: str, *, base_url: str = ""
) -> CatalogCapabilities | None:
    provider_id = resolve_catalog_provider_id(provider, model=model, base_url=base_url)
    raw = _model_entry(provider_id, model)
    if raw is None:
        return None
    modalities, modalities_known = _input_modalities(raw)
    reasoning = raw.get("supports_reasoning") is True
    max_input_tokens = _positive_int(raw.get("max_input_tokens"))
    max_output_tokens = _positive_int(raw.get("max_output_tokens") or raw.get("max_tokens"))
    return CatalogCapabilities(
        context_window=(max_input_tokens + max_output_tokens if max_input_tokens and max_output_tokens else max_input_tokens),
        max_output_tokens=max_output_tokens,
        input_modalities=modalities,
        input_modalities_known=modalities_known,
        reasoning=reasoning,
        tool_call=raw.get("supports_function_calling") is True,
        supported_reasoning_efforts=_reasoning_efforts(raw) if reasoning else (),
        supports_parallel_tool_calls=raw.get("supports_parallel_function_calling") is not False,
    )


def resolve_catalog_provider_id(
    provider: str, *, model: str = "", base_url: str = ""
) -> str:
    if base_url:
        try:
            from genai_prices.data_snapshot import get_snapshot

            matched_provider = get_snapshot().find_provider(None, None, base_url)
            match = re.match(matched_provider.api_pattern, base_url)
            suffix = base_url[match.end() :] if match is not None else "invalid"
            if not suffix or suffix.startswith(("/", "?", "#")):
                detected = matched_provider.id
                if detected is not None:
                    return _PROVIDER_ALIASES.get(detected, detected)
        except (ImportError, LookupError):
            pass
    lowered_url = base_url.strip().lower()
    for host, provider_id in _BASE_URL_PROVIDERS.items():
        if re.match(rf"^https?://{re.escape(host)}(?:[:/]|$)", lowered_url):
            return provider_id
    raw_provider = provider.strip().lower()
    normalized = _PROVIDER_ALIASES.get(raw_provider, raw_provider)
    if normalized in _known_provider_ids():
        return normalized
    if "/" in model:
        prefix = model.split("/", 1)[0].lower()
        if prefix in _known_provider_ids():
            return prefix
    return normalized if normalized in {"codex", "opencode-go"} else ""


@lru_cache(maxsize=1)
def _registry() -> dict[str, dict[str, Any]]:
    try:
        import litellm
    except ImportError:
        return {}
    return cast(dict[str, dict[str, Any]], litellm.model_cost)


@lru_cache(maxsize=1)
def _known_provider_ids() -> frozenset[str]:
    providers = {
        str(raw.get("litellm_provider") or "").lower()
        for raw in _registry().values()
    }
    providers.discard("")
    providers.update(_PROVIDER_ALIASES.values())
    providers.update(_BASE_URL_PROVIDERS.values())
    return frozenset(providers)


def _model_entry(provider: str, model: str) -> dict[str, Any] | None:
    normalized_model = model.strip()
    if not normalized_model:
        return None
    litellm_provider = _LITELLM_PROVIDER_ALIASES.get(provider, provider)
    candidates = []
    if litellm_provider and not normalized_model.startswith(f"{litellm_provider}/"):
        candidates.append(f"{litellm_provider}/{normalized_model}")
    candidates.append(normalized_model)
    return next((_registry()[item] for item in candidates if item in _registry()), None)


def _input_modalities(raw: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    declared = raw.get("supported_modalities")
    if isinstance(declared, list):
        if not all(isinstance(item, str) for item in declared):
            return ("text",), False
        values = tuple(dict.fromkeys(cast(list[str], declared)))
        return ("text",) + tuple(item.lower() for item in values if item.lower() != "text"), True
    vision = raw.get("supports_vision")
    if isinstance(vision, bool):
        return (("text", "image") if vision else ("text",)), True
    return ("text",), False


def _reasoning_efforts(raw: dict[str, Any]) -> tuple[str, ...]:
    efforts = [name for name, flag in _EFFORT_FLAGS[:2] if raw.get(flag) is True]
    if raw.get("supports_low_reasoning_effort") is not False:
        efforts.append("low")
    efforts.extend(("medium", "high"))
    efforts.extend(name for name, flag in _EFFORT_FLAGS[2:] if raw.get(flag) is True)
    return tuple(efforts)


def _positive_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
