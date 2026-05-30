"""Config-driven custom providers.

Users can register additional providers without writing code, via a JSON file or
environment variables. This is what makes the runtime truly endpoint-agnostic:
point the OpenAI-compatible adapter (or Anthropic/Gemini) at any base URL.

Sources (later sources override earlier per-field):

1. JSON file at ``$OWF_PROVIDERS_FILE`` (default ``~/.workflows/providers.json``):

   {
     "providers": {
       "groq":  {"base_url": "https://api.groq.com/openai/v1",
                 "api_key_env": "GROQ_API_KEY", "default_model": "llama-3.3-70b"},
       "local": {"kind": "openai", "base_url": "http://localhost:11434/v1",
                 "api_key_env": "OLLAMA_KEY", "default_model": "qwen2.5"}
     }
   }

2. Environment variables ``OWF_PROVIDER_<NAME>_{BASE_URL,API_KEY_ENV,MODEL,KIND}``:

   OWF_PROVIDER_GROQ_BASE_URL=https://api.groq.com/openai/v1
   OWF_PROVIDER_GROQ_API_KEY_ENV=GROQ_API_KEY
   OWF_PROVIDER_GROQ_MODEL=llama-3.3-70b

``kind`` is ``openai`` (default), ``anthropic``, or ``gemini``. Validation is
deferred to adapter construction, so one malformed entry never breaks the others.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from ._common import AdapterError
from .api import AnthropicAPIAdapter, GeminiAPIAdapter, OpenAICompatibleAdapter

_ENV_PREFIX = "OWF_PROVIDER_"
_ENV_FIELDS = (
    ("_BASE_URL", "base_url"),
    ("_API_KEY_ENV", "api_key_env"),
    ("_MODEL", "default_model"),
    ("_KIND", "kind"),
)


def _config_path() -> Path:
    override = os.environ.get("OWF_PROVIDERS_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".workflows" / "providers.json"


def _file_configs() -> dict[str, dict[str, Any]]:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"could not read provider config {path}: {exc}") from exc
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return {}
    return {str(name).lower(): cfg for name, cfg in providers.items() if isinstance(cfg, dict)}


def _env_configs() -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        rest = key[len(_ENV_PREFIX):]
        for suffix, attr in _ENV_FIELDS:
            if rest.endswith(suffix):
                name = rest[: -len(suffix)].lower()
                if name:
                    configs.setdefault(name, {})[attr] = value
                break
    return configs


def load_provider_configs() -> dict[str, dict[str, Any]]:
    """Return merged custom-provider configs (file first, env overrides per field)."""
    merged = _file_configs()
    for name, cfg in _env_configs().items():
        merged.setdefault(name, {}).update(cfg)
    return merged


_DEFAULT_CONCURRENCY = 8


def provider_concurrency(provider: str | None) -> int:
    """Max concurrent in-flight calls allowed for a provider.

    Resolution: ``OWF_PROVIDER_<NAME>_CONCURRENCY`` env, then the provider's
    ``concurrency`` field in providers.json, then the global
    ``OWF_MAX_CONCURRENCY`` env, then a built-in default. This caps fan-out
    against rate-limited APIs without changing per-call-site ``parallel()`` caps.
    """
    name = (provider or "").lower()
    env_key = "OWF_PROVIDER_" + name.upper().replace("-", "_") + "_CONCURRENCY"
    for value in (os.environ.get(env_key), None):
        if value and value.isdigit() and int(value) > 0:
            return int(value)
    cfg = load_provider_configs().get(name) or {}
    if isinstance(cfg.get("concurrency"), int) and cfg["concurrency"] > 0:
        return int(cfg["concurrency"])
    global_value = os.environ.get("OWF_MAX_CONCURRENCY")
    if global_value and global_value.isdigit() and int(global_value) > 0:
        return int(global_value)
    return _DEFAULT_CONCURRENCY


def _build(name: str, cfg: dict[str, Any]):
    kind = (cfg.get("kind") or "openai").lower()
    base_url = cfg.get("base_url")
    api_key_env = cfg.get("api_key_env")
    default_model = cfg.get("default_model")

    if kind in ("openai", "openai-compatible"):
        if not base_url or not api_key_env or not default_model:
            raise AdapterError(
                f"custom provider {name!r} needs base_url, api_key_env, and default_model"
            )
        return OpenAICompatibleAdapter(
            name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model
        )

    if kind == "anthropic":
        kwargs: dict[str, Any] = {"name": name}
        for attr in ("base_url", "api_key_env", "default_model"):
            if cfg.get(attr):
                kwargs[attr] = cfg[attr]
        return AnthropicAPIAdapter(**kwargs)

    if kind in ("gemini", "google"):
        kwargs = {"name": name}
        for attr in ("base_url", "api_key_env", "default_model"):
            if cfg.get(attr):
                kwargs[attr] = cfg[attr]
        return GeminiAPIAdapter(**kwargs)

    raise AdapterError(f"custom provider {name!r} has unknown kind {kind!r}")


def custom_provider_factories() -> dict[str, Callable[[], Any]]:
    """Map custom provider name -> lazy factory. Config errors surface only when
    a given provider is actually built, never at load time."""
    configs = load_provider_configs()
    return {name: (lambda c=cfg, n=name: _build(n, c)) for name, cfg in configs.items()}
