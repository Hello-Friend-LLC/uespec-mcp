from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator

from ..bridge import BridgeError, bridge_error_fields, get_bridge, structured_error
from ._register import add_tool


_SCHEMA: dict[str, Any] | None = None
_VALIDATOR: Draft202012Validator | None = None


def validate_spec(spec_content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(spec_content)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "stage": "json",
            "normalizedJson": None,
            "errors": [
                structured_error(
                    "INVALID_JSON",
                    exc.msg,
                    path=f"$[{exc.lineno}:{exc.colno}]",
                    hint="Provide valid JSON text before attempting UE semantic validation.",
                )
            ],
            "semanticValidation": {"status": "skipped", "reason": "JSON parsing failed."},
        }

    validator = _get_validator()
    warnings = collect_unsupported_key_warnings(parsed, _load_schema())
    schema_errors = sorted(validator.iter_errors(parsed), key=lambda error: _jsonschema_path(error))
    if schema_errors:
        return {
            "ok": False,
            "stage": "schema",
            "normalizedJson": None,
            "validationLevel": "schema-only",
            "errors": [_schema_error_to_structured_error(error) for error in schema_errors],
            "warnings": warnings,
            "semanticValidation": {"status": "skipped", "reason": "Schema validation failed."},
        }

    normalized_json = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        semantic = get_bridge().validate_spec_content(normalized_json)
    except BridgeError as exc:
        # Never ok:true with the semantic stage skipped: the caller would read "valid" for a spec the compiler never saw.
        return {
            **bridge_error_fields(exc),
            "ok": False,
            "code": "BRIDGE_UNAVAILABLE",
            "stage": "schema",
            "validationLevel": "schema-only",
            "normalizedJson": normalized_json,
            "errors": [
                structured_error(
                    "BRIDGE_UNAVAILABLE",
                    f"Semantic validation could not run ({exc.code}): {exc}",
                    hint="Start the project editor (live mode) or configure the commandlet bridge, then validate again. The spec passed JSON-schema validation only.",
                )
            ],
            "warnings": warnings,
            "semanticValidation": {"status": "skipped", "reason": str(exc), "bridgeCode": exc.code},
        }

    semantic["stage"] = "semantic"
    semantic["warnings"] = warnings
    semantic.setdefault("validationLevel", "semantic")
    semantic.setdefault("semanticValidation", {"status": "completed"})
    if semantic.get("normalizedJson") is None:
        semantic["normalizedJson"] = normalized_json
    return semantic


def validate_text(spec_content: str) -> dict[str, Any]:
    return validate_spec(spec_content)


def _load_schema() -> dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        schema_text = files("uespec_mcp").joinpath("schemas/uispec.schema.json").read_text(encoding="utf-8")
        _SCHEMA = json.loads(schema_text)
    return _SCHEMA


def _get_validator() -> Draft202012Validator:
    global _VALIDATOR
    if _VALIDATOR is None:
        _VALIDATOR = Draft202012Validator(_load_schema())
    return _VALIDATOR


def _jsonschema_path(error: Any) -> str:
    parts = ["$"]
    for segment in error.absolute_path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            parts.append(f".{segment}")
    return "".join(parts)


def _schema_error_to_structured_error(error: Any) -> dict[str, Any]:
    hint = ""
    if error.validator == "required":
        missing_key = list(error.validator_value) if isinstance(error.validator_value, list) else []
        if missing_key:
            hint = f"Add the missing required key(s): {', '.join(missing_key)}."
        else:
            hint = "Add the missing required key."
    elif error.validator == "enum":
        hint = f"Allowed values: {', '.join(str(item) for item in error.validator_value)}."
    elif error.validator == "oneOf":
        hint = "Match one supported shape for this field."
    elif error.validator == "type":
        hint = f"Expected type: {error.validator_value}."

    bind_error = _find_bind_pattern_error(error)
    if bind_error is not None:
        # Same code and path shape the UE compiler uses for a bind it cannot generate (UESpecBindingBuilder).
        return structured_error(
            "UNSUPPORTED_BINDING_PATH",
            f"Bind path '{bind_error.instance}' uses an index or call expression ([ ] ( )), which bindings do not support.",
            path=_jsonschema_path(bind_error),
            hint="Bind a flat ViewModel field (for example expose the indexed value as its own field on the ViewModel).",
        )

    return structured_error(
        "SCHEMA_VALIDATION_FAILED",
        error.message,
        path=_jsonschema_path(error),
        hint=hint,
    )


def _find_bind_pattern_error(error: Any) -> Any | None:
    """The bind-path pattern failure, whether reported directly or buried in a oneOf branch (widget content/visibility)."""
    if error.validator == "pattern" and _jsonschema_path(error).endswith(".bind"):
        return error
    for sub_error in error.context or []:
        found = _find_bind_pattern_error(sub_error)
        if found is not None:
            return found
    return None


def collect_unsupported_key_warnings(instance: Any, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Warn for every key the schema marks ``"x-status": "unsupported"`` (accepted by the schema, consumed by nothing)."""
    warnings: list[dict[str, Any]] = []
    _walk_unsupported(instance, schema, schema, "$", warnings)
    return warnings


def _resolve_ref(subschema: Any, root: dict[str, Any]) -> Any:
    while isinstance(subschema, dict) and isinstance(subschema.get("$ref"), str):
        node: Any = root
        for part in subschema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        subschema = node
    return subschema


def _instance_matches_type(instance: Any, subschema: dict[str, Any]) -> bool:
    expected = subschema.get("type")
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    return expected is None


def _walk_unsupported(instance: Any, subschema: Any, root: dict[str, Any], path: str, warnings: list[dict[str, Any]]) -> None:
    subschema = _resolve_ref(subschema, root)
    if not isinstance(subschema, dict):
        return
    for branch in subschema.get("oneOf", []):
        resolved = _resolve_ref(branch, root)
        if isinstance(resolved, dict) and resolved.get("type") is not None and _instance_matches_type(instance, resolved):
            _walk_unsupported(instance, resolved, root, path, warnings)

    if isinstance(instance, dict):
        properties = subschema.get("properties", {})
        for key, value in instance.items():
            child_schema = _resolve_ref(properties.get(key), root)
            if not isinstance(child_schema, dict):
                continue
            child_path = f"{path}.{key}"
            if child_schema.get("x-status") == "unsupported":
                warnings.append(
                    structured_error(
                        "UNSUPPORTED_KEY",
                        f"Key '{key}' is accepted by the schema but has no consumer: it is ignored on compile.",
                        path=child_path,
                        hint="Remove the key, or do not rely on it.",
                    )
                )
            _walk_unsupported(value, child_schema, root, child_path, warnings)
    elif isinstance(instance, list) and isinstance(subschema.get("items"), (dict,)):
        for index, item in enumerate(instance):
            _walk_unsupported(item, subschema["items"], root, f"{path}[{index}]", warnings)


def get_schema(section: str | None = None) -> dict[str, Any]:
    """Reference material kept out of tools/list: the UISpec JSON schema, or one ``$defs`` entry by name.

    No ``section`` returns an outline (top-level keys, ``$defs`` names, byte sizes); ``section="all"`` returns the whole schema.
    """
    schema = _load_schema()
    defs = schema.get("$defs", {})
    if not section:
        return {
            "ok": True,
            "schemaId": schema.get("$id"),
            "topLevelKeys": sorted(schema.get("properties", {})),
            "required": schema.get("required", []),
            "sections": {name: len(json.dumps(body)) for name, body in sorted(defs.items())},
            "hint": "Pass section=<name> for one definition or section='all' for the whole schema.",
        }
    if section == "all":
        return {"ok": True, "schema": schema}
    if section in defs:
        return {"ok": True, "section": section, "schema": defs[section]}
    if section in schema.get("properties", {}):
        return {"ok": True, "section": section, "schema": schema["properties"][section]}
    return {
        "ok": False,
        "errors": [
            structured_error(
                "UNKNOWN_SCHEMA_SECTION",
                f"No schema section named '{section}'.",
                path="$.section",
                hint="Call get_schema with no arguments to list section names.",
            )
        ],
    }


def register(mcp: Any) -> None:
    add_tool(
        mcp,
        "validate_spec",
        "Validate UISpec JSON text: JSON schema first, then semantic validation in the editor. Never ok:true with the semantic stage skipped.",
        validate_spec,
    )
    add_tool(
        mcp,
        "get_schema",
        "Return the UISpec JSON schema outline, or one definition by section name ('all' for the whole schema).",
        get_schema,
    )
