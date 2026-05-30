"""HTTP API adapters: OpenAI-compatible, Anthropic Messages, and Gemini.

All three are standard-library only (``urllib`` via :mod:`._common`). API keys
are read from environment variables and never written to the run store. The
blocking HTTP call runs inside ``asyncio.to_thread`` so adapters stay async.

The OpenAI-compatible adapter is parameterized by ``base_url`` / ``api_key_env``
/ ``default_model``, so a single class serves OpenAI, DeepSeek, OpenRouter, and
any other endpoint that implements ``POST /chat/completions`` (Groq, Together,
local vLLM/Ollama, …).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..models import AgentResult
from ._common import (
    AdapterError,
    correction_instruction,
    estimate_cost,
    missing_key_result,
    ok_text_result,
    parse_and_validate,
    post_json,
    provider_failed_result,
    resolve_model,
    schema_instruction,
    strict_schema,
)

# A single provider call returns (text, usage, cost, error_message).
_CallResult = tuple[str, dict[str, Any], float | None, str | None]


def _is_response_format_error(body: str) -> bool:
    """Heuristic: does this 4xx body complain about response_format/json_schema?"""
    low = body.lower()
    return "response_format" in low or "json_schema" in low or "schema" in low


def _http_error_message(status: int, body: str) -> str:
    """Extract a readable message from a provider error body."""
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return f"HTTP {status}: {body[:300]}".strip()
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return f"HTTP {status}: {err['message']}"
    if isinstance(err, str):
        return f"HTTP {status}: {err}"
    return f"HTTP {status}: {body[:300]}".strip()


async def _complete_with_retry(
    request,
    base_prompt: str,
    schema: dict[str, Any] | None,
    do_call: Callable[[str], Awaitable[_CallResult]],
    *,
    inject_schema_text: bool,
    max_attempts: int = 2,
) -> AgentResult:
    """Drive ``do_call`` with a schema-validation retry loop.

    On a schema mismatch the prompt is re-sent once with the validation error so
    the model can self-correct. Token usage and cost accumulate across attempts.
    The returned text is validated again by the runtime, which assigns the final
    ``schema_failed`` status if the last attempt is still invalid.
    """
    prompt = base_prompt
    if schema and inject_schema_text:
        prompt = base_prompt + schema_instruction(schema)

    total_in = total_out = 0
    total_cost = 0.0
    have_cost = False
    last_text = ""

    for attempt in range(max_attempts):
        try:
            text, usage, cost, error = await do_call(prompt)
        except AdapterError as exc:
            return provider_failed_result(request, str(exc))
        if error is not None:
            return provider_failed_result(request, error)

        total_in += usage.get("input_tokens") or 0
        total_out += usage.get("output_tokens") or 0
        if cost is not None:
            total_cost += cost
            have_cost = True
        last_text = text

        if not schema:
            break
        ok, verr = parse_and_validate(text, schema)
        if ok or attempt == max_attempts - 1:
            break
        prompt = base_prompt
        if inject_schema_text:
            prompt = base_prompt + schema_instruction(schema)
        prompt = prompt + correction_instruction(verr or "invalid")

    return ok_text_result(
        request,
        last_text,
        usage={"input_tokens": total_in or None, "output_tokens": total_out or None},
        cost=round(total_cost, 9) if have_cost else None,
    )


@dataclass(slots=True)
class OpenAICompatibleAdapter:
    """Adapter for any OpenAI ``/chat/completions``-compatible endpoint."""

    name: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    default_model: str = "gpt-5.4-mini"
    default_timeout_seconds: int = 600
    extra_headers: dict[str, str] = field(default_factory=dict)

    async def run(self, request) -> AgentResult:
        key = os.environ.get(self.api_key_env)
        if not key:
            return missing_key_result(request, provider=self.name, env_var=self.api_key_env)

        model = resolve_model(request.model, self.default_model)
        schema = request.schema
        timeout = request.timeout_seconds or self.default_timeout_seconds
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}", **self.extra_headers}

        # Prefer strict json_schema enforcement when the schema is fully specified
        # (no optional fields); otherwise fall back to plain JSON-object mode.
        strict = strict_schema(schema) if schema else None

        def _payload(prompt: str, *, use_strict: bool) -> dict[str, Any]:
            payload: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": prompt}]}
            if schema:
                if use_strict and strict is not None:
                    payload["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": "result", "schema": strict, "strict": True},
                    }
                else:
                    payload["response_format"] = {"type": "json_object"}
            return payload

        async def do_call(prompt: str) -> _CallResult:
            use_strict = strict is not None
            res = await asyncio.to_thread(post_json, url, _payload(prompt, use_strict=use_strict), headers, timeout=timeout)
            # Endpoint may not support json_schema mode — retry once in object mode.
            if res.status != 200 and use_strict and _is_response_format_error(res.body):
                res = await asyncio.to_thread(post_json, url, _payload(prompt, use_strict=False), headers, timeout=timeout)
            if res.status != 200:
                return "", {}, None, _http_error_message(res.status, res.body)
            obj = json.loads(res.body)
            choices = obj.get("choices") or []
            text = (choices[0].get("message", {}).get("content") if choices else "") or ""
            usage = obj.get("usage") or {}
            u = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}
            cost = estimate_cost(model, u["input_tokens"], u["output_tokens"])
            return text.strip(), u, cost, None

        return await _complete_with_retry(request, request.prompt, schema, do_call, inject_schema_text=True)


@dataclass(slots=True)
class AnthropicAPIAdapter:
    """Adapter for the Anthropic Messages API (``/v1/messages``).

    Structured output uses forced tool-use (``emit_result``) so the model returns
    schema-shaped JSON natively rather than via a prompt instruction.
    """

    name: str = "anthropic"
    base_url: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    default_model: str = "claude-sonnet-4-6"
    api_version: str = "2023-06-01"
    max_tokens: int = 4096
    default_timeout_seconds: int = 600

    async def run(self, request) -> AgentResult:
        key = os.environ.get(self.api_key_env)
        if not key:
            return missing_key_result(request, provider=self.name, env_var=self.api_key_env)

        model = resolve_model(request.model, self.default_model)
        schema = request.schema
        timeout = request.timeout_seconds or self.default_timeout_seconds
        url = self.base_url.rstrip("/") + "/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": self.api_version}

        async def do_call(prompt: str) -> _CallResult:
            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if schema:
                payload["tools"] = [
                    {"name": "emit_result", "description": "Return the structured result.", "input_schema": schema}
                ]
                payload["tool_choice"] = {"type": "tool", "name": "emit_result"}
            res = await asyncio.to_thread(post_json, url, payload, headers, timeout=timeout)
            if res.status != 200:
                return "", {}, None, _http_error_message(res.status, res.body)
            obj = json.loads(res.body)
            content = obj.get("content") or []
            text = ""
            if schema:
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "emit_result":
                        text = json.dumps(block.get("input"))
                        break
            if not text:
                text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            usage = obj.get("usage") or {}
            u = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}
            cost = estimate_cost(model, u["input_tokens"], u["output_tokens"])
            return text.strip(), u, cost, None

        # Tool-use already constrains the shape; don't also append schema text.
        return await _complete_with_retry(request, request.prompt, schema, do_call, inject_schema_text=False)


@dataclass(slots=True)
class GeminiAPIAdapter:
    """Adapter for the Google Gemini ``generateContent`` API."""

    name: str = "gemini"
    base_url: str = "https://generativelanguage.googleapis.com"
    api_key_env: str = "GEMINI_API_KEY"
    default_model: str = "gemini-3.5-flash"
    default_timeout_seconds: int = 600

    async def run(self, request) -> AgentResult:
        key = os.environ.get(self.api_key_env) or os.environ.get("GOOGLE_API_KEY")
        if not key:
            return missing_key_result(request, provider=self.name, env_var=self.api_key_env)

        model = resolve_model(request.model, self.default_model)
        schema = request.schema
        timeout = request.timeout_seconds or self.default_timeout_seconds
        url = f"{self.base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key}

        async def do_call(prompt: str) -> _CallResult:
            payload: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
            if schema:
                payload["generationConfig"] = {"responseMimeType": "application/json"}
            res = await asyncio.to_thread(post_json, url, payload, headers, timeout=timeout)
            if res.status != 200:
                return "", {}, None, _http_error_message(res.status, res.body)
            obj = json.loads(res.body)
            candidates = obj.get("candidates") or []
            parts = []
            for cand in candidates:
                for part in (cand.get("content", {}).get("parts") or []):
                    if isinstance(part, dict) and "text" in part:
                        parts.append(part["text"])
            text = "".join(parts)
            um = obj.get("usageMetadata") or {}
            u = {"input_tokens": um.get("promptTokenCount"), "output_tokens": um.get("candidatesTokenCount")}
            cost = estimate_cost(model, u["input_tokens"], u["output_tokens"])
            return text.strip(), u, cost, None

        return await _complete_with_retry(request, request.prompt, schema, do_call, inject_schema_text=True)
