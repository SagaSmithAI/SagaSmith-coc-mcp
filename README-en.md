# SagaSmith CoC MCP

[中文](README.md) · [English](README-en.md) · [Website](https://sagasmithai.github.io) · [Platform overview](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md) · [Hosted service](https://github.com/SagaSmithAI/SagaSmith-service) · [Content catalog](https://github.com/SagaSmithAI/SagaSmith-dnd-content-library)

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

Snapshots remain independently restorable full state documents at the public
boundary. Core schema v8 stores each document as one bounded, checksummed
`zlib-1` record. Snapshot query/restore, branch checkout, undo/redo, and restart
recovery do not replay an ancestor chain.

Play and Combat expose two source-explicit actor-state settlements:

- `coc_sanity_check` atomically rolls the SAN check, loss, required INT check, temporary/indefinite/permanent insanity, bout, and duration, then commits the campaign random stream and investigator sheet in one revision group.
- `coc_hp_change` atomically applies damage or healing. A major single blow draws any required CON check from the authoritative stream and persists major-wound, unconscious, dying, dead, and recovery state. A fixed HP change with no random draw does not manufacture a campaign revision.

Both tools require actor-control authorization, campaign and character revisions, and an idempotency key. An exact retry returns the original response without drawing or settling twice.

Authoritative combat uses task-shaped native tools instead of allowing callers to patch `campaign.state` directly:

```text
combat_start -> combat_query
             -> combat_action(move|join|end_turn)
             -> combat_attack(open -> resolve|abort)
             -> combat_end
```

`combat_start` checks participant character revisions, then enters Combat using DEX, DEX+50 for a readied firearm, and stable ties. An attack first persists a pending response; the target controller then chooses dodge, fight back, dive for cover, or no response. `resolve` draws from the campaign stream and atomically settles attack, defense, extreme/impaling damage, ammunition, CON, HP, and wounds in one campaign/character revision group. Grid mode owns coordinates and validates movement and melee distance. Agent mode creates no coordinates and accepts only explicit Agent spatial facts. `combat_end` returns to Play and reports actors that still require dying recovery.

A real stdio-host regression covers Lobby → Play → Combat → Play. After each phase change the host refreshes the native list, observes the old tools disappear, and directly loads and calls the next legal phase tools.

Chases stay within Play and use `chase_start/query/action/end`, with strict mutual exclusion against Combat. At chase start, MCP reads an explicitly named CON, Drive Auto, or Pack-defined skill from each sheet, resolves speed checks from the campaign stream, then derives per-round action points from the slowest effective MOV. `chase_action` owns DEX order, action-point consumption, route position, obstacle checks, and round resets. A Pack or Agent must explicitly supply the sourced position effects of success and failure; MCP does not guess narrative terrain. Players can act only for controlled actors, start/end remain Keeper-only, and every random/state transition has revision and exact-idempotency receipts.

Investigation continuity uses three distinct ledgers instead of treating narration as shared knowledge:

- `campaign_event` appends a branch-local chronology entry with an explicit `dm`, `party`, `public`, or `actor` audience and optional speaker/listener/witness/target participants.
- `continuity_context` returns one budgeted, branch-scoped context projection. Non-Keeper callers are always forced through the player projection and may retrieve private knowledge only for an owned actor.
- `memory_change(action="commit")` atomically settles one event together with objective fact revisions, per-actor knowledge revisions, and an optional snapshot. Every derived fact and knowledge row defaults its provenance to the committed event; exact retries replay the same response and any failed component rolls back the whole settlement.

Objective `memory_query` and all continuity writes are Keeper-only. Players cannot use the objective fact ledger to bypass clue, secret, false-belief, or split-party boundaries. Continuity remains available during Combat as a safe read, while chronology and memory writes close until Play resumes. Real stdio coverage verifies these native tools are added and removed with the phase-specific schema.

Source-backed investigation checks use `investigation_check(open|spend_luck|push|settle|abort)` plus `investigation_query`. The MCP reads the exact named skill, characteristic, or Luck value from the actor sheet, rolls from the campaign stream, and persists the unresolved human choice. Optional Spending Luck must be enabled in campaign settings; the exact Luck cost and actor revision settle atomically. A Push requires an explicit new approach and Keeper failure consequence, rolls a second time, and can never be adjusted with Luck. Pending choices survive restart and block Combat, Chase, or return to Lobby until settled or Keeper-aborted. Successful skills are marked once for later development.

Combined checks use the same resumable workflow but compare one shared d100 with two to eight sheet-backed skills or characteristics. The Keeper must explicitly choose `requirement="any"` or `"all"`; Spending Luck buys exactly that aggregate requirement and a successful component skill receives its own development mark. CoC does not invent a D&D-style majority-success group rule. For a true group Luck roll, `group_luck_query/check` reads every scene participant's current sheet and permits only an investigator tied for the lowest Luck to represent the group; a lowest-value tie requires an explicit Keeper selection.

End-of-session growth is a Lobby operation. `development_query` lists the actor's checked skills and `development_settle` rolls all eligible checks in one campaign stream transaction, updates skills and any first-time mastery SAN reward, clears every check mark, and stores a bounded audit receipt. Cthulhu Mythos is reported and cleared as ineligible rather than being silently advanced by the ordinary skill procedure. Player control, campaign/character revisions, branch scope, restart recovery, and exact idempotent replay are enforced at the write boundary.

Checks do not invent clue meaning or audience. `settle` and `group_luck_check` return mechanical receipts and direct the Agent to record source-specific narration, objective facts, actor knowledge, and failure consequences through `memory_change(action="commit")`. An obvious or indispensable clue bypasses the check entirely and uses that continuity settlement directly, so repeated poor dice cannot deadlock the scenario.

## Module Pack authoring

CoC scenarios use the unified `sagasmith.content-package` schema version 2 lifecycle:

```text
module_draft(start)
  -> module_draft(edit, operation="advance")  # only after an interrupted first pass
  -> module_draft(evidence)
  -> module_draft(edit, operation="statblock|content|asset|actor")
  -> module_draft(edit, operation="package")
  -> module_draft(finalize)
  -> content_pack(import)
  -> content_pack(activate)
  -> content_pack(deactivate|remove)
```

`start` accepts either an allowlisted PDF/Markdown/text `source_path` or generated `name` plus `content`. Mechanical import creates an inactive draft; `advance` resumes from a committed intermediate step after interruption. `evidence` exposes bounded chunks, managed PDF-page render receipts, assets, and content reviews. `edit` supports checksum-bound PDF transcription repair, reviewed CoC content, current CoC statblock-schema validation, allowlisted assets, actor bindings, and Pack decisions. Statblocks may preserve source-true partial non-combat NPC data; only an explicit `combat_ready` declaration requires combat fields. Any source-text repair creates a new inactive mechanical revision and invalidates downstream draft decisions. Pack profile and catalog decisions must use the exact source receipts returned by `evidence`. Finalization requires an explicit Agent confirmation and writes an immutable `.sagasmith-pack` archive. Only a module re-imported from that finalized archive may be activated.

Commercial rulebooks and scenarios remain local. Configure allowed source roots with `SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS`, separated by the platform path separator. Source books and extracted assets are never bundled in this repository.

Pack import uses a deterministic recovery protocol. The Pack checksum belongs to candidate
version identity, while module, asset, content-review, actor, and binding steps converge by
content identity or child idempotency keys. If the process stops before the final receipt,
retry the original request and `idempotency_key`; no duplicate runtime objects are created.
Activation, deactivation, and removal each commit an exact receipt, including replay after
the target was removed.

## Run

```bash
pip install -e "../sagasmith-core[documents]"
pip install -e ../sagasmith-coc
pip install -e .
sagasmith-coc-mcp
```

The unified local stack runs the authority over streamable HTTP and serves the
Workbench through a sticky-session gateway:

```powershell
$env:SAGASMITH_COC_MCP_TRANSPORT = "streamable-http"
$env:SAGASMITH_COC_MCP_HTTP_PORT = "8769"
sagasmith-coc-mcp

# another terminal
$env:SAGASMITH_COC_MCP_URL = "http://127.0.0.1:8769/mcp"
$env:SAGASMITH_COC_GATEWAY_PORT = "8768"
sagasmith-coc-gateway
```

The browser never submits a principal. The gateway binds identity server-side,
keeps an MCP session sticky per browser/campaign, and refreshes the native list
after `tools/list_changed`.

State defaults to `.sagasmith-coc-mcp/`. The main configuration variables are:

- `SAGASMITH_COC_MCP_HOME`
- `SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS`
- `SAGASMITH_COC_SKILLS_DIR`
- `SAGASMITH_MODULEGEN_SKILLS_DIR`
- `SAGASMITH_COC_MCP_BOUND_PRINCIPAL_ID`

The server applies Core Alembic migrations at startup and requires the current
Snapshot schema v8. Before deployment, stop the server and take a consistent
backup of `data/ttrpgbase.db` after its SQLite WAL has settled, or use the
external database's native backup mechanism. There is no downgrade or
dual-protocol mode; rollback restores the database together with matching Core,
CoC, and MCP versions as one unit.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

Original code is licensed under Apache-2.0. Call of Cthulhu and related commercial content remain the property of their respective rights holders.
