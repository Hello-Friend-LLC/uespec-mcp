from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bridge import BridgeError, bridge_error_fields, get_bridge, structured_error
from . import context


def _default_output_report_path(spec_path: Path) -> Path:
    return (spec_path.parent / "Saved" / "UESpec" / "TestReports" / f"{spec_path.stem}.junit.xml").resolve()


def _default_failure_report_path(spec_path: Path) -> Path:
    return (spec_path.parent / "Saved" / "UESpec" / "TestReports" / f"{spec_path.stem}.failure-report.json").resolve()


def run_test_spec(
    spec_path: str | None = None,
    output_report: str | None = None,
    failure_report_path: str | None = None,
) -> dict[str, Any]:
    try:
        resolved_spec = context.resolve_spec_path(spec_path)
    except (FileNotFoundError, ValueError) as exc:
        code = "ACTIVE_SPEC_REQUIRED" if isinstance(exc, ValueError) else "SPEC_NOT_FOUND"
        return {
            "ok": False,
            "summary": {},
            "errors": [
                structured_error(
                    code,
                    str(exc),
                    path="$.spec_path",
                    hint="Pass spec_path explicitly or call set_active_spec first.",
                )
            ],
        }

    resolved_report = Path(output_report).expanduser().resolve() if output_report else _default_output_report_path(resolved_spec)
    resolved_failure_report = (
        Path(failure_report_path).expanduser().resolve()
        if failure_report_path
        else _default_failure_report_path(resolved_spec)
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
    resolved_report = Path(report_path).expanduser().resolve()
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
    mcp.tool(name="run_test_spec", description="Run a UISpec test document and return a report path plus summary.")(run_test_spec)
    mcp.tool(name="get_failure_report", description="Read a UESpec failure-report.json file and return its parsed payload.")(get_failure_report)
