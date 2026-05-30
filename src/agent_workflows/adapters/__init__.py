from typing import Any, Callable

from ._common import AdapterError
from ._config import custom_provider_factories, load_provider_configs
from .api import AnthropicAPIAdapter, GeminiAPIAdapter, OpenAICompatibleAdapter
from .cli import ClaudeCLIAdapter, CodexCLIAdapter
from .fake import FakeAdapter

__all__ = [
    "AdapterError",
    "AnthropicAPIAdapter",
    "ClaudeCLIAdapter",
    "CodexCLIAdapter",
    "FakeAdapter",
    "GeminiAPIAdapter",
    "OpenAICompatibleAdapter",
    "build_adapter",
    "list_providers",
]


# Provider name -> adapter factory. A factory is any zero-arg callable returning
# a fresh adapter instance, so the OpenAI-compatible adapter can be pre-bound to
# a base URL / key env / default model per provider. Unknown providers raise
# AdapterError.
#
# Naming: `claude` / `codex` are the local CLI adapters (reuse the CLI's own
# auth). `openai` / `anthropic` / `gemini` are the direct HTTP API adapters and
# read keys from the environment. `deepseek` / `openrouter` reuse the
# OpenAI-compatible adapter against their own endpoints.
_REGISTRY: dict[str, Callable[[], Any]] = {
    # Offline
    "fake": FakeAdapter,
    "fixture": FakeAdapter,
    # Local CLIs (no API key needed)
    "claude": ClaudeCLIAdapter,
    "claude-cli": ClaudeCLIAdapter,
    "codex": CodexCLIAdapter,
    "codex-cli": CodexCLIAdapter,
    # HTTP APIs (keys from the environment)
    "openai": OpenAICompatibleAdapter,
    "anthropic": AnthropicAPIAdapter,
    "gemini": GeminiAPIAdapter,
    "google": GeminiAPIAdapter,
    "deepseek": lambda: OpenAICompatibleAdapter(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-v4-flash",
    ),
    "openrouter": lambda: OpenAICompatibleAdapter(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        default_model="openai/gpt-5.5",
    ),
}


def build_adapter(provider: str | None):
    """Resolve a provider name to a fresh adapter instance.

    Resolution order: built-in providers, then custom providers from the config
    file / environment (see :mod:`._config`). Defaults to the fake adapter when
    no provider is given so examples and tests run offline. Raises AdapterError
    for an unrecognized provider.
    """
    key = (provider or "fake").lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        factory = custom_provider_factories().get(key)
    if factory is None:
        custom = sorted(load_provider_configs())
        known = ", ".join(sorted(_REGISTRY) + custom)
        raise AdapterError(f"unknown provider {provider!r}; known providers: {known}")
    return factory()


def list_providers() -> list[dict[str, Any]]:
    """Enumerate available providers (built-in + custom) for discovery/UX."""
    rows: list[dict[str, Any]] = []
    for name in sorted(_REGISTRY):
        adapter = _REGISTRY[name]()
        rows.append(
            {
                "name": name,
                "source": "builtin",
                "adapter": type(adapter).__name__,
                "api_key_env": getattr(adapter, "api_key_env", None),
                "default_model": getattr(adapter, "default_model", None),
            }
        )
    builtin = set(_REGISTRY)
    for name, cfg in sorted(load_provider_configs().items()):
        if name in builtin:
            continue
        rows.append(
            {
                "name": name,
                "source": "custom",
                "adapter": (cfg.get("kind") or "openai"),
                "api_key_env": cfg.get("api_key_env"),
                "default_model": cfg.get("default_model"),
                "base_url": cfg.get("base_url"),
            }
        )
    return rows
