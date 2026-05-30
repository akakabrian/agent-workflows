"""Subprocess-backed adapters that drive local coding-agent CLIs.

These adapters shell out to the installed `claude` and `codex` binaries in their
non-interactive ("print"/"exec") modes. They require no API keys of their own —
they reuse whatever authentication the CLI already has — which makes them the
default "real" providers for this runtime.

The adapters depend only on the standard library (asyncio + subprocess) and the
local models; they must not import from runtime.py (to avoid an import cycle).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import AgentResult
from ._common import (
    AdapterError,
    estimate_cost,
)
from ._common import ok_text_result as _ok_text
from ._common import provider_failed_result as _provider_failed
from ._common import schema_instruction as _schema_instruction
from ._common import timed_out_result as _timed_out

__all__ = ["AdapterError", "ClaudeCLIAdapter", "CodexCLIAdapter"]


async def _run_subprocess(
    argv: list[str],
    *,
    stdin_text: str | None,
    cwd: str | None,
    timeout_seconds: int | None,
) -> tuple[int, str, str]:
    """Run a subprocess, feeding stdin, returning (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    payload = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=payload),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout_b.decode("utf-8", "replace"), stderr_b.decode("utf-8", "replace")


@dataclass(slots=True)
class ClaudeCLIAdapter:
    """Drive the `claude` CLI in print mode with JSON output.

    Contract (claude -p --output-format json) returns a single JSON object with
    `result` (text), `is_error`, `usage.{input_tokens,output_tokens,
    cache_read_input_tokens,cache_creation_input_tokens}`, and `total_cost_usd`.
    """

    name: str = "claude"
    binary: str = "claude"
    default_timeout_seconds: int = 600

    async def run(self, request) -> AgentResult:
        prompt = request.prompt
        if request.schema:
            prompt = prompt + _schema_instruction(request.schema)

        argv = [self.binary, "-p", "--output-format", "json"]
        model = _real_model(request.model)
        if model:
            argv += ["--model", model]

        timeout = request.timeout_seconds or self.default_timeout_seconds
        try:
            code, stdout, stderr = await _run_subprocess(
                argv, stdin_text=prompt, cwd=request.cwd, timeout_seconds=timeout
            )
        except FileNotFoundError as exc:
            return _missing_cli(request, provider="claude", binary=self.binary, exc=exc)
        except asyncio.TimeoutError:
            return _timed_out(request, timeout)

        if code != 0:
            return _provider_failed(request, (stderr or stdout or "claude exited nonzero").strip())

        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError:
            # Fall back to treating stdout as plain text.
            return _ok_text(request, stdout.strip(), usage={}, cost=None)

        if obj.get("is_error"):
            return _provider_failed(request, str(obj.get("result") or obj.get("error") or "claude reported an error"))

        usage = obj.get("usage") or {}
        return _ok_text(
            request,
            (obj.get("result") or "").strip(),
            usage={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
            },
            cost=obj.get("total_cost_usd"),
        )


@dataclass(slots=True)
class CodexCLIAdapter:
    """Drive the `codex exec` CLI with JSONL events and a last-message file.

    Uses `--json` for the event stream (parsed for token usage) and
    `-o/--output-last-message` for the final agent message text. Defaults to the
    `read-only` sandbox; a workspace-write sandbox is selected when the call
    declares a write scope or worktree isolation.
    """

    name: str = "codex"
    binary: str = "codex"
    default_timeout_seconds: int = 600

    async def run(self, request) -> AgentResult:
        prompt = request.prompt
        if request.schema:
            prompt = prompt + _schema_instruction(request.schema)

        sandbox = "workspace-write" if (request.write_scope or request.isolation == "worktree") else "read-only"
        with tempfile.TemporaryDirectory(prefix="owf-codex-") as tmp:
            last_path = Path(tmp) / "last.txt"
            argv = [self.binary, "exec", "--json", "--color", "never", "-s", sandbox, "-o", str(last_path)]
            model = _real_model(request.model)
            if model:
                argv += ["-m", model]
            if request.cwd:
                argv += ["-C", request.cwd]
            argv.append("-")  # read prompt from stdin

            timeout = request.timeout_seconds or self.default_timeout_seconds
            try:
                code, stdout, stderr = await _run_subprocess(
                    argv, stdin_text=prompt, cwd=request.cwd, timeout_seconds=timeout
                )
            except FileNotFoundError as exc:
                return _missing_cli(request, provider="codex", binary=self.binary, exc=exc)
            except asyncio.TimeoutError:
                return _timed_out(request, timeout)

            text = last_path.read_text(encoding="utf-8").strip() if last_path.exists() else ""

        usage, turn_error, message_text = _parse_codex_events(stdout)
        if turn_error:
            return _provider_failed(request, turn_error)
        if not text:
            text = message_text
        if code != 0 and not text:
            return _provider_failed(request, (stderr or stdout or "codex exited nonzero").strip())

        cost = estimate_cost(_real_model(request.model), usage.get("input_tokens"), usage.get("output_tokens"))
        return _ok_text(request, text, usage=usage, cost=cost)


def _parse_codex_events(stdout: str) -> tuple[dict[str, Any], str | None, str]:
    """Extract token usage, any turn failure, and the final agent message text
    from the codex JSONL event stream."""
    usage: dict[str, Any] = {}
    error: str | None = None
    message_text = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype in ("turn.failed", "error"):
            err = event.get("error")
            if isinstance(err, dict):
                error = err.get("message") or json.dumps(err)
            else:
                error = event.get("message") or "codex turn failed"
        if etype == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
                message_text = str(item["text"]).strip()
        # Token usage arrives on the turn.completed event.
        u = event.get("usage")
        if isinstance(u, dict):
            for src, dst in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cached_input_tokens", "cache_read_tokens"),
                ("cache_read_tokens", "cache_read_tokens"),
            ):
                if isinstance(u.get(src), int):
                    usage[dst] = u[src]
    return usage, error, message_text


def _real_model(model: str | None) -> str | None:
    """Only forward an explicit real model name to the CLI.

    Placeholder labels used as runtime defaults (fake/default/<provider name>)
    should not be passed as `--model`, so the CLI uses its own configured default.
    """
    if not model:
        return None
    if model.lower() in ("fake", "default", "claude", "codex"):
        return None
    return model


def _missing_cli(request, *, provider: str, binary: str, exc: FileNotFoundError) -> AgentResult:
    return _provider_failed(
        request,
        (
            f"Provider `{provider}` requires the `{binary}` CLI on PATH.\n\n"
            f"Try:\n  {binary} --help\n\n"
            "Or run offline first:\n  owf run examples/hello_workflow.py --provider fake"
            f"\n\nDetails: {exc}"
        ),
    )
