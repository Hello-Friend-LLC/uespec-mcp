from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from uespec_mcp.llm.base import LLMClient
from uespec_mcp.llm.types import LLMRequest, LLMResponse
from uespec_mcp.orchestrator.core import Orchestrator
from uespec_mcp.tools import orchestrator as orchestrator_tools
from uespec_mcp.tools import surface as surface_tool


class SequenceLLMClient(LLMClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_history: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.call_history.append(request)
        if not self._responses:
            raise RuntimeError("No more mock LLM responses are available.")
        return self._responses.pop(0)

    def complete_with_tools(self, request: LLMRequest, tools: list[dict[str, Any]]) -> LLMResponse:
        del tools
        return self.complete(request)


class FakeBridge:
    def __init__(
        self,
        ui_spec_path: Path,
        *,
        compile_behavior: Any | None = None,
        test_behavior: Any | None = None,
    ) -> None:
        self.ui_spec_path = ui_spec_path
        self.compile_behavior = compile_behavior
        self.test_behavior = test_behavior
        self.compile_calls = 0
        self.test_calls = 0
        self.last_test_spec_path: Path | None = None
        self.last_test_spec_target: str | None = None

    def compile_spec(self, spec_path: Path, output_dir: str) -> dict[str, Any]:
        del spec_path
        del output_dir
        self.compile_calls += 1
        if callable(self.compile_behavior):
            return self.compile_behavior()
        return self.compile_behavior or {"ok": True, "generatedAssets": ["/Game/UESpec/Generated/WBP_Test"], "errors": []}

    def run_test_spec(self, spec_path: Path, output_report: Path, failure_report_path: Path) -> dict[str, Any]:
        del output_report
        del failure_report_path
        self.test_calls += 1
        self.last_test_spec_path = spec_path
        try:
            payload = json.loads(spec_path.read_text(encoding="utf-8"))
            test_spec = payload.get("testSpec") if isinstance(payload, dict) else None
            if isinstance(test_spec, dict):
                self.last_test_spec_target = str(test_spec.get("target") or "")
        except Exception:
            self.last_test_spec_target = None
        if callable(self.test_behavior):
            return self.test_behavior()
        return self.test_behavior or {
            "ok": True,
            "summary": {"total": 1, "pass": 1, "fail": 0, "skip": 0},
            "errors": [],
            "failureReport": {"summary": {"total": 1, "pass": 1, "fail": 0, "skip": 0}, "failures": []},
        }


@pytest.fixture(autouse=True)
def empty_support_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surface_tool, "_ensure_surface_json", lambda force=False: {})


def test_orchestrator_fixes_compile_error_in_one_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_path = _write_ui_spec(
        tmp_path / "broken-hud.json",
        {
            "apiVersion": "0.2",
            "kind": "HUD",
            "name": "BrokenHUD",
            "viewModel": {"class": "VM_SkillBar", "methods": [{"name": "UseSkill"}]},
            "root": {
                "type": "CanvasPanel",
                "id": "rootPanel",
                "children": [{"type": "Button", "id": "slotQ", "actions": [{"on": "Clicked", "do": "vm.UseSkil(0)"}]}],
            },
        },
    )
    test_spec_path = _write_test_spec(tmp_path / "broken-hud-test.json")

    monkeypatch.setattr(
        orchestrator_tools.context,
        "resolve_spec_path",
        lambda spec_path_value, must_exist=True: Path(spec_path_value).expanduser().resolve(),
    )

    def fake_validate(spec_content: str) -> dict[str, Any]:
        if "UseSkil(0)" in spec_content:
            return {
                "ok": False,
                "errors": [
                    {
                        "code": "VM_METHOD_NOT_DECLARED",
                        "path": "$.root.children[0].actions[0].do",
                        "message": "ViewModel method 'UseSkil' is not declared.",
                        "hint": "",
                        "nearestDeclared": ["UseSkill"],
                    }
                ],
                "normalizedJson": spec_content,
            }
        return {"ok": True, "errors": [], "normalizedJson": spec_content}

    monkeypatch.setattr("uespec_mcp.orchestrator.core.validate_tool.validate_spec", fake_validate)

    llm_client = SequenceLLMClient(
        [
            LLMResponse(
                content='[{"op":"replace","path":"/root/children/0/actions/0/do","value":"vm.UseSkill(0)"}]',
                parsed_output=[{"op": "replace", "path": "/root/children/0/actions/0/do", "value": "vm.UseSkill(0)"}],
                raw_response={"provider": "mock"},
                input_tokens=12,
                output_tokens=8,
                model="mock-llm",
            )
        ]
    )
    bridge = FakeBridge(spec_path)
    orchestrator = Orchestrator(
        bridge,
        llm_client,
        max_attempts=3,
        history_dir=tmp_path / "Saved" / "UESpec" / "PatchHistory",
        support_surface={},
    )

    result = orchestrator.run_with_autofix(spec_path, test_spec_path)

    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["attemptCount"] == 1
    assert "UseSkill(0)" in spec_path.read_text(encoding="utf-8")
    assert bridge.compile_calls == 1
    assert bridge.test_calls == 1


def test_orchestrator_retries_on_partial_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_path = _write_ui_spec(
        tmp_path / "retry-hud.json",
        {
            "apiVersion": "0.2",
            "kind": "HUD",
            "name": "RetryHUD",
            "meta": {"stage": "WrongOne"},
            "root": {"type": "CanvasPanel", "id": "rootPanel"},
        },
    )
    test_spec_path = _write_test_spec(tmp_path / "retry-hud-test.json")

    def fake_validate(spec_content: str) -> dict[str, Any]:
        if '"stage": "WrongOne"' in spec_content:
            return {
                "ok": False,
                "errors": [{"code": "UNKNOWN_STAGE_ONE", "path": "$.meta.stage", "message": "Stage one is invalid.", "hint": "", "nearestDeclared": []}],
                "normalizedJson": spec_content,
            }
        if '"stage": "WrongTwo"' in spec_content:
            return {
                "ok": False,
                "errors": [{"code": "UNKNOWN_STAGE_TWO", "path": "$.meta.stage", "message": "Stage two is invalid.", "hint": "", "nearestDeclared": []}],
                "normalizedJson": spec_content,
            }
        return {"ok": True, "errors": [], "normalizedJson": spec_content}

    monkeypatch.setattr("uespec_mcp.orchestrator.core.validate_tool.validate_spec", fake_validate)

    llm_client = SequenceLLMClient(
        [
            LLMResponse(
                content='[{"op":"replace","path":"/meta/stage","value":"WrongTwo"}]',
                parsed_output=[{"op": "replace", "path": "/meta/stage", "value": "WrongTwo"}],
                raw_response={"provider": "mock"},
                model="mock-llm",
            ),
            LLMResponse(
                content='[{"op":"replace","path":"/meta/stage","value":"Fixed"}]',
                parsed_output=[{"op": "replace", "path": "/meta/stage", "value": "Fixed"}],
                raw_response={"provider": "mock"},
                model="mock-llm",
            ),
        ]
    )
    orchestrator = Orchestrator(
        FakeBridge(spec_path),
        llm_client,
        max_attempts=3,
        history_dir=tmp_path / "Saved" / "UESpec" / "PatchHistory",
        support_surface={},
    )

    result = orchestrator.run_with_autofix(spec_path, test_spec_path)

    assert result["ok"] is True
    assert result["attemptCount"] == 2
    assert '"stage": "Fixed"' in spec_path.read_text(encoding="utf-8")
    assert len(llm_client.call_history) == 2


def test_orchestrator_stops_at_max_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_path = _write_ui_spec(
        tmp_path / "max-attempts.json",
        {
            "apiVersion": "0.2",
            "kind": "HUD",
            "name": "MaxAttemptsHUD",
            "meta": {},
            "root": {"type": "CanvasPanel", "id": "rootPanel"},
        },
    )
    test_spec_path = _write_test_spec(tmp_path / "max-attempts-test.json")

    monkeypatch.setattr(
        "uespec_mcp.orchestrator.core.validate_tool.validate_spec",
        lambda spec_content: {
            "ok": False,
            "errors": [{"code": "UNKNOWN_CONVERTER", "path": "$.meta", "message": "Still broken.", "hint": "", "nearestDeclared": []}],
            "normalizedJson": spec_content,
        },
    )

    llm_client = SequenceLLMClient(
        [
            LLMResponse(
                content='[{"op":"add","path":"/meta/firstAttemptNote","value":"patch-attempt-one-with-longer-text"}]',
                parsed_output=[{"op": "add", "path": "/meta/firstAttemptNote", "value": "patch-attempt-one-with-longer-text"}],
                raw_response={"provider": "mock"},
                model="mock-llm",
            ),
            LLMResponse(
                content='[{"op":"add","path":"/meta/secondAttemptMarker","value":"patch-attempt-two-with-different-longer-text"}]',
                parsed_output=[{"op": "add", "path": "/meta/secondAttemptMarker", "value": "patch-attempt-two-with-different-longer-text"}],
                raw_response={"provider": "mock"},
                model="mock-llm",
            ),
        ]
    )
    orchestrator = Orchestrator(
        FakeBridge(spec_path),
        llm_client,
        max_attempts=2,
        history_dir=tmp_path / "Saved" / "UESpec" / "PatchHistory",
        support_surface={},
    )

    result = orchestrator.run_with_autofix(spec_path, test_spec_path)

    assert result["ok"] is False
    assert result["status"] == "max_attempts_exceeded"
    assert result["attemptCount"] == 2
    assert "secondAttemptMarker" in spec_path.read_text(encoding="utf-8")


def test_orchestrator_detects_stalled_patches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_path = _write_ui_spec(
        tmp_path / "stalled.json",
        {
            "apiVersion": "0.2",
            "kind": "HUD",
            "name": "StalledHUD",
            "meta": {"attempt": ""},
            "root": {"type": "CanvasPanel", "id": "rootPanel"},
        },
    )
    test_spec_path = _write_test_spec(tmp_path / "stalled-test.json")

    monkeypatch.setattr(
        "uespec_mcp.orchestrator.core.validate_tool.validate_spec",
        lambda spec_content: {
            "ok": False,
            "errors": [{"code": "UNKNOWN_CONVERTER", "path": "$.meta", "message": "Still broken.", "hint": "", "nearestDeclared": []}],
            "normalizedJson": spec_content,
        },
    )

    llm_client = SequenceLLMClient(
        [
            LLMResponse(
                content='[{"op":"replace","path":"/meta/attempt","value":"alpha"}]',
                parsed_output=[{"op": "replace", "path": "/meta/attempt", "value": "alpha"}],
                raw_response={"provider": "mock"},
                model="mock-llm",
            ),
            LLMResponse(
                content='[{"op":"replace","path":"/meta/attempt","value":"alphb"}]',
                parsed_output=[{"op": "replace", "path": "/meta/attempt", "value": "alphb"}],
                raw_response={"provider": "mock"},
                model="mock-llm",
            ),
        ]
    )
    orchestrator = Orchestrator(
        FakeBridge(spec_path),
        llm_client,
        max_attempts=3,
        history_dir=tmp_path / "Saved" / "UESpec" / "PatchHistory",
        support_surface={},
    )

    result = orchestrator.run_with_autofix(spec_path, test_spec_path)

    assert result["ok"] is False
    assert result["status"] == "stalled"
    assert result["attemptCount"] == 2
    assert '"attempt": "alpha"' in spec_path.read_text(encoding="utf-8")


def test_orchestrator_preserves_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_path = _write_ui_spec(
        tmp_path / "history.json",
        {
            "apiVersion": "0.2",
            "kind": "HUD",
            "name": "HistoryHUD",
            "viewModel": {"class": "VM_SkillBar", "methods": [{"name": "UseSkill"}]},
            "root": {
                "type": "CanvasPanel",
                "id": "rootPanel",
                "children": [{"type": "Button", "id": "slotQ", "actions": [{"on": "Clicked", "do": "vm.UseSkil(0)"}]}],
            },
        },
    )
    test_spec_path = _write_test_spec(tmp_path / "history-test.json")

    def fake_validate(spec_content: str) -> dict[str, Any]:
        if "UseSkil(0)" in spec_content:
            return {
                "ok": False,
                "errors": [{"code": "VM_METHOD_NOT_DECLARED", "path": "$.root.children[0].actions[0].do", "message": "Wrong VM method.", "hint": "", "nearestDeclared": ["UseSkill"]}],
                "normalizedJson": spec_content,
            }
        return {"ok": True, "errors": [], "normalizedJson": spec_content}

    monkeypatch.setattr("uespec_mcp.orchestrator.core.validate_tool.validate_spec", fake_validate)

    orchestrator = Orchestrator(
        FakeBridge(spec_path),
        SequenceLLMClient(
            [
                LLMResponse(
                    content='[{"op":"replace","path":"/root/children/0/actions/0/do","value":"vm.UseSkill(0)"}]',
                    parsed_output=[{"op": "replace", "path": "/root/children/0/actions/0/do", "value": "vm.UseSkill(0)"}],
                    raw_response={"provider": "mock"},
                    model="mock-llm",
                )
            ]
        ),
        max_attempts=3,
        history_dir=tmp_path / "Saved" / "UESpec" / "PatchHistory",
        support_surface={},
    )

    result = orchestrator.run_with_autofix(spec_path, test_spec_path)
    history_dir = Path(result["historyDir"])

    assert result["ok"] is True
    assert (history_dir / "final-result.json").is_file()
    attempt_dir = history_dir / "attempt-01"
    for file_name in (
        "failure-report.json",
        "llm-request.json",
        "llm-response.json",
        "proposed-patch.json",
        "applied-spec.json",
        "test-result.json",
    ):
        assert (attempt_dir / file_name).is_file(), file_name


def test_orchestrator_rolls_back_on_regression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_payload = {
        "apiVersion": "0.2",
        "kind": "HUD",
        "name": "RegressionHUD",
        "viewModel": {"class": "VM_SkillBar", "methods": [{"name": "UseSkill"}]},
        "root": {
            "type": "CanvasPanel",
            "id": "rootPanel",
            "children": [{"type": "Button", "id": "slotQ", "actions": [{"on": "Clicked", "do": "vm.UseSkil(0)"}]}],
        },
    }
    spec_path = _write_ui_spec(tmp_path / "regression.json", original_payload)
    test_spec_path = _write_test_spec(tmp_path / "regression-test.json")
    original_text = spec_path.read_text(encoding="utf-8")

    def fake_validate(spec_content: str) -> dict[str, Any]:
        if "UseSkil(0)" in spec_content:
            return {
                "ok": False,
                "errors": [{"code": "VM_METHOD_NOT_DECLARED", "path": "$.root.children[0].actions[0].do", "message": "Wrong VM method.", "hint": "", "nearestDeclared": ["UseSkill"]}],
                "normalizedJson": spec_content,
            }
        return {"ok": True, "errors": [], "normalizedJson": spec_content}

    monkeypatch.setattr("uespec_mcp.orchestrator.core.validate_tool.validate_spec", fake_validate)

    def regressing_test_behavior() -> dict[str, Any]:
        return {
            "ok": False,
            "summary": {"total": 2, "pass": 0, "fail": 2, "skip": 0},
            "errors": [],
            "failureReport": {
                "summary": {"total": 2, "pass": 0, "fail": 2, "skip": 0},
                "failures": [
                    {"category": "ASSERTION_FAILED", "testStep": "assert one", "failureMessage": "Mismatch one."},
                    {"category": "TIMEOUT", "testStep": "assert two", "failureMessage": "Timeout two."},
                ],
            },
        }

    orchestrator = Orchestrator(
        FakeBridge(spec_path, test_behavior=regressing_test_behavior),
        SequenceLLMClient(
            [
                LLMResponse(
                    content='[{"op":"replace","path":"/root/children/0/actions/0/do","value":"vm.UseSkill(0)"}]',
                    parsed_output=[{"op": "replace", "path": "/root/children/0/actions/0/do", "value": "vm.UseSkill(0)"}],
                    raw_response={"provider": "mock"},
                    model="mock-llm",
                )
            ]
        ),
        max_attempts=3,
        history_dir=tmp_path / "Saved" / "UESpec" / "PatchHistory",
        support_surface={},
    )

    result = orchestrator.run_with_autofix(spec_path, test_spec_path)

    assert result["ok"] is False
    assert result["status"] == "regression_detected"
    assert spec_path.read_text(encoding="utf-8") == original_text


def test_orchestrator_runs_tests_against_latest_generated_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_path = _write_ui_spec(
        tmp_path / "generated-asset.json",
        {
            "apiVersion": "0.2",
            "kind": "HUD",
            "name": "GeneratedAssetHUD",
            "root": {"type": "CanvasPanel", "id": "rootPanel"},
        },
    )
    test_spec_path = _write_test_spec(tmp_path / "generated-asset-test.json")
    original_test_spec = test_spec_path.read_text(encoding="utf-8")

    monkeypatch.setattr(
        "uespec_mcp.orchestrator.core.validate_tool.validate_spec",
        lambda spec_content: {"ok": True, "errors": [], "normalizedJson": spec_content},
    )

    bridge = FakeBridge(
        spec_path,
        compile_behavior={"ok": True, "generatedAssets": ["/Game/UESpec/Generated/WBP_GeneratedAssetHUD14"], "errors": []},
    )
    orchestrator = Orchestrator(
        bridge,
        SequenceLLMClient([]),
        max_attempts=1,
        history_dir=tmp_path / "Saved" / "UESpec" / "PatchHistory",
        support_surface={},
    )

    result = orchestrator.run_with_autofix(spec_path, test_spec_path)

    assert result["ok"] is True
    assert bridge.last_test_spec_target == "/Game/UESpec/Generated/WBP_GeneratedAssetHUD14"
    assert bridge.last_test_spec_path is not None
    assert bridge.last_test_spec_path != test_spec_path
    assert bridge.last_test_spec_path.name.endswith(".runtime.json")
    assert test_spec_path.read_text(encoding="utf-8") == original_test_spec


def test_orchestrator_tools_register_on_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_LLM_TOOLS", "on")
    monkeypatch.setattr(orchestrator_tools.context, "resolve_spec_path", lambda value, must_exist=True: Path(value).expanduser().resolve())
    server = __import__("uespec_mcp.server", fromlist=["build_server", "list_registered_tools"])
    mcp = server.build_server()
    tools = server.list_registered_tools(mcp)

    assert "propose_patch_from_failure" in tools
    assert "run_with_autofix" in tools


def _write_ui_spec(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_test_spec(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "apiVersion": "0.2",
                "kind": "Test",
                "name": "SmokeTest",
                "testSpec": {"target": "BrokenHUD", "phases": {"assert": []}},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
