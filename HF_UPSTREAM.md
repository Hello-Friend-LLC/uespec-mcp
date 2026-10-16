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

Update procedure: `git remote add upstream https://github.com/Mithril0rd/uespec-mcp-public.git`,
fetch, merge `upstream/main` into `hf-main`, push, re-run `pytest -q`. Keep local patches as
minimal, upstream-PR-shaped commits.
