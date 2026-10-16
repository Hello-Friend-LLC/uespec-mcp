from __future__ import annotations

import uuid
from typing import Any

from ..bridge import BridgeError, bridge_error_fields, get_bridge, structured_error
from . import context

PREVIEW_DESCRIPTION = (
    "Compile a UISpec JSON file into a Widget Blueprint in the live editor, then render it offscreen through VibeUE "
    "capture_preview_state (no PIE) and return the PNG path under Saved/WidgetPreviews plus an image verdict "
    "(set UESPEC_IMG_CHECK to an img_check.py-compatible command to enable it; otherwise imageVerdict is 'unchecked'). "
    "A blank first capture (cold asset load) is retried once. Pass asset_path to preview an existing asset without compiling. "
    "PIE is NOT driven by this backend: start/stop PIE and runtime captures with ue_driver.py or the HF editor tools."
)


def preview_spec(
    spec_path: str | None = None,
    output_dir: str | None = None,
    asset_path: str | None = None,
    width: int = 1280,
    height: int = 720,
    state: str = "normal",
) -> dict[str, Any]:
    bridge = get_bridge()
    unavailable = getattr(bridge, "preview_unavailable_code", None)
    if unavailable:
        return {
            "ok": False,
            "code": unavailable,
            "backend": getattr(bridge, "backend_name", None),
            "correlationId": str(uuid.uuid4()),
            "durationMs": 0.0,
            "artifacts": [],
            "errors": [
                structured_error(
                    unavailable,
                    f"preview_spec needs the live editor bridge; the '{getattr(bridge, 'backend_name', 'unknown')}' backend cannot render previews.",
                    hint="Set UESPEC_BRIDGE_MODE=live (or auto) with UESPEC_PROJECT_ROOT and a running editor.",
                )
            ],
        }

    correlation_id = str(uuid.uuid4())
    compile_summary: dict[str, Any] | None = None
    target_asset = asset_path.strip() if asset_path else ""

    if not target_asset:
        try:
            resolved_spec = context.resolve_spec_path(spec_path)
        except (FileNotFoundError, ValueError) as exc:
            code = "ACTIVE_SPEC_REQUIRED" if isinstance(exc, ValueError) else "SPEC_NOT_FOUND"
            return {
                "ok": False,
                "artifacts": [],
                "errors": [structured_error(code, str(exc), path="$.spec_path", hint="Pass spec_path (or asset_path) explicitly.")],
            }
        default_output_dir = getattr(getattr(bridge, "config", None), "output_dir", None) or "/Game/UESpec/Generated"
        resolved_output_dir = output_dir.strip() if output_dir else default_output_dir
        try:
            compiled = bridge.compile_spec(resolved_spec, resolved_output_dir, correlation_id=correlation_id)
        except BridgeError as exc:
            return {
                **bridge_error_fields(exc),
                "ok": False,
                "stage": "compile",
                "specPath": str(resolved_spec),
                "errors": [structured_error(exc.code, str(exc), hint="Compile failed before any preview was attempted.")],
            }
        compile_summary = {
            "ok": bool(compiled.get("ok")),
            "assetAction": compiled.get("assetAction"),
            "assetPath": compiled.get("assetPath"),
            "durationMs": compiled.get("durationMs"),
            "correlationId": compiled.get("correlationId"),
        }
        if not compiled.get("ok"):
            return {
                **{key: compiled.get(key) for key in ("backend", "correlationId", "durationMs", "artifacts") if key in compiled},
                "ok": False,
                "stage": "compile",
                "specPath": str(resolved_spec),
                "outputDir": resolved_output_dir,
                "compile": compile_summary,
                "errors": compiled.get("errors") or [structured_error("COMPILE_FAILED", "Compile did not succeed; no preview was attempted.")],
            }
        generated = compiled.get("generatedAssets") or []
        target_asset = str(compiled.get("assetPath") or (generated[0] if generated else "")).strip()
        if not target_asset:
            return {
                **{key: compiled.get(key) for key in ("backend", "correlationId", "durationMs", "artifacts") if key in compiled},
                "ok": False,
                "stage": "compile",
                "compile": compile_summary,
                "errors": [structured_error("COMPILED_ASSET_UNKNOWN", "Compile succeeded but reported no assetPath to preview.")],
            }

    try:
        preview = bridge.capture_preview(target_asset, width, height, state, correlation_id=correlation_id)
    except BridgeError as exc:
        return {
            **bridge_error_fields(exc),
            "ok": False,
            "stage": "preview",
            "assetPath": target_asset,
            "compile": compile_summary,
            "errors": [structured_error(exc.code, str(exc), hint="Is the project editor running with the HF UnrealMCPServer?")],
        }

    preview["stage"] = "preview"
    preview["compile"] = compile_summary
    if compile_summary is not None:
        preview["durationMs"] = round(float(preview.get("durationMs") or 0.0) + float(compile_summary.get("durationMs") or 0.0), 1)
        preview["artifacts"] = [
            {"kind": "asset", "path": str(compile_summary["assetPath"])},
            *[entry for entry in preview.get("artifacts", []) if entry.get("path") != compile_summary["assetPath"]],
        ] if compile_summary.get("assetPath") else preview.get("artifacts", [])
    return preview


def register(mcp: Any) -> None:
    mcp.tool(name="preview_spec", description=PREVIEW_DESCRIPTION)(preview_spec)
