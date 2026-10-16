# Changelog

All notable project milestones are summarized here. This project has not cut a
versioned public release yet, so entries are grouped by development round.

## Unreleased - prerelease cleanup

- HF (WI-1586 t211): `validate_spec` rejects indexed/call bind paths with `UNSUPPORTED_BINDING_PATH` and
  returns `UNSUPPORTED_KEY` warnings for schema keys marked `x-status: unsupported`; `MockBridge` mirrors the
  UESpec plugin converters, signals, action event/primitive status and binding/action error codes.

- Fixed FastMCP tool-registration compatibility across the fallback shim and the
  real FastMCP dependency.
- Fixed a path separator assertion that made the default report-path test
  platform-specific.
- Recorded a passing pytest baseline in `tests/_test_baseline.txt`.
- Added `mock` bridge mode for offline compile-error evaluation without Unreal.
- Added a real Claude compile-error eval report:
  `evals/results/round5-claude-compile-error.md`.
- Rewrote the README around the current MCP server, bridge, eval, and known
  limitation state.
- Clarified that screenshot eval inputs are not bundled and must be supplied
  locally.
- Added reproducible prerelease demo scripts and a Windows transcript recorder
  under `docs/demo/`.

## Round 7 - Screenshot to UISpec (experimental)

- Added `generate_spec_from_image` and `generate_spec_from_image_with_autofix`
  MCP tools.
- Added screenshot prompt construction with support-surface context and example
  retrieval.
- Added `evals/run_screenshot_eval.py` and a 5-case ARPG/MMO screenshot eval
  manifest.
- Known limitation: the manifest references local image paths, but example PNGs
  are intentionally not bundled for licensing and size reasons.

## Round 6 - ARPG/MMO UI framework direction

- Established the plugin-side direction for reusable MMOARPG/ARPG UI component
  generation and testing.
- Kept this MCP repository focused on orchestration, tool exposure, evals, and
  commandlet integration rather than owning the Unreal component library itself.

## Round 5 - Closed-loop test-failure autofix

- Added structured failure report handling and LLM patch proposal plumbing.
- Added failure handlers for `COMPILE_ERROR`, `WIDGET_MISSING`,
  `ASSERTION_FAILED`, `SLATE_EVENT_NOT_HANDLED`, and `TIMEOUT`.
- Added safety guardrails for JSON Patch application, rollback, patch history,
  and bounded retry loops.
- Added a 20-case eval suite across the five failure categories.
- Added relay-compatible Claude configuration via `ANTHROPIC_BASE_URL` and
  `UESPEC_CLAUDE_BASE_URL`.
- Hardened real-eval behavior around compile-to-test flow, invalid fixtures,
  widget text hydration, and timeout/test-spec drift.

## Round 4 - AngelScript integration

- Aligned the MCP bridge with the UESpec Unreal commandlet interface.
- Added MCP-side assumptions and documentation for commandlet-driven compile,
  validate, support-surface, and test execution.
- Main AngelScript and UMG generation implementation work lives in the UESpec
  Unreal plugin repository.

## Round 3 - MCP skeleton

- Created the initial Python MCP server and commandlet bridge structure.
- Added core MCP tools for support-surface lookup, spec validation, WBP compile,
  test execution, failure-report retrieval, active spec selection, and patch
  proposal.
- Added the fallback FastMCP shim so local tests can run without the external
  FastMCP package.
