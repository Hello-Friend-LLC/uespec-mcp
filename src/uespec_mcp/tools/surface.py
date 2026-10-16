from __future__ import annotations

from typing import Any

from ..bridge import BridgeError, bridge_error_fields, get_bridge, structured_error


_SUPPORT_SURFACE_CACHE: dict[str, Any] | None = None


def get_support_surface(format: str = "markdown") -> dict[str, Any]:
    global _SUPPORT_SURFACE_CACHE
    try:
        result = get_bridge().get_support_surface(format=format)
    except BridgeError as exc:
        return {
            **bridge_error_fields(exc),
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

    if result["ok"] and result["format"] == "json" and isinstance(result["content"], dict):
        _SUPPORT_SURFACE_CACHE = result["content"]
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


def _ensure_surface_json() -> dict[str, Any] | None:
    global _SUPPORT_SURFACE_CACHE
    if _SUPPORT_SURFACE_CACHE is not None:
        return _SUPPORT_SURFACE_CACHE
    result = get_support_surface(format="json")
    if result.get("ok") and isinstance(result.get("content"), dict):
        _SUPPORT_SURFACE_CACHE = result["content"]
    return _SUPPORT_SURFACE_CACHE


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
    mcp.tool(name="get_support_surface", description="Dump the current UESpec support surface from Unreal.")(get_support_surface)
    mcp.tool(name="suggest_nearest", description="Suggest the nearest widget/converter/token/signal from the support surface.")(suggest_nearest)
