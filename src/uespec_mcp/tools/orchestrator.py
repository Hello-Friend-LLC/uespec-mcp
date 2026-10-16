from __future__ import annotations

from pathlib import Path
from typing import Any

from ..bridge import BridgeError, get_bridge, structured_error
from ..llm.base import LLMConfigurationError
from ..llm.factory import create_llm_client
from ..orchestrator.core import Orchestrator, load_primary_failure, propose_patch_from_failure as propose_patch_from_failure_core
from . import context, test as test_tools
from ._register import add_tool


def propose_patch_from_failure(
    spec_path: str,
    failure_report_path: str,
    screenshot_path: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    del screenshot_path
    del max_attempts

    try:
        resolved_spec = context.resolve_spec_path(spec_path)
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "errors": [context.path_error(exc)]}

    failure_report_result = test_tools.get_failure_report(failure_report_path)
    if not failure_report_result.get("ok"):
        return {
            "ok": False,
            "errors": failure_report_result.get("errors", []),
        }

    failure_report = failure_report_result.get("failureReport") or {}
    failure = load_primary_failure(failure_report)
    if failure is None:
        return {
            "ok": False,
            "errors": [
                structured_error(
                    "FAILURE_REPORT_EMPTY",
                    "Failure report does not contain any failures.",
                    path="$.failure_report_path",
                    hint="Pass a failure report generated from a failing test run.",
                )
            ],
        }

    try:
        llm_client = create_llm_client()
        proposal = propose_patch_from_failure_core(
            spec_path=resolved_spec,
            failure=failure,
            llm_client=llm_client,
        )
    except (BridgeError, LLMConfigurationError, ValueError) as exc:
        return {
            "ok": False,
            "errors": [
                structured_error(
                    "PATCH_PROPOSAL_FAILED",
                    str(exc),
                    hint="Check the failure category, LLM provider configuration, and Unreal bridge setup.",
                )
            ],
        }

    return {
        "ok": True,
        "proposal": proposal,
        "reportPath": failure_report_result["reportPath"],
    }


def run_with_autofix(
    spec_path: str,
    test_spec_path: str,
    max_attempts: int = 3,
) -> dict[str, Any]:
    try:
        resolved_spec = context.resolve_spec_path(spec_path)
        resolved_test_spec = context.resolve_spec_path(test_spec_path)
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "errors": [context.path_error(exc)]}

    try:
        orchestrator = Orchestrator(
            get_bridge(),
            create_llm_client(),
            max_attempts=max_attempts,
        )
        return orchestrator.run_with_autofix(resolved_spec, resolved_test_spec)
    except (BridgeError, LLMConfigurationError, ValueError) as exc:
        return {
            "ok": False,
            "errors": [
                structured_error(
                    "AUTOFIX_FAILED",
                    str(exc),
                    hint="Check bridge configuration, LLM provider configuration, and supplied spec paths.",
                )
            ],
        }


def register(mcp: Any) -> None:
    add_tool(mcp, "propose_patch_from_failure", "Propose a JSON Patch for a spec from a failure report, using the configured LLM adapter.", propose_patch_from_failure)
    add_tool(mcp, "run_with_autofix", "Run the closed-loop autofix orchestrator (LLM adapter) against a spec and its test spec.", run_with_autofix, compact=True)
