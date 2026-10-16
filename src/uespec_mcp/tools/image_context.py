"""``image_to_spec_context``: everything a host Claude session needs to turn a screenshot into a UESpec, except the pixels.

The host reads the PNG itself (it is a vision model); this tool returns the image path and metadata, a compact component
catalog digest and schema digest (from the cached support surface, ``surface_cache``), a skeleton spec for the requested
parent class, the constraints the compiler enforces (supported events/primitives/converters per the surface statuses) and
the nearest example specs. With the editor down and no cached surface the surface-derived parts are marked unavailable with
the bridge's error code; nothing is fabricated.
"""
from __future__ import annotations

import json
import os
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Any

from ..bridge import structured_error
from ..examples import retrieve_examples
from . import context, surface, validate
from ._envelope import write_full_result
from ._register import add_tool
from .generate import _DEFAULT_MAX_IMAGE_BYTES, _SUPPORTED_IMAGE_TYPES, _SUPPORTED_TARGET_KINDS

_ROOT_PREFERENCE = ("CanvasPanel", "Overlay", "VerticalBox")
_SCHEMA_DEF_NAMES = ("widget", "slot", "bindingExpr", "action", "actionBlock", "viewModel", "viewModelField")
_RULES = (
    "Use only widget types from catalogDigest.widgets; style tokens are @-names from catalogDigest.styleTokens or literals.",
    "Action `on` values must be constraints.events.supported; `do` primitives must be constraints.actionPrimitives.supported.",
    "Bind `convert` values must be constraints.converters; bind paths are plain dotted names (vm.Field), no [ ] indexers or ( ) calls.",
    "Every vm.* path needs a viewModel.fields entry; with no ViewModel use literal content.",
    "ids lowerCamelCase, root id rootPanel, spec name PascalCase, apiVersion 0.2; keep the tree simple and approximate the layout.",
)
_NEXT_STEPS = (
    "Read imagePath yourself (you are the vision model).",
    "Write the spec JSON starting from skeleton, using only catalogDigest/constraints names.",
    "validate_spec(content), compile_spec_to_wbp(spec_path), preview_spec(spec_path), then compare the preview to the image.",
    "On a failure call get_failure_report and fix it with apply_spec_patch.",
)


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) == 24 and header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR":
        width, height = struct.unpack(">II", header[16:24])
        return int(width), int(height)
    return None


def _inspect_image(image_path: str) -> dict[str, Any]:
    """Validate the image file without reading it into memory; ``ok`` replies carry the path and metadata."""
    if not str(image_path or "").strip():
        return {"ok": False, "errors": [structured_error("IMAGE_PATH_REQUIRED", "image_path is required.", path="$.image_path", hint="Pass an absolute PNG/JPG/WEBP/GIF path.")]}
    resolved = context.expand_path(str(image_path).strip())
    if not resolved.is_file():
        return {"ok": False, "errors": [structured_error("IMAGE_NOT_FOUND", f"Image file '{resolved}' does not exist.", path="$.image_path", hint="Pass an existing PNG, JPG, WEBP, or GIF file.")]}
    media_type = _SUPPORTED_IMAGE_TYPES.get(resolved.suffix.lower())
    if media_type is None:
        return {"ok": False, "errors": [structured_error("UNSUPPORTED_IMAGE_TYPE", f"Image file '{resolved}' has unsupported type '{resolved.suffix}'.", path="$.image_path", hint="Use PNG, JPG, WEBP, or GIF.")]}
    size = resolved.stat().st_size
    limit = int(os.getenv("UESPEC_MAX_IMAGE_BYTES", str(_DEFAULT_MAX_IMAGE_BYTES)) or _DEFAULT_MAX_IMAGE_BYTES)
    if size > limit:
        return {"ok": False, "errors": [structured_error("IMAGE_TOO_LARGE", f"Image file '{resolved}' is {size} bytes, above the {limit} byte limit.", path="$.image_path", hint="Use a smaller screenshot or raise UESPEC_MAX_IMAGE_BYTES.")]}
    info: dict[str, Any] = {"path": str(resolved).replace("\\", "/"), "mediaType": media_type, "sizeBytes": size}
    dims = _png_size(resolved) if media_type == "image/png" else None
    if dims:
        info["width"], info["height"] = dims
    return {"ok": True, "image": info, "stem": resolved.stem}


def _pascal(stem: str) -> str:
    stem = re.sub(r"^(WBP|T|SS)_", "", stem)
    stem = re.sub(r"[_-]?\d+x\d+$", "", stem)
    parts = [part for part in re.split(r"[^0-9A-Za-z]+", stem) if part]
    name = "".join(part[:1].upper() + part[1:] for part in parts)
    if not name or not name[0].isalpha():
        name = f"Screenshot{name}"
    return name


def _section(snapshot: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [entry for entry in surface.surface_section(snapshot, key) if isinstance(entry, dict)]


def _entry_str(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key) or entry.get(key[:1].upper() + key[1:])
    return value if isinstance(value, str) and value else None


def _catalog_digest(snapshot: dict[str, Any]) -> dict[str, Any]:
    widget_rows: list[tuple[str, str, list[str], list[str]]] = []
    for key, source in (("RegisteredComponents", "component"), ("BuiltInUMGWidgets", "umg"), ("CommonUIWidgets", "commonui")):
        for entry in _section(snapshot, key):
            name = _entry_str(entry, "type")
            if name:
                keys = [str(k) for k in (entry.get("keys") or entry.get("Keys") or [])]
                accepts = [str(a) for a in (entry.get("accepts") or entry.get("Accepts") or [])]
                widget_rows.append((name, source, keys, accepts))
    common: set[str] = set(widget_rows[0][2]) if widget_rows else set()
    for _name, _source, keys, _accepts in widget_rows[1:]:
        common &= set(keys)
    groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
    for name, _source, keys, accepts in sorted(widget_rows):
        groups.setdefault((tuple(sorted(set(keys) - common)), tuple(accepts)), []).append(name)
    widget_groups: list[dict[str, Any]] = []
    for (extra, accepts), names in groups.items():
        group: dict[str, Any] = {"types": names}
        if extra:
            group["extraKeys"] = list(extra)
        if accepts:
            group["accepts"] = list(accepts)
        widget_groups.append(group)
    converters = [
        {"name": name, **({"signature": _entry_str(entry, "signature")} if _entry_str(entry, "signature") else {})}
        for entry in _section(snapshot, "Converters")
        if (name := _entry_str(entry, "name"))
    ]
    tokens = {name: entry.get("value", entry.get("Value")) for entry in _section(snapshot, "StyleTokens") if (name := _entry_str(entry, "name"))}
    return {
        "available": True,
        "supportSurfaceVersion": snapshot.get("SupportSurfaceVersion") or snapshot.get("supportSurfaceVersion"),
        "commonWidgetKeys": sorted(common),
        "widgets": widget_groups,
        "registeredComponents": sorted(name for name, source, _k, _a in widget_rows if source == "component"),
        "converters": converters,
        "styleTokens": tokens,
        "signals": sorted(name for entry in _section(snapshot, "Signals") if (name := _entry_str(entry, "name"))),
    }


def _statuses(snapshot: dict[str, Any], key: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"supported": [], "unsupported": [], "unknownStatus": []}
    for entry in _section(snapshot, key):
        name = _entry_str(entry, "name")
        if not name:
            continue
        status = str(entry.get("status") or entry.get("Status") or "").lower()
        out["supported" if status == "supported" else "unsupported" if status else "unknownStatus"].append(name)
    if not out["unknownStatus"]:
        del out["unknownStatus"]
    return out


def _constraints(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": True,
        "events": _statuses(snapshot, "ActionEvents"),
        "actionPrimitives": _statuses(snapshot, "ActionPrimitives"),
        "converters": sorted(name for entry in _section(snapshot, "Converters") if (name := _entry_str(entry, "name"))),
        "rules": list(_RULES),
    }


def _schema_digest() -> dict[str, Any]:
    schema = validate._load_schema()
    defs = schema.get("$defs", {})
    return {
        "schemaId": schema.get("$id"),
        "topLevelKeys": sorted(schema.get("properties", {})),
        "required": schema.get("required", []),
        "defs": {name: sorted(defs[name].get("properties", {})) for name in _SCHEMA_DEF_NAMES if name in defs},
        "fullSchema": "get_schema(section=<name>) or section='all'",
    }


def _resolve_parent(snapshot: dict[str, Any], target_kind: str, parent_class: str) -> tuple[str, str | None, dict[str, Any] | None]:
    """(kind, base class path, error). ``parent_class`` (path or short name) selects a kind whose baseClass matches it."""
    kinds = _section(snapshot, "Kinds")
    by_kind = {_entry_str(entry, "kind"): _entry_str(entry, "baseClass") for entry in kinds}
    wanted = (parent_class or "").strip()
    if not wanted:
        return target_kind, by_kind.get(target_kind), None
    short = wanted.rsplit(".", 1)[-1].rsplit("/", 1)[-1].lower()
    matches = [(kind, base) for kind, base in by_kind.items() if kind in _SUPPORTED_TARGET_KINDS and base and base.rsplit(".", 1)[-1].lower() == short]
    if not matches:
        bases = sorted({base for kind, base in by_kind.items() if kind in _SUPPORTED_TARGET_KINDS and base})
        return target_kind, None, structured_error("PARENT_CLASS_UNKNOWN", f"No spec kind maps to parent class '{wanted}'.", path="$.parent_class", hint=f"Use one of: {', '.join(bases) or 'unavailable (no surface)'}.")
    for kind, base in matches:
        if kind == target_kind:
            return kind, base, None
    return matches[0][0], matches[0][1], None


def _skeleton(kind: str, stem: str, source_name: str, widgets: set[str]) -> dict[str, Any]:
    root_type = next((candidate for candidate in _ROOT_PREFERENCE if not widgets or candidate in widgets), "CanvasPanel")
    return {
        "apiVersion": "0.2",
        "kind": kind,
        "name": _pascal(stem),
        "description": f"Recreated from {source_name}.",
        "meta": {"generatedBy": "image_to_spec_context", "sourceImage": source_name},
        "root": {"type": root_type, "id": "rootPanel", "children": []},
    }


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))


def image_to_spec_context(
    image_path: str,
    hints: str = "",
    target_kind: str = "HUD",
    parent_class: str = "",
) -> dict[str, Any]:
    """Context bundle for writing a UESpec from a screenshot in the host session (see the module docstring).

    Refresh a stale surface cache with ``get_support_surface(force=true)``; this tool only reads the cache (or the editor on a miss).
    """
    started = time.perf_counter()
    kind = (target_kind or "HUD").strip() or "HUD"
    if kind not in _SUPPORTED_TARGET_KINDS:
        return {"ok": False, "errors": [structured_error("INVALID_TARGET_KIND", f"Unsupported target_kind '{target_kind}'.", path="$.target_kind", hint=f"Use one of: {', '.join(sorted(_SUPPORTED_TARGET_KINDS))}.")]}
    inspected = _inspect_image(image_path)
    if not inspected.get("ok"):
        return inspected

    surface_result = surface.get_support_surface(format="json")
    snapshot = surface_result.get("content") if surface_result.get("ok") and isinstance(surface_result.get("content"), dict) else None
    surface_info: dict[str, Any] = {"available": snapshot is not None}
    for field in ("cache", "cacheHash", "cacheFile", "backend"):
        if surface_result.get(field) is not None:
            surface_info[field] = surface_result[field]
    warnings: list[dict[str, Any]] = []
    if snapshot is None:
        first = (surface_result.get("errors") or [{}])[0]
        code = surface_result.get("code") or first.get("code") or "SUPPORT_SURFACE_UNAVAILABLE"
        message = str(first.get("message") or surface_result.get("message") or "The support surface is not available.")[:240]
        surface_info.update({"code": code, "message": message})
        unavailable = {"available": False, "code": code, "message": "Start the PrjEfz editor once (get_support_surface) to fill the surface cache, then call again."}
        catalog: dict[str, Any] = dict(unavailable)
        constraints: dict[str, Any] = {**unavailable, "rules": list(_RULES)}
        resolved_kind, parent, parent_error = kind, None, None
        if parent_class.strip():
            parent = parent_class.strip()
            warnings.append({"code": "PARENT_CLASS_UNVERIFIED", "message": "No surface to map parent_class to a spec kind; echoed as given."})
        widget_names: set[str] = set()
    else:
        catalog = _catalog_digest(snapshot)
        constraints = _constraints(snapshot)
        resolved_kind, parent, parent_error = _resolve_parent(snapshot, kind, parent_class)
        widget_names = {name for group in catalog["widgets"] for name in group["types"]}
    if parent_error is not None:
        return {"ok": False, "errors": [parent_error]}

    skeleton = _skeleton(resolved_kind, inspected["stem"], Path(inspected["image"]["path"]).name, widget_names)
    schema_errors = list(validate._get_validator().iter_errors(skeleton))

    component_names = {_entry_str(entry, "type") or "" for entry in _section(snapshot or {}, "RegisteredComponents")} - {""}
    examples, where = retrieve_examples(resolved_kind, component_names, max_examples=3, snippet_chars=0)
    examples_info: dict[str, Any] = {"source": where["source"], "dirs": where["dirs"], "items": [example.as_result_dict() for example in examples]}
    if where["source"] == "legacy-fallback":
        warnings.append({"code": "EXAMPLES_DIR_LEGACY_FALLBACK", "message": "No examples_dir configured and no UESpec plugin resolved; used the sibling-checkout lookup."})
    if not examples:
        warnings.append({"code": "NO_EXAMPLES_FOUND", "message": "No example specs found; set UESPEC_EXAMPLES_DIR or examplesDir in uespec.hf.json."})

    result: dict[str, Any] = {
        "ok": True,
        "imagePath": inspected["image"]["path"],
        "image": inspected["image"],
        "hints": (hints or "").strip(),
        "targetKind": resolved_kind,
        "parentClass": parent,
        "catalogDigest": catalog,
        "schemaDigest": _schema_digest(),
        "skeleton": skeleton,
        "skeletonSchemaValid": not schema_errors,
        "constraints": constraints,
        "examples": examples_info,
        "surface": surface_info,
        "nextSteps": list(_NEXT_STEPS),
        "correlationId": str(uuid.uuid4()),
    }
    if warnings:
        result["warnings"] = warnings
    sizes = {key: _size(result[key]) for key in ("catalogDigest", "schemaDigest", "skeleton", "constraints", "examples")}
    full_path = write_full_result("image_to_spec_context", result, context.project_root())
    result["artifacts"] = [{"kind": "image-context", "path": full_path.replace("\\", "/")}] if full_path else []
    result["sizes"] = {**sizes, "total": _size(result)}
    result["durationMs"] = round((time.perf_counter() - started) * 1000.0, 1)
    return result


def register(mcp: Any) -> None:
    add_tool(
        mcp,
        "image_to_spec_context",
        "Screenshot-to-spec context for the host (you read the PNG): component/schema digests, parent-class skeleton, supported events/converters, examples; served from the surface cache.",
        image_to_spec_context,
    )
