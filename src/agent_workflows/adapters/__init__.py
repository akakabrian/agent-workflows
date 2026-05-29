from .cli import AdapterError, ClaudeCLIAdapter, CodexCLIAdapter
from .fake import FakeAdapter

__all__ = ["AdapterError", "ClaudeCLIAdapter", "CodexCLIAdapter", "FakeAdapter", "build_adapter"]


# Provider name -> adapter factory. Aliases let a workflow request a provider by
# a few natural names; unknown providers raise AdapterError.
_REGISTRY: dict[str, type] = {
    "fake": FakeAdapter,
    "fixture": FakeAdapter,
    "claude": ClaudeCLIAdapter,
    "claude-cli": ClaudeCLIAdapter,
    "anthropic": ClaudeCLIAdapter,
    "codex": CodexCLIAdapter,
    "codex-cli": CodexCLIAdapter,
}


def build_adapter(provider: str | None):
    """Resolve a provider name to a fresh adapter instance.

    Defaults to the fake adapter when no provider is given so examples and tests
    run offline. Raises AdapterError for an unrecognized provider.
    """
    key = (provider or "fake").lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY))
        raise AdapterError(f"unknown provider {provider!r}; known providers: {known}")
    return factory()
