"""Regression guard for host-driven repairs of test specs (WI-1586 t500).

A repair that makes a failing test pass by deleting the assertion (or lowering the number it expects) is not a repair.
``compare_assertions`` lists, between the document before a patch and the document after it, every assertion step that
disappeared (or changed its assertion kind / phase), every parameter that was dropped and every numeric expectation that
went down. The tool layer rejects such a patch with ``ASSERTION_REMOVAL_REJECTED`` unless the caller states a reason.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_COUNT_KEYS = ("expected", "count", "expectedCount", "min", "minCount")


def is_test_spec(document: Any, spec_path: str | Path | None = None) -> bool:
    if spec_path is not None and str(spec_path).lower().endswith(".test.json"):
        return True
    return isinstance(document, dict) and (document.get("kind") == "Test" or isinstance(document.get("testSpec"), dict))


def _steps(document: Any) -> list[tuple[str, int, dict[str, Any]]]:
    out: list[tuple[str, int, dict[str, Any]]] = []
    test_spec = document.get("testSpec") if isinstance(document, dict) else None
    phases = test_spec.get("phases") if isinstance(test_spec, dict) else None
    if not isinstance(phases, dict):
        return out
    for phase, steps in phases.items():
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if isinstance(step, dict):
                out.append((str(phase), index, step))
    return out


def collect_assertions(document: Any) -> list[dict[str, Any]]:
    """Every ``assert*`` entry of every phase (``assume`` preconditions included), in document order."""
    found: list[dict[str, Any]] = []
    for phase, index, step in _steps(document):
        for key, body in step.items():
            if isinstance(key, str) and key.startswith("assert"):
                found.append(
                    {
                        "phase": phase,
                        "index": index,
                        "step": str(step.get("step") or ""),
                        "kind": key,
                        "body": body,
                        "signature": (phase, str(step.get("step") or ""), key),
                    }
                )
    return found


def _parameter_names(document: Any) -> list[str]:
    test_spec = document.get("testSpec") if isinstance(document, dict) else None
    parameters = test_spec.get("parameters") if isinstance(test_spec, dict) else None
    return [str(item.get("name")) for item in parameters if isinstance(item, dict) and item.get("name")] if isinstance(parameters, list) else []


def _numbers(body: Any) -> dict[str, float]:
    found: dict[str, float] = {}
    if isinstance(body, dict):
        for key in _COUNT_KEYS:
            value = body.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found[key] = float(value)
    return found


def compare_assertions(before: Any, after: Any) -> dict[str, Any]:
    """``{"removed": [...], "lowered": [...], "beforeCount": n, "afterCount": m}``; both lists empty means the patch is safe."""
    before_items = collect_assertions(before)
    after_items = collect_assertions(after)

    remaining = Counter(item["signature"] for item in after_items)
    removed: list[dict[str, Any]] = []
    for item in before_items:
        if remaining[item["signature"]] > 0:
            remaining[item["signature"]] -= 1
        else:
            removed.append({"phase": item["phase"], "step": item["step"], "assertion": item["kind"]})

    after_by_signature: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for item in after_items:
        after_by_signature[item["signature"]].append(item)
    lowered: list[dict[str, Any]] = []
    seen: Counter = Counter()
    for item in before_items:
        candidates = after_by_signature.get(item["signature"], [])
        position = seen[item["signature"]]
        seen[item["signature"]] += 1
        if position >= len(candidates):
            continue
        old_numbers = _numbers(item["body"])
        new_numbers = _numbers(candidates[position]["body"])
        for key, old_value in old_numbers.items():
            new_value = new_numbers.get(key)
            if new_value is None or new_value < old_value:
                lowered.append(
                    {
                        "phase": item["phase"],
                        "step": item["step"],
                        "assertion": item["kind"],
                        "field": key,
                        "from": old_value,
                        "to": new_value,
                    }
                )

    dropped_parameters = sorted(set(_parameter_names(before)) - set(_parameter_names(after)))
    for name in dropped_parameters:
        removed.append({"phase": "parameters", "step": name, "assertion": "parameter"})

    return {"removed": removed, "lowered": lowered, "beforeCount": len(before_items), "afterCount": len(after_items)}
