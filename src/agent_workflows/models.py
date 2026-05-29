from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


RUNTIME_VERSION = "0.1.0"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def hash_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def hash_json(value: Any) -> str:
    return hash_text(canonical_json(value))


@dataclass(slots=True)
class WorkflowMeta:
    name: str
    description: str | None = None
    phases: list[str] = field(default_factory=list)
    mutates_files: bool = False
    approval_required: bool = False


@dataclass(slots=True)
class WorkflowBudget:
    total_tokens: int | None = None
    total_cost_usd: float | None = None
    spent_tokens: int = 0
    spent_cost_usd: float = 0.0

    def remaining_tokens(self) -> int | None:
        if self.total_tokens is None:
            return None
        return max(self.total_tokens - self.spent_tokens, 0)

    def remaining_cost_usd(self) -> float | None:
        if self.total_cost_usd is None:
            return None
        return max(self.total_cost_usd - self.spent_cost_usd, 0.0)

    def can_spend(self, estimated_tokens: int | None = None) -> bool:
        remaining = self.remaining_tokens()
        if remaining is not None and remaining <= 0:
            return False
        if remaining is not None and estimated_tokens is not None and estimated_tokens > remaining:
            return False
        return True

    def reserve(self, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        if self.total_tokens is not None and tokens > self.remaining_tokens():
            raise ValueError("budget exhausted")

    def consume(self, tokens: int = 0, cost_usd: float = 0.0) -> None:
        self.spent_tokens += max(tokens, 0)
        self.spent_cost_usd += max(cost_usd, 0.0)


@dataclass(slots=True)
class WorkflowArtifact:
    kind: str
    path: str
    sha256: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentResult:
    ok: bool
    status: str
    value: Any | None = None
    text: str | None = None
    error: str | None = None
    call_id: str | None = None
    label: str | None = None
    phase: str | None = None
    provider: str | None = None
    model: str | None = None
    agent_type: str | None = None
    cache_status: str = "miss"
    cache_key: str | None = None
    prompt_path: str | None = None
    output_text_path: str | None = None
    output_json_path: str | None = None
    raw_output_path: str | None = None
    transcript_path: str | None = None
    artifacts: list[WorkflowArtifact] = field(default_factory=list)
    schema_hash: str | None = None
    validation_status: str | None = None
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    validation_attempts: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    estimated_cost_usd: float | None = None
    duration_ms: int | None = None
    isolation: str = "none"
    worktree_path: str | None = None
    worktree_branch: str | None = None
    changed_files: list[str] = field(default_factory=list)

    def require_ok(self) -> "AgentResult":
        if not self.ok:
            raise RuntimeError(self.error or self.status)
        return self

    def value_or_raise(self) -> Any:
        return self.require_ok().value

    def text_or_raise(self) -> str:
        result = self.require_ok()
        return result.text if result.text is not None else str(result.value)


@dataclass(slots=True)
class WorkflowCallRequest:
    prompt: str
    label: str | None = None
    phase: str | None = None
    schema: dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    agent_type: str | None = None
    isolation: str = "none"
    cwd: str | None = None
    cache_policy: str = "auto"
    read_scope: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    permissions: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    cache_namespace: str | None = None

    def call_key(self, script_hash: str) -> str:
        payload = {
            "runtime_version": RUNTIME_VERSION,
            "script_hash": script_hash,
            "prompt": self.prompt,
            "label": self.label,
            "phase": self.phase,
            "schema_hash": hash_json(self.schema) if self.schema else None,
            "provider": self.provider,
            "model": self.model,
            "agent_type": self.agent_type,
            "isolation": self.isolation,
            "cache_policy": self.cache_policy,
            "read_scope": self.read_scope,
            "write_scope": self.write_scope,
            "permissions": self.permissions,
            "metadata": self.metadata,
            "cache_namespace": self.cache_namespace,
        }
        return hash_json(payload)

