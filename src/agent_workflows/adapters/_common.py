"""Shared adapter helpers used by both the CLI and HTTP API adapters.

Everything here is standard-library only. The HTTP helper uses ``urllib`` so the
package keeps its zero-dependency promise; blocking calls are meant to be run
inside ``asyncio.to_thread`` by the API adapters.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..models import AgentResult, WorkflowArtifact
from ..validation import SchemaValidationError, validate_schema


class AdapterError(RuntimeError):
    """Raised when an adapter is misconfigured (e.g. binary or key missing)."""


# Model names used as runtime placeholders rather than real provider models.
_PLACEHOLDER_MODELS = {"fake", "default", "claude", "codex", "openai", "anthropic", "gemini", "google"}


def resolve_model(model: str | None, default_model: str) -> str:
    """Return a concrete model name, falling back to ``default_model`` for the
    runtime placeholder labels (``fake``/``default``/provider names)."""
    if not model or model.lower() in _PLACEHOLDER_MODELS:
        return default_model
    return model


def schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "\n\nReturn ONLY a single JSON value that conforms to this JSON Schema. "
        "Do not wrap it in Markdown code fences or add prose:\n"
        + json.dumps(schema, separators=(",", ":"))
    )


def correction_instruction(error: str) -> str:
    return (
        f"\n\nYour previous response was not valid for the schema: {error}\n"
        "Return corrected JSON only — no prose, no code fences."
    )


def strict_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy of ``schema`` made valid for strict structured output, or
    None if the schema declares optional fields.

    Strict mode (OpenAI ``json_schema`` strict / Gemini ``responseSchema``)
    requires every object property to be required and forbids extra properties.
    We only enable it when the schema has no optional fields, so we never silently
    change the caller's intended shape; otherwise the caller falls back to plain
    JSON-object mode plus the validation retry.
    """

    def convert(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        out = dict(node)
        if out.get("type") == "object" or "properties" in out:
            props = out.get("properties")
            if isinstance(props, dict):
                if set(out.get("required", [])) != set(props):
                    raise _NotStrict()
                out["properties"] = {k: convert(v) for k, v in props.items()}
                out["required"] = list(props)
                out["additionalProperties"] = False
        if "items" in out:
            out["items"] = convert(out["items"])
        return out

    try:
        return convert(schema)
    except _NotStrict:
        return None


class _NotStrict(Exception):
    pass


def parse_and_validate(text: str, schema: dict[str, Any]) -> tuple[bool, str | None]:
    """Parse ``text`` as JSON and validate it against ``schema``.

    Returns ``(ok, error_message)``. Mirrors the runtime's own validation so an
    adapter can retry before the runtime records a ``schema_failed`` result.
    """
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return False, "response was not valid JSON"
    try:
        validate_schema(value, schema)
    except SchemaValidationError as exc:
        return False, f"{exc.path}: {exc.message}"
    return True, None


# ---------------------------------------------------------------------------
# AgentResult builders (shared with the CLI adapters)
# ---------------------------------------------------------------------------

def ok_text_result(
    request,
    text: str,
    *,
    usage: dict[str, Any] | None = None,
    cost: float | None = None,
) -> AgentResult:
    clean_usage = {k: v for k, v in (usage or {}).items() if v is not None}
    artifacts: list[WorkflowArtifact] = []
    return AgentResult(
        ok=True,
        status="done",
        value=None,
        text=text,
        provider=request.provider,
        model=request.model,
        agent_type=request.agent_type,
        input_tokens=clean_usage.get("input_tokens"),
        output_tokens=clean_usage.get("output_tokens"),
        cache_read_tokens=clean_usage.get("cache_read_tokens"),
        cache_write_tokens=clean_usage.get("cache_write_tokens"),
        estimated_cost_usd=cost,
        artifacts=artifacts,
    )


def provider_failed_result(request, message: str) -> AgentResult:
    return AgentResult(
        ok=False,
        status="provider_failed",
        value=None,
        text=None,
        error=message,
        provider=request.provider,
        model=request.model,
        agent_type=request.agent_type,
    )


def timed_out_result(request, timeout: int) -> AgentResult:
    return AgentResult(
        ok=False,
        status="timeout",
        value=None,
        text=None,
        error=f"provider call exceeded {timeout}s timeout",
        provider=request.provider,
        model=request.model,
        agent_type=request.agent_type,
    )


def missing_key_result(request, *, provider: str, env_var: str) -> AgentResult:
    return provider_failed_result(
        request,
        (
            f"Provider `{provider}` needs an API key in ${env_var}.\n\n"
            f"Set it, e.g.:\n  export {env_var}=...\n\n"
            "Or run offline first:\n  owf run examples/hello_workflow.py --provider fake"
        ),
    )


# ---------------------------------------------------------------------------
# Static price table (approximate USD per 1M tokens)
# ---------------------------------------------------------------------------
# These are coarse, hand-maintained estimates used only to populate
# ``estimated_cost_usd`` so the ``--budget-cost-usd`` gate is usable. They are
# not billing-accurate; prefer the provider's own reported cost when available.
# Keys are matched by longest-prefix against a lowercased model name.

_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    # OpenAI — GPT-5.x family (May 2026). Longest-prefix match, so the more
    # specific "-pro"/"-mini"/"-nano" keys win over the base name.
    "gpt-5.5-pro": (30.00, 180.00),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.4-pro": (30.00, 180.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4": (2.50, 15.00),
    # OpenAI — legacy
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "o4-mini": (1.10, 4.40),
    # DeepSeek — V4 family (May 2026); chat/reasoner are deprecated aliases of V4-Flash.
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (1.74, 3.48),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-reasoner": (0.14, 0.28),
    # Anthropic — Claude 4.x (May 2026). Opus uses standard-mode pricing (fast
    # mode is ~2x and not distinguishable from the model id).
    "claude-opus-4": (5.00, 25.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4": (1.00, 5.00),
    # Anthropic — legacy 3.x
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    # Gemini (May 2026)
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.1-pro": (2.00, 12.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3-flash": (0.50, 3.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
}


# Optional override file (refreshable via `owf prices --refresh`) layered on top
# of the static table. Cached per (path, mtime) so repeated estimate_cost calls
# are cheap but a refreshed file is picked up.
_PRICE_OVERRIDE_CACHE: dict[str, tuple[float, dict[str, tuple[float, float]]]] = {}


def price_overrides_path() -> Path:
    override = os.environ.get("OWF_PRICES_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".workflows" / "prices.json"


def _normalize_price_map(raw: Any) -> dict[str, tuple[float, float]]:
    prices = raw.get("prices") if isinstance(raw, dict) and "prices" in raw else raw
    table: dict[str, tuple[float, float]] = {}
    if isinstance(prices, dict):
        for key, value in prices.items():
            if isinstance(value, (list, tuple)) and len(value) == 2:
                try:
                    table[str(key).lower()] = (float(value[0]), float(value[1]))
                except (TypeError, ValueError):
                    continue
    return table


def _load_price_overrides() -> dict[str, tuple[float, float]]:
    path = price_overrides_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _PRICE_OVERRIDE_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        table = _normalize_price_map(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}
    _PRICE_OVERRIDE_CACHE[str(path)] = (mtime, table)
    return table


def price_table() -> dict[str, tuple[float, float]]:
    """Static prices merged with any user override file (override wins)."""
    return {**_PRICES_PER_MTOK, **_load_price_overrides()}


def estimate_cost(model: str | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Estimate USD cost from the merged price table. Returns None when the model
    is unknown or token counts are missing."""
    if not model or input_tokens is None or output_tokens is None:
        return None
    name = model.lower()
    match: tuple[float, float] | None = None
    matched_len = -1
    for prefix, prices in price_table().items():
        if name.startswith(prefix) and len(prefix) > matched_len:
            match = prices
            matched_len = len(prefix)
    if match is None:
        return None
    in_price, out_price = match
    return round((input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price, 9)


# ---------------------------------------------------------------------------
# HTTP helper with retry/backoff (blocking — call inside asyncio.to_thread)
# ---------------------------------------------------------------------------

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpResult:
    __slots__ = ("status", "body")

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int,
    retries: int = 2,
    backoff_base: float = 0.5,
    sleep=time.sleep,
) -> HttpResult:
    """POST a JSON body and return the status + raw response text.

    Retries on connection errors and retryable HTTP statuses (429/5xx) with
    linear backoff. ``sleep`` is injectable so tests can avoid real delays.
    Raises ``AdapterError`` only when every attempt fails to connect.
    """
    data = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", **headers}
    last_error: str | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed provider URLs
                return HttpResult(getattr(resp, "status", 200), resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
            if exc.code in _RETRY_STATUSES and attempt < retries:
                last_error = f"HTTP {exc.code}"
                sleep(backoff_base * (attempt + 1))
                continue
            return HttpResult(exc.code, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            if attempt < retries:
                sleep(backoff_base * (attempt + 1))
                continue
            raise AdapterError(f"request to {url} failed after {retries + 1} attempts: {last_error}") from exc
    raise AdapterError(f"request to {url} failed: {last_error}")  # pragma: no cover - loop always returns/raises


def request_text(url: str, headers: dict[str, str], *, method: str = "GET", data: bytes | None = None, timeout: int = 60) -> HttpResult:
    """Issue a request and return status + raw text (used by the batch clients)."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed provider URLs
            return HttpResult(getattr(resp, "status", 200), resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        return HttpResult(exc.code, body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"request to {url} failed: {exc}") from exc


def post_multipart(
    url: str,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    headers: dict[str, str],
    *,
    timeout: int = 120,
) -> HttpResult:
    """POST a multipart/form-data body (for OpenAI's file upload). Stdlib-only."""
    boundary = "----owf" + os.urandom(16).hex()
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    request_headers = {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed provider URLs
            return HttpResult(getattr(resp, "status", 200), resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        return HttpResult(exc.code, body_text)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"upload to {url} failed: {exc}") from exc


def refresh_prices(url: str, *, timeout: int = 30) -> int:
    """Fetch a price table from ``url`` and write it to the overrides file.

    The remote document is JSON shaped as ``{"model-prefix": [in, out], ...}`` or
    ``{"prices": {...}}`` (USD per 1M tokens). Returns the number of entries
    written. Raises AdapterError on a non-200 or unparseable response.
    """
    res = request_text(url, {"Accept": "application/json"}, timeout=timeout)
    if res.status != 200:
        raise AdapterError(f"price refresh failed: HTTP {res.status}")
    try:
        table = _normalize_price_map(json.loads(res.body))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"price refresh returned invalid JSON: {exc}") from exc
    if not table:
        raise AdapterError("price refresh returned no usable {prefix: [in, out]} entries")
    path = price_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"prices": {k: list(v) for k, v in table.items()}}, indent=2, sort_keys=True), encoding="utf-8")
    _PRICE_OVERRIDE_CACHE.pop(str(path), None)
    return len(table)
