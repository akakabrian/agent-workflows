from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import inspect
import json
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable, Iterable, TypeVar

from .adapters import build_adapter
from .adapters._config import provider_concurrency
from .models import (
    AgentResult,
    WorkflowArtifact,
    WorkflowBudget,
    WorkflowCallRequest,
    WorkflowMeta,
    canonical_json,
    hash_json,
    hash_text,
)
from .storage import (
    create_run,
    get_cache_entry,
    get_calls,
    get_run,
    initialize,
    latest_run_id,
    list_runs,
    make_call_id,
    record_artifact,
    record_call,
    record_event,
    update_run,
    upsert_cache_entry,
)
from .validation import SchemaValidationError, validate_schema

T = TypeVar("T")

RUNTIME_VERSION = "0.1.0"

_runtime_var: contextvars.ContextVar["WorkflowRuntime | None"] = contextvars.ContextVar(
    "owf_runtime", default=None
)
_load_meta_var: contextvars.ContextVar[WorkflowMeta | None] = contextvars.ContextVar(
    "owf_load_meta", default=None
)


class WorkflowError(RuntimeError):
    pass


class WorkflowRuntime:
    def __init__(
        self,
        *,
        home: Path,
        adapter: Any | None = None,
        provider: str = "fake",
        model: str = "fake",
        workflow_depth: int = 0,
    ) -> None:
        self.home = home
        # An explicit adapter overrides provider routing (used by tests/embedders).
        # Otherwise adapters are resolved per call from the call's provider.
        self.adapter_override = adapter
        self._adapter_cache: dict[str, Any] = {}
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {}
        self.provider = provider
        self.model = model
        self.workflow_depth = workflow_depth
        self.meta = WorkflowMeta(name="workflow")
        self.args: dict[str, Any] = {}
        self.script_path: Path | None = None
        self.script_hash: str | None = None
        self.run_record: dict[str, Any] | None = None
        self.run_id: str | None = None
        self.current_phase: str | None = None
        self.budget = WorkflowBudget()
        self.call_index = 0
        self.current_output_dir: Path | None = None
        self.current_manifest_path: Path | None = None

    def resolve_adapter(self, provider: str | None) -> Any:
        if self.adapter_override is not None:
            return self.adapter_override
        key = (provider or self.provider or "fake").lower()
        if key not in self._adapter_cache:
            self._adapter_cache[key] = build_adapter(key)
        return self._adapter_cache[key]

    def provider_semaphore(self, provider: str | None) -> asyncio.Semaphore:
        """Per-provider concurrency gate, so fan-out across many calls does not
        exceed a provider's rate limits. Lazily created on the running loop."""
        key = (provider or self.provider or "fake").lower()
        sem = self._provider_semaphores.get(key)
        if sem is None:
            sem = asyncio.Semaphore(provider_concurrency(key))
            self._provider_semaphores[key] = sem
        return sem

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "script_path": str(self.script_path) if self.script_path else None,
            "script_hash": self.script_hash,
            "workflow": asdict(self.meta),
            "args": self.args,
            "budget": {
                "total_tokens": self.budget.total_tokens,
                "total_cost_usd": self.budget.total_cost_usd,
                "spent_tokens": self.budget.spent_tokens,
                "spent_cost_usd": self.budget.spent_cost_usd,
            },
        }


def _runtime() -> WorkflowRuntime:
    runtime = _runtime_var.get()
    if runtime is None:
        raise WorkflowError("workflow APIs can only be used while a script is running")
    return runtime


def meta(
    *,
    name: str,
    description: str | None = None,
    phases: list[str] | None = None,
    mutates_files: bool = False,
    approval_required: bool = False,
) -> WorkflowMeta:
    value = WorkflowMeta(
        name=name,
        description=description,
        phases=list(phases or []),
        mutates_files=mutates_files,
        approval_required=approval_required,
    )
    _load_meta_var.set(value)
    runtime = _runtime_var.get()
    if runtime is not None:
        runtime.meta = value
    return value


class _BudgetProxy:
    @property
    def total_tokens(self) -> int | None:
        return _runtime().budget.total_tokens

    @property
    def total_cost_usd(self) -> float | None:
        return _runtime().budget.total_cost_usd

    @property
    def spent_tokens(self) -> int:
        return _runtime().budget.spent_tokens

    @property
    def spent_cost_usd(self) -> float:
        return _runtime().budget.spent_cost_usd

    @property
    def remaining_tokens(self) -> int | None:
        return _runtime().budget.remaining_tokens()

    @property
    def remaining_cost_usd(self) -> float | None:
        return _runtime().budget.remaining_cost_usd()

    def can_spend(self, estimated_tokens: int | None = None) -> bool:
        return _runtime().budget.can_spend(estimated_tokens)

    def reserve(self, tokens: int) -> None:
        _runtime().budget.reserve(tokens)


budget = _BudgetProxy()


def phase(name: str) -> None:
    runtime = _runtime()
    runtime.current_phase = name
    if runtime.run_id:
        record_event(runtime.home, runtime.run_id, call_id=None, event_type="phase", message=name, data={"phase": name})


def log(message: str, **metadata: Any) -> None:
    runtime = _runtime()
    if runtime.run_id:
        record_event(runtime.home, runtime.run_id, call_id=None, event_type="log", message=message, data=metadata)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Exception):
        return str(value)
    if hasattr(value, "__dict__") and not isinstance(value, (dict, list, tuple, str, int, float, bool, type(None))):
        try:
            return asdict(value)  # dataclasses
        except Exception:
            return str(value)
    return value


def _dump_json(value: Any) -> str:
    return json.dumps(_normalize_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return _dump_json(value)
    except Exception:
        return str(value)


def _call_dir(runtime: WorkflowRuntime, call_id: str) -> Path:
    if runtime.current_output_dir is None:
        raise WorkflowError("run output directory not initialized")
    call_dir = runtime.current_output_dir / "calls" / call_id
    call_dir.mkdir(parents=True, exist_ok=True)
    return call_dir


def _write_artifact(
    runtime: WorkflowRuntime,
    run_id: str,
    call_id: str,
    *,
    kind: str,
    filename: str,
    content: str,
) -> WorkflowArtifact:
    call_dir = _call_dir(runtime, call_id)
    path = call_dir / filename
    path.write_text(content, encoding="utf-8")
    artifact = WorkflowArtifact(kind=kind, path=str(path), sha256=hash_text(content), metadata={})
    record_artifact(runtime.home, run_id, call_id=call_id, artifact=artifact)
    return artifact


def _result_from_cache(row: dict[str, Any]) -> AgentResult:
    artifacts: list[WorkflowArtifact] = []
    artifacts_json = row.get("artifacts_json")
    if artifacts_json:
        try:
            for item in json.loads(artifacts_json):
                artifacts.append(WorkflowArtifact(**item))
        except Exception:
            artifacts = []
    value = None
    output_json = row.get("output_json")
    if output_json is not None:
        try:
            value = json.loads(output_json)
        except Exception:
            value = output_json
    return AgentResult(
        ok=row.get("status") == "done",
        status=row.get("status") or "done",
        value=value,
        text=row.get("output_text"),
        error=row.get("error"),
        call_id=row.get("call_id"),
        label=row.get("label"),
        phase=row.get("phase"),
        provider=row.get("provider"),
        model=row.get("model"),
        agent_type=row.get("agent_type"),
        cache_status=row.get("cache_status") or "hit",
        cache_key=row.get("call_key"),
        prompt_path=None,
        output_text_path=row.get("output_text_path"),
        output_json_path=row.get("output_json_path"),
        raw_output_path=row.get("raw_output_path"),
        transcript_path=row.get("transcript_path"),
        artifacts=artifacts,
        schema_hash=row.get("schema_hash"),
        validation_status=row.get("validation_status"),
        validation_errors=json.loads(row["validation_errors_json"]) if row.get("validation_errors_json") else [],
        validation_attempts=1,
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        cache_read_tokens=row.get("cache_read_tokens"),
        cache_write_tokens=row.get("cache_write_tokens"),
        estimated_cost_usd=row.get("estimated_cost_usd"),
        duration_ms=row.get("duration_ms"),
        isolation=row.get("isolation") or "none",
        worktree_path=row.get("worktree_path"),
        worktree_branch=row.get("worktree_branch"),
        changed_files=json.loads(row["changed_files_json"]) if row.get("changed_files_json") else [],
    )


def _validate_or_raise(value: Any, schema: dict[str, Any] | None) -> tuple[str | None, list[dict[str, Any]]]:
    if schema is None:
        return None, []
    try:
        validate_schema(value, schema)
    except SchemaValidationError as exc:
        return "schema_failed", [{"path": exc.path, "message": exc.message}]
    return "ok", []


@dataclass(slots=True)
class _Worktree:
    root: str
    path: str
    branch: str


def _git(args: list[str], *, cwd: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _create_worktree(runtime: WorkflowRuntime, call_id: str) -> _Worktree:
    """Create a fresh git worktree for a mutating call.

    Worktree isolation is fail-closed: if setup cannot complete, the provider
    call must not run in the user's current working tree.
    """
    base = str(runtime.script_path.parent) if runtime.script_path else str(Path.cwd())
    code, out, err = _git(["rev-parse", "--show-toplevel"], cwd=base)
    if code != 0:
        detail = (err or out or "not inside a git repository").strip()
        raise WorkflowError(f"worktree isolation failed: {detail}")
    root = out.strip()
    if runtime.current_output_dir is None:
        raise WorkflowError("worktree isolation failed: run output directory not initialized")
    wt_path = runtime.current_output_dir / "worktrees" / call_id
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"owf/{runtime.run_id or 'run'}/{call_id}"
    code, _, err = _git(["worktree", "add", "-b", branch, str(wt_path), "HEAD"], cwd=root)
    if code != 0:
        detail = (err or "git worktree add failed").strip()
        raise WorkflowError(f"worktree isolation failed: {detail}")
    return _Worktree(root=root, path=str(wt_path), branch=branch)


def _record_worktree_failure(
    runtime: WorkflowRuntime,
    *,
    call_id: str,
    request: WorkflowCallRequest,
    request_record: dict[str, Any],
    error: str,
    started: float,
) -> AgentResult:
    result = AgentResult(
        ok=False,
        status="worktree_failed",
        error=error,
        call_id=call_id,
        label=request.label,
        phase=request.phase,
        provider=request.provider,
        model=request.model,
        agent_type=request.agent_type,
        cache_key=request_record["call_key"],
        cache_status="bypassed",
        duration_ms=int((time.perf_counter() - started) * 1000),
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        isolation=request.isolation,
    )
    artifacts: list[WorkflowArtifact] = []
    if runtime.run_id:
        artifacts.append(
            _write_artifact(runtime, runtime.run_id, call_id, kind="prompt", filename="prompt.txt", content=request.prompt)
        )
        result.prompt_path = artifacts[-1].path
        artifacts.append(
            _write_artifact(runtime, runtime.run_id, call_id, kind="error", filename="error.txt", content=error)
        )
        result.artifacts = artifacts
        record_call(runtime.home, runtime.run_id, runtime.call_index, result, request_record)
    return result


def _finalize_worktree(worktree: _Worktree, result: AgentResult) -> None:
    """Record changed files; preserve the worktree if it changed, else remove it."""
    code, out, _ = _git(["status", "--porcelain"], cwd=worktree.path)
    changed = [line[3:] for line in out.splitlines() if line.strip()] if code == 0 else []
    result.changed_files = changed
    if changed:
        result.worktree_path = worktree.path
        result.worktree_branch = worktree.branch
    else:
        # No edits: tear the worktree and its branch back down to stay tidy.
        _git(["worktree", "remove", "--force", worktree.path], cwd=worktree.root)
        _git(["branch", "-D", worktree.branch], cwd=worktree.root)
        result.worktree_path = None
        result.worktree_branch = None


async def agent(
    prompt: str,
    *,
    label: str | None = None,
    phase: str | None = None,
    schema: dict[str, Any] | None = None,
    provider: str | None = None,
    model: str | None = None,
    agent_type: str | None = None,
    isolation: str = "none",
    cache_policy: str = "auto",
    read_scope: list[str] | None = None,
    write_scope: list[str] | None = None,
    permissions: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    metadata: dict[str, Any] | None = None,
    cache_namespace: str | None = None,
) -> AgentResult:
    runtime = _runtime()
    if runtime.budget.total_tokens is not None and runtime.budget.remaining_tokens() == 0:
        raise WorkflowError("budget exhausted")

    runtime.call_index += 1
    call_id = make_call_id(runtime.run_id or "pending_run", runtime.call_index)
    request = WorkflowCallRequest(
        prompt=prompt,
        label=label,
        phase=phase or runtime.current_phase,
        schema=schema,
        provider=provider or runtime.provider,
        model=model or runtime.model,
        agent_type=agent_type,
        isolation=isolation,
        cache_policy=cache_policy,
        read_scope=list(read_scope or []),
        write_scope=list(write_scope or []),
        permissions=dict(permissions or {}),
        timeout_seconds=timeout_seconds,
        metadata=dict(metadata or {}),
        cache_namespace=cache_namespace,
    )
    call_key = request.call_key(runtime.script_hash or "")
    now = time.perf_counter()

    # Mutating calls (write scope or worktree isolation) must never reuse a
    # prompt-only cache entry: the prior output does not prove the filesystem
    # side effects still hold. See PRD sections 6.5, 16, 18.3 and AC8.
    is_mutating = bool(request.write_scope) or request.isolation != "none"
    cache_readable = cache_policy in ("auto", "read_only") and not is_mutating
    cache_writable = cache_policy in ("auto", "read_only", "refresh") and not is_mutating

    cached_row = get_cache_entry(runtime.home, call_key) if cache_readable else None
    if cached_row is not None:
        result = _result_from_cache(cached_row)
        artifacts: list[WorkflowArtifact] = []
        result.call_id = call_id
        result.label = label
        result.phase = request.phase
        result.provider = request.provider
        result.model = request.model
        result.agent_type = request.agent_type
        result.cache_key = call_key
        result.cache_status = "hit"
        result.input_tokens = result.input_tokens or 0
        result.output_tokens = result.output_tokens or 0
        result.cache_read_tokens = result.cache_read_tokens or 0
        result.cache_write_tokens = result.cache_write_tokens or 0
        result.duration_ms = int((time.perf_counter() - now) * 1000)
        result.prompt_path = None
        result.output_text_path = None
        result.output_json_path = None
        result.raw_output_path = None
        result.transcript_path = None
        if runtime.run_id:
            artifacts.append(
                _write_artifact(runtime, runtime.run_id, call_id, kind="prompt", filename="prompt.txt", content=prompt)
            )
            result.prompt_path = artifacts[-1].path
        if result.text is not None and runtime.run_id:
            artifacts.append(
                _write_artifact(
                    runtime,
                    runtime.run_id,
                    call_id,
                    kind="output_text",
                    filename="output.txt",
                    content=result.text,
                )
            )
            result.output_text_path = artifacts[-1].path
        if result.value is not None and runtime.run_id:
            artifacts.append(
                _write_artifact(
                    runtime,
                    runtime.run_id,
                    call_id,
                    kind="output_json",
                    filename="output.json",
                    content=_dump_json(result.value),
                )
            )
            result.output_json_path = artifacts[-1].path
        if artifacts:
            result.artifacts = artifacts
        if runtime.run_id:
            record_call(
                runtime.home,
                runtime.run_id,
                runtime.call_index,
                result,
                {
                    "prompt": prompt,
                    "prompt_hash": hash_text(prompt),
                    "call_key": call_key,
                    "schema": schema,
                    "schema_hash": hash_json(schema) if schema else None,
                },
            )
        runtime.budget.consume((result.input_tokens or 0) + (result.output_tokens or 0), result.estimated_cost_usd or 0.0)
        if runtime.run_id:
            update_run(
                runtime.home,
                runtime.run_id,
                budget_spent_tokens=runtime.budget.spent_tokens,
                budget_spent_cost_usd=runtime.budget.spent_cost_usd,
            )
        return result

    request_record = {
        "prompt": prompt,
        "prompt_hash": hash_text(prompt),
        "call_key": call_key,
        "schema": schema,
        "schema_hash": hash_json(schema) if schema else None,
    }

    # Worktree isolation: run a mutating call inside a fresh git worktree so its
    # edits never touch the user's working tree and can be inspected afterward.
    worktree: _Worktree | None = None
    if request.isolation == "worktree":
        try:
            worktree = _create_worktree(runtime, call_id)
        except WorkflowError as exc:
            return _record_worktree_failure(
                runtime,
                call_id=call_id,
                request=request,
                request_record=request_record,
                error=str(exc),
                started=now,
            )
        request.cwd = worktree.path

    adapter = runtime.resolve_adapter(request.provider)

    started = time.perf_counter()
    async with runtime.provider_semaphore(request.provider):
        result = await adapter.run(request)
    artifacts: list[WorkflowArtifact] = []
    result.call_id = call_id
    result.label = label
    result.phase = request.phase
    result.provider = request.provider
    result.model = request.model
    result.agent_type = request.agent_type
    result.cache_key = call_key
    if cache_policy == "disabled":
        result.cache_status = "disabled"
    elif is_mutating:
        result.cache_status = "bypassed"
    else:
        result.cache_status = "miss"
    result.output_text_path = None
    result.output_json_path = None
    result.raw_output_path = None
    result.transcript_path = None
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.input_tokens = result.input_tokens or 0
    result.output_tokens = result.output_tokens or 0
    result.cache_read_tokens = result.cache_read_tokens or 0
    result.cache_write_tokens = result.cache_write_tokens or 0
    result.isolation = request.isolation
    if worktree is not None:
        _finalize_worktree(worktree, result)

    if runtime.run_id:
        artifacts.append(
            _write_artifact(runtime, runtime.run_id, call_id, kind="prompt", filename="prompt.txt", content=prompt)
        )
        result.prompt_path = artifacts[-1].path

    if result.ok:
        value = result.value
        if schema is not None:
            inferred = value
            if inferred is None and result.text:
                try:
                    inferred = json.loads(result.text)
                except Exception:
                    inferred = result.text
            validation_status, errors = _validate_or_raise(inferred, schema)
            result.validation_status = validation_status
            result.validation_errors = errors
            if validation_status != "ok":
                result.ok = False
                result.status = "schema_failed"
                result.error = errors[0]["message"] if errors else "schema validation failed"
            else:
                result.value = inferred
                value = inferred
        if value is not None:
            if isinstance(value, (dict, list)):
                if runtime.run_id:
                    artifacts.append(
                        _write_artifact(
                            runtime,
                            runtime.run_id,
                            call_id,
                            kind="output_json",
                            filename="output.json",
                            content=_dump_json(value),
                        )
                    )
                    result.output_json_path = artifacts[-1].path
            else:
                if runtime.run_id:
                    artifacts.append(
                        _write_artifact(
                            runtime,
                            runtime.run_id,
                            call_id,
                            kind="output_text",
                            filename="output.txt",
                            content=_safe_text(value),
                        )
                    )
                    result.output_text_path = artifacts[-1].path
        elif result.text is not None and runtime.run_id:
            artifacts.append(
                _write_artifact(
                    runtime,
                    runtime.run_id,
                    call_id,
                    kind="output_text",
                    filename="output.txt",
                    content=result.text,
                )
            )
            result.output_text_path = artifacts[-1].path
        if runtime.run_id:
            result.artifacts = artifacts
            if cache_writable:
                cache_payload = {
                    "call_key": call_key,
                    "call_id": call_id,
                    "status": result.status,
                    "output_text": result.text,
                    "output_json": _dump_json(result.value) if result.value is not None else None,
                    "artifacts_json": canonical_json([asdict(a) for a in result.artifacts]),
                }
                upsert_cache_entry(runtime.home, cache_payload)
    else:
        if runtime.run_id and result.error:
            artifacts.append(
                _write_artifact(
                    runtime,
                    runtime.run_id,
                    call_id,
                    kind="error",
                    filename="error.txt",
                    content=result.error,
                )
            )
        if runtime.run_id:
            result.artifacts = artifacts

    if runtime.run_id:
        record_call(runtime.home, runtime.run_id, runtime.call_index, result, request_record)
    runtime.budget.consume((result.input_tokens or 0) + (result.output_tokens or 0), result.estimated_cost_usd or 0.0)
    if runtime.run_id:
        update_run(
            runtime.home,
            runtime.run_id,
            budget_spent_tokens=runtime.budget.spent_tokens,
            budget_spent_cost_usd=runtime.budget.spent_cost_usd,
        )
    return result


async def parallel(
    thunks: list[Callable[[], Awaitable[AgentResult]]],
    *,
    concurrency: int | None = None,
    fail_fast: bool = False,
) -> list[AgentResult]:
    cap = concurrency or len(thunks) or 1
    if not fail_fast:
        semaphore = asyncio.Semaphore(cap)

        async def run_one(index: int, thunk: Callable[[], Awaitable[AgentResult]]):
            async with semaphore:
                try:
                    return index, await thunk()
                except Exception as exc:
                    return index, AgentResult(ok=False, status="failed", error=str(exc))

        results = await asyncio.gather(*[run_one(i, thunk) for i, thunk in enumerate(thunks)])
        return [result for _, result in sorted(results, key=lambda item: item[0])]

    results: list[AgentResult | None] = [None] * len(thunks)
    next_index = 0
    pending: dict[asyncio.Task[tuple[int, AgentResult]], int] = {}

    async def run_one(index: int, thunk: Callable[[], Awaitable[AgentResult]]) -> tuple[int, AgentResult]:
        try:
            return index, await thunk()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return index, AgentResult(ok=False, status="failed", error=str(exc))

    def schedule() -> None:
        nonlocal next_index
        while next_index < len(thunks) and len(pending) < cap:
            task = asyncio.create_task(run_one(next_index, thunks[next_index]))
            pending[task] = next_index
            next_index += 1

    schedule()
    while pending:
        done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        failed = False
        for task in done:
            index = pending.pop(task)
            try:
                _, result = task.result()
            except asyncio.CancelledError:
                result = AgentResult(ok=False, status="cancelled", error="cancelled after parallel failure")
            results[index] = result
            if not result.ok:
                failed = True
        if failed:
            for task, index in list(pending.items()):
                task.cancel()
                results[index] = AgentResult(ok=False, status="cancelled", error="cancelled after parallel failure")
            if pending:
                await asyncio.gather(*pending.keys(), return_exceptions=True)
                pending.clear()
            for index in range(next_index, len(thunks)):
                results[index] = AgentResult(ok=False, status="cancelled", error="not scheduled after parallel failure")
            break
        schedule()
    return [result or AgentResult(ok=False, status="cancelled", error="not scheduled") for result in results]


async def pipeline(
    items: Iterable[T],
    fn: Callable[[T], Awaitable[AgentResult]],
    *,
    stop_on_error: bool = False,
) -> list[AgentResult]:
    results: list[AgentResult] = []
    for item in items:
        result = await fn(item)
        results.append(result)
        if stop_on_error and not result.ok:
            break
    return results


async def workflow(name_or_ref: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    parent = _runtime()
    if parent.workflow_depth >= 1:
        raise WorkflowError("workflow() nesting is limited to one child level")
    path = Path(name_or_ref)
    if not path.is_absolute() and not path.exists() and parent.script_path is not None:
        # Resolve child references relative to the parent script first, so a
        # workflow can compose siblings without depending on the caller's cwd.
        candidate = parent.script_path.parent / name_or_ref
        if candidate.exists():
            path = candidate
    if not path.exists():
        raise WorkflowError(f"workflow path not found: {name_or_ref}")
    # Run one nested level inside the parent's event loop, sharing the parent
    # home and budget so child spend accrues to the parent run.
    return await _arun_script(
        path,
        args=args or {},
        home=parent.home,
        provider=parent.provider,
        model=parent.model,
        cache_policy="auto",
        budget=parent.budget,
        parent_run_id=parent.run_id,
        workflow_depth=parent.workflow_depth + 1,
    )


def _load_module(script_path: Path) -> tuple[ModuleType, str, str, WorkflowMeta]:
    source = script_path.read_text(encoding="utf-8")
    script_hash = hash_text(source)
    module_name = f"agent_workflow_{script_path.stem}_{script_hash[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise WorkflowError(f"unable to load {script_path}")
    module = importlib.util.module_from_spec(spec)
    path_entry = str(script_path.parent)
    added_path = False
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)
        added_path = True
    token = _load_meta_var.set(None)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        meta_value = _load_meta_var.get()
    finally:
        _load_meta_var.reset(token)
        if added_path and sys.path and sys.path[0] == path_entry:
            sys.path.pop(0)
    return module, script_hash, source, meta_value or WorkflowMeta(name=script_path.stem)


def validate_script(script_path: str | Path) -> dict[str, Any]:
    path = Path(script_path)
    module, script_hash, _, meta_value = _load_module(path)
    main = getattr(module, "main", None)
    if main is None or not callable(main):
        raise WorkflowError("workflow scripts must define async main(args)")
    return {
        "script_path": str(path),
        "script_hash": script_hash,
        "workflow": asdict(meta_value),
    }


def draft_manifest(
    script_path: str | Path,
    *,
    args: dict[str, Any] | None = None,
    provider: str = "fake",
    model: str = "fake",
    budget_tokens: int | None = None,
    budget_cost_usd: float | None = None,
) -> dict[str, Any]:
    path = Path(script_path)
    _, script_hash, _, meta_value = _load_module(path)
    return {
        "runtime_version": RUNTIME_VERSION,
        "script_path": str(path),
        "script_hash": script_hash,
        "workflow": asdict(meta_value),
        "provider": provider,
        "model": model,
        "args": args or {},
        "budget": {
            "total_tokens": budget_tokens,
            "total_cost_usd": budget_cost_usd,
        },
    }


def load_script(script_path: str | Path):
    return _load_module(Path(script_path))


def _finalize_run(
    *,
    runtime: WorkflowRuntime,
    path: Path,
    result: Any,
    summary_path: Path,
) -> dict[str, Any]:
    output_path = runtime.current_output_dir / "output.json" if runtime.current_output_dir else None
    if output_path is not None:
        output_path.write_text(_dump_json(result) + "\n", encoding="utf-8")
    summary_path.write_text(
        "# Run Summary\n\n"
        f"- Status: done\n"
        f"- Script: `{path}`\n"
        f"- Run: `{runtime.run_id}`\n"
        f"- Provider: `{runtime.provider}`\n"
        f"- Model: `{runtime.model}`\n"
        f"- Budget spent: {runtime.budget.spent_tokens}\n",
        encoding="utf-8",
    )
    update_run(
        runtime.home,
        runtime.run_id,
        status="done",
        completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        budget_spent_tokens=runtime.budget.spent_tokens,
        budget_spent_cost_usd=runtime.budget.spent_cost_usd,
    )
    return {
        "run_id": runtime.run_id,
        "status": "done",
        "output": result,
        "run_dir": str(runtime.current_output_dir),
    }


def _prepare_run(
    script_path: str | Path,
    *,
    args: dict[str, Any] | None,
    home: str | Path | None,
    provider: str,
    model: str,
    budget_tokens: int | None,
    budget_cost_usd: float | None,
    cache_policy: str,
    budget: WorkflowBudget | None = None,
    parent_run_id: str | None = None,
    resumed_from_run_id: str | None = None,
    workflow_depth: int = 0,
) -> tuple[WorkflowRuntime, Callable[..., Any], Path, Any, Path]:
    path = Path(script_path).expanduser().resolve()
    home_path = initialize(Path(home) if home else path.parent / ".workflows")
    module, script_hash, _, meta_value = _load_module(path)
    main = getattr(module, "main", None)
    if main is None or not callable(main):
        raise WorkflowError("workflow scripts must define async main(args)")

    runtime = WorkflowRuntime(home=home_path, provider=provider, model=model, workflow_depth=workflow_depth)
    runtime.meta = meta_value
    runtime.args = dict(args or {})
    runtime.script_path = path
    runtime.script_hash = script_hash
    runtime.budget = budget if budget is not None else WorkflowBudget(
        total_tokens=budget_tokens, total_cost_usd=budget_cost_usd
    )
    token = _runtime_var.set(runtime)
    run_record = create_run(
        home_path,
        script_path=str(path),
        script_hash=script_hash,
        meta=meta_value,
        args=runtime.args,
        budget=runtime.budget,
        provider=provider,
        model=model,
        cache_policy=cache_policy,
        parent_run_id=parent_run_id,
        resumed_from_run_id=resumed_from_run_id,
    )
    runtime.run_record = run_record
    runtime.run_id = run_record["id"]
    runtime.current_output_dir = Path(run_record["output_dir"])
    runtime.current_manifest_path = Path(run_record["manifest_path"])
    summary_path = Path(run_record["summary_path"])
    runtime.current_manifest_path.write_text(
        _dump_json(
            draft_manifest(
                path,
                args=runtime.args,
                provider=provider,
                model=model,
                budget_tokens=runtime.budget.total_tokens,
                budget_cost_usd=runtime.budget.total_cost_usd,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    record_event(runtime.home, runtime.run_id, call_id=None, event_type="run_started", message=path.name, data=runtime.snapshot())
    return runtime, main, summary_path, token, path


async def _arun_script(
    script_path: str | Path,
    *,
    args: dict[str, Any] | None = None,
    home: str | Path | None = None,
    provider: str = "fake",
    model: str = "fake",
    budget_tokens: int | None = None,
    budget_cost_usd: float | None = None,
    cache_policy: str = "auto",
    budget: WorkflowBudget | None = None,
    parent_run_id: str | None = None,
    resumed_from_run_id: str | None = None,
    workflow_depth: int = 0,
) -> dict[str, Any]:
    runtime, main, summary_path, token, path = _prepare_run(
        script_path,
        args=args,
        home=home,
        provider=provider,
        model=model,
        budget_tokens=budget_tokens,
        budget_cost_usd=budget_cost_usd,
        cache_policy=cache_policy,
        budget=budget,
        parent_run_id=parent_run_id,
        resumed_from_run_id=resumed_from_run_id,
        workflow_depth=workflow_depth,
    )
    try:
        result = main(runtime.args)
        if inspect.isawaitable(result):
            result = await result
        return _finalize_run(runtime=runtime, path=path, result=result, summary_path=summary_path)
    except Exception as exc:
        if runtime.current_output_dir is not None:
            traceback_path = runtime.current_output_dir / "traceback.txt"
            traceback_path.write_text(traceback.format_exc(), encoding="utf-8")
        if runtime.run_id:
            update_run(
                runtime.home,
                runtime.run_id,
                status="failed",
                completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                error_kind=exc.__class__.__name__,
                error_message=str(exc),
                budget_spent_tokens=runtime.budget.spent_tokens,
                budget_spent_cost_usd=runtime.budget.spent_cost_usd,
            )
        summary_path.write_text(
            "# Run Summary\n\n"
            f"- Status: failed\n"
            f"- Script: `{path}`\n"
            f"- Run: `{runtime.run_id}`\n"
            f"- Error: `{exc}`\n",
            encoding="utf-8",
        )
        raise
    finally:
        _runtime_var.reset(token)


def run_script(
    script_path: str | Path,
    *,
    args: dict[str, Any] | None = None,
    home: str | Path | None = None,
    provider: str = "fake",
    model: str = "fake",
    budget_tokens: int | None = None,
    budget_cost_usd: float | None = None,
    cache_policy: str = "auto",
) -> dict[str, Any]:
    return asyncio.run(
        _arun_script(
            script_path,
            args=args,
            home=home,
            provider=provider,
            model=model,
            budget_tokens=budget_tokens,
            budget_cost_usd=budget_cost_usd,
            cache_policy=cache_policy,
        )
    )


def resume_run(
    run_id: str,
    *,
    home: str | Path | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    home_path = initialize(Path(home) if home else Path.cwd() / ".workflows")
    previous = get_run(home_path, run_id)
    script_path = previous["script_path"]
    args = json.loads(previous["args_json"])
    return run_script(
        script_path,
        args=args,
        home=home_path,
        provider=provider or previous.get("provider") or "fake",
        model=model or previous.get("model") or "fake",
        budget_tokens=previous.get("budget_total_tokens"),
        budget_cost_usd=previous.get("budget_total_cost_usd"),
        cache_policy=previous.get("cache_policy") or "auto",
    )


def run_status(home: str | Path, run_id: str) -> dict[str, Any]:
    home_path = Path(home)
    return {"run": get_run(home_path, run_id), "calls": get_calls(home_path, run_id)}


def latest_run(home: str | Path) -> dict[str, Any] | None:
    home_path = Path(home)
    run_id = latest_run_id(home_path)
    if run_id is None:
        return None
    return run_status(home_path, run_id)


_CACHE_REASONS = {
    "hit": "reused a prior read-only result (prompt, options, schema, provider, and model matched)",
    "miss": "no prior cached result existed for this call key",
    "bypassed": "mutating call (write scope or worktree isolation); prompt-only cache is unsafe",
    "disabled": "caching was disabled for this run",
}


def explain_cache(home: str | Path, run_id: str) -> list[dict[str, Any]]:
    """Return a per-call, human-readable explanation of each cache decision."""
    home_path = Path(home)
    rows = get_calls(home_path, run_id)
    explained = []
    for row in rows:
        status = row.get("cache_status") or "miss"
        explained.append(
            {
                "call_index": row.get("call_index"),
                "call_id": row.get("id"),
                "label": row.get("label"),
                "phase": row.get("phase"),
                "cache_status": status,
                "reason": _CACHE_REASONS.get(status, f"cache status: {status}"),
            }
        )
    return explained


def build_report(home: str | Path, run_id: str) -> dict[str, Any]:
    """Assemble the data used to render a run report."""
    home_path = Path(home)
    run = get_run(home_path, run_id)
    calls = get_calls(home_path, run_id)
    cache_counts: dict[str, int] = {}
    for call in calls:
        key = call.get("cache_status") or "miss"
        cache_counts[key] = cache_counts.get(key, 0) + 1
    failures = [c for c in calls if (c.get("status") or "") not in ("done", "cached")]
    return {"run": run, "calls": calls, "cache_counts": cache_counts, "failures": failures}


def usage_rollup(home: str | Path) -> dict[str, Any]:
    """Aggregate token/cost usage across all runs, grouped by provider and model.

    Returns ``{"totals": {...}, "by_provider": [...], "by_model": [...],
    "runs": N}``. Cost sums the per-call ``estimated_cost_usd`` recorded at run
    time, so it reflects whatever the price table knew then."""
    home_path = Path(home)
    runs = list_runs(home_path)
    by_provider: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def bucket(store: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
        return store.setdefault(key, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})

    for run in runs:
        for call in get_calls(home_path, run["id"]):
            provider = call.get("provider") or "unknown"
            model = call.get("model") or "unknown"
            in_tok = call.get("input_tokens") or 0
            out_tok = call.get("output_tokens") or 0
            cost = call.get("estimated_cost_usd") or 0.0
            for store, key in ((by_provider, provider), (by_model, f"{provider}/{model}")):
                row = bucket(store, key)
                row["calls"] += 1
                row["input_tokens"] += in_tok
                row["output_tokens"] += out_tok
                row["cost_usd"] = round(row["cost_usd"] + cost, 6)
            totals["calls"] += 1
            totals["input_tokens"] += in_tok
            totals["output_tokens"] += out_tok
            totals["cost_usd"] = round(totals["cost_usd"] + cost, 6)

    def rows(store: dict[str, dict[str, Any]], label: str) -> list[dict[str, Any]]:
        return [{label: name, **vals} for name, vals in sorted(store.items(), key=lambda kv: -kv[1]["cost_usd"])]

    return {
        "runs": len(runs),
        "totals": totals,
        "by_provider": rows(by_provider, "provider"),
        "by_model": rows(by_model, "model"),
    }


def render_usage_markdown(data: dict[str, Any]) -> str:
    t = data["totals"]
    lines = [
        "# Usage across runs",
        "",
        f"- Runs: {data['runs']}",
        f"- Calls: {t['calls']}",
        f"- Tokens: {t['input_tokens']} in / {t['output_tokens']} out",
        f"- Estimated cost: ${t['cost_usd']:.4f}",
        "",
        "## By provider",
        "",
        "| provider | calls | in | out | cost |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in data["by_provider"]:
        lines.append(f"| {row['provider']} | {row['calls']} | {row['input_tokens']} | {row['output_tokens']} | ${row['cost_usd']:.4f} |")
    lines += ["", "## By model", "", "| model | calls | in | out | cost |", "| --- | --- | --- | --- | --- |"]
    for row in data["by_model"]:
        lines.append(f"| {row['model']} | {row['calls']} | {row['input_tokens']} | {row['output_tokens']} | ${row['cost_usd']:.4f} |")
    return "\n".join(lines) + "\n"


def render_report_markdown(data: dict[str, Any]) -> str:
    run = data["run"]
    calls = data["calls"]
    lines = [
        f"# Workflow run {run['id']}",
        "",
        f"- Workflow: `{run.get('workflow_name')}`",
        f"- Status: `{run.get('status')}`",
        f"- Script: `{run.get('script_path')}`",
        f"- Provider/model: `{run.get('provider')}` / `{run.get('model')}`",
        f"- Budget: {run.get('budget_spent_tokens') or 0} / {run.get('budget_total_tokens') if run.get('budget_total_tokens') is not None else '∞'} tokens",
        "- Cache: " + ", ".join(f"{v} {k}" for k, v in data["cache_counts"].items()) if data["cache_counts"] else "- Cache: (no calls)",
        "",
        "## Calls",
        "",
        "| # | label | phase | status | cache | provider/model | tokens |",
        "| - | ----- | ----- | ------ | ----- | -------------- | ------ |",
    ]
    for c in calls:
        tokens = (c.get("input_tokens") or 0) + (c.get("output_tokens") or 0)
        lines.append(
            f"| {c.get('call_index')} | {c.get('label') or ''} | {c.get('phase') or ''} | "
            f"{c.get('status')} | {c.get('cache_status')} | {c.get('provider') or ''}/{c.get('model') or ''} | {tokens} |"
        )
    if data["failures"]:
        lines += ["", "## Failures", ""]
        for c in data["failures"]:
            lines.append(f"- `{c.get('label') or c.get('id')}` — {c.get('status')}: {c.get('error') or ''}")
    lines += ["", f"Resume with: `owf resume {run['id']}`", ""]
    return "\n".join(lines)


def render_report_html(data: dict[str, Any]) -> str:
    run = data["run"]
    rows = []
    for c in data["calls"]:
        tokens = (c.get("input_tokens") or 0) + (c.get("output_tokens") or 0)
        ok = (c.get("status") or "") in ("done", "cached")
        cls = "ok" if ok else "fail"
        rows.append(
            f"<tr class='{cls}'><td>{c.get('call_index')}</td><td>{_esc(c.get('label'))}</td>"
            f"<td>{_esc(c.get('phase'))}</td><td>{_esc(c.get('status'))}</td>"
            f"<td>{_esc(c.get('cache_status'))}</td><td>{_esc(c.get('provider'))}/{_esc(c.get('model'))}</td>"
            f"<td>{tokens}</td></tr>"
        )
    cache_summary = ", ".join(f"{v} {k}" for k, v in data["cache_counts"].items()) or "no calls"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Workflow run {_esc(run['id'])}</title><style>"
        "body{font-family:system-ui,sans-serif;margin:2rem;color:#1b1b1b}"
        "h1{font-size:1.3rem}table{border-collapse:collapse;width:100%;margin-top:1rem}"
        "th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:.9rem}"
        "th{background:#f4f4f4}tr.fail{background:#fff0f0}tr.ok td:nth-child(4){color:#137333}"
        ".meta{color:#555;font-size:.9rem;line-height:1.7}</style></head><body>"
        f"<h1>Workflow run {_esc(run['id'])}</h1>"
        f"<div class='meta'>Workflow: <b>{_esc(run.get('workflow_name'))}</b><br>"
        f"Status: <b>{_esc(run.get('status'))}</b><br>"
        f"Script: <code>{_esc(run.get('script_path'))}</code><br>"
        f"Provider/model: {_esc(run.get('provider'))}/{_esc(run.get('model'))}<br>"
        f"Cache: {_esc(cache_summary)}</div>"
        "<table><thead><tr><th>#</th><th>label</th><th>phase</th><th>status</th>"
        "<th>cache</th><th>provider/model</th><th>tokens</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
