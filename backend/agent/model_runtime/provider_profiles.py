from __future__ import annotations

from dataclasses import dataclass

OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    default_base_url: str
    messages_model_prefixes: tuple[str, ...]
    input_modalities: tuple[str, ...] = ("text",)

    def classify_model(self, model: str) -> str:
        normalized = model.strip().lower()
        if not normalized:
            return "unknown"
        if normalized.startswith(self.messages_model_prefixes):
            return "messages"
        return "chat_completions"


OPENCODE_GO_PROFILE = ProviderProfile(
    provider_id="opencode-go",
    default_base_url=OPENCODE_GO_BASE_URL,
    messages_model_prefixes=("minimax-", "qwen"),
)


def validate_profile_runtime(
    *, provider: str, model: str, input_modalities: tuple[str, ...]
) -> None:
    if provider.strip().lower() != OPENCODE_GO_PROFILE.provider_id:
        return
    protocol = OPENCODE_GO_PROFILE.classify_model(model)
    if protocol == "messages":
        raise ValueError(
            f"provider opencode-go 的模型 {model} 使用 Messages API，当前仅支持 Chat Completions 模型"
        )
    if protocol == "unknown":
        raise ValueError("provider opencode-go 的模型 ID 不能为空")
    if input_modalities != OPENCODE_GO_PROFILE.input_modalities:
        raise ValueError("provider opencode-go 仅支持 input_modalities = ['text']")
