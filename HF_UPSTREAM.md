# HF upstream record

This repository is a Hello-Friend-LLC fork of a third-party project, imported under
the HF convention (DOC-0073, DOC-0074). HF changes live on branch `hf-main` only.

| Field | Value |
|---|---|
| Upstream URL | https://github.com/Mithril0rd/uespec-mcp-public |
| Upstream branch | `main` |
| Pinned upstream SHA | `36cc5559d80021b33083375f0aec5c71a365fea5` |
| Upstream commit date | 2026-04-29 |
| Fork import date | 2026-10-16 |
| License (SPDX) | Apache-2.0 (see `LICENSE`) |
| HF branch | `hf-main` |
| Companion plugin | https://github.com/Hello-Friend-LLC/UESpec (branch `hf-5-8`) |

HF patches (WI-1586 t211): schema `x-status` markers + indexed-bind rejection, `validate_spec` `warnings`,
`MockBridge` aligned to the plugin surface (converters, signals, action status, binding/action error codes).

Update procedure: `git remote add upstream https://github.com/Mithril0rd/uespec-mcp-public.git`,
fetch, merge `upstream/main` into `hf-main`, push, re-run `pytest -q`. Keep local patches as
minimal, upstream-PR-shaped commits.

HF patches (WI-1586 t510): `image_to_spec_context` (`tools/image_context.py`), `examples.py` (examples_dir config path), `generate_spec_from_image` provider none / MCP sampling probe.

HF patches (WI-1586 t500): host-driven repair flow (`tools/repair.py`, `orchestrator/repair_history.py`, `orchestrator/assertion_guard.py`), `autofix_spec`, compile ledger hook in `compile_spec_to_wbp`.

HF patches (WI-1586 t420): removed the global active spec, array `spec_path`, absolute `artifacts[]`, on-disk surface cache
(`surface_cache.py`), `get_component_catalog` / `get_schema`, compact `tools/list`, compact artifact replies (`tools/_envelope.py`).
