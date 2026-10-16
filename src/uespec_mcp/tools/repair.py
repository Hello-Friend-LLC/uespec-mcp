"""Host-Claude repair flow (WI-1586 t500): no nested LLM call.

``get_failure_report`` returns structured failures for the host to reason over, ``apply_spec_patch`` applies the RFC 6902
patch the host composed (history first, then the spec, then validation), ``rollback_spec`` restores any earlier state
(and recompiles the asset when it was built from a later one) and ``repair_status`` reports attempts and history. All state
is on disk under ``<project>/Saved/UESpec/PatchHistory/<spec-stem>/`` keyed by the explicit spec path.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonpatch
import jsonpointer

from ..bridge import BridgeConfig, structured_error
from ..orchestrator import assertion_guard
from ..orchestrator.repair_history import (
    RepairHistory,
    file_hash,
    max_attempts as resolve_max_attempts,
    now_iso,
    read_spec_text,
    text_hash,
    write_spec_text,
)
from . import compile as compile_tools
from . import context, surface, test as test_tools, validate
from ._register import add_tool

MAX_REPORTED_FAILURES = 3
_NEAREST_CATEGORY = {"UNKNOWN_CONVERTER": "converter", "INVALID_CONVERTER": "converter", "UNKNOWN_WIDGET_TYPE": "widget"}
_PATH_TOKEN = re.compile(r"\.([^.\[\]]+)|\[(\d+)\]")


# -- shared helpers ---------------------------------------------------------------------------------------------------


def history_root(spec: Path) -> Path:
    root = context.project_root()
    return (Path(root) if root is not None else spec.parent) / "Saved" / "UESpec" / "PatchHistory"


def history_for(spec: Path) -> RepairHistory:
    return RepairHistory(spec, history_root(spec))


def configured_max_attempts() -> int:
    try:
        configured = BridgeConfig.from_env().max_attempts
    except Exception:  # noqa: BLE001 - config trouble must not break the repair tools
        configured = None
    return resolve_max_attempts(configured)


def to_pointer(path: str | None) -> str | None:
    """``$.root.children[1].content.convert`` -> ``/root/children/1/content/convert`` (None when the path is not a document path)."""
    if not path or not path.startswith("$"):
        return None
    rest = path[1:]
    tokens: list[str] = []
    position = 0
    for match in _PATH_TOKEN.finditer(rest):
        if match.start() != position:
            return None
        position = match.end()
        tokens.append(match.group(1) if match.group(1) is not None else match.group(2))
    if position != len(rest):
        return None
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


def _short(value: Any, limit: int) -> Any:
    return value[:limit] if isinstance(value, str) else value


def _bridge_fields(source: dict[str, Any]) -> dict[str, Any]:
    return {key: source[key] for key in ("backend", "correlationId", "durationMs") if key in source}


def _shrink_validation(result: dict[str, Any]) -> dict[str, Any]:
    errors = [
        {key: _short(value, 200) for key, value in error.items() if key != "nearestDeclared" or value}
        for error in (result.get("errors") or [])[:3]
        if isinstance(error, dict)
    ]
    summary: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "stage": result.get("stage"),
        "validationLevel": result.get("validationLevel"),
        "errorCount": len(result.get("errors") or []),
        "errors": errors,
    }
    if result.get("code"):
        summary["code"] = result["code"]
    return summary


def _error_reply(spec: str | Path | None, code: str, message: str, *, path: str = "$", hint: str = "", **extra: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {"ok": False, "code": code, "errors": [structured_error(code, message, path=path, hint=hint)]}
    if spec is not None:
        reply["specPath"] = str(spec)
    reply.update(extra)
    return reply


def _resolve(spec_path: str | None) -> tuple[Path | None, dict[str, Any] | None]:
    try:
        return context.resolve_spec_path(spec_path), None
    except (FileNotFoundError, ValueError) as exc:
        return None, {"ok": False, "specPath": spec_path, "errors": [context.path_error(exc)]}


def _read_document(text: str) -> Any:
    return json.loads(text.lstrip("\ufeff"))


def _serialize(document: Any, original_text: str) -> str:
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if "\r\n" in original_text:
        text = text.replace("\n", "\r\n")
    if original_text.startswith("\ufeff"):
        text = "\ufeff" + text
    return text


def record_compile(spec: Path, result: dict[str, Any]) -> None:
    """Called by compile_spec_to_wbp after a successful compile: remembers which asset came from which spec text."""
    try:
        history = history_for(spec)
        entries = history.entries()
        assets = result.get("generatedAssets") or []
        saved = result.get("savedPackageFile")
        history.record_compile(
            {
                "ts": now_iso(),
                "entry": int(entries[-1]["n"]) if entries else 0,
                "sourceHash": file_hash(spec),
                "outputDir": result.get("outputDir"),
                "assetPath": (assets[0].get("assetPath") if isinstance(assets[0], dict) else assets[0]) if assets else result.get("assetPath"),
                "specHash": result.get("specHash"),
                "savedPackageFile": saved,
                "packageHash": file_hash(saved) if isinstance(saved, str) and saved else None,
                "assetAction": result.get("assetAction"),
            }
        )
    except Exception:  # noqa: BLE001 - the ledger is advisory; it must never fail a compile
        return


def _repair_block(history: RepairHistory, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = entries if entries is not None else history.entries()
    limit = configured_max_attempts()
    used = history.attempts_used(items)
    return {"attemptsUsed": used, "attemptsRemaining": max(limit - used, 0), "maxAttempts": limit, "limitReached": used >= limit}


# -- get_failure_report -----------------------------------------------------------------------------------------------


def _failure_from_error(error: dict[str, Any], stage: str, document: Any) -> dict[str, Any]:
    path = str(error.get("path") or "$")
    pointer = to_pointer(path)
    code = str(error.get("code") or "UNKNOWN_ERROR")
    nearest = [str(item) for item in (error.get("nearestDeclared") or [])][:3]
    value: Any = None
    if pointer is not None and document is not None:
        try:
            value = jsonpointer.resolve_pointer(document, pointer)
        except jsonpointer.JsonPointerException:
            value = None
    if not nearest and code in _NEAREST_CATEGORY and isinstance(value, str):
        try:
            suggestion = surface.suggest_nearest(value, _NEAREST_CATEGORY[code])
            nearest = [str(item) for item in suggestion.get("suggestions", [])][:3] if suggestion.get("ok") else []
        except Exception:  # noqa: BLE001 - suggestions are best effort
            nearest = []
    failure: dict[str, Any] = {
        "stage": stage,
        "code": code,
        "path": path,
        "pointer": pointer,
        "message": _short(error.get("message"), 240),
    }
    if error.get("hint"):
        failure["hint"] = _short(error["hint"], 200)
    if isinstance(value, (str, int, float, bool)):
        failure["value"] = _short(value, 120)
    if nearest:
        failure["nearest"] = nearest
        replacement = _candidate_value(value, nearest[0])
        if pointer and replacement is not None:
            failure["candidatePatch"] = [{"op": "replace", "path": pointer, "value": replacement}]
    return failure


def _candidate_value(value: Any, nearest: str) -> str | None:
    """A best-effort replacement for the offending string: the nearest declared symbol, keeping a ``vm.`` prefix."""
    if not isinstance(value, str):
        return None
    if value.startswith("vm."):
        return "vm." + nearest
    return None if "." in value else nearest


def _step_pointer(document: Any, step_title: str) -> str | None:
    test_spec = document.get("testSpec") if isinstance(document, dict) else None
    phases = test_spec.get("phases") if isinstance(test_spec, dict) else None
    if not isinstance(phases, dict):
        return None
    for phase, steps in phases.items():
        for index, step in enumerate(steps if isinstance(steps, list) else []):
            if isinstance(step, dict) and step.get("step") == step_title:
                return f"/testSpec/phases/{phase}/{index}"
    return None


def _failure_from_test(entry: dict[str, Any], document: Any) -> dict[str, Any]:
    step = str(entry.get("testStep") or entry.get("step") or "")
    failure: dict[str, Any] = {
        "stage": "test",
        "code": str(entry.get("category") or entry.get("code") or "TEST_FAILED"),
        "message": _short(entry.get("failureMessage") or entry.get("message"), 240),
    }
    if step:
        failure["step"] = _short(step, 120)
        pointer = _step_pointer(document, step)
        if pointer:
            failure["pointer"] = pointer
    for key in ("expected", "actual", "widgetId", "id", "hint", "nearestDeclared"):
        if key in entry and entry[key] not in (None, "", []):
            failure["nearest" if key == "nearestDeclared" else key] = _short(entry[key], 160)
    return failure


def get_failure_report(
    spec_path: str = "",
    test_spec_path: str = "",
    report_path: str = "",
    compile: bool = False,
    output_dir: str = "",
) -> dict[str, Any]:
    """Structured failures for a spec (schema + live validation, optionally a compile) or a test spec (runs it).

    ``report_path`` alone keeps the original behaviour: read a *.failure-report.json file.
    """
    if not spec_path and not test_spec_path:
        if report_path:
            return test_tools.get_failure_report(report_path)
        return _error_reply(None, "SPEC_PATH_REQUIRED", "Pass spec_path (a UISpec) or test_spec_path (a test spec).", path="$.spec_path",
                            hint="get_failure_report validates spec_path, or runs test_spec_path and reports its failures.")

    failures: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    stages: list[str] = []
    envelope: dict[str, Any] = {}
    primary: Path | None = None
    primary_hash: str | None = None

    if spec_path:
        spec, error = _resolve(spec_path)
        if error:
            return error
        assert spec is not None
        primary = spec
        text = read_spec_text(spec)
        primary_hash = text_hash(text)
        try:
            document = _read_document(text)
        except json.JSONDecodeError:
            document = None
        result = validate.validate_spec(text)
        envelope = _bridge_fields(result)
        stages.append(str(result.get("stage") or "validate"))
        if not result.get("ok"):
            stage = str(result.get("stage") or "validate")
            if result.get("code") == "BRIDGE_UNAVAILABLE":
                return {
                    **_error_reply(spec, "BRIDGE_UNAVAILABLE", "Semantic validation could not run; start the project editor (live mode).",
                                   hint="Schema validation passed; the editor must be up for the semantic and compile checks.",
                                   stage=stage, passed=False, sourceHash=primary_hash),
                    **envelope,
                }
            failures += [_failure_from_error(error_entry, stage, document) for error_entry in result.get("errors") or [] if isinstance(error_entry, dict)]
        elif compile:
            compile_result = compile_tools._compile_one(str(spec), output_dir or None)
            envelope = _bridge_fields(compile_result) or envelope
            stages.append("compile")
            if not compile_result.get("ok"):
                failures += [_failure_from_error(error_entry, "compile", document) for error_entry in compile_result.get("errors") or [] if isinstance(error_entry, dict)]

    if test_spec_path and not failures:
        test_spec, error = _resolve(test_spec_path)
        if error:
            return error
        assert test_spec is not None
        primary = primary or test_spec
        try:
            test_document = _read_document(read_spec_text(test_spec))
        except json.JSONDecodeError:
            test_document = None
        run = test_tools._run_one(str(test_spec), None, None)
        envelope = _bridge_fields(run) or envelope
        stages.append("test")
        for entry in run.get("artifacts") or []:
            if entry not in artifacts:
                artifacts.append(entry)
        report = run.get("failureReport") if isinstance(run.get("failureReport"), dict) else {}
        for entry in report.get("failures") or []:
            if isinstance(entry, dict):
                failures.append(_failure_from_test(entry, test_document))
        if not failures and not run.get("ok"):
            for error_entry in run.get("errors") or []:
                if isinstance(error_entry, dict):
                    failures.append(_failure_from_error(error_entry, "test", test_document))
        primary_hash = primary_hash or file_hash(test_spec)

    assert primary is not None
    history = history_for(primary)
    reply: dict[str, Any] = {
        "ok": True,
        "passed": not failures,
        "stage": stages[-1] if stages else None,
        "specPath": str(primary),
        "sourceHash": primary_hash,
        "failureCount": len(failures),
        "failures": failures[:MAX_REPORTED_FAILURES],
        "repair": _repair_block(history),
        **envelope,
    }
    if len(failures) > MAX_REPORTED_FAILURES:
        reply["moreFailures"] = len(failures) - MAX_REPORTED_FAILURES
    if failures:
        reply["next"] = "apply_spec_patch with an RFC 6902 patch aimed at failures[].pointer (candidatePatch is an unverified suggestion)."
    full = _envelope_write("get_failure_report", {**reply, "failures": failures}, envelope.get("correlationId"))
    if full:
        artifacts.append({"kind": "full-result", "path": full})
    if artifacts:
        reply["artifacts"] = artifacts
    return reply


def _envelope_write(tool: str, result: dict[str, Any], correlation_id: Any) -> str | None:
    from . import _envelope

    payload = dict(result)
    if correlation_id:
        payload["correlationId"] = correlation_id
    return _envelope.write_full_result(tool, payload, context.project_root())


# -- apply_spec_patch -------------------------------------------------------------------------------------------------


def apply_spec_patch(
    spec_path: str,
    patch: list[dict[str, Any]],
    note: str = "",
    allow_assertion_change: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Apply an RFC 6902 patch to a spec (or test spec), recording history first, then validate (schema + live)."""
    spec, error = _resolve(spec_path)
    if error:
        return error
    assert spec is not None
    if isinstance(patch, str):
        try:
            patch = json.loads(patch)
        except json.JSONDecodeError as exc:
            return _error_reply(spec, "INVALID_PATCH", f"patch is not valid JSON: {exc.msg}", path="$.patch", hint="Pass an RFC 6902 array.")
    if not isinstance(patch, list) or not patch or not all(isinstance(op, dict) for op in patch):
        return _error_reply(spec, "INVALID_PATCH", "patch must be a non-empty RFC 6902 array of operation objects.", path="$.patch",
                            hint='Example: [{"op":"replace","path":"/root/children/0/content/convert","value":"Percent"}].')

    history = history_for(spec)
    entries = history.entries()
    repair = _repair_block(history, entries)
    if repair["limitReached"]:
        return _error_reply(
            spec,
            "REPAIR_LIMIT_REACHED",
            f"{repair['attemptsUsed']} patch attempt(s) since the last green state; the limit is {repair['maxAttempts']}.",
            path="$.patch",
            hint="rollback_spec to a good attempt and stop to report, or repair_status with reset=true to start a new repair session.",
            repair=repair,
            lastAttempt=int(entries[-1]["n"]) if entries else 0,
        )

    try:
        before_text = read_spec_text(spec)
        before_document = _read_document(before_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _error_reply(spec, "SPEC_NOT_JSON", f"The spec file is not valid UTF-8 JSON: {exc}", path="$.spec_path",
                            hint="Fix the file by hand, or rollback_spec to an earlier attempt.")
    try:
        after_document = jsonpatch.JsonPatch(patch).apply(before_document, in_place=False)
    except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException, TypeError, KeyError, IndexError, ValueError) as exc:
        return _error_reply(spec, "INVALID_PATCH", str(exc), path="$.patch", hint="Check each op's path against the spec document; the spec was not touched.")

    guard: dict[str, Any] | None = None
    if assertion_guard.is_test_spec(before_document, spec):
        comparison = assertion_guard.compare_assertions(before_document, after_document)
        if comparison["removed"] or comparison["lowered"]:
            if not allow_assertion_change:
                return _error_reply(
                    spec,
                    "ASSERTION_REMOVAL_REJECTED",
                    f"The patch removes {len(comparison['removed'])} assertion(s) and lowers {len(comparison['lowered'])} expected value(s) of the test spec.",
                    path="$.patch",
                    hint="Fix the spec under test instead. If the test itself is wrong, resend with allow_assertion_change=true and a reason.",
                    removed=comparison["removed"][:5],
                    lowered=comparison["lowered"][:5],
                )
            if not (reason and reason.strip()):
                return _error_reply(spec, "ASSERTION_CHANGE_REASON_REQUIRED", "allow_assertion_change=true needs a non-empty reason.", path="$.reason",
                                    hint="State why the assertion change is correct; it is recorded in the attempt history.")
            guard = {"reason": reason.strip(), "removed": comparison["removed"], "lowered": comparison["lowered"]}

    after_text = _serialize(after_document, before_text)
    if after_text == before_text:
        return _error_reply(spec, "PATCH_NO_CHANGE", "The patch leaves the spec text unchanged; no attempt was consumed.", path="$.patch")

    n = history.next_n()
    entry: dict[str, Any] = {
        "n": n,
        "kind": "patch",
        "specPath": spec.as_posix(),
        "ts": now_iso(),
        "note": note or "",
        "patch": patch,
        "before": before_text,
        "after": after_text,
        "beforeHash": text_hash(before_text),
        "afterHash": text_hash(after_text),
        "validation": None,
    }
    if guard:
        entry["assertionChange"] = guard
    history_file = history.write_entry(entry)  # history first, then the spec
    write_spec_text(spec, after_text)

    validation = validate.validate_spec(after_text)
    summary = _shrink_validation(validation)
    history.update_entry(n, validation=summary)

    used_entries = history.entries()
    repair = _repair_block(history, used_entries)
    reply: dict[str, Any] = {
        "ok": bool(validation.get("ok")),
        "applied": True,
        "attempt": n,
        "specPath": str(spec),
        "sourceHash": entry["afterHash"],
        "beforeSourceHash": entry["beforeHash"],
        "validation": summary,
        "repair": repair,
        "artifacts": [{"kind": "patch-history", "path": history_file.as_posix()}],
        **_bridge_fields(validation),
    }
    if validation.get("code"):
        reply["code"] = validation["code"]
    if not validation.get("ok"):
        reply["next"] = f"get_failure_report to inspect, apply_spec_patch again, or rollback_spec with to={n - 1} (to=0 is the original)."
    if guard:
        reply["assertionChange"] = {"reason": guard["reason"], "removed": len(guard["removed"]), "lowered": len(guard["lowered"])}
    return reply


# -- rollback_spec ----------------------------------------------------------------------------------------------------


def _find_compile(history: RepairHistory, source_hash: str) -> dict[str, Any] | None:
    matches = [record for record in history.compiles() if record.get("sourceHash") == source_hash]
    return matches[-1] if matches else None


def rollback_spec(
    spec_path: str,
    to: int,
    recompile: bool = True,
    output_dir: str = "",
) -> dict[str, Any]:
    """Restore the spec as it was after attempt ``to`` (0 = original) and, when the asset was built from a later state, recompile it in place."""
    spec, error = _resolve(spec_path)
    if error:
        return error
    assert spec is not None
    history = history_for(spec)
    entries = history.entries()
    if isinstance(to, bool) or not isinstance(to, int) or to < 0:
        return _error_reply(spec, "INVALID_ROLLBACK_TARGET", "to must be an attempt number >= 0 (0 restores the original spec).", path="$.to")
    target_text = history.text_at(to, entries)
    if target_text is None:
        known = [int(entry["n"]) for entry in entries]
        return _error_reply(spec, "HISTORY_ENTRY_NOT_FOUND", f"No attempt {to} in this spec's history (known: {known}).", path="$.to",
                            hint="repair_status lists the attempts.")

    try:
        current_text = read_spec_text(spec)
    except UnicodeDecodeError:
        current_text = ""
    target_hash = text_hash(target_text)
    ledger = history.compiles()
    last_compile = ledger[-1] if ledger else None
    pre_patch = _find_compile(history, target_hash)

    reply: dict[str, Any] = {"ok": True, "specPath": str(spec), "to": to, "sourceHash": target_hash}
    rollback_n: int | None = None
    if current_text == target_text:
        reply["restored"] = False
        reply["note"] = "The spec already equals that state."
    else:
        rollback_n = history.next_n()
        history.write_entry(
            {
                "n": rollback_n,
                "kind": "rollback",
                "to": to,
                "specPath": spec.as_posix(),
                "ts": now_iso(),
                "note": f"rollback to {to}",
                "before": current_text,
                "after": target_text,
                "beforeHash": text_hash(current_text),
                "afterHash": target_hash,
                "validation": None,
            }
        )
        write_spec_text(spec, target_text)
        validation = validate.validate_spec(target_text)
        history.update_entry(rollback_n, validation=_shrink_validation(validation))
        reply.update({"restored": True, "attempt": rollback_n, "validation": _shrink_validation(validation), **_bridge_fields(validation)})

    asset_in_sync = bool(last_compile) and last_compile.get("sourceHash") == target_hash
    should_compile = recompile and not (asset_in_sync and not output_dir)
    target_output_dir = output_dir or (last_compile or {}).get("outputDir")
    if last_compile is None and not output_dir:
        reply["compile"] = None
        reply["assetInSync"] = None
        reply["assetNote"] = "No asset was compiled from this spec through compile_spec_to_wbp, so there is nothing to recompile."
    elif not should_compile:
        reply["compile"] = None
        reply["assetInSync"] = asset_in_sync
        if not asset_in_sync:
            reply["assetNote"] = "recompile=false: the asset still reflects a later spec state."
    elif not target_output_dir:
        return {**reply, **_error_reply(spec, "OUTPUT_DIR_REQUIRED", "recompile needs the package path the asset was built into.", path="$.output_dir",
                                        hint="Pass output_dir (for example /Game/HF/UISpecDemo/T500); the spec file is already restored.")}
    else:
        compiled = compile_tools._compile_one(str(spec), target_output_dir)
        compile_summary = {
            "ok": bool(compiled.get("ok")),
            "assetAction": compiled.get("assetAction"),
            "assetPath": (compiled.get("generatedAssets") or [None])[0] if compiled.get("generatedAssets") else compiled.get("assetPath"),
            "specHash": compiled.get("specHash"),
            "savedPackageFile": compiled.get("savedPackageFile"),
            "packageHash": file_hash(compiled["savedPackageFile"]) if isinstance(compiled.get("savedPackageFile"), str) else None,
            "outputDir": target_output_dir,
        }
        if isinstance(compile_summary["assetPath"], dict):
            compile_summary["assetPath"] = compile_summary["assetPath"].get("assetPath")
        reply["compile"] = compile_summary
        reply["assetInSync"] = bool(compiled.get("ok"))
        if not compiled.get("ok"):
            reply["ok"] = False
            reply["code"] = "ROLLBACK_RECOMPILE_FAILED"
            reply["errors"] = [structured_error("ROLLBACK_RECOMPILE_FAILED", "The spec file was restored but the in-place recompile failed.",
                                                hint="get_failure_report with compile=true shows why; the asset still reflects the later state.")]
        elif pre_patch is not None:
            expected = pre_patch.get("specHash")
            actual = compile_summary["specHash"]
            reply["assetHash"] = {
                "prePatchSpecHash": expected,
                "restoredSpecHash": actual,
                "matches": (expected == actual) if expected and actual else None,
                "prePatchPackageHash": pre_patch.get("packageHash"),
                "restoredPackageHash": compile_summary["packageHash"],
                "packageMatches": (pre_patch.get("packageHash") == compile_summary["packageHash"]) if pre_patch.get("packageHash") and compile_summary["packageHash"] else None,
            }
    if last_compile is not None:
        reply["lastCompileBefore"] = {key: last_compile.get(key) for key in ("entry", "assetPath", "specHash", "sourceHash")}
    return reply


# -- repair_status ----------------------------------------------------------------------------------------------------


def repair_status(spec_path: str, reset: bool = False) -> dict[str, Any]:
    """Attempts used and remaining, the recent history and the compile ledger for one spec; reset=true starts a new repair session."""
    spec, error = _resolve(spec_path)
    if error:
        return error
    assert spec is not None
    history = history_for(spec)
    try:
        current_text = read_spec_text(spec)
    except UnicodeDecodeError:
        current_text = ""
    current_hash = text_hash(current_text)
    if reset:
        history.write_entry(
            {
                "n": history.next_n(),
                "kind": "reset",
                "specPath": spec.as_posix(),
                "ts": now_iso(),
                "note": "repair session reset",
                "before": current_text,
                "after": current_text,
                "beforeHash": current_hash,
                "afterHash": current_hash,
                "validation": None,
            }
        )
    entries = history.entries()
    rows = []
    current_entry: int | None = None
    last_good: int | None = None
    for entry in entries:
        validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
        if entry.get("afterHash") == current_hash:
            current_entry = int(entry["n"])
        if entry.get("kind") == "patch" and validation.get("ok") is True:
            last_good = int(entry["n"])
    for entry in entries[-8:]:
        validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
        row = {"n": int(entry["n"]), "kind": entry.get("kind"), "ts": entry.get("ts"), "note": _short(entry.get("note"), 80), "afterHash": entry.get("afterHash"),
               "validationOk": validation.get("ok")}
        if entry.get("kind") == "rollback":
            row["to"] = entry.get("to")
        rows.append(row)
    ledger = history.compiles()
    last_compile = ledger[-1] if ledger else None
    reply: dict[str, Any] = {
        "ok": True,
        "specPath": str(spec),
        "sourceHash": current_hash,
        "repair": _repair_block(history, entries),
        "entryCount": len(entries),
        "currentEntry": current_entry,
        "lastGood": last_good,
        "entries": rows,
        "lastCompile": ({key: last_compile.get(key) for key in ("entry", "assetPath", "outputDir", "specHash", "sourceHash")} | {"assetInSync": last_compile.get("sourceHash") == current_hash}) if last_compile else None,
        "artifacts": [{"kind": "patch-history-dir", "path": history.directory.as_posix()}],
    }
    if reset:
        reply["reset"] = True
    return reply


def register(mcp: Any) -> None:
    add_tool(
        mcp,
        "get_failure_report",
        "Structured failures for spec_path (validation; compile=true also compiles) or test_spec_path (runs it): code, JSON pointer, nearest names, candidatePatch. report_path alone reads a report file.",
        get_failure_report,
    )
    add_tool(
        mcp,
        "apply_spec_patch",
        "Apply your RFC 6902 patch to spec_path (history, file, then validation). Returns the attempt and attempts left; REPAIR_LIMIT_REACHED after the limit. Test specs reject assertion removal unless allow_assertion_change and reason.",
        apply_spec_patch,
        compact=True,
    )
    add_tool(
        mcp,
        "rollback_spec",
        "Restore spec_path to its state after attempt `to` (0 = original); recompiles the asset in place if it was built from a later state and reports whether the asset hash matches.",
        rollback_spec,
        compact=True,
    )
    add_tool(
        mcp,
        "repair_status",
        "Attempts used/left, recent patch history and last compile for spec_path; reset=true starts a new repair session.",
        repair_status,
    )
