from __future__ import annotations

import time
import uuid
from typing import Any

from .. import surface_cache
from ..bridge import BridgeError, bridge_error_fields, get_bridge, structured_error
from ._register import add_tool


def _cache_context(bridge: Any) -> tuple[Any, Any] | None:
    """(project_root, key) when the surface of this bridge is cacheable, else None (mock backend, no project root)."""
    config = getattr(bridge, "config", None)
    if config is None or getattr(bridge, "backend_name", "") == "mock" or not hasattr(config, "resolved_project_root"):
        return None
    root = config.resolved_project_root()
    if root is None:
        return None
    try:
        return root, surface_cache.compute_key(config)
    except Exception:  # noqa: BLE001 - a key problem must degrade to "no cache", never to a failed tool call
        return None


def get_support_surface(format: str = "markdown", force: bool = False) -> dict[str, Any]:
    """The UESpec support surface. Cached under <project>/Saved/UESpec/cache; ``force`` re-reads it from the editor.

    The reply carries ``cache`` ("hit" | "miss" | "off"), ``cacheKey`` and ``cacheFile``.
    """
    bridge = get_bridge()
    normalized_format = str(format).strip().lower()
    cacheable = _cache_context(bridge) if normalized_format in {"markdown", "json"} else None
    digest = surface_cache.key_hash(cacheable[1]) if cacheable else None
    started = time.perf_counter()

    if cacheable and not force:
        entry = surface_cache.load(cacheable[0], digest, normalized_format)
        if entry is not None:
            cache_file = str(surface_cache.cache_path(cacheable[0], digest).resolve())
            return {
                "ok": True,
                "format": normalized_format,
                "content": entry["contents"][normalized_format],
                "identity": entry.get("identity"),
                "cache": "hit",
                "cacheKey": cacheable[1],
                "cacheHash": digest,
                "cachedAt": entry.get("cachedAt"),
                "backend": getattr(bridge, "backend_name", None),
                "correlationId": str(uuid.uuid4()),
                "durationMs": round((time.perf_counter() - started) * 1000.0, 1),
                "artifacts": [{"kind": "surface-cache", "path": cache_file}],
            }

    cache_fields: dict[str, Any] = {"cache": "miss" if cacheable else "off"}
    if cacheable:
        cache_fields.update({"cacheKey": cacheable[1], "cacheHash": digest, "forced": bool(force)})
    try:
        result = bridge.get_support_surface(format=format)
    except BridgeError as exc:
        return {
            **bridge_error_fields(exc),
            **cache_fields,
            "ok": False,
            "format": format,
            "content": None,
            "errors": [
                structured_error(
                    exc.code,
                    str(exc),
                    path="$.format",
                    hint="Set UESPEC_UPROJECT_PATH and, if needed, UESPEC_UE_ENGINE_PATH before calling UE-backed tools.",
                )
            ],
        }

    result.update(cache_fields)
    if cacheable and result.get("ok") and result.get("content") is not None:
        cache_file = surface_cache.store(
            cacheable[0], digest, cacheable[1], normalized_format, result["content"], result.get("identity")
        )
        if cache_file:
            result["cacheFile"] = cache_file
            result["artifacts"] = [*(result.get("artifacts") or []), {"kind": "surface-cache", "path": cache_file}]
    return result


def suggest_nearest(needle: str, category: str) -> dict[str, Any]:
    normalized_category = category.strip().lower()
    if normalized_category not in {"widget", "converter", "token", "signal"}:
        return {
            "ok": False,
            "needle": needle,
            "category": category,
            "suggestions": [],
            "errors": [
                structured_error(
                    "INVALID_CATEGORY",
                    f"Unknown support-surface category '{category}'.",
                    path="$.category",
                    hint="Use one of: widget, converter, token, signal.",
                )
            ],
        }

    surface_snapshot = _ensure_surface_json()
    if surface_snapshot is None:
        return {
            "ok": False,
            "needle": needle,
            "category": normalized_category,
            "suggestions": [],
            "errors": [
                structured_error(
                    "SUPPORT_SURFACE_UNAVAILABLE",
                    "Support surface JSON is not available yet.",
                    hint="Call get_support_surface(format='json') after configuring the UE bridge.",
                )
            ],
        }

    candidates = _extract_candidates(surface_snapshot, normalized_category)
    ranked = sorted(
        (
            (_levenshtein_distance(needle.lower(), candidate.lower()), candidate)
            for candidate in candidates
        ),
        key=lambda item: (item[0], item[1].lower()),
    )
    suggestions = [candidate for _, candidate in ranked[:5]]
    return {
        "ok": True,
        "needle": needle,
        "category": normalized_category,
        "suggestions": suggestions,
        "supportSurfaceVersion": surface_snapshot.get("SupportSurfaceVersion")
        or surface_snapshot.get("supportSurfaceVersion"),
    }


def _ensure_surface_json(force: bool = False) -> dict[str, Any] | None:
    """The JSON surface via get_support_surface, so it is served from the per-project disk cache (no process-global copy)."""
    result = get_support_surface(format="json", force=force)
    if result.get("ok") and isinstance(result.get("content"), dict):
        return result["content"]
    return None


_CATALOG_CATEGORIES = ("widget", "converter", "token", "signal")


def get_component_catalog(category: str | None = None, force: bool = False) -> dict[str, Any]:
    """Names only: the widget types, converters, style tokens and signals the compiler accepts (from the cached surface)."""
    wanted = (category or "").strip().lower()
    if wanted and wanted not in _CATALOG_CATEGORIES:
        return {
            "ok": False,
            "errors": [
                structured_error(
                    "INVALID_CATEGORY",
                    f"Unknown catalog category '{category}'.",
                    path="$.category",
                    hint="Use one of: widget, converter, token, signal (or omit for all).",
                )
            ],
        }
    surface_result = get_support_surface(format="json", force=force)
    content = surface_result.get("content")
    if not surface_result.get("ok") or not isinstance(content, dict):
        return {
            **{key: surface_result[key] for key in ("backend", "correlationId", "durationMs", "cache", "artifacts") if key in surface_result},
            "ok": False,
            "errors": surface_result.get("errors")
            or [structured_error("SUPPORT_SURFACE_UNAVAILABLE", "Support surface JSON is not available.", hint="Start the project editor or pass force=false once a surface is cached.")],
        }
    categories = [wanted] if wanted else list(_CATALOG_CATEGORIES)
    catalog = {f"{name}s": _extract_candidates(content, name) for name in categories}
    return {
        "ok": True,
        "catalog": catalog,
        "counts": {key: len(value) for key, value in catalog.items()},
        "supportSurfaceVersion": content.get("SupportSurfaceVersion") or content.get("supportSurfaceVersion"),
        **{key: surface_result[key] for key in ("backend", "correlationId", "durationMs", "cache", "cacheKey", "cacheHash", "artifacts") if key in surface_result},
    }


def _extract_candidates(snapshot: dict[str, Any], category: str) -> list[str]:
    if category == "widget":
        widget_entries = []
        for key in ("BuiltInUMGWidgets", "CommonUIWidgets", "RegisteredComponents"):
            widget_entries.extend(snapshot.get(key, []))
        return sorted(
            {
                entry.get("Type") or entry.get("type")
                for entry in widget_entries
                if isinstance(entry, dict) and (entry.get("Type") or entry.get("type"))
            }
        )
    if category == "converter":
        return sorted(
            {
                entry.get("Name") or entry.get("name")
                for entry in snapshot.get("Converters", [])
                if isinstance(entry, dict) and (entry.get("Name") or entry.get("name"))
            }
        )
    if category == "token":
        return sorted(
            {
                entry.get("Name") or entry.get("name")
                for entry in snapshot.get("StyleTokens", [])
                if isinstance(entry, dict) and (entry.get("Name") or entry.get("name"))
            }
        )
    return sorted(
        {
            entry.get("Name") or entry.get("name")
            for entry in snapshot.get("Signals", [])
            if isinstance(entry, dict) and (entry.get("Name") or entry.get("name"))
        }
    )


def _levenshtein_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            substitution_cost = 0 if left_char == right_char else 1
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + substitution_cost,
                )
            )
        previous = current
    return previous[-1]


def register(mcp: Any) -> None:
    add_tool(
        mcp,
        "get_support_surface",
        "UESpec support surface (format markdown|json). Cached per project/engine/plugin build under Saved/UESpec/cache; reply has cache hit|miss, force=true re-reads it from the editor.",
        get_support_surface,
    )
    add_tool(
        mcp,
        "get_component_catalog",
        "Compact name lists of supported widgets, converters, style tokens and signals (category optional), from the cached surface.",
        get_component_catalog,
    )
    add_tool(
        mcp,
        "suggest_nearest",
        "Nearest widget/converter/token/signal names to a misspelled needle.",
        suggest_nearest,
    )
