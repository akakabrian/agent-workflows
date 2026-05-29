from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import AgentResult, WorkflowArtifact
from ..validation import validate_schema, SchemaValidationError


def _fixture_schema_value(schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        result: dict[str, Any] = {}
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child in properties.items():
                result[key] = _fixture_schema_value(child) if isinstance(child, dict) else None
        for key in schema.get("required", []):
            result.setdefault(key, None)
        return result
    if schema_type == "array":
        return []
    if schema_type == "string":
        return ""
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return False
    if schema_type == "null":
        return None
    return {}


@dataclass(slots=True)
class FakeAdapter:
    name: str = "fake"
    delay_seconds: float = 0.0

    async def run(self, request) -> AgentResult:
        if request.schema:
            value = _fixture_schema_value(request.schema)
            try:
                validate_schema(value, request.schema)
                validation_status = "ok"
                errors: list[dict[str, Any]] = []
            except SchemaValidationError as exc:
                validation_status = "schema_failed"
                errors = [{"path": exc.path, "message": exc.message}]
                return AgentResult(
                    ok=False,
                    status="schema_failed",
                    value=value,
                    text=None,
                    error=exc.message,
                    provider=request.provider,
                    model=request.model,
                    agent_type=request.agent_type,
                    validation_status=validation_status,
                    validation_errors=errors,
                )
            text = None
        else:
            value = None
            text = request.prompt
            validation_status = None
            errors = []

        return AgentResult(
            ok=True,
            status="done",
            value=value if request.schema else None,
            text=text,
            provider=request.provider,
            model=request.model,
            agent_type=request.agent_type,
            validation_status=validation_status,
            validation_errors=errors,
            artifacts=[],
        )

