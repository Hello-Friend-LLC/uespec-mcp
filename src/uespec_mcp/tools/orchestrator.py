from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..bridge import BridgeError, get_bridge, structured_error
from ..llm.base import LLMConfigurationError
from ..llm.factory import create_llm_client
from ..orchestrator.core import Orchestrator, load_primary_failure, propose_patch_from_failure as propose_patch_from_failure_core
from . import context, repair, test as test_tools
from ._register import add_tool


def propose_patch_from_failure(
    spec_path: str,
    failure_report_path: str,
    screenshot_path: str | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    # Upstream silently ignored both arguments; a caller that sets one expects an effect, so say it has none.
    for name, value in (("screenshot_path", screenshot_path), ("max_attempts", max_attempts)):
        if value is not None:
            return {
                "ok": False,
                "errors": [
                    structured_error(
                        "UNSUPPORTED_ARGUMENT",
                        f"propose_patch_from_failure does not use {name}: it returns one proposal from the failure report.",
                        path=f"$.{name}",
                        hint="Omit it. Attempt limits apply to autofix_spec and apply_spec_patch.",
                    )
                ],
            }

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
    max_attempts: int | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    try:
        resolved_spec = context.resolve_spec_path(spec_path)
        resolved_test_spec = context.resolve_spec_path(test_spec_path)
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "errors": [context.path_error(exc)]}

    try:
        orchestrator = Orchestrator(
            get_bridge(),
            create_llm_client(provider),
            max_attempts=max_attempts if max_attempts is not None else repair.configured_max_attempts(),
            history_dir=repair.history_root(resolved_spec),
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


def autofix_spec(
    spec_path: str,
    test_spec_path: str,
    provider: str = "none",
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Explicit provider loop (validate, compile, test, LLM patch, repeat); the default repair flow is host-driven (apply_spec_patch)."""
    chosen = (provider or "none").strip().lower()
    if chosen == "none":
        chosen = os.getenv("UESPEC_LLM_PROVIDER", "").strip().lower() or "none"
    if chosen == "none":
        return {
            "ok": False,
            "code": "PROVIDER_NOT_CONFIGURED",
            "errors": [
                structured_error(
                    "PROVIDER_NOT_CONFIGURED",
                    "autofix_spec needs an LLM provider: pass provider=claude|openai|mock, or set UESPEC_LLM_PROVIDER.",
                    path="$.provider",
                    hint="claude reads ANTHROPIC_API_KEY, openai reads OPENAI_API_KEY, mock reads UESPEC_LLM_MOCK_FIXTURES. "
                    "Without a provider, repair from the host session: get_failure_report, apply_spec_patch, rollback_spec.",
                )
            ],
        }
    return run_with_autofix(spec_path, test_spec_path, max_attempts=max_attempts, provider=chosen)


def register(mcp: Any) -> None:
    add_tool(mcp, "propose_patch_from_failure", "Propose a JSON Patch for a spec from a failure report, using the configured LLM adapter.", propose_patch_from_failure)
    add_tool(
        mcp,
        "autofix_spec",
        "Closed-loop autofix with an LLM provider (provider=claude|openai|mock, default none -> PROVIDER_NOT_CONFIGURED naming UESPEC_LLM_PROVIDER). Prefer the host-driven flow: get_failure_report, apply_spec_patch, rollback_spec.",
        autofix_spec,
        compact=True,
    )
