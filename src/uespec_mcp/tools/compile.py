from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bridge import BridgeError, bridge_error_fields, get_bridge, structured_error
from . import context


def compile_spec_to_wbp(spec_path: str | None = None, output_dir: str | None = None) -> dict[str, Any]:
    try:
        resolved_spec = context.resolve_spec_path(spec_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "ok": False,
            "generatedAssets": [],
            "errors": [_path_resolution_error(exc)],
        }

    bridge = get_bridge()
    default_output_dir = getattr(getattr(bridge, "config", None), "output_dir", None) or "/Game/UESpec/Generated"
    resolved_output_dir = output_dir.strip() if output_dir else default_output_dir

    try:
        return bridge.compile_spec(resolved_spec, resolved_output_dir)
    except BridgeError as exc:
        return {
            **bridge_error_fields(exc),
            "ok": False,
            "specPath": str(resolved_spec),
            "outputDir": resolved_output_dir,
            "generatedAssets": [],
            "errors": [
                structured_error(
                    exc.code,
                    str(exc),
                    hint="Use an Unreal package path such as '/Game/UESpec/Generated' and configure Unreal bridge environment variables before compiling a spec.",
                )
            ],
        }


def list_existing_specs(base_dir: str | None = None) -> dict[str, Any]:
    try:
        search_root = context.resolve_base_dir(base_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return {
            "ok": False,
            "count": 0,
            "items": [],
            "errors": [
                structured_error(
                    "INVALID_BASE_DIR",
                    str(exc),
                    path="$.base_dir",
                    hint="Point base_dir at an existing folder that contains UISpec JSON files.",
                )
            ],
        }

    active_spec = context.get_active_spec()
    items: list[dict[str, Any]] = []
    for path in sorted(search_root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if not {"apiVersion", "kind", "name"}.issubset(payload):
            continue
        items.append(
            {
                "name": payload.get("name"),
                "kind": payload.get("kind"),
                "path": str(path.resolve()),
                "description": payload.get("description", ""),
                "active": str(path.resolve()) == active_spec,
            }
        )

    return {"ok": True, "baseDir": str(search_root), "count": len(items), "items": items}


def _path_resolution_error(exc: Exception) -> dict[str, Any]:
    message = str(exc)
    code = "ACTIVE_SPEC_REQUIRED" if isinstance(exc, ValueError) else "SPEC_NOT_FOUND"
    return structured_error(
        code,
        message,
        path="$.spec_path",
        hint="Pass spec_path explicitly or call set_active_spec first.",
    )


def register(mcp: Any) -> None:
    mcp.tool(name="compile_spec_to_wbp", description="Compile a UISpec JSON file into a Widget Blueprint asset. output_dir must be an Unreal package path such as /Game/UESpec/Generated.")(compile_spec_to_wbp)
    mcp.tool(name="list_existing_specs", description="List existing UISpec JSON files on disk.")(list_existing_specs)
