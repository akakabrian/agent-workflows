from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

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
from .storage import get_artifacts, get_call, initialize


TEMPLATE = """from workflows import agent, log, phase


async def main(args):
    phase("hello")
    result = await agent("Reply with a short greeting.", label="hello")
    log("completed", greeting=result.text)
    return {"greeting": result.text, "args": args}
"""


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Exception):
        return str(value)
    return str(value)


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=_json_default)


def _parse_args_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("--args-json must decode to an object")
    return data


def _parse_kv_args(items: list[str] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"argument must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        result[key] = value
    return result


def _load_args(ns: argparse.Namespace) -> dict[str, Any]:
    args = {}
    args.update(_parse_args_json(getattr(ns, "args_json", None)))
    args.update(_parse_kv_args(getattr(ns, "arg", None)))
    return args


def _resolve_home(ns: argparse.Namespace, default: str | Path | None = None) -> Path:
    if getattr(ns, "home", None):
        return Path(ns.home).expanduser()
    if default is not None:
        return Path(default).expanduser()
    return Path.cwd() / ".workflows"


def _resolve_run_id(home: Path, requested: str) -> str:
    if requested != "latest":
        return requested
    info = latest_run(home)
    if info is None:
        raise WorkflowError("no runs found")
    return info["run"]["id"]


def cmd_init(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    initialize(home)
    print(str(home))
    return 0


def cmd_validate(ns: argparse.Namespace) -> int:
    payload = validate_script(ns.script)
    if ns.json:
        print(_dump(payload))
    else:
        workflow = payload["workflow"]
        print(f"script: {payload['script_path']}")
        print(f"name: {workflow['name']}")
        if workflow.get("description"):
            print(f"description: {workflow['description']}")
    return 0


def cmd_dry_run(ns: argparse.Namespace) -> int:
    args = _load_args(ns)
    payload = draft_manifest(
        ns.script,
        args=args,
        provider=ns.provider,
        model=ns.model,
        budget_tokens=ns.budget_tokens,
        budget_cost_usd=ns.budget_cost_usd,
    )
    if ns.json:
        print(_dump(payload))
    else:
        print(_dump(payload))
    return 0


def cmd_run(ns: argparse.Namespace) -> int:
    args = _load_args(ns)
    payload = run_script(
        ns.script,
        args=args,
        home=_resolve_home(ns, default=Path(ns.script).expanduser().resolve().parent / ".workflows"),
        provider=ns.provider,
        model=ns.model,
        budget_tokens=ns.budget_tokens,
        budget_cost_usd=ns.budget_cost_usd,
        cache_policy=ns.cache_policy,
    )
    if ns.json:
        print(_dump(payload))
    else:
        print(f"run_id: {payload['run_id']}")
        print(f"status: {payload['status']}")
        print(f"run_dir: {payload['run_dir']}")
    return 0


def cmd_resume(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    run_id = _resolve_run_id(home, ns.run_id)
    payload = resume_run(run_id, home=home, provider=ns.provider, model=ns.model)
    if ns.json:
        print(_dump(payload))
    else:
        print(f"run_id: {payload['run_id']}")
        print(f"status: {payload['status']}")
        print(f"run_dir: {payload['run_dir']}")
    return 0


def cmd_status(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    run_id = _resolve_run_id(home, ns.run_id)
    payload = run_status(home, run_id)
    if ns.json:
        print(_dump(payload))
    else:
        run = payload["run"]
        print(f"run_id: {run['id']}")
        print(f"status: {run['status']}")
        print(f"script: {run['script_path']}")
        print(f"calls: {len(payload['calls'])}")
    return 0


def cmd_output(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    run_id = _resolve_run_id(home, ns.run_id)
    payload = run_status(home, run_id)
    run = payload["run"]
    output_path = Path(run["output_dir"]) / "output.json"
    if output_path.exists():
        text = output_path.read_text(encoding="utf-8").strip()
        if ns.json:
            print(text)
        else:
            print(text)
        return 0
    if ns.json:
        print(_dump({"run": run, "calls": payload["calls"]}))
    else:
        print(f"no output found for {run_id}")
    return 0


def cmd_calls(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    run_id = _resolve_run_id(home, ns.run_id)
    payload = run_status(home, run_id)["calls"]
    if ns.json:
        print(_dump(payload))
    else:
        for call in payload:
            print(f"{call['call_index']:04d} {call['status']} {call.get('label') or ''}".rstrip())
    return 0


def cmd_explain_cache(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    run_id = _resolve_run_id(home, ns.run_id)
    rows = explain_cache(home, run_id)
    if ns.json:
        print(_dump(rows))
    else:
        for row in rows:
            label = row.get("label") or row.get("call_id")
            print(f"{row['cache_status']:9} {label}: {row['reason']}")
    return 0


def cmd_report(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    run_id = _resolve_run_id(home, ns.run_id)
    data = build_report(home, run_id)
    if ns.html:
        content = render_report_html(data)
        suffix = "report.html"
    else:
        content = render_report_markdown(data)
        suffix = "report.md"
    if ns.out:
        out_path = Path(ns.out).expanduser()
    else:
        out_path = Path(data["run"]["output_dir"]) / suffix
    out_path.write_text(content, encoding="utf-8")
    if ns.stdout:
        print(content)
    else:
        print(str(out_path))
    return 0


def cmd_artifacts(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    run_id = _resolve_run_id(home, ns.run_id)
    rows = get_artifacts(home, run_id)
    if ns.json:
        print(_dump(rows))
    else:
        for row in rows:
            size = row.get("size_bytes")
            print(f"{row['kind']:12} {row.get('call_id') or '-':12} {size if size is not None else '?':>8}  {row['path']}")
    return 0


def cmd_cat(ns: argparse.Namespace) -> int:
    home = _resolve_home(ns)
    call = get_call(home, ns.call_id)
    if call is None:
        raise WorkflowError(f"call not found: {ns.call_id}")
    if ns.prompt:
        print(call.get("prompt") or "")
        return 0
    # default: output
    text = call.get("output_text")
    if text is None:
        path = call.get("output_json_path") or call.get("output_text_path")
        if path and Path(path).exists():
            text = Path(path).read_text(encoding="utf-8")
    print((text or "").rstrip())
    return 0


def cmd_new(ns: argparse.Namespace) -> int:
    path = Path(ns.path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not ns.force:
        raise WorkflowError(f"{path} already exists")
    path.write_text(TEMPLATE, encoding="utf-8")
    print(str(path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="owf", description="Open Agent Workflows")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="Workflow home directory")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize the local workflow store", parents=[common])
    init.set_defaults(func=cmd_init)

    validate = sub.add_parser("validate", help="Validate a workflow script", parents=[common])
    validate.add_argument("script")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_validate)

    dry_run = sub.add_parser("dry-run", help="Draft a run manifest without executing", parents=[common])
    dry_run.add_argument("script")
    dry_run.add_argument("--provider", default="fake")
    dry_run.add_argument("--model", default="fake")
    dry_run.add_argument("--budget-tokens", type=int)
    dry_run.add_argument("--budget-cost-usd", type=float)
    dry_run.add_argument("--args-json")
    dry_run.add_argument("--arg", action="append")
    dry_run.add_argument("--json", action="store_true")
    dry_run.set_defaults(func=cmd_dry_run)

    run = sub.add_parser("run", help="Run a workflow script", parents=[common])
    run.add_argument("script")
    run.add_argument("--provider", default="fake")
    run.add_argument("--model", default="fake")
    run.add_argument("--budget-tokens", type=int)
    run.add_argument("--budget-cost-usd", type=float)
    run.add_argument("--cache-policy", default="auto", choices=["auto", "disabled", "read_only", "refresh"])
    run.add_argument("--args-json")
    run.add_argument("--arg", action="append")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="Resume a prior run", parents=[common])
    resume.add_argument("run_id")
    resume.add_argument("--provider", default=None)
    resume.add_argument("--model", default=None)
    resume.add_argument("--json", action="store_true")
    resume.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="Show run status", parents=[common])
    status.add_argument("run_id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    output = sub.add_parser("output", help="Print run output", parents=[common])
    output.add_argument("run_id")
    output.add_argument("--json", action="store_true")
    output.set_defaults(func=cmd_output)

    calls = sub.add_parser("calls", help="List call records for a run", parents=[common])
    calls.add_argument("run_id")
    calls.add_argument("--json", action="store_true")
    calls.set_defaults(func=cmd_calls)

    new = sub.add_parser("new", help="Create a starter workflow script", parents=[common])
    new.add_argument("path")
    new.add_argument("--force", action="store_true")
    new.set_defaults(func=cmd_new)

    explain = sub.add_parser("explain-cache", help="Explain each call's cache decision", parents=[common])
    explain.add_argument("run_id")
    explain.add_argument("--json", action="store_true")
    explain.set_defaults(func=cmd_explain_cache)

    report = sub.add_parser("report", help="Write a Markdown or HTML run report", parents=[common])
    report.add_argument("run_id")
    report.add_argument("--html", action="store_true", help="Render HTML instead of Markdown")
    report.add_argument("--out", help="Output path (defaults to the run directory)")
    report.add_argument("--stdout", action="store_true", help="Also print the report to stdout")
    report.set_defaults(func=cmd_report)

    artifacts = sub.add_parser("artifacts", help="List stored artifacts for a run", parents=[common])
    artifacts.add_argument("run_id")
    artifacts.add_argument("--json", action="store_true")
    artifacts.set_defaults(func=cmd_artifacts)

    cat = sub.add_parser("cat", help="Print a call's prompt or output", parents=[common])
    cat.add_argument("call_id")
    cat.add_argument("--prompt", action="store_true", help="Print the prompt instead of the output")
    cat.set_defaults(func=cmd_cat)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    try:
        return ns.func(ns)
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface a clean message; details persist in the run artifacts
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
