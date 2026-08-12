# SagaSmith CoC MCP

[中文](README.md) · [English](README-en.md) · [Platform overview](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md)

The local MCP boundary for SagaSmithAI's Call of Cthulhu 7e stack. It combines campaign persistence, branch-aware memory, per-actor knowledge, snapshots and scenario indexes from `sagasmith-core` with the d100, sanity, combat and chase resolvers from `sagasmith-coc`.

Its progressive exposure manager is server-owned and session-scoped. Every MCP client gets an independent Lobby, Play or Combat capability set. Exposure is bound to a principal and campaign, while explicit actor grants protect private investigator and NPC state.

Resolution and mutation are intentionally separate: `coc_resolve` returns a rules result, and a subsequent explicit write updates SAN, HP or another character field. Player-facing module queries reveal only `visibility=player` scenes.

## Run

```bash
pip install -e "../sagasmith-core[documents]"
pip install -e ../sagasmith-coc
pip install -e .
sagasmith-coc-mcp
```

State defaults to `.sagasmith-coc-mcp/`. Configure `SAGASMITH_COC_MCP_HOME`, `SAGASMITH_COC_SKILLS_DIR`, and `SAGASMITH_MODULEGEN_SKILLS_DIR` when embedding the server in another Agent runtime.

The capability flow is `exposure(open) → exposure(search) → exposure(set) → native domain tool`. Hosts must refresh native tool schemas; there is no fixed-superset, text imitation, or `exposure_call` fallback. `coc_dice_roll`, `coc_check`, and random `coc_resolve` operations atomically commit the campaign random-stream position, receipt, revision, and idempotent replay response.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

Original code is licensed under Apache-2.0. Commercial Call of Cthulhu books and scenarios are not distributed by this repository.
