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

from uespec_mcp import server
from uespec_mcp.llm.types import LLMRequest, LLMResponse
from uespec_mcp.tools import generate


class FakeLLMClient:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content="",
            parsed_output={
                "spec": {
                    "apiVersion": "0.2",
                    "kind": "HUD",
                    "name": "GeneratedHud",
                    "description": "Generated from screenshot.",
                    "root": {"type": "CanvasPanel", "id": "rootPanel"},
                },
                "notes": "ok",
                "confidence": 0.8,
            },
            raw_response={},
            input_tokens=10,
            output_tokens=20,
            model="fake-vlm",
        )

    def complete_with_tools(self, request: LLMRequest, tools: list[dict[str, object]]) -> LLMResponse:
        del tools
        return self.complete(request)


class FakeBridge:
    def __init__(self) -> None:
        self.compile_calls = 0
        self.test_calls = 0

    def compile_spec(self, spec_path: Path, output_dir: str) -> dict[str, Any]:
        del spec_path
        del output_dir
        self.compile_calls += 1
        return {"ok": True, "generatedAssets": ["/Game/UESpec/Generated/WBP_GeneratedHud"], "errors": []}

    def run_test_spec(self, spec_path: Path, output_report: Path, failure_report_path: Path) -> dict[str, Any]:
        del spec_path
        del output_report
        del failure_report_path
        self.test_calls += 1
        return {
            "ok": True,
            "summary": {"total": 1, "pass": 1, "fail": 0, "skip": 0},
            "errors": [],
            "failureReport": {"summary": {"total": 1, "pass": 1, "fail": 0, "skip": 0}, "failures": []},
        }


def test_generate_spec_from_image_uses_surface_examples_and_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"not-a-real-png-but-good-enough-for-unit-tests")

    examples_root = tmp_path / "Examples"
    (examples_root / "HUDs").mkdir(parents=True)
    (examples_root / "HUDs" / "combat.json").write_text(
        json.dumps(
            {
                "apiVersion": "0.2",
                "kind": "HUD",
                "name": "CombatHud",
                "root": {"type": "HealthBar", "id": "playerHealthShell"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("UESPEC_EXAMPLES_ROOT", str(examples_root))
    monkeypatch.setattr(
        generate.surface,
        "get_support_surface",
        lambda format="json": {
            "ok": True,
            "format": format,
            "content": {
                "SupportSurfaceVersion": "test",
                "RegisteredComponents": [{"Type": "HealthBar"}, {"Type": "SkillSlot"}],
                "StyleTokens": [{"Name": "color.health.fill"}],
                "Converters": [{"Name": "BoolToVisibility"}],
                "Signals": [{"Name": "onSkillUsed"}],
            },
        },
    )
    monkeypatch.setattr(generate.validate, "validate_spec", lambda spec: {"ok": True, "stage": "schema", "errors": [], "normalizedJson": spec})
    fake_client = FakeLLMClient()
    monkeypatch.setattr(generate, "create_llm_client", lambda provider=None: fake_client)

    result = generate.generate_spec_from_image(str(image_path), target_kind="HUD", visual_prompt="health HUD", provider="mock")

    assert result["ok"] is True
    assert result["generatedSpec"]["name"] == "GeneratedHud"
    assert result["retrievedExamples"][0]["name"] == "CombatHud"
    assert result["supportSurfaceVersion"] == "test"
    assert fake_client.requests[0].images[0].media_type == "image/png"
    assert "HealthBar" in fake_client.requests[0].user_prompt
    assert "CombatHud" in fake_client.requests[0].user_prompt


def test_generate_spec_from_image_with_autofix_runs_orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fake-png")
    output_path = tmp_path / "generated-hud.json"
    test_spec_path = tmp_path / "generated-hud-test.json"
    test_spec_path.write_text(
        json.dumps(
            {
                "apiVersion": "0.2",
                "kind": "Test",
                "name": "GeneratedHudSmoke",
                "testSpec": {"target": "GeneratedHud", "phases": {"assert": []}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(generate.surface, "get_support_surface", lambda format="json": {"ok": True, "format": format, "content": {}})
    monkeypatch.setattr(generate.validate, "validate_spec", lambda spec: {"ok": True, "stage": "schema", "errors": [], "normalizedJson": spec})
    fake_client = FakeLLMClient()
    monkeypatch.setattr(generate, "create_llm_client", lambda provider=None: fake_client)
    fake_bridge = FakeBridge()
    monkeypatch.setattr(generate, "get_bridge", lambda: fake_bridge)

    result = generate.generate_spec_from_image_with_autofix(
        str(image_path),
        str(test_spec_path),
        output_path=str(output_path),
        history_dir=str(tmp_path / "PatchHistory"),
        provider="mock",
    )

    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["specPath"] == str(output_path.resolve())
    assert result["generation"]["generatedSpec"]["name"] == "GeneratedHud"
    assert result["autofix"]["ok"] is True
    assert output_path.is_file()
    assert fake_bridge.compile_calls == 1
    assert fake_bridge.test_calls == 1


def test_generate_spec_from_image_with_autofix_rejects_missing_test_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fake-png")
    output_path = tmp_path / "generated-hud.json"

    monkeypatch.setattr(generate.surface, "get_support_surface", lambda format="json": {"ok": True, "format": format, "content": {}})
    monkeypatch.setattr(generate.validate, "validate_spec", lambda spec: {"ok": True, "stage": "schema", "errors": [], "normalizedJson": spec})
    monkeypatch.setattr(generate, "create_llm_client", lambda provider=None: FakeLLMClient())

    result = generate.generate_spec_from_image_with_autofix(
        str(image_path),
        str(tmp_path / "missing-test.json"),
        output_path=str(output_path),
        provider="mock",
    )

    assert result["ok"] is False
    assert result["status"] == "test_spec_not_found"
    assert result["errors"][0]["code"] == "TEST_SPEC_NOT_FOUND"
    assert output_path.is_file()


def test_generate_spec_from_image_rejects_missing_image() -> None:
    result = generate.generate_spec_from_image("missing.png", provider="mock")

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "IMAGE_NOT_FOUND"


def test_generate_spec_from_image_rejects_bad_target_kind(tmp_path: Path) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"x")

    result = generate.generate_spec_from_image(str(image_path), target_kind="Invalid")

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "INVALID_TARGET_KIND"


def test_generate_tool_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_LLM_TOOLS", "on")
    mcp = server.build_server()
    tools = server.list_registered_tools(mcp)

    assert "generate_spec_from_image" in tools
    assert "generate_spec_from_image_with_autofix" in tools
