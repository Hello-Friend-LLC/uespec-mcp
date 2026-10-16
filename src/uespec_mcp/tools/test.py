from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bridge import BridgeError, bridge_error_fields, get_bridge, structured_error
from . import _envelope, context
from ._register import add_tool


def _default_output_report_path(spec_path: Path) -> Path:
    return (spec_path.parent / "Saved" / "UESpec" / "TestReports" / f"{spec_path.stem}.junit.xml").resolve()


def _default_failure_report_path(spec_path: Path) -> Path:
    return (spec_path.parent / "Saved" / "UESpec" / "TestReports" / f"{spec_path.stem}.failure-report.json").resolve()


def run_test_spec(
    spec_path: str | list[str],
    output_report: str | None = None,
    failure_report_path: str | None = None,
) -> dict[str, Any]:
    if isinstance(spec_path, (list, tuple)):
        items = context.path_list(spec_path)
        if len(items) > 1 and (output_report or failure_report_path):
            return {
                "ok": False,
                "summary": {},
                "errors": [
                    structured_error(
                        "PATH_ARRAY_CONFLICT",
                        "output_report/failure_report_path name one file; they cannot be combined with several spec paths.",
                        path="$.spec_path",
                        hint="Omit output_report and failure_report_path to get per-spec defaults under Saved/UESpec/TestReports.",
                    )
                ],
            }
        return _envelope.aggregate(
            "run_test_spec", items, [_run_one(item, output_report, failure_report_path) for item in items]
        )
    return _run_one(spec_path, output_report, failure_report_path)


def _run_one(spec_path: str | None, output_report: str | None, failure_report_path: str | None) -> dict[str, Any]:
    try:
        resolved_spec = context.resolve_spec_path(spec_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "ok": False,
            "specPath": spec_path,
            "summary": {},
            "errors": [context.path_error(exc)],
        }

    resolved_report = context.expand_path(output_report) if output_report else _default_output_report_path(resolved_spec)
    resolved_failure_report = (
        context.expand_path(failure_report_path) if failure_report_path else _default_failure_report_path(resolved_spec)
    )

    try:
        return get_bridge().run_test_spec(resolved_spec, resolved_report, resolved_failure_report)
    except BridgeError as exc:
        return {
            **bridge_error_fields(exc),
            "ok": False,
            "specPath": str(resolved_spec),
            "outputReport": str(resolved_report),
            "failureReportPath": str(resolved_failure_report),
            "summary": {},
            "failureReport": {},
            "errors": [
                structured_error(
                    exc.code,
                    str(exc),
                    hint="Configure Unreal bridge environment variables before running test specs.",
                )
            ],
        }


def get_failure_report(report_path: str) -> dict[str, Any]:
    resolved_report = context.expand_path(report_path)
    if not resolved_report.is_file():
        return {
            "ok": False,
            "reportPath": str(resolved_report),
            "errors": [
                structured_error(
                    "FAILURE_REPORT_NOT_FOUND",
                    f"Failure report '{resolved_report}' does not exist.",
                    path="$.report_path",
                    hint="Pass an existing *.failure-report.json file path.",
                )
            ],
        }

    try:
        payload = json.loads(resolved_report.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "reportPath": str(resolved_report),
            "errors": [
                structured_error(
                    "INVALID_FAILURE_REPORT_JSON",
                    str(exc),
                    path="$.report_path",
                    hint="Ensure the report was produced by UESpecRunTests with -JsonReport.",
                )
            ],
        }

    return {
        "ok": True,
        "reportPath": str(resolved_report),
        "failureReport": payload,
        "errors": [],
    }


def register(mcp: Any) -> None:
    add_tool(
        mcp,
        "run_test_spec",
        "Run UISpec test document(s). spec_path is a path or an array of paths. Returns a summary plus absolute JUnit and failure-report paths in artifacts[].",
        run_test_spec,
        compact=True,
    )
