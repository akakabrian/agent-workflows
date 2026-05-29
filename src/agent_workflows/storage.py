from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import AgentResult, WorkflowArtifact, WorkflowBudget, WorkflowMeta, canonical_json

HOME_DIR = ".workflows"
DB_FILE = "workflow.sqlite"


SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
  id TEXT PRIMARY KEY,
  script_path TEXT NOT NULL,
  script_hash TEXT NOT NULL,
  workflow_name TEXT NOT NULL,
  workflow_description TEXT,
  args_json TEXT NOT NULL,
  status TEXT NOT NULL,
  parent_run_id TEXT,
  resumed_from_run_id TEXT,
  attempt_number INTEGER DEFAULT 1,
  budget_total_tokens INTEGER,
  budget_spent_tokens INTEGER DEFAULT 0,
  budget_total_cost_usd REAL,
  budget_spent_cost_usd REAL DEFAULT 0,
  provider TEXT,
  model TEXT,
  cache_policy TEXT,
  output_dir TEXT NOT NULL,
  manifest_path TEXT,
  summary_path TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  updated_at TEXT,
  error_kind TEXT,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS workflow_calls (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  call_index INTEGER NOT NULL,
  phase TEXT,
  label TEXT,
  prompt TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  opts_json TEXT NOT NULL,
  call_key TEXT NOT NULL,
  status TEXT NOT NULL,
  cache_status TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  agent_type TEXT,
  schema_json TEXT,
  schema_hash TEXT,
  validation_status TEXT,
  validation_errors_json TEXT,
  output_text TEXT,
  output_text_path TEXT,
  output_json_path TEXT,
  raw_output_path TEXT,
  transcript_path TEXT,
  artifacts_json TEXT,
  isolation TEXT,
  worktree_path TEXT,
  worktree_branch TEXT,
  changed_files_json TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  estimated_cost_usd REAL,
  duration_ms INTEGER,
  error TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  call_id TEXT,
  type TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  message TEXT,
  data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  call_id TEXT,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT,
  size_bytes INTEGER,
  mime_type TEXT,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_cache_entries (
  call_key TEXT PRIMARY KEY,
  call_id TEXT,
  status TEXT NOT NULL,
  output_text TEXT,
  output_json TEXT,
  artifacts_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _now_z() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def project_home(root: str | Path | None = None) -> Path:
    if root is None:
        return Path.cwd() / HOME_DIR
    return Path(root).expanduser()


def db_path(home: Path) -> Path:
    return home / DB_FILE


def initialize(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path(home)) as connection:
        connection.executescript(SCHEMA)
    return home


def connect(home: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path(home))
    connection.row_factory = sqlite3.Row
    return connection


def make_run_id() -> str:
    import secrets

    from datetime import datetime, timezone

    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)


def make_call_id(run_id: str, index: int) -> str:
    return f"{run_id}_call_{index:04d}"


def create_run(
    home: Path,
    *,
    script_path: str,
    script_hash: str,
    meta: WorkflowMeta,
    args: dict[str, Any],
    budget: WorkflowBudget,
    provider: str | None,
    model: str | None,
    cache_policy: str,
    parent_run_id: str | None = None,
    resumed_from_run_id: str | None = None,
    attempt_number: int = 1,
) -> dict[str, Any]:
    run_id = make_run_id()
    now = _now_z()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.md"
    manifest_path = run_dir / "manifest.json"
    record = {
        "id": run_id,
        "script_path": str(script_path),
        "script_hash": script_hash,
        "workflow_name": meta.name,
        "workflow_description": meta.description,
        "args_json": canonical_json(args),
        "status": "running",
        "parent_run_id": parent_run_id,
        "resumed_from_run_id": resumed_from_run_id,
        "attempt_number": attempt_number,
        "budget_total_tokens": budget.total_tokens,
        "budget_spent_tokens": budget.spent_tokens,
        "budget_total_cost_usd": budget.total_cost_usd,
        "budget_spent_cost_usd": budget.spent_cost_usd,
        "provider": provider,
        "model": model,
        "cache_policy": cache_policy,
        "output_dir": str(run_dir),
        "manifest_path": str(manifest_path),
        "summary_path": str(summary_path),
        "created_at": now,
        "started_at": now,
        "completed_at": None,
        "updated_at": now,
        "error_kind": None,
        "error_message": None,
    }
    with connect(home) as connection:
        connection.execute(
            """
            INSERT INTO workflow_runs (
              id, script_path, script_hash, workflow_name, workflow_description,
              args_json, status, parent_run_id, resumed_from_run_id, attempt_number,
              budget_total_tokens, budget_spent_tokens, budget_total_cost_usd,
              budget_spent_cost_usd, provider, model, cache_policy, output_dir,
              manifest_path, summary_path, created_at, started_at, completed_at,
              updated_at, error_kind, error_message
            ) VALUES (
              :id, :script_path, :script_hash, :workflow_name, :workflow_description,
              :args_json, :status, :parent_run_id, :resumed_from_run_id, :attempt_number,
              :budget_total_tokens, :budget_spent_tokens, :budget_total_cost_usd,
              :budget_spent_cost_usd, :provider, :model, :cache_policy, :output_dir,
              :manifest_path, :summary_path, :created_at, :started_at, :completed_at,
              :updated_at, :error_kind, :error_message
            )
            """,
            record,
        )
    return record


def update_run(home: Path, run_id: str, **updates: Any) -> None:
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return
    updates["updated_at"] = _now_z()
    assignments = ", ".join(f"{key} = :{key}" for key in updates)
    updates["id"] = run_id
    with connect(home) as connection:
        connection.execute(f"UPDATE workflow_runs SET {assignments} WHERE id = :id", updates)


def record_event(home: Path, run_id: str, *, call_id: str | None, event_type: str, message: str | None = None, data: dict[str, Any] | None = None) -> None:
    import secrets

    payload = data or {}
    event = {
        "id": "evt_" + secrets.token_hex(6),
        "run_id": run_id,
        "call_id": call_id,
        "type": event_type,
        "timestamp": _now_z(),
        "message": message,
        "data_json": canonical_json(payload),
    }
    with connect(home) as connection:
        connection.execute(
            """
            INSERT INTO workflow_events (id, run_id, call_id, type, timestamp, message, data_json)
            VALUES (:id, :run_id, :call_id, :type, :timestamp, :message, :data_json)
            """,
            event,
        )


def record_artifact(home: Path, run_id: str, *, call_id: str | None, artifact: WorkflowArtifact) -> None:
    import secrets

    path = Path(artifact.path)
    size = path.stat().st_size if path.exists() else None
    record = {
        "id": "art_" + secrets.token_hex(8),
        "run_id": run_id,
        "call_id": call_id,
        "kind": artifact.kind,
        "path": artifact.path,
        "sha256": artifact.sha256,
        "size_bytes": size,
        "mime_type": "application/json" if path.suffix == ".json" else "text/plain",
        "metadata_json": canonical_json(artifact.metadata),
        "created_at": _now_z(),
    }
    with connect(home) as connection:
        connection.execute(
            """
            INSERT INTO workflow_artifacts (
              id, run_id, call_id, kind, path, sha256, size_bytes, mime_type,
              metadata_json, created_at
            ) VALUES (
              :id, :run_id, :call_id, :kind, :path, :sha256, :size_bytes, :mime_type,
              :metadata_json, :created_at
            )
            """,
            record,
        )


def record_call(home: Path, run_id: str, call_index: int, result: AgentResult, request_json: dict[str, Any]) -> None:
    now = _now_z()
    with connect(home) as connection:
        connection.execute(
            """
            INSERT INTO workflow_calls (
              id, run_id, call_index, phase, label, prompt, prompt_hash, opts_json,
              call_key, status, cache_status, provider, model, agent_type, schema_json,
              schema_hash, validation_status, validation_errors_json, output_text,
              output_text_path, output_json_path, raw_output_path, transcript_path,
              artifacts_json, isolation, worktree_path, worktree_branch, changed_files_json,
              input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
              estimated_cost_usd, duration_ms, error, created_at, completed_at, updated_at
            ) VALUES (
              :id, :run_id, :call_index, :phase, :label, :prompt, :prompt_hash, :opts_json,
              :call_key, :status, :cache_status, :provider, :model, :agent_type, :schema_json,
              :schema_hash, :validation_status, :validation_errors_json, :output_text,
              :output_text_path, :output_json_path, :raw_output_path, :transcript_path,
              :artifacts_json, :isolation, :worktree_path, :worktree_branch, :changed_files_json,
              :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens,
              :estimated_cost_usd, :duration_ms, :error, :created_at, :completed_at, :updated_at
            )
            """,
            {
                "id": result.call_id,
                "run_id": run_id,
                "call_index": call_index,
                "phase": result.phase,
                "label": result.label,
                "prompt": request_json["prompt"],
                "prompt_hash": request_json["prompt_hash"],
                "opts_json": canonical_json(request_json),
                "call_key": request_json["call_key"],
                "status": result.status,
                "cache_status": result.cache_status,
                "provider": result.provider,
                "model": result.model,
                "agent_type": result.agent_type,
                "schema_json": canonical_json(request_json.get("schema")) if request_json.get("schema") is not None else None,
                "schema_hash": request_json.get("schema_hash"),
                "validation_status": result.validation_status,
                "validation_errors_json": canonical_json(result.validation_errors) if result.validation_errors else None,
                "output_text": result.text,
                "output_text_path": result.output_text_path,
                "output_json_path": result.output_json_path,
                "raw_output_path": result.raw_output_path,
                "transcript_path": result.transcript_path,
                "artifacts_json": canonical_json([asdict(a) for a in result.artifacts]),
                "isolation": result.isolation,
                "worktree_path": result.worktree_path,
                "worktree_branch": result.worktree_branch,
                "changed_files_json": canonical_json(result.changed_files),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cache_read_tokens": result.cache_read_tokens,
                "cache_write_tokens": result.cache_write_tokens,
                "estimated_cost_usd": result.estimated_cost_usd,
                "duration_ms": result.duration_ms,
                "error": result.error,
                "created_at": now,
                "completed_at": now,
                "updated_at": now,
            },
        )


def get_run(home: Path, run_id: str) -> dict[str, Any]:
    with connect(home) as connection:
        row = connection.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return dict(row)


def get_calls(home: Path, run_id: str) -> list[dict[str, Any]]:
    with connect(home) as connection:
        rows = connection.execute(
            "SELECT * FROM workflow_calls WHERE run_id = ? ORDER BY call_index ASC",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_runs(home: Path) -> list[dict[str, Any]]:
    with connect(home) as connection:
        rows = connection.execute("SELECT * FROM workflow_runs ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def latest_run_id(home: Path) -> str | None:
    rows = list_runs(home)
    return rows[0]["id"] if rows else None


def get_call(home: Path, call_id: str) -> dict[str, Any] | None:
    with connect(home) as connection:
        row = connection.execute("SELECT * FROM workflow_calls WHERE id = ?", (call_id,)).fetchone()
    return dict(row) if row is not None else None


def get_events(home: Path, run_id: str) -> list[dict[str, Any]]:
    with connect(home) as connection:
        rows = connection.execute(
            "SELECT * FROM workflow_events WHERE run_id = ? ORDER BY timestamp ASC, id ASC",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_artifacts(home: Path, run_id: str) -> list[dict[str, Any]]:
    with connect(home) as connection:
        rows = connection.execute(
            "SELECT * FROM workflow_artifacts WHERE run_id = ? ORDER BY created_at ASC, id ASC",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_cache_entry(home: Path, call_key: str) -> dict[str, Any] | None:
    with connect(home) as connection:
        row = connection.execute(
            "SELECT * FROM workflow_cache_entries WHERE call_key = ?",
            (call_key,),
        ).fetchone()
    return dict(row) if row is not None else None


def upsert_cache_entry(home: Path, entry: dict[str, Any]) -> None:
    now = _now_z()
    payload = {
        "call_key": entry["call_key"],
        "call_id": entry.get("call_id"),
        "status": entry.get("status", "done"),
        "output_text": entry.get("output_text"),
        "output_json": entry.get("output_json"),
        "artifacts_json": entry.get("artifacts_json"),
        "created_at": now,
        "updated_at": now,
    }
    with connect(home) as connection:
        connection.execute(
            """
            INSERT INTO workflow_cache_entries (
              call_key, call_id, status, output_text, output_json, artifacts_json,
              created_at, updated_at
            ) VALUES (
              :call_key, :call_id, :status, :output_text, :output_json, :artifacts_json,
              :created_at, :updated_at
            )
            ON CONFLICT(call_key) DO UPDATE SET
              call_id = excluded.call_id,
              status = excluded.status,
              output_text = excluded.output_text,
              output_json = excluded.output_json,
              artifacts_json = excluded.artifacts_json,
              updated_at = excluded.updated_at
            """,
            payload,
        )
