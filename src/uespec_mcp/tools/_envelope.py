"""Shared reply shaping for the artifact-returning tools.

* ``compact`` keeps a tool reply under ~2 KB: heavy values are replaced by ``{"truncated": true, "bytes": n}`` and the full
  reply is written under ``<project>/Saved/UESpec/artifacts/`` (absolute path in ``artifacts[]`` and ``fullResult``).
* ``aggregate`` folds the per-path results of an array-valued ``spec_path`` into one envelope.

Direct Python calls (the orchestrator, tests) keep the full reply; only the MCP registration wraps with ``compact``.
"""
from __future__ import annotations

import functools
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

SUMMARY_LIMIT_BYTES = 2048
_KEEP_KEYS = ("ok", "code", "stage", "backend", "correlationId", "durationMs", "specPath", "assetPath", "assetAction", "outputDir", "cache")
_ROW_LIMIT_BYTES = 700


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))


def _project_root() -> Path | None:
    from . import context

    return context.project_root()


def _shrink_errors(errors: Any, message_limit: int = 240, count: int = 3) -> Any:
    if not isinstance(errors, list):
        return errors
    out: list[Any] = []
    for entry in errors[:count]:
        if isinstance(entry, dict):
            entry = {key: (value[:message_limit] if isinstance(value, str) else value) for key, value in entry.items() if key != "nearestDeclared"}
        out.append(entry)
    if len(errors) > count:
        out.append({"code": "MORE_ERRORS", "message": f"{len(errors) - count} more error(s) in the full result."})
    return out


def write_full_result(tool: str, result: dict[str, Any], root: Path | None) -> str | None:
    if root is None:
        return None
    try:
        target_dir = Path(root) / "Saved" / "UESpec" / "artifacts"
        target_dir.mkdir(parents=True, exist_ok=True)
        cid = str(result.get("correlationId") or uuid.uuid4())
        path = (target_dir / f"{tool}-{cid}.json").resolve()
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return str(path)
    except OSError:
        return None


def compact(result: Any, tool: str, limit: int = SUMMARY_LIMIT_BYTES) -> Any:
    if not isinstance(result, dict) or _size(result) <= limit:
        return result
    full_path = write_full_result(tool, result, _project_root())
    summary: dict[str, Any] = {}
    for threshold in (320, 160, 80, 0):
        summary = {}
        for key, value in result.items():
            if key == "artifacts":
                summary[key] = list(value or [])
            elif key == "errors":
                summary[key] = _shrink_errors(value)
            elif isinstance(value, (bool, int, float)) or value is None:
                summary[key] = value
            elif key in _KEEP_KEYS and isinstance(value, str):
                summary[key] = value
            elif _size(value) <= threshold:
                summary[key] = value
            else:
                summary[key] = {"truncated": True, "bytes": _size(value)}
        if full_path:
            summary["artifacts"] = [*summary.get("artifacts", []), {"kind": "full-result", "path": full_path}]
            summary["fullResult"] = full_path
        summary["summaryTruncated"] = True
        if _size(summary) <= limit:
            break
    return summary


def compacting(tool: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for the registered (MCP-facing) callable; keeps the original signature for schema generation."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return compact(func(*args, **kwargs), tool)

        return wrapper

    return decorator


def _row(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"ok": False, "code": "NO_RESULT"}
    row: dict[str, Any] = {}
    for key in (*_KEEP_KEYS, "outputReport", "failureReportPath"):
        if key in result:
            row[key] = result[key]
    summary = result.get("summary")
    if isinstance(summary, dict) and _size(summary) <= 240:
        row["summary"] = summary
    if result.get("generatedAssets"):
        row["generatedAssets"] = list(result["generatedAssets"])[:5]
    if result.get("errors"):
        row["errors"] = _shrink_errors(result["errors"], message_limit=160, count=2)
    if result.get("artifacts"):
        row["artifacts"] = list(result["artifacts"])
    while _size(row) > _ROW_LIMIT_BYTES and "errors" in row and len(row["errors"]) > 1:
        row["errors"] = row["errors"][:1]
    return row


def aggregate(tool: str, specs: Iterable[str], results: list[dict[str, Any]]) -> dict[str, Any]:
    """One envelope for an array-valued ``spec_path``: per-item rows, merged absolute ``artifacts[]``, summed duration."""
    spec_list = list(specs)
    rows = []
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec, result in zip(spec_list, results):
        row = _row(result)
        row.setdefault("specPath", spec)
        rows.append(row)
        for entry in (result or {}).get("artifacts") or []:
            key = f"{entry.get('kind')}|{entry.get('path')}"
            if key not in seen:
                seen.add(key)
                artifacts.append(entry)
    failed = [row for row in rows if not row.get("ok")]
    envelope: dict[str, Any] = {
        "ok": bool(rows) and not failed,
        "count": len(rows),
        "failed": len(failed),
        "backend": next((r.get("backend") for r in rows if r.get("backend")), None),
        "correlationId": str(uuid.uuid4()),
        "durationMs": round(sum(float(r.get("durationMs") or 0.0) for r in rows), 1),
        "results": rows,
        "artifacts": artifacts,
    }
    full_path = write_full_result(tool, {**envelope, "results": results}, _project_root())
    if full_path:
        envelope["fullResult"] = full_path
        envelope["artifacts"] = [*artifacts, {"kind": "full-result", "path": full_path}]
    return envelope
