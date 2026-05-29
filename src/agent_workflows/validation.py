from __future__ import annotations

from typing import Any


class SchemaValidationError(RuntimeError):
    def __init__(self, message: str, path: str = "$"):
        super().__init__(message)
        self.message = message
        self.path = path


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    schema_type = schema.get("type")
    if schema_type and not _type_matches(value, schema_type):
        raise SchemaValidationError(f"Expected {schema_type}, got {type(value).__name__}", path)

    if schema_type == "object" or "properties" in schema:
        if not isinstance(value, dict):
            raise SchemaValidationError("Expected object", path)
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise SchemaValidationError(f"Missing required field {key!r}", f"{path}.{key}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, dict):
                    validate_schema(value[key], child_schema, f"{path}.{key}")

    if schema_type == "array":
        if not isinstance(value, list):
            raise SchemaValidationError("Expected array", path)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_schema(item, item_schema, f"{path}[{index}]")

    enum_values = schema.get("enum")
    if enum_values is not None and value not in enum_values:
        raise SchemaValidationError(f"Expected one of {enum_values!r}", path)

