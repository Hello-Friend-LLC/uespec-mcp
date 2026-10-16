from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from uespec_mcp import server
from uespec_mcp.tools import compile as compile_tools
from uespec_mcp.tools import context, surface, test as test_tools, validate


@pytest.fixture(autouse=True)
def isolated_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No ambient project: caches and relative-path resolution must not read the developer's UESPEC_* environment.
    for name in ("UESPEC_PROJECT_ROOT", "UESPEC_UPROJECT_PATH", "UESPEC_BRIDGE_MODE", "UESPEC_MCP_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_get_support_surface_no_ue(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBridge:
        def get_support_surface(self, format: str = "markdown") -> dict[str, object]:
            return {"ok": True, "format": format, "content": {"Signals": [], "Converters": []}, "command": []}

    monkeypatch.setattr(surface, "get_bridge", lambda: FakeBridge())
    mcp = server.build_server()
    tools = server.list_registered_tools(mcp)

    assert "get_support_surface" in tools
    result = tools["get_support_surface"]("json")

    assert result["ok"] is True
    assert result["format"] == "json"
    assert result["content"] == {"Signals": [], "Converters": []}


def test_validate_bad_json_schema_layer() -> None:
    result = validate.validate_spec(json.dumps({"apiVersion": "0.2", "kind": "HUD"}))

    assert result["ok"] is False
    assert result["stage"] == "schema"
    assert any(error["code"] == "SCHEMA_VALIDATION_FAILED" for error in result["errors"])


def test_list_existing_specs(tmp_path: Path) -> None:
    valid_spec = tmp_path / "hud-skill-bar.json"
    valid_spec.write_text(
        json.dumps(
            {
                "apiVersion": "0.2",
                "kind": "HUD",
                "name": "SkillBarHUD",
                "description": "Example HUD spec.",
                "root": {"type": "CanvasPanel", "id": "rootPanel"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "not-a-spec.json").write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    result = compile_tools.list_existing_specs(str(tmp_path))

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["items"][0]["name"] == "SkillBarHUD"
    assert result["items"][0]["kind"] == "HUD"


def _write_hud(path: Path, name: str) -> Path:
    path.write_text(
        json.dumps({"apiVersion": "0.2", "kind": "HUD", "name": name, "root": {"type": "CanvasPanel", "id": "rootPanel"}}),
        encoding="utf-8",
    )
    return path


def test_no_process_global_active_spec(tmp_path: Path) -> None:
    tools = server.list_registered_tools(server.build_server())

    assert "set_active_spec" not in tools and "get_active_spec" not in tools
    assert not hasattr(context, "set_active_spec") and not hasattr(context, "_ACTIVE_SPEC")
    for tool_name, call in (("compile_spec_to_wbp", lambda: compile_tools.compile_spec_to_wbp("")), ("run_test_spec", lambda: test_tools.run_test_spec(""))):
        result = call()
        assert result["ok"] is False, tool_name
        assert result["errors"][0]["code"] == "SPEC_PATH_REQUIRED", tool_name


def test_interleaved_clients_never_see_each_other_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two MCP clients share one backend process; a spec path sent by one must never be used for the other's call."""
    import asyncio

    from fastmcp import Client

    spec_a = _write_hud(tmp_path / "client-a.json", "ClientAHUD")
    spec_b = _write_hud(tmp_path / "client-b.json", "ClientBHUD")
    seen: list[str] = []

    class FakeBridge:
        def compile_spec(self, spec_path_arg: Path, output_dir_arg: str) -> dict[str, object]:
            seen.append(Path(spec_path_arg).name)
            return {"ok": True, "generatedAssets": [f"/Game/UI/WBP_{Path(spec_path_arg).stem}"], "errors": []}

    monkeypatch.setattr(compile_tools, "get_bridge", lambda: FakeBridge())
    mcp = server.build_server()

    async def run() -> list[dict[str, object]]:
        async with Client(mcp) as client_a, Client(mcp) as client_b:
            async def call(client: Client, arguments: dict[str, object]) -> dict[str, object]:
                reply = await client.call_tool("compile_spec_to_wbp", arguments, raise_on_error=False)
                return json.loads(reply.content[0].text)

            first_a = await call(client_a, {"spec_path": str(spec_a)})
            first_b = await call(client_b, {"spec_path": str(spec_b)})
            # Neither client names a spec now: nothing may fall back to the other client's last path.
            bare_a = await call(client_a, {"spec_path": ""})
            bare_b = await call(client_b, {"spec_path": ""})
            second_a = await call(client_a, {"spec_path": [str(spec_a)]})
            return [first_a, first_b, bare_a, bare_b, second_a]

    first_a, first_b, bare_a, bare_b, second_a = asyncio.run(run())

    assert first_a["generatedAssets"] == ["/Game/UI/WBP_client-a"]
    assert first_b["generatedAssets"] == ["/Game/UI/WBP_client-b"]
    assert bare_a["errors"][0]["code"] == "SPEC_PATH_REQUIRED"
    assert bare_b["errors"][0]["code"] == "SPEC_PATH_REQUIRED"
    assert second_a["ok"] is True and second_a["count"] == 1
    assert seen == ["client-a.json", "client-b.json", "client-a.json"]


def test_array_spec_paths_resolve_on_the_executing_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    specs = [_write_hud(tmp_path / f"hud-{index}.json", f"Hud{index}") for index in range(2)]
    seen: list[str] = []

    class FakeBridge:
        def compile_spec(self, spec_path_arg: Path, output_dir_arg: str) -> dict[str, object]:
            seen.append(str(spec_path_arg))
            return {
                "ok": True,
                "generatedAssets": [f"/Game/UI/WBP_{Path(spec_path_arg).stem}"],
                "outputReport": "Saved/UESpec/TestReports/relative.junit.xml",
                "artifacts": [{"kind": "asset", "path": "/Game/UI/x"}],
            }

    monkeypatch.setattr(compile_tools, "get_bridge", lambda: FakeBridge())
    result = compile_tools.compile_spec_to_wbp([str(specs[0]), str(specs[1]), str(tmp_path / "missing.json")])

    assert seen == [str(path.resolve()) for path in specs]
    assert result["count"] == 3 and result["failed"] == 1 and result["ok"] is False
    assert result["results"][2]["errors"][0]["code"] == "SPEC_NOT_FOUND"


def test_artifacts_are_absolute_file_paths(tmp_path: Path) -> None:
    from uespec_mcp import bridge as bridge_module

    config = bridge_module.BridgeConfig(project_root=tmp_path)
    artifacts = bridge_module._collect_artifacts(
        {
            "generatedAssets": ["/Game/UI/WBP_A"],
            "outputReport": "Saved/UESpec/TestReports/a.junit.xml",
            "failureReportPath": str(tmp_path / "Saved" / "b.json"),
            "pngPath": "Saved/WidgetPreviews/a.png",
        },
        config,
    )

    by_kind = {entry["kind"]: entry["path"] for entry in artifacts}
    assert by_kind["asset"] == "/Game/UI/WBP_A"
    for kind in ("junit", "failure-report", "png"):
        assert Path(by_kind[kind]).is_absolute(), kind
    assert Path(by_kind["png"]) == (tmp_path / "Saved" / "WidgetPreviews" / "a.png").resolve()


def test_compile_spec_to_wbp_takes_explicit_spec_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_path = tmp_path / "compile-me.json"
    spec_path.write_text(
        json.dumps(
            {
                "apiVersion": "0.2",
                "kind": "HUD",
                "name": "CompileMeHUD",
                "root": {"type": "CanvasPanel", "id": "rootPanel"},
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, str] = {}

    class FakeBridge:
        def compile_spec(self, spec_path_arg: Path, output_dir_arg: str) -> dict[str, object]:
            captured["spec_path"] = str(spec_path_arg)
            captured["output_dir"] = output_dir_arg
            return {"ok": True, "generatedAssets": ["/Game/UI/WBP_CompileMeHUD"], "errors": []}

    monkeypatch.setattr(compile_tools, "get_bridge", lambda: FakeBridge())
    result = compile_tools.compile_spec_to_wbp(str(spec_path), output_dir="/Game/UI/Generated")

    assert result["ok"] is True
    assert captured["spec_path"] == str(spec_path.resolve())
    assert captured["output_dir"] == "/Game/UI/Generated"


def test_run_test_spec_uses_default_report_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_path = tmp_path / "skill-bar-test.json"
    spec_path.write_text(
        json.dumps(
            {
                "apiVersion": "0.2",
                "kind": "Test",
                "name": "SkillBarTest",
                "testSpec": {"target": "SkillBarHUD", "phases": {"assert": []}},
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, str] = {}

    class FakeBridge:
        def run_test_spec(self, spec_path_arg: Path, output_report_arg: Path, failure_report_arg: Path) -> dict[str, object]:
            captured["spec_path"] = str(spec_path_arg)
            captured["output_report"] = str(output_report_arg)
            captured["failure_report"] = str(failure_report_arg)
            return {"ok": True, "summary": {"passed": 1, "failed": 0}, "errors": [], "failureReport": {}}

    monkeypatch.setattr(test_tools, "get_bridge", lambda: FakeBridge())
    result = test_tools.run_test_spec(str(spec_path))

    assert result["ok"] is True
    assert captured["spec_path"] == str(spec_path.resolve())
    expected_report = Path("Saved") / "UESpec" / "TestReports" / "skill-bar-test.junit.xml"
    expected_failure = Path("Saved") / "UESpec" / "TestReports" / "skill-bar-test.failure-report.json"
    assert captured["output_report"].endswith(str(expected_report))
    assert captured["failure_report"].endswith(str(expected_failure))


def test_bridge_parses_failure_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from uespec_mcp.bridge import BridgeClient, BridgeConfig

    spec_path = tmp_path / "skill-bar-test.json"
    spec_path.write_text(
        json.dumps(
            {
                "apiVersion": "0.2",
                "kind": "Test",
                "name": "SkillBarTest",
                "testSpec": {"target": "SkillBarHUD", "phases": {"assert": []}},
            }
        ),
        encoding="utf-8",
    )

    report_path = tmp_path / "Saved" / "UESpec" / "TestReports" / "skill-bar-test.junit.xml"
    failure_report_path = tmp_path / "Saved" / "UESpec" / "TestReports" / "skill-bar-test.failure-report.json"

    client = BridgeClient(BridgeConfig(uproject_path=tmp_path / "Fake.uproject", ue_engine_path=tmp_path / "Engine"))

    def fake_run_commandlet(*args: object, **kwargs: object) -> object:
        failure_report_path.parent.mkdir(parents=True, exist_ok=True)
        failure_report_path.write_text(
            json.dumps(
                {
                    "summary": {"total": 1, "pass": 0, "fail": 1, "skip": 0, "duration": 0.1},
                    "failures": [{"testName": "Case1", "category": "WIDGET_MISSING"}],
                }
            ),
            encoding="utf-8",
        )

        output_path = Path(kwargs["output_path"])
        output_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "summary": {"total": 1, "passed": 0, "failed": 1, "skipped": 0},
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )

        from uespec_mcp.bridge import CommandletInvocation

        return CommandletInvocation(
            ok=False,
            command=["UnrealEditor-Cmd.exe", "-run=UESpecRunTests"],
            outputPath=str(output_path),
            payload=json.loads(output_path.read_text(encoding="utf-8")),
        )

    monkeypatch.setattr(client, "_run_commandlet", fake_run_commandlet)
    result = client.run_test_spec(spec_path, report_path, failure_report_path)

    assert result["ok"] is False
    assert result["failureReportPath"] == str(failure_report_path.resolve())
    assert result["failureReport"]["failures"][0]["category"] == "WIDGET_MISSING"


def test_get_failure_report_reads_json_file(tmp_path: Path) -> None:
    report_path = tmp_path / "failure-report.json"
    report_payload = {
        "summary": {"total": 1, "pass": 0, "fail": 1, "skip": 0, "duration": 0.1},
        "failures": [{"testName": "Case1", "category": "ASSERTION_FAILED"}],
    }
    report_path.write_text(json.dumps(report_payload), encoding="utf-8")

    result = test_tools.get_failure_report(str(report_path))

    assert result["ok"] is True
    assert result["failureReport"] == report_payload


def test_suggest_nearest_uses_support_surface_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {
        "BuiltInUMGWidgets": [{"Type": "CanvasPanel"}, {"Type": "Button"}],
        "CommonUIWidgets": [{"Type": "CommonButtonBase"}],
        "RegisteredComponents": [{"Type": "SkillSlot"}],
        "Converters": [{"Name": "Percent"}, {"Name": "BoolToVisibility"}],
        "StyleTokens": [{"Name": "color.primary"}],
        "Signals": [{"Name": "onSkillUsed"}],
    }
    monkeypatch.setattr(surface, "_ensure_surface_json", lambda force=False: snapshot)

    result = surface.suggest_nearest("SkillSlo", "widget")

    assert result["ok"] is True
    assert result["suggestions"][0] == "SkillSlot"


def test_tools_list_is_compact_and_llm_tools_follow_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from fastmcp import Client

    for name in ("UESPEC_LLM_TOOLS", "UESPEC_LLM_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    async def list_tools(mcp: object) -> list[object]:
        async with Client(mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(list_tools(server.build_server()))
    names = {tool.name for tool in tools}

    assert {"get_support_surface", "get_component_catalog", "get_schema", "validate_spec", "compile_spec_to_wbp", "run_test_spec", "preview_spec"} <= names
    assert not names & {"run_with_autofix", "propose_patch_from_failure", "generate_spec_from_image", "set_active_spec", "get_active_spec"}
    for tool in tools:
        assert "\n" not in (tool.description or "") and len(tool.description or "") <= 420, tool.name
        assert tool.outputSchema is None, tool.name
    assert len(json.dumps([tool.model_dump(mode="json", exclude_none=True) for tool in tools])) < 5000

    monkeypatch.setenv("UESPEC_LLM_PROVIDER", "mock")
    assert "run_with_autofix" in server.list_registered_tools(server.build_server())
