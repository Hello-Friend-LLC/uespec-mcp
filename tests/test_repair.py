"""Host-Claude repair flow (WI-1586 t500): history, rollback, attempt limit, assertion regression guard, get_failure_report."""
from __future__ import annotations

import hashlib
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
from uespec_mcp.orchestrator import assertion_guard
from uespec_mcp.tools import compile as compile_tools
from uespec_mcp.tools import orchestrator as orchestrator_tools
from uespec_mcp.tools import repair, test as test_tools, validate


def _spec(convert: str = "Percent") -> dict[str, Any]:
    return {
        "apiVersion": "0.2",
        "kind": "HUD",
        "name": "RepairHUD",
        "root": {"type": "TextBlock", "id": "label", "content": {"bind": "vm.Ratio", "convert": convert}},
    }


class FakeBridge:
    """Validation fails for specs mentioning BAD; compile 'builds' an asset whose hash follows the spec text."""

    backend_name = "fake"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.compile_calls = 0

    def compile_spec(self, spec_path: Path, output_dir: str) -> dict[str, Any]:
        self.compile_calls += 1
        text = Path(spec_path).read_text(encoding="utf-8")
        if "Percnt" in text:
            return {"ok": False, "errors": [{"code": "UNKNOWN_CONVERTER", "path": "$.root.content.convert", "message": "Converter 'Percnt' is not registered.", "hint": "", "nearestDeclared": ["Percent"]}],
                    "generatedAssets": []}
        package = self.root / "Content" / "WBP_RepairHUD.uasset"
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(text.encode("utf-8"))
        return {
            "ok": True,
            "errors": [],
            "generatedAssets": [f"{output_dir}/WBP_RepairHUD"],
            "specHash": hashlib.sha256(json.dumps(json.loads(text), sort_keys=True).encode()).hexdigest()[:12],
            "savedPackageFile": str(package),
            "assetAction": "updated",
            "outputDir": output_dir,
            "specPath": str(spec_path),
        }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("UESPEC_PROJECT_ROOT", str(tmp_path))
    bridge = FakeBridge(tmp_path)
    monkeypatch.setattr(compile_tools, "get_bridge", lambda: bridge)

    def fake_validate(text: str) -> dict[str, Any]:
        if "BAD" in text:
            return {"ok": False, "stage": "semantic", "errors": [{"code": "BROKEN", "path": "$.name", "message": "bad name"}]}
        return {"ok": True, "stage": "semantic", "validationLevel": "semantic", "errors": []}

    monkeypatch.setattr(validate, "validate_spec", fake_validate)
    return tmp_path


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _rename(value: str) -> list[dict[str, Any]]:
    return [{"op": "replace", "path": "/name", "value": value}]


def test_apply_spec_patch_writes_history_before_the_spec_and_counts_attempts(project: Path) -> None:
    spec = _write(project / "hud.json", _spec())
    before = spec.read_text(encoding="utf-8")

    result = repair.apply_spec_patch(str(spec), _rename("RenamedHUD"), note="rename")

    assert result["ok"] is True and result["applied"] is True and result["attempt"] == 1
    assert result["repair"] == {"attemptsUsed": 0, "attemptsRemaining": 3, "maxAttempts": 3, "limitReached": False}
    entry = json.loads(Path(result["artifacts"][0]["path"]).read_text(encoding="utf-8"))
    assert entry["before"] == before and json.loads(entry["after"])["name"] == "RenamedHUD"
    assert entry["patch"] == _rename("RenamedHUD") and entry["note"] == "rename" and entry["ts"]
    assert json.loads(spec.read_text(encoding="utf-8"))["name"] == "RenamedHUD"
    assert Path(result["artifacts"][0]["path"]).parent == project / "Saved" / "UESpec" / "PatchHistory" / "hud"


def test_failing_patch_stays_applied_counts_and_hits_the_limit(project: Path) -> None:
    spec = _write(project / "hud.json", _spec())
    results = [repair.apply_spec_patch(str(spec), _rename(f"BAD{index}")) for index in range(3)]

    assert [r["ok"] for r in results] == [False, False, False]
    assert [r["repair"]["attemptsRemaining"] for r in results] == [2, 1, 0]
    assert json.loads(spec.read_text(encoding="utf-8"))["name"] == "BAD2"

    blocked = repair.apply_spec_patch(str(spec), _rename("Fine"))
    assert blocked["ok"] is False and blocked["code"] == "REPAIR_LIMIT_REACHED"
    assert json.loads(spec.read_text(encoding="utf-8"))["name"] == "BAD2"
    assert len(repair.history_for(spec).entries()) == 3

    reset = repair.repair_status(str(spec), reset=True)
    assert reset["repair"]["attemptsRemaining"] == 3
    assert repair.apply_spec_patch(str(spec), _rename("Fine"))["ok"] is True


def test_green_patch_refunds_attempts_and_max_attempts_is_configurable(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_MAX_ATTEMPTS", "2")
    spec = _write(project / "hud.json", _spec())

    assert repair.apply_spec_patch(str(spec), _rename("BAD"))["repair"]["attemptsRemaining"] == 1
    assert repair.apply_spec_patch(str(spec), _rename("Good"))["repair"]["attemptsRemaining"] == 2
    assert repair.apply_spec_patch(str(spec), _rename("BAD2"))["repair"]["maxAttempts"] == 2


def test_invalid_and_noop_patches_do_not_touch_the_spec_or_consume_attempts(project: Path) -> None:
    spec = _write(project / "hud.json", _spec())
    original = spec.read_text(encoding="utf-8")

    bad_path = repair.apply_spec_patch(str(spec), [{"op": "replace", "path": "/nope/deeper", "value": 1}])
    not_a_list = repair.apply_spec_patch(str(spec), {"op": "add"})  # type: ignore[arg-type]
    same = repair.apply_spec_patch(str(spec), _rename("RepairHUD"))

    assert [bad_path["code"], not_a_list["code"], same["code"]] == ["INVALID_PATCH", "INVALID_PATCH", "PATCH_NO_CHANGE"]
    assert spec.read_text(encoding="utf-8") == original
    assert repair.history_for(spec).entries() == []
    assert repair.apply_spec_patch("", _rename("x"))["errors"][0]["code"] == "SPEC_PATH_REQUIRED"


def test_rollback_restores_exact_bytes_and_recompiles_the_asset_to_the_prepatch_hash(project: Path) -> None:
    spec = _write(project / "hud.json", _spec())
    spec.write_bytes(spec.read_bytes().replace(b"\n", b"\r\n"))  # CRLF survives the patch and the rollback
    original = spec.read_bytes()

    compiled = compile_tools.compile_spec_to_wbp(str(spec), "/Game/T500")
    pre_patch_hash = compiled["specHash"]
    assert repair.apply_spec_patch(str(spec), [{"op": "replace", "path": "/root/content/convert", "value": "Stringify"}], note="good")["ok"]
    assert b"\r\n" in spec.read_bytes()
    after_good = compile_tools.compile_spec_to_wbp(str(spec), "/Game/T500")
    assert after_good["specHash"] != pre_patch_hash
    assert repair.apply_spec_patch(str(spec), [{"op": "replace", "path": "/root/content/convert", "value": "Negate"}], note="bad idea")["ok"]
    bad_compile = compile_tools.compile_spec_to_wbp(str(spec), "/Game/T500")

    result = repair.rollback_spec(str(spec), to=0)

    assert result["ok"] is True and result["restored"] is True and spec.read_bytes() == original
    assert result["compile"]["ok"] is True and result["assetInSync"] is True
    assert result["assetHash"]["matches"] is True and result["assetHash"]["restoredSpecHash"] == pre_patch_hash != bad_compile["specHash"]
    assert result["assetHash"]["packageMatches"] is True
    kinds = [entry["kind"] for entry in repair.history_for(spec).entries()]
    assert kinds == ["patch", "patch", "rollback"]


def test_rollback_to_an_attempt_keeps_that_attempts_text_and_skips_recompile_when_in_sync(project: Path) -> None:
    spec = _write(project / "hud.json", _spec())
    repair.apply_spec_patch(str(spec), _rename("One"))
    one_text = spec.read_text(encoding="utf-8")
    compile_tools.compile_spec_to_wbp(str(spec), "/Game/T500")
    repair.apply_spec_patch(str(spec), _rename("Two"))

    unknown = repair.rollback_spec(str(spec), to=9)
    assert unknown["code"] == "HISTORY_ENTRY_NOT_FOUND"

    skipped = repair.rollback_spec(str(spec), to=1, recompile=False)
    assert spec.read_text(encoding="utf-8") == one_text and skipped["compile"] is None
    again = repair.rollback_spec(str(spec), to=1)
    assert again["restored"] is False and again["assetInSync"] is True and again["compile"] is None


def test_rollback_without_a_compiled_asset_only_restores_the_file(project: Path) -> None:
    spec = _write(project / "hud.json", _spec())
    repair.apply_spec_patch(str(spec), _rename("One"))

    result = repair.rollback_spec(str(spec), to=0)

    assert result["ok"] is True and result["compile"] is None and result["assetInSync"] is None
    assert json.loads(spec.read_text(encoding="utf-8"))["name"] == "RepairHUD"


def test_repair_status_lists_attempts_and_the_current_state(project: Path) -> None:
    spec = _write(project / "hud.json", _spec())
    repair.apply_spec_patch(str(spec), _rename("Good"))
    repair.apply_spec_patch(str(spec), _rename("BAD"))
    repair.rollback_spec(str(spec), to=1)

    status = repair.repair_status(str(spec))

    assert status["entryCount"] == 3 and status["lastGood"] == 1 and status["currentEntry"] in (1, 3)
    assert [row["kind"] for row in status["entries"]] == ["patch", "patch", "rollback"]
    assert status["repair"]["attemptsUsed"] == 1


def test_history_is_keyed_by_spec_path_and_same_stem_specs_do_not_collide(project: Path) -> None:
    (project / "a").mkdir()
    (project / "b").mkdir()
    first = _write(project / "a" / "hud.json", _spec())
    second = _write(project / "b" / "hud.json", _spec())
    repair.apply_spec_patch(str(first), _rename("A"))
    repair.apply_spec_patch(str(second), _rename("B"))

    assert len(repair.history_for(first).entries()) == 1 and len(repair.history_for(second).entries()) == 1
    assert repair.history_for(first).directory != repair.history_for(second).directory


def _test_spec(expected_count: int = 3) -> dict[str, Any]:
    return {
        "apiVersion": "0.2",
        "kind": "Test",
        "name": "RepairHUD_Test",
        "testSpec": {
            "target": "RepairHUD",
            "phases": {
                "assert": [
                    {"step": "name shown", "assertText": {"id": "label", "expected": "Knight", "match": "Exact"}},
                    {"step": "three rows", "assertCount": {"id": "rows", "expected": expected_count}},
                    {"step": "visible", "assertVisible": {"id": "label", "expected": True}},
                ]
            },
        },
    }


def test_assertion_removal_is_rejected_unless_allowed_with_a_reason(project: Path) -> None:
    spec = _write(project / "hud.test.json", _test_spec())
    original = spec.read_text(encoding="utf-8")
    remove = [{"op": "remove", "path": "/testSpec/phases/assert/2"}]
    lower = [{"op": "replace", "path": "/testSpec/phases/assert/1/assertCount/expected", "value": 1}]
    fix_text = [{"op": "replace", "path": "/testSpec/phases/assert/0/assertText/expected", "value": "Knight-01"}]

    removed = repair.apply_spec_patch(str(spec), remove)
    lowered = repair.apply_spec_patch(str(spec), lower)
    assert removed["code"] == "ASSERTION_REMOVAL_REJECTED" and removed["removed"][0]["step"] == "visible"
    assert lowered["code"] == "ASSERTION_REMOVAL_REJECTED" and lowered["lowered"][0]["from"] == 3.0 and lowered["lowered"][0]["to"] == 1.0
    assert spec.read_text(encoding="utf-8") == original and repair.history_for(spec).entries() == []

    assert repair.apply_spec_patch(str(spec), remove, allow_assertion_change=True)["code"] == "ASSERTION_CHANGE_REASON_REQUIRED"
    allowed = repair.apply_spec_patch(str(spec), remove, allow_assertion_change=True, reason="visibility is covered by another spec")
    assert allowed["ok"] is True and allowed["assertionChange"]["removed"] == 1
    assert repair.history_for(spec).entries()[0]["assertionChange"]["reason"].startswith("visibility")
    assert repair.apply_spec_patch(str(spec), fix_text)["ok"] is True  # editing an expected string is a legitimate repair


def test_assertion_guard_is_inert_for_a_plain_spec_and_flags_dropped_parameters() -> None:
    plain = _spec()
    assert not assertion_guard.is_test_spec(plain, "hud.json")
    base = _test_spec()
    base["testSpec"]["parameters"] = [{"name": "low"}, {"name": "high"}]
    after = json.loads(json.dumps(base))
    after["testSpec"]["parameters"] = [{"name": "low"}]
    assert assertion_guard.compare_assertions(base, after)["removed"] == [{"phase": "parameters", "step": "high", "assertion": "parameter"}]
    assert assertion_guard.compare_assertions(base, base) == {"removed": [], "lowered": [], "beforeCount": 3, "afterCount": 3}


def test_get_failure_report_for_a_spec_returns_pointer_nearest_and_candidate_patch(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _write(project / "hud.json", _spec("Percnt"))

    def failing_validate(text: str) -> dict[str, Any]:
        return {
            "ok": False,
            "stage": "semantic",
            "errors": [{"code": "UNKNOWN_CONVERTER", "path": "$.root.content.convert", "message": "Converter 'Percnt' is not registered.", "hint": "", "nearestDeclared": ["Percent"]}],
        }

    monkeypatch.setattr(validate, "validate_spec", failing_validate)
    result = repair.get_failure_report(spec_path=str(spec))

    assert result["ok"] is True and result["passed"] is False and result["failureCount"] == 1
    failure = result["failures"][0]
    assert failure["code"] == "UNKNOWN_CONVERTER" and failure["pointer"] == "/root/content/convert" and failure["value"] == "Percnt"
    assert failure["nearest"] == ["Percent"]
    assert failure["candidatePatch"] == [{"op": "replace", "path": "/root/content/convert", "value": "Percent"}]
    assert result["repair"]["attemptsRemaining"] == 3
    assert any(a["kind"] == "full-result" for a in result["artifacts"])

    fixed = repair.apply_spec_patch(str(spec), failure["candidatePatch"])
    assert fixed["ok"] is False  # the fake validator above still reports the failure; the patch itself was applied
    assert json.loads(spec.read_text(encoding="utf-8"))["root"]["content"]["convert"] == "Percent"


def test_get_failure_report_compile_stage_and_legacy_report_path(project: Path, tmp_path: Path) -> None:
    spec = _write(project / "hud.json", _spec("Percnt"))

    compiled = repair.get_failure_report(spec_path=str(spec), compile=True, output_dir="/Game/T500")
    assert compiled["passed"] is False and compiled["stage"] == "compile" and compiled["failures"][0]["code"] == "UNKNOWN_CONVERTER"

    report = tmp_path / "failure-report.json"
    report.write_text(json.dumps({"failures": [{"category": "ASSERTION_FAILED"}]}), encoding="utf-8")
    assert repair.get_failure_report(report_path=str(report))["failureReport"]["failures"][0]["category"] == "ASSERTION_FAILED"
    assert repair.get_failure_report()["errors"][0]["code"] == "SPEC_PATH_REQUIRED"


def test_get_failure_report_for_a_test_spec_maps_failures_to_step_pointers(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_spec = _write(project / "hud.test.json", _test_spec())
    report = {"failures": [{"category": "ASSERTION_FAILED", "testStep": "name shown", "failureMessage": "expected Knight", "expected": "Knight", "actual": "Rogue"}]}
    monkeypatch.setattr(test_tools, "_run_one", lambda spec_path, output_report, failure_report_path: {"ok": False, "failureReport": report, "errors": []})

    result = repair.get_failure_report(test_spec_path=str(test_spec))

    assert result["passed"] is False and result["stage"] == "test"
    assert result["failures"][0]["pointer"] == "/testSpec/phases/assert/0" and result["failures"][0]["actual"] == "Rogue"


def test_autofix_spec_without_a_provider_names_the_env_var_and_propose_rejects_ignored_args(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _write(project / "hud.json", _spec())

    result = orchestrator_tools.autofix_spec(str(spec), str(spec))
    assert result["ok"] is False and result["code"] == "PROVIDER_NOT_CONFIGURED"
    assert "UESPEC_LLM_PROVIDER" in result["errors"][0]["message"]

    for argument in ({"screenshot_path": "shot.png"}, {"max_attempts": 2}):
        rejected = orchestrator_tools.propose_patch_from_failure(str(spec), "report.json", **argument)
        assert rejected["errors"][0]["code"] == "UNSUPPORTED_ARGUMENT"


def test_repair_tools_register_and_stay_session_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = server.list_registered_tools(server.build_server())
    assert {"get_failure_report", "apply_spec_patch", "rollback_spec", "repair_status"} <= set(tools)
    assert "autofix_spec" not in tools  # LLM-adapter tool: hidden without a provider
    monkeypatch.setenv("UESPEC_LLM_TOOLS", "on")
    assert "autofix_spec" in server.list_registered_tools(server.build_server())
