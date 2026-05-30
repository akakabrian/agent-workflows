"""A zero-dependency Model Context Protocol (MCP) server for Open Agent Workflows.

The server speaks JSON-RPC 2.0 over the MCP stdio transport (newline-delimited
JSON on stdin/stdout) and exposes the core ``owf`` capabilities as MCP tools so
an MCP-capable agent (Claude Code, Codex, etc.) can author, run, and inspect
workflows without shelling out to the CLI.

Like the rest of the package it uses only the Python standard library. Run it
with ``owf mcp`` and register that command as an MCP stdio server.

Tools exposed:

- ``owf_run_workflow``  — execute a workflow script
- ``owf_status``        — run summary (status, script, call count)
- ``owf_output``        — the return value of ``main()`` (output.json)
- ``owf_report``        — render a Markdown or HTML run report
- ``owf_list_runs``     — list recorded runs, newest first
- ``owf_read_artifact`` — read one artifact file from a run directory
- ``owf_new_workflow``  — scaffold a starter or example workflow script
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from .runtime import (
    WorkflowError,
    build_report,
    draft_manifest,
    explain_cache,
    latest_run,
    render_report_html,
    render_report_markdown,
    resume_run,
    run_script,
    run_status,
    validate_script,
)
from .storage import get_artifacts, initialize, list_runs

# Default cap on a single owf_read_artifact read, so a huge artifact can't blow
# up the response. Callers can request a larger window explicitly via max_bytes.
DEFAULT_MAX_ARTIFACT_BYTES = 200_000

# Protocol version we implement; we echo the client's requested version when it
# sends one, falling back to this default for older/unspecified clients.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "open-agent-workflows"
SERVER_VERSION = "0.1.0"

# Starter template shared with `owf new`.
STARTER_TEMPLATE = """from workflows import agent, log, phase


async def main(args):
    phase("hello")
    result = await agent("Reply with a short greeting.", label="hello")
    log("completed", greeting=result.text)
    return {"greeting": result.text, "args": args}
"""


# ---------------------------------------------------------------------------
# JSON-RPC error helper
# ---------------------------------------------------------------------------

class _RpcError(Exception):
    """A JSON-RPC error with a code and optional data."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# Standard JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Exception):
        return str(value)
    return str(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=_json_default)


def _repo_root() -> Path:
    # src/agent_workflows/mcp_server.py -> repo root is parents[2].
    return Path(__file__).resolve().parents[2]


def _resolve_home(arguments: dict[str, Any], *, default: Path | None = None) -> Path:
    home = arguments.get("home")
    if home:
        resolved = Path(home).expanduser()
    elif default is not None:
        resolved = default
    else:
        resolved = Path.cwd() / ".workflows"
    # Ensure the SQLite store exists so reads on an empty home return cleanly
    # (e.g. latest_run -> None) instead of raising on a missing table.
    initialize(resolved)
    return resolved


def _resolve_run_id(home: Path, requested: str | None) -> str:
    run_id = requested or "latest"
    if run_id != "latest":
        return run_id
    info = latest_run(home)
    if info is None:
        raise _RpcError(INVALID_PARAMS, "no runs found")
    return info["run"]["id"]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_run_workflow(arguments: dict[str, Any]) -> dict[str, Any]:
    script = arguments.get("path")
    if not script:
        raise _RpcError(INVALID_PARAMS, "'path' is required")
    script_path = Path(script).expanduser()
    default_home = script_path.resolve().parent / ".workflows"
    args = arguments.get("args") or {}
    if not isinstance(args, dict):
        raise _RpcError(INVALID_PARAMS, "'args' must be an object")
    payload = run_script(
        script_path,
        args=args,
        home=_resolve_home(arguments, default=default_home),
        provider=arguments.get("provider", "fake"),
        model=arguments.get("model", "fake"),
        budget_tokens=arguments.get("budget_tokens"),
        budget_cost_usd=arguments.get("budget_cost_usd"),
        cache_policy=arguments.get("cache_policy", "auto"),
    )
    return payload


def _tool_status(arguments: dict[str, Any]) -> dict[str, Any]:
    home = _resolve_home(arguments)
    run_id = _resolve_run_id(home, arguments.get("run_id"))
    return run_status(home, run_id)


def _tool_output(arguments: dict[str, Any]) -> dict[str, Any]:
    home = _resolve_home(arguments)
    run_id = _resolve_run_id(home, arguments.get("run_id"))
    payload = run_status(home, run_id)
    run = payload["run"]
    output_path = Path(run["output_dir"]) / "output.json"
    if output_path.exists():
        text = output_path.read_text(encoding="utf-8").strip()
        try:
            return {"run_id": run_id, "output": json.loads(text)}
        except json.JSONDecodeError:
            return {"run_id": run_id, "output_text": text}
    return {"run_id": run_id, "output": None, "run": run, "calls": payload["calls"]}


def _tool_report(arguments: dict[str, Any]) -> str:
    home = _resolve_home(arguments)
    run_id = _resolve_run_id(home, arguments.get("run_id"))
    data = build_report(home, run_id)
    fmt = (arguments.get("format") or "markdown").lower()
    if fmt in ("html", "report.html"):
        return render_report_html(data)
    if fmt in ("markdown", "md"):
        return render_report_markdown(data)
    raise _RpcError(INVALID_PARAMS, f"unknown format: {fmt!r} (use 'markdown' or 'html')")


def _tool_list_runs(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    home = _resolve_home(arguments)
    rows = list_runs(home)
    limit = arguments.get("limit")
    if isinstance(limit, int) and limit > 0:
        rows = rows[:limit]
    return rows


def _tool_validate_workflow(arguments: dict[str, Any]) -> dict[str, Any]:
    script = arguments.get("path")
    if not script:
        raise _RpcError(INVALID_PARAMS, "'path' is required")
    return validate_script(Path(script).expanduser())


def _tool_dry_run(arguments: dict[str, Any]) -> dict[str, Any]:
    script = arguments.get("path")
    if not script:
        raise _RpcError(INVALID_PARAMS, "'path' is required")
    args = arguments.get("args") or {}
    if not isinstance(args, dict):
        raise _RpcError(INVALID_PARAMS, "'args' must be an object")
    return draft_manifest(
        Path(script).expanduser(),
        args=args,
        provider=arguments.get("provider", "fake"),
        model=arguments.get("model", "fake"),
        budget_tokens=arguments.get("budget_tokens"),
        budget_cost_usd=arguments.get("budget_cost_usd"),
    )


def _tool_resume(arguments: dict[str, Any]) -> dict[str, Any]:
    home = _resolve_home(arguments)
    run_id = _resolve_run_id(home, arguments.get("run_id"))
    return resume_run(run_id, home=home, provider=arguments.get("provider"), model=arguments.get("model"))


def _tool_calls(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    home = _resolve_home(arguments)
    run_id = _resolve_run_id(home, arguments.get("run_id"))
    return run_status(home, run_id)["calls"]


def _tool_explain_cache(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    home = _resolve_home(arguments)
    run_id = _resolve_run_id(home, arguments.get("run_id"))
    return explain_cache(home, run_id)


def _tool_artifacts(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    home = _resolve_home(arguments)
    run_id = _resolve_run_id(home, arguments.get("run_id"))
    return get_artifacts(home, run_id)


def _tool_read_artifact(arguments: dict[str, Any]) -> dict[str, Any]:
    rel = arguments.get("path")
    if not rel:
        raise _RpcError(INVALID_PARAMS, "'path' is required")
    home = _resolve_home(arguments)
    run_id = _resolve_run_id(home, arguments.get("run_id"))
    run = run_status(home, run_id)["run"]
    base = Path(run["output_dir"]).resolve()
    target = (base / rel).resolve()
    # Path-traversal guard: the resolved target must stay under the run dir.
    if base != target and base not in target.parents:
        raise _RpcError(INVALID_PARAMS, f"path escapes the run directory: {rel!r}")
    if not target.exists() or not target.is_file():
        raise _RpcError(INVALID_PARAMS, f"artifact not found: {rel!r}")

    size = target.stat().st_size
    offset = max(int(arguments.get("offset") or 0), 0)
    max_bytes = arguments.get("max_bytes")
    max_bytes = int(max_bytes) if isinstance(max_bytes, int) and max_bytes > 0 else DEFAULT_MAX_ARTIFACT_BYTES
    with target.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(max_bytes)
    return {
        "run_id": run_id,
        "path": str(target.relative_to(base)),
        "size_bytes": size,
        "offset": offset,
        "returned_bytes": len(chunk),
        "truncated": offset + len(chunk) < size,
        "content": chunk.decode("utf-8", "replace"),
    }


def _tool_new_workflow(arguments: dict[str, Any]) -> dict[str, Any]:
    output_path = arguments.get("output_path")
    if not output_path:
        raise _RpcError(INVALID_PARAMS, "'output_path' is required")

    # Workspace guard: by default writes must stay under workspace_root (or cwd).
    # Set allow_absolute=true to write anywhere the process can reach.
    allow_absolute = bool(arguments.get("allow_absolute"))
    root = Path(arguments.get("workspace_root") or Path.cwd()).expanduser().resolve()
    raw = Path(output_path).expanduser()
    if allow_absolute:
        path = (raw if raw.is_absolute() else root / raw).resolve()
    else:
        path = (raw.resolve() if raw.is_absolute() else (root / raw).resolve())
        if path != root and root not in path.parents:
            raise _RpcError(
                INVALID_PARAMS,
                f"output_path escapes workspace_root ({root}); pass allow_absolute=true to override",
            )

    if path.exists() and not arguments.get("force"):
        raise _RpcError(INVALID_PARAMS, f"{path} already exists (pass force=true to overwrite)")

    template_name = arguments.get("template_name") or "hello"
    content = _template_content(template_name)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "template": template_name, "bytes": len(content)}


def _available_templates() -> dict[str, Path]:
    """Map template name -> source file for bundled examples."""
    templates: dict[str, Path] = {}
    examples_dir = _repo_root() / "examples"
    if examples_dir.exists():
        for example in sorted(examples_dir.glob("*.py")):
            templates[example.stem] = example
            # Also accept the short stem without a trailing "_workflow".
            short = example.stem.removesuffix("_workflow")
            templates.setdefault(short, example)
    return templates


def _template_content(template_name: str) -> str:
    if template_name in ("hello", "starter", "default"):
        return STARTER_TEMPLATE
    templates = _available_templates()
    source = templates.get(template_name)
    if source is None:
        names = ", ".join(["hello", *sorted(templates)])
        raise _RpcError(INVALID_PARAMS, f"unknown template {template_name!r}; available: {names}")
    return source.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tool registry: name -> (handler, returns_text, definition)
# ---------------------------------------------------------------------------

_HOME_PROP = {
    "home": {
        "type": "string",
        "description": "Workflow home directory (defaults to ./.workflows, or <script_dir>/.workflows for run).",
    }
}
_RUN_ID_PROP = {
    "run_id": {
        "type": "string",
        "description": "Run id, or 'latest' for the most recent run.",
        "default": "latest",
    }
}


TOOLS: dict[str, dict[str, Any]] = {
    "owf_run_workflow": {
        "handler": _tool_run_workflow,
        "returns_text": False,
        "description": "Execute a workflow script and return its run id, status, and run directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the workflow .py script."},
                "args": {"type": "object", "description": "Arguments dict passed to main(args)."},
                "provider": {
                    "type": "string",
                    "description": "Adapter: fake, claude, codex (and aliases). Defaults to fake (offline).",
                    "default": "fake",
                },
                "model": {"type": "string", "description": "Model passed to the provider CLI."},
                "budget_tokens": {"type": "integer", "description": "Optional total token budget."},
                "budget_cost_usd": {"type": "number", "description": "Optional total cost budget in USD."},
                "cache_policy": {
                    "type": "string",
                    "enum": ["auto", "disabled", "read_only", "refresh"],
                    "default": "auto",
                },
                **_HOME_PROP,
            },
            "required": ["path"],
        },
    },
    "owf_status": {
        "handler": _tool_status,
        "returns_text": False,
        "description": "Return a run's summary plus its call records.",
        "inputSchema": {
            "type": "object",
            "properties": {**_RUN_ID_PROP, **_HOME_PROP},
        },
    },
    "owf_output": {
        "handler": _tool_output,
        "returns_text": False,
        "description": "Return the value returned by a run's main() (output.json).",
        "inputSchema": {
            "type": "object",
            "properties": {**_RUN_ID_PROP, **_HOME_PROP},
        },
    },
    "owf_report": {
        "handler": _tool_report,
        "returns_text": True,
        "description": "Render a Markdown or HTML report for a run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_RUN_ID_PROP,
                "format": {"type": "string", "enum": ["markdown", "html"], "default": "markdown"},
                **_HOME_PROP,
            },
        },
    },
    "owf_list_runs": {
        "handler": _tool_list_runs,
        "returns_text": False,
        "description": "List recorded runs, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of runs to return."},
                **_HOME_PROP,
            },
        },
    },
    "owf_read_artifact": {
        "handler": _tool_read_artifact,
        "returns_text": False,
        "description": "Read one artifact file from a run directory (path is relative to the run's output dir). Reads are bounded; use offset/max_bytes to page large files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_RUN_ID_PROP,
                "path": {
                    "type": "string",
                    "description": "Artifact path relative to the run's output directory, e.g. 'calls/<id>/output.json'.",
                },
                "offset": {"type": "integer", "description": "Byte offset to start reading from (default 0).", "default": 0},
                "max_bytes": {"type": "integer", "description": f"Max bytes to return (default {DEFAULT_MAX_ARTIFACT_BYTES}). Response includes returned_bytes and truncated."},
                **_HOME_PROP,
            },
            "required": ["path"],
        },
    },
    "owf_validate_workflow": {
        "handler": _tool_validate_workflow,
        "returns_text": False,
        "description": "Parse a workflow script and return its declared meta (name/description/phases) without running it.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to the workflow .py script."}},
            "required": ["path"],
        },
    },
    "owf_dry_run": {
        "handler": _tool_dry_run,
        "returns_text": False,
        "description": "Draft a run manifest (provider/model/budget plan) without executing the workflow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the workflow .py script."},
                "args": {"type": "object", "description": "Arguments dict passed to main(args)."},
                "provider": {"type": "string", "default": "fake"},
                "model": {"type": "string"},
                "budget_tokens": {"type": "integer"},
                "budget_cost_usd": {"type": "number"},
            },
            "required": ["path"],
        },
    },
    "owf_resume": {
        "handler": _tool_resume,
        "returns_text": False,
        "description": "Resume a prior run, replaying cached read-only calls and re-executing the rest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_RUN_ID_PROP,
                "provider": {"type": "string", "description": "Override the original run's provider."},
                "model": {"type": "string", "description": "Override the original run's model."},
                **_HOME_PROP,
            },
        },
    },
    "owf_calls": {
        "handler": _tool_calls,
        "returns_text": False,
        "description": "List the call records for a run (index, label, phase, status, cache, tokens).",
        "inputSchema": {"type": "object", "properties": {**_RUN_ID_PROP, **_HOME_PROP}},
    },
    "owf_explain_cache": {
        "handler": _tool_explain_cache,
        "returns_text": False,
        "description": "Explain each call's cache decision (hit/miss/bypassed/disabled) with a reason.",
        "inputSchema": {"type": "object", "properties": {**_RUN_ID_PROP, **_HOME_PROP}},
    },
    "owf_artifacts": {
        "handler": _tool_artifacts,
        "returns_text": False,
        "description": "List stored artifacts for a run (kind, call id, size, path).",
        "inputSchema": {"type": "object", "properties": {**_RUN_ID_PROP, **_HOME_PROP}},
    },
    "owf_new_workflow": {
        "handler": _tool_new_workflow,
        "returns_text": False,
        "description": "Scaffold a new workflow script from a starter or bundled example template. Writes are confined to workspace_root (default cwd) unless allow_absolute=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_path": {"type": "string", "description": "Where to write the new script (relative to workspace_root by default)."},
                "template_name": {
                    "type": "string",
                    "description": "Template to use: 'hello' (default) or a bundled example name (e.g. 'schema_validation').",
                    "default": "hello",
                },
                "workspace_root": {"type": "string", "description": "Root that writes must stay under (default: current working directory)."},
                "allow_absolute": {"type": "boolean", "description": "Allow writing outside workspace_root.", "default": False},
                "force": {"type": "boolean", "description": "Overwrite an existing file.", "default": False},
            },
            "required": ["output_path"],
        },
    },
}


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        }
        for name, spec in TOOLS.items()
    ]


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if spec is None:
        raise _RpcError(METHOD_NOT_FOUND, f"unknown tool: {name}")
    handler: Callable[[dict[str, Any]], Any] = spec["handler"]
    try:
        result = handler(arguments or {})
    except _RpcError:
        raise
    except WorkflowError as exc:
        # Workflow-level failures are reported as tool errors, not protocol errors.
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except (KeyError, FileNotFoundError) as exc:
        return {"content": [{"type": "text", "text": f"not found: {exc}"}], "isError": True}
    text = result if spec["returns_text"] and isinstance(result, str) else _dumps(result)
    return {"content": [{"type": "text", "text": text}], "isError": False}


# ---------------------------------------------------------------------------
# JSON-RPC method dispatch
# ---------------------------------------------------------------------------

def _handle_method(method: str, params: dict[str, Any]) -> Any:
    if method == "initialize":
        protocol = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tool_definitions()}
    if method == "tools/call":
        name = params.get("name")
        if not name:
            raise _RpcError(INVALID_PARAMS, "'name' is required")
        return _call_tool(name, params.get("arguments") or {})
    raise _RpcError(METHOD_NOT_FOUND, f"unknown method: {method}")


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one decoded JSON-RPC message.

    Returns the response dict, or None for notifications (messages with no id),
    which per JSON-RPC must not be answered.
    """
    msg_id = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")
    params = message.get("params") or {}

    if not isinstance(method, str):
        if is_notification:
            return None
        return _error_response(msg_id, INVALID_REQUEST, "missing or invalid 'method'")

    try:
        result = _handle_method(method, params)
    except _RpcError as exc:
        if is_notification:
            return None
        return _error_response(msg_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # noqa: BLE001 - convert unexpected errors to JSON-RPC
        if is_notification:
            return None
        return _error_response(msg_id, INTERNAL_ERROR, f"{exc.__class__.__name__}: {exc}")

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


# ---------------------------------------------------------------------------
# stdio transport loop
# ---------------------------------------------------------------------------

def serve(
    *,
    stdin: Any = None,
    stdout: Any = None,
    home: str | Path | None = None,
) -> int:
    """Run the MCP stdio server loop until stdin closes.

    Reads newline-delimited JSON-RPC messages from ``stdin`` and writes
    responses to ``stdout``. ``home`` is initialized eagerly so the SQLite store
    exists before the first tool call.
    """
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout

    if home is not None:
        initialize(Path(home).expanduser())

    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write(out_stream, _error_response(None, PARSE_ERROR, "invalid JSON"))
            continue
        if not isinstance(message, dict):
            _write(out_stream, _error_response(None, INVALID_REQUEST, "message must be an object"))
            continue
        response = handle_message(message)
        if response is not None:
            _write(out_stream, response)
    return 0


def _write(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, default=_json_default) + "\n")
    stream.flush()
