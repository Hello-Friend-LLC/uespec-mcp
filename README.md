# UESpec-Mcp

[![CI](https://github.com/Mithril0rd/uespec-mcp-public/actions/workflows/ci.yml/badge.svg)](https://github.com/Mithril0rd/uespec-mcp-public/actions/workflows/ci.yml)

Python MCP server for driving the UESpec Unreal plugin from AI coding agents.

UESpec-Mcp connects spec-driven UMG generation, Unreal commandlets, automated UI tests, and LLM-based repair loops. The main use case is: generate or edit a UISpec JSON document, compile it into a Widget Blueprint through Unreal, run a UESpec test spec, and let an LLM propose JSON Patch fixes when validation, compile, or test steps fail.

## What It Does

- Reads UISpec JSON and calls the UESpec Unreal plugin to compile UMG Widget Blueprints.
- Runs UESpec test specs and collects structured failure reports.
- Uses LLM adapters to turn failures into RFC 6902 JSON Patch proposals.
- Runs a closed-loop autofix orchestrator: validate, compile, test, patch, retry, rollback on regression.
- Handles five failure categories: `COMPILE_ERROR`, `WIDGET_MISSING`, `ASSERTION_FAILED`, `SLATE_EVENT_NOT_HANDLED`, and `TIMEOUT`.
- Provides an experimental screenshot-to-UISpec flow with image input, support-surface context, example retrieval, and optional autofix.

## Status

| Module | Status |
| --- | --- |
| MCP stdio server | Stable |
| Unreal commandlet bridge | Stable, requires UE 5.x + UESpec plugin |
| Mock bridge mode | Stable for offline compile-error evals |
| Closed-loop autofix | Stable for the current eval suite |
| LLM adapters | Claude, OpenAI, and mock clients |
| Round 5 eval suite | 20 cases total; 4 compile-error cases run offline with real Claude |
| Screenshot to UISpec | Experimental, requires local screenshots |
| Socket bridge mode | Placeholder only |

Latest checked baseline:

- `python -m pytest`: 95 passed
- `UESPEC_BRIDGE_MODE=mock UESPEC_LLM_PROVIDER=mock python evals/run_eval.py --category compile_error`: 4/4 fixed
- Real Claude compile-error eval: see `evals/results/round5-claude-compile-error.md`

## Quickstart Without Unreal

This path works on a clean Python environment and does not require Unreal or a real LLM key.

```bash
pip install -e .
UESPEC_BRIDGE_MODE=mock UESPEC_LLM_PROVIDER=mock python evals/run_eval.py --category compile_error
```

The command writes:

- `evals/last-results.json`
- `evals/last-report.md`

## Demo

A reproducible one-minute terminal demo script is available in
`docs/demo/README.md`. On Windows, use
`docs/demo/record-windows-demo.ps1` to create a PowerShell transcript recording
without extra tools. For GIF or MP4 output, record the same demo script with a
GUI recorder such as ScreenToGif or OBS.

## Real LLM Eval

Set a provider key, keep bridge mode as `mock`, and run the compile-error evals:

```bash
export UESPEC_BRIDGE_MODE=mock
export UESPEC_LLM_PROVIDER=claude
export ANTHROPIC_API_KEY=...
export ANTHROPIC_BASE_URL=... # optional relay
export UESPEC_CLAUDE_MODEL=claude-sonnet-4-5

python evals/run_eval.py \
  --category compile_error \
  --provider claude \
  --bridge-mode mock \
  --max-attempts 3 \
  --output-json evals/results/round5-claude-compile-error.json \
  --output-report evals/results/round5-claude-compile-error.md
```

Current checked report:

- `evals/results/round5-claude-compile-error.md`
- 4/4 fixed
- Average attempts: 1.0
- Token usage: 2406 input, 139 output

## Architecture

```text
AI client / Claude Desktop / Codex
        |
        v
UESpec-Mcp FastMCP stdio server
        |
        +-- tools: validate, compile, test, context, generate, orchestrator
        |
        +-- bridge
        |     +-- commandlet mode -> UnrealEditor-Cmd + UESpec plugin
        |     +-- mock mode       -> offline eval semantics
        |
        +-- LLM adapters
              +-- Claude
              +-- OpenAI
              +-- mock fixtures
```

The orchestrator keeps every patch attempt under `Saved/UESpec/PatchHistory` by default. That directory is intentionally ignored by git.

## Tools

There is no session or "active spec" state: one backend process serves every client, so every tool takes an explicit
`spec_path` (a string, or an array of strings for `compile_spec_to_wbp`, `run_test_spec`, `preview_spec`; relative paths
resolve against the project root on the machine running the backend). `artifacts[]` entries are package paths for assets
and absolute paths for files. Replies from the artifact-returning tools stay under ~2 KB (the full reply is written to
`<project>/Saved/UESpec/artifacts/` and listed as `fullResult`).

- `get_support_surface` (cached under `<project>/Saved/UESpec/cache/surface-<hash>.json`, keyed by project, engine `Build.version`, UESpec plugin HEAD/uplugin hash and HF_UISpec uplugin hash; replies carry `cache: hit|miss|off`; `force: true` re-reads it)
- `get_component_catalog` (compact widget/converter/token/signal names from the cached surface)
- `get_schema` (UISpec schema outline or one `$defs` section; reference material kept out of `tools/list`)
- `validate_spec`
- `compile_spec_to_wbp`
- `run_test_spec`
- Host-driven repair flow (no nested LLM call; state under `<project>/Saved/UESpec/PatchHistory/<spec-stem>/`):
  `get_failure_report` (`spec_path` validates, `compile=true` also compiles, `test_spec_path` runs a test spec; returns code, JSON pointer, nearest names and a `candidatePatch`; `report_path` alone reads a report file),
  `apply_spec_patch` (RFC 6902 `patch`, `note`; writes `<n>.json` {before, after, patch, note, ts} before touching the spec, then validates; `REPAIR_LIMIT_REACHED` after `maxAttempts` failing attempts, default 3, `UESPEC_MAX_ATTEMPTS` or project `maxAttempts`; test specs reject assertion removal or lowered expected counts with `ASSERTION_REMOVAL_REJECTED` unless `allow_assertion_change` and `reason`),
  `rollback_spec` (`to` = attempt number, 0 = original; recompiles the asset in place when it was built from a later state and compares the asset hash),
  `repair_status` (`reset=true` starts a new repair session)
- `list_existing_specs`
- `preview_spec` (live editor only: compile, then offscreen render through VibeUE `capture_preview_state`; PIE is driven by `ue_driver.py`/HF tools, not the backend; `UESPEC_IMG_CHECK` enables the image verdict, `UESPEC_EPIC_URL` overrides the `:8000` fallback; a blank first capture is retried once)
- `suggest_nearest`
- LLM-adapter tools, registered only when a provider is configured (`UESPEC_LLM_PROVIDER`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`; force with `UESPEC_LLM_TOOLS=on|off`): `propose_patch_from_failure`, `autofix_spec` (`provider` claude|openai|mock, default none -> `PROVIDER_NOT_CONFIGURED`), `generate_spec_from_image`, `generate_spec_from_image_with_autofix`

## Environment

Core bridge settings:

- `UESPEC_BRIDGE_MODE`: `commandlet`, `mock`, or `socket`; defaults to `commandlet`.
- `UESPEC_MCP_MODE`: legacy alias for bridge mode when `UESPEC_BRIDGE_MODE` is not set.
- `UESPEC_UPROJECT_PATH`: absolute path to the target `.uproject`; required for commandlet mode.
- `UESPEC_UE_ENGINE_PATH`: Unreal Engine root.
- `UESPEC_UE_EDITOR_CMD`: direct path to `UnrealEditor-Cmd`.

Commandlet names:

- `UESPEC_SUPPORT_SURFACE_COMMANDLET`: defaults to `UESpecDumpSupportSurface`.
- `UESPEC_VALIDATE_COMMANDLET`: defaults to `UESpecValidate`.
- `UESPEC_COMPILE_COMMANDLET`: defaults to `UESpecCompile`.
- `UESPEC_TEST_COMMANDLET`: defaults to `UESpecRunTests`.

LLM settings:

- `UESPEC_LLM_PROVIDER`: `claude`, `openai`, or `mock`; defaults to `claude`.
- `UESPEC_CLAUDE_MODEL`: defaults to `claude-opus-4-1`.
- `ANTHROPIC_API_KEY`: required for the Claude adapter.
- `ANTHROPIC_BASE_URL`: optional Anthropic-compatible relay URL.
- `UESPEC_CLAUDE_BASE_URL`: optional alias for `ANTHROPIC_BASE_URL`.
- `UESPEC_OPENAI_MODEL`: defaults to `gpt-5`.
- `OPENAI_API_KEY`: required for the OpenAI adapter.
- `UESPEC_LLM_MOCK_FIXTURES`: optional path to canned mock LLM responses.

Relay deployments do not always expose the default Claude model alias. If `/v1/models` returns different names, set `UESPEC_CLAUDE_MODEL` to one of the advertised model IDs.

Screenshot generation settings:

- `UESPEC_MAX_IMAGE_BYTES`: defaults to 8 MiB.
- `UESPEC_EXAMPLES_ROOT`: optional examples directory override for retrieval.
- `UESPEC_REPO_ROOT`: optional UESpec plugin repo root used to find `Examples/`.

## Commandlet Interface

- `UESpecDumpSupportSurface -Format=markdown|json -Output=<file>`
- `UESpecValidate -SpecFile=<spec.json> -Output=<file>`
- `UESpecCompile -SpecFile=<spec.json> -OutputDir=/Game/UESpec/Generated -Output=<file>`
- `UESpecRunTests -SpecFile=<test-spec.json> -Report=<file> -JsonReport=<file> -Output=<file>`

Notes:

- `SpecFile`, `Output`, `Report`, and `JsonReport` are filesystem paths.
- `OutputDir` is an Unreal package path, not a filesystem path.
- `UESpecRunTests` expects `testSpec.target` to resolve to a compiled Widget Blueprint asset or widget class path.
- `run_with_autofix` rewrites runtime test specs to point at the latest generated asset without mutating the source test spec.

## Screenshot Eval

The screenshot suite is experimental:

```bash
python evals/run_screenshot_eval.py --dry-run
```

The manifest is in `evals/screenshot_cases/manifest.json`. PNGs are not bundled; see `evals/screenshot_cases/README.md` for why and how to bring local screenshots.

## Roadmap

- Round 3: MCP skeleton and commandlet bridge.
- Round 4: UESpec plugin work happened in the Unreal repo, including AngelScript-facing work.
- Round 5: closed-loop test-failure autofix with five handlers and eval fixtures.
- Round 6: ARPG/MMO component library work lives in the UESpec plugin repo.
- Round 7: screenshot-to-UISpec generation, currently experimental.

## Known Limitations

- `socket` bridge mode is not implemented.
- `mock` bridge mode is designed for offline eval confidence, not as a replacement for Unreal.
- The mock bridge mirrors the HF UESpec plugin surface (components, the 7 converters, signals, action events/primitives with `supported`/`unimplemented` status) and a subset of its error codes (`UNSUPPORTED_BINDING_PATH`, `UNRESOLVED_BINDING_PATH`, `UNSUPPORTED_BINDING_TARGET`, `UNSUPPORTED_EVENT`, `UNSUPPORTED_ACTION`, `UNKNOWN_CONVERTER`, `UNKNOWN_WIDGET_TYPE`, `VM_METHOD_NOT_DECLARED`). `INVALID_CONVERTER` needs the live MVVM type system and is never emitted by the mock. The old mock-only `UNKNOWN_SIGNAL` was dropped because the plugin does not emit it; the eval cases that relied on it (`compile_error/case-03-unknown-signal`, `assertion_failed/case-02-state-transition-guard`, `timeout/case-01-wait-for-state-transition`) report `invalid_fixture` under the mock.
- Schema keys that nothing consumes (`extends`, `resolutions`, `viewModel.scope`, `viewModel.fields[].notify`, binding `oneTime`, action `cooldown` and `sound`) carry `"x-status": "unsupported"` in `uispec.schema.json`; `validate_spec` accepts them and lists each in `warnings` as `UNSUPPORTED_KEY`.
- Indexed or call bind paths (`skills[0].x`, `Fn()`) are rejected at schema validation as `UNSUPPORTED_BINDING_PATH`, the same code the UE compiler reports.
- Screenshot eval cases ship without PNGs for licensing and size reasons.
- The real commandlet bridge still requires a local Unreal project with the UESpec plugin installed.
- The current real-LLM public report covers the four compile-error eval cases only.

## Development

```bash
pip install -e .
python -m pytest
```

Run the MCP server:

```bash
uespec-mcp
```

Example Claude Desktop config:

- `examples/claude_desktop_config.json`
