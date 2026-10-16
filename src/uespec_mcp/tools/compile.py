from __future__ import annotations

import json
from typing import Any

from ..bridge import BridgeError, bridge_error_fields, get_bridge, structured_error
from . import _envelope, context
from ._register import add_tool


def compile_spec_to_wbp(spec_path: str | list[str], output_dir: str | None = None) -> dict[str, Any]:
    items = context.path_list(spec_path)
    if isinstance(spec_path, (list, tuple)):
        return _envelope.aggregate("compile_spec_to_wbp", items, [_compile_one(item, output_dir) for item in items])
    return _compile_one(spec_path, output_dir)


def _compile_one(spec_path: str | None, output_dir: str | None) -> dict[str, Any]:
    try:
        resolved_spec = context.resolve_spec_path(spec_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "ok": False,
            "specPath": spec_path,
            "generatedAssets": [],
            "errors": [context.path_error(exc)],
        }

    bridge = get_bridge()
    default_output_dir = getattr(getattr(bridge, "config", None), "output_dir", None) or "/Game/UESpec/Generated"
    resolved_output_dir = output_dir.strip() if output_dir else default_output_dir

    try:
        result = bridge.compile_spec(resolved_spec, resolved_output_dir)
        if isinstance(result, dict) and result.get("ok"):
            from . import repair  # lazy: repair imports this module

            repair.record_compile(resolved_spec, result)  # which asset came from which spec text (rollback_spec uses it)
        return result
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
                    hint="Bridge unavailable: start the project editor (live bridge), or close it and set UESPEC_BRIDGE_MODE=commandlet with UESPEC_PROJECT_ROOT/UESPEC_UE_ENGINE_PATH configured, then retry.",
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
            }
        )

    return {"ok": True, "baseDir": str(search_root), "count": len(items), "items": items}


def register(mcp: Any) -> None:
    add_tool(
        mcp,
        "compile_spec_to_wbp",
        "Compile UISpec JSON file(s) into Widget Blueprint assets. spec_path is a path or an array of paths; output_dir is an Unreal package path (default /Game/UESpec/Generated). Returns asset paths and absolute artifacts[].",
        compile_spec_to_wbp,
        compact=True,
    )
    add_tool(
        mcp,
        "list_existing_specs",
        "List UISpec JSON files under base_dir (default: the project's UESpec folder).",
        list_existing_specs,
    )
