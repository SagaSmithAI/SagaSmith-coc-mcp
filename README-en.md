# SagaSmith CoC MCP

[中文](README.md) · [English](README-en.md) · [Platform overview](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md)

The local authoritative MCP server for SagaSmithAI's Call of Cthulhu 7e stack. It combines campaign persistence, branch-aware memory, per-actor knowledge, snapshots, module retrieval, and unified Content Packs from `sagasmith-core` with CoC d100, sanity, combat, chase, and replayable random-stream mechanics from `sagasmith-coc`.

## Runtime boundary

- MCP owns campaign state, authorization, revisions, idempotency, random-stream receipts, and atomic random resolution.
- Each MCP session owns an independent native tool exposure. Lobby, Play, and Combat policies are enforced again at call time.
- Hosts must refresh native schemas after `tools/list_changed`. There is no fixed-superset, text imitation, or `exposure_call` fallback.
- The Agent owns source interpretation and scenario-specific semantic decisions. Finalized Pack decisions retain source evidence.

The native capability flow is:

```text
exposure(open) -> exposure(search) -> exposure(set) -> native domain tool
```

Keeper recovery uses `branch_query/change`, `snapshot_query/change`, and
`state_revision`. Every mutation requires explicit revision, branch or history-cursor
guards plus an `idempotency_key`. When checkout, restore, undo, or redo changes the
authoritative phase, the server emits `tools/list_changed`; after refreshing, the host can
load and directly call the next legal native tool for that phase.

## Module Pack authoring

CoC scenarios use the unified `sagasmith.content-package` schema version 2 lifecycle:

```text
module_draft(start)
  -> module_draft(evidence)
  -> module_draft(edit, operation="package")
  -> module_draft(finalize)
  -> content_pack(import)
  -> content_pack(activate)
```

`start` accepts either an allowlisted PDF/Markdown/text `source_path` or generated `name` plus `content`. Mechanical import creates an inactive draft. `evidence` exposes bounded chunks, managed PDF-page render receipts, assets, and content reviews. `edit` supports checksum-bound PDF transcription repair, reviewed CoC content, allowlisted assets, actor bindings, and Pack decisions. Any source-text repair creates a new inactive mechanical revision and invalidates downstream draft decisions. Pack profile and catalog decisions must use the exact source receipts returned by `evidence`. Finalization requires an explicit Agent confirmation and writes an immutable `.sagasmith-pack` archive. Only a module re-imported from that finalized archive may be activated.

Commercial rulebooks and scenarios remain local. Configure allowed source roots with `SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS`, separated by the platform path separator. Source books and extracted assets are never bundled in this repository.

## Run

```bash
pip install -e "../sagasmith-core[documents]"
pip install -e ../sagasmith-coc
pip install -e .
sagasmith-coc-mcp
```

State defaults to `.sagasmith-coc-mcp/`. The main configuration variables are:

- `SAGASMITH_COC_MCP_HOME`
- `SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS`
- `SAGASMITH_COC_SKILLS_DIR`
- `SAGASMITH_MODULEGEN_SKILLS_DIR`
- `SAGASMITH_COC_MCP_BOUND_PRINCIPAL_ID`

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

Original code is licensed under Apache-2.0. Call of Cthulhu and related commercial content remain the property of their respective rights holders.
