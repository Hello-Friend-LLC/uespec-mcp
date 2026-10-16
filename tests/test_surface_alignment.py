"""Guards for the WI-1586 t211 alignment of schema validation and MockBridge with the real UESpec plugin surface."""

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

from uespec_mcp.bridge import MockBridge
from uespec_mcp.tools import validate


def _spec(root_children: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": "0.2",
        "kind": "HUD",
        "name": "Aligned",
        "root": {"id": "rootPanel", "type": "CanvasPanel", "children": root_children},
        **extra,
    }


def _compile(tmp_path: Path, document: dict[str, Any]) -> dict[str, Any]:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(document), encoding="utf-8")
    return MockBridge().compile_spec(spec_path, "/Game/UESpec/Generated")


def _codes_at(result: dict[str, Any]) -> list[tuple[str, str]]:
    return [(error["code"], error["path"]) for error in result["errors"]]


def test_schema_rejects_indexed_and_call_bind_paths_on_content_and_visibility() -> None:
    document = _spec(
        [
            {
                "id": "cooldownText",
                "type": "TextBlock",
                "content": {"bind": "skills[0].cooldownProgress"},
                "visibility": {"bind": "vm.IsReady()"},
            }
        ]
    )

    result = validate.validate_spec(json.dumps(document))

    assert result["ok"] is False
    assert result["stage"] == "schema"
    assert _codes_at(result) == [
        ("UNSUPPORTED_BINDING_PATH", "$.root.children[0].content.bind"),
        ("UNSUPPORTED_BINDING_PATH", "$.root.children[0].visibility.bind"),
    ]


def test_schema_accepts_flat_and_nested_field_bind_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "mock")  # semantic stage must have a bridge; schema acceptance is what is asserted
    document = _spec([{"id": "hp", "type": "ProgressBar", "content": {"bind": "vm.health.current", "convert": "Percent"}}])

    result = validate.validate_spec(json.dumps(document))

    assert result["errors"] == []


def test_unsupported_keys_warn_at_every_nesting_level_without_failing_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "mock")
    document = _spec(
        [
            {
                "id": "skillButton",
                "type": "Button",
                "visibility": {"bind": "vm.visible", "oneTime": True},
                "actions": [{"on": "Clicked", "do": "vm.UseSkill(0)", "cooldown": 1.5, "sound": "click"}],
            }
        ],
        extends="Base",
        resolutions=["1920x1080"],
        viewModel={"class": "Vm", "scope": "Transient", "fields": [{"name": "visible", "type": "bool", "notify": True}]},
    )

    result = validate.validate_spec(json.dumps(document))

    assert result["ok"] is True
    assert sorted(warning["path"] for warning in result["warnings"]) == sorted(
        [
            "$.extends",
            "$.resolutions",
            "$.viewModel.scope",
            "$.viewModel.fields[0].notify",
            "$.root.children[0].visibility.oneTime",
            "$.root.children[0].actions[0].cooldown",
            "$.root.children[0].actions[0].sound",
        ]
    )
    assert {warning["code"] for warning in result["warnings"]} == {"UNSUPPORTED_KEY"}


def test_supported_keys_produce_no_warnings() -> None:
    document = _spec(
        [{"id": "label", "type": "TextBlock", "content": {"bind": "vm.title", "convert": "LocText", "fallback": ""}}],
        viewModel={"class": "Vm", "fields": [{"name": "title", "type": "string", "default": ""}]},
    )

    assert validate.validate_spec(json.dumps(document))["warnings"] == []


def test_warnings_are_kept_when_schema_validation_fails() -> None:
    document = _spec([{"id": "BadId", "type": "Button"}], extends="Base")

    result = validate.validate_spec(json.dumps(document))

    assert result["ok"] is False
    assert [warning["path"] for warning in result["warnings"]] == ["$.extends"]


def test_mock_compile_reports_indexed_bind_and_unresolved_vm_field(tmp_path: Path) -> None:
    document = _spec(
        [
            {"id": "a", "type": "TextBlock", "content": {"bind": "vm.skills[0].name"}},
            {"id": "b", "type": "TextBlock", "content": {"bind": "vm.Healht"}},
            {"id": "c", "type": "TextBlock", "content": {"bind": "vm.Health"}},
            {"id": "d", "type": "TextBlock", "content": {"bind": "Health.Current"}},
        ],
        viewModel={"class": "Vm", "fields": [{"name": "Health", "type": "float"}]},
    )

    result = _compile(tmp_path, document)

    assert result["ok"] is False
    assert _codes_at(result) == [
        ("UNSUPPORTED_BINDING_PATH", "$.root.children[0].content.bind"),
        ("UNRESOLVED_BINDING_PATH", "$.root.children[1].content.bind"),
    ]
    assert result["errors"][1]["nearestDeclared"] == ["Health"]


def test_mock_compile_cannot_resolve_binds_without_declared_view_model_fields(tmp_path: Path) -> None:
    document = _spec([{"id": "a", "type": "TextBlock", "content": {"bind": "vm.Anything"}}])

    assert _compile(tmp_path, document)["ok"] is True


def test_mock_compile_content_bind_needs_a_text_or_percent_destination(tmp_path: Path) -> None:
    document = _spec(
        [
            {"id": "icon", "type": "Image", "content": {"bind": "vm.Icon"}, "visibility": {"bind": "vm.Shown", "convert": "BoolToVisibility"}},
            {"id": "name", "type": "TextBlock", "content": {"bind": "vm.Name"}},
        ]
    )

    result = _compile(tmp_path, document)

    assert _codes_at(result) == [("UNSUPPORTED_BINDING_TARGET", "$.root.children[0].content")]


def test_mock_converters_follow_the_plugin_list_for_content_and_visibility(tmp_path: Path) -> None:
    document = _spec(
        [
            {"id": "a", "type": "TextBlock", "content": {"bind": "vm.Seconds", "convert": "TimeFormat"}},
            {"id": "b", "type": "TextBlock", "content": {"bind": "vm.Seconds", "convert": "SecondsToCooldownText"}},
            {"id": "c", "type": "TextBlock", "content": {"bind": "vm.Delta", "convert": "SignedDeltaText"}},
        ]
    )

    result = _compile(tmp_path, document)

    assert _codes_at(result) == [
        ("UNKNOWN_CONVERTER", "$.root.children[1].content.convert"),
        ("UNKNOWN_CONVERTER", "$.root.children[2].content.convert"),
    ]


def test_mock_rejects_every_unimplemented_event_and_primitive_the_surface_lists(tmp_path: Path) -> None:
    surface = MockBridge().get_support_surface(format="json")["content"]
    bad_events = [entry["Name"] for entry in surface["ActionEvents"] if entry["Status"] == "unimplemented"]
    bad_primitives = [entry["Name"] for entry in surface["ActionPrimitives"] if entry["Status"] == "unimplemented"]
    good_event = next(entry["Name"] for entry in surface["ActionEvents"] if entry["Status"] == "supported")
    assert bad_events and bad_primitives

    actions = [{"on": event, "do": {"callVM": "vm.Go()"}} for event in bad_events]
    actions += [{"on": good_event, "do": {primitive: "x"}} for primitive in bad_primitives]
    actions.append({"on": good_event, "do": {"callVM": "vm.Go()"}})
    document = _spec([{"id": "button", "type": "Button", "actions": actions}])

    compiled = _compile(tmp_path, document)
    validated = MockBridge().validate_spec_content(json.dumps(document))

    expected = [("UNSUPPORTED_EVENT", f"$.root.children[0].actions[{index}].on") for index in range(len(bad_events))]
    expected += [
        ("UNSUPPORTED_ACTION", f"$.root.children[0].actions[{len(bad_events) + index}].do.{primitive}")
        for index, primitive in enumerate(bad_primitives)
    ]
    assert _codes_at(compiled) == expected
    assert _codes_at(validated) == expected
    assert validated["ok"] is False


def test_mock_does_not_emit_signal_errors_the_plugin_does_not_emit(tmp_path: Path) -> None:
    document = _spec(
        [],
        stateMachine={
            "initial": "Normal",
            "states": [{"name": "Normal", "transitions": [{"to": "Normal", "when": "onSkillUseed"}]}],
        },
    )

    result = _compile(tmp_path, document)

    assert result["ok"] is True


def test_mock_support_surface_carries_action_status_in_both_formats() -> None:
    bridge = MockBridge()

    markdown = bridge.get_support_surface(format="markdown")["content"]
    surface = bridge.get_support_surface(format="json")["content"]

    assert "- playAnim (unimplemented)" in markdown
    assert "- Clicked\n" in markdown
    assert {"Name": "Drop", "Status": "unimplemented"} in surface["ActionEvents"]
    assert "MinimapMarker" not in {entry["Type"] for entry in surface["BuiltInUMGWidgets"]}
