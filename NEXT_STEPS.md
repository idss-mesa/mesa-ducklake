# mesa-ducklake: status and next steps

A current-status page: what works, what is known to be open, and what
comes next. It carries no host names, credentials or machine-specific
paths. Deployment details belong in `docs/deploy/` and in your own ops
notes. Last reviewed 2026-09-24.

## Done

- **`DuckLakeClient` API** (the only public surface):
  `register_project`, `get_project`, `find_project_by_path`,
  `record_changes`, `get_avus`, `get_avus_as_of`, `get_history`,
  `list_snapshots`, `diff` and `recover_pending_pushes`. Reads and
  writes accept a per-call `session=`. See
  [`docs/user/usage.md`](docs/user/usage.md).
- **Two catalog backends behind the `CatalogStore` Protocol.**
  Postgres (numbered migrations `0001`, `0002`) and a single-writer
  DuckDB file (inline schema bootstrap). The backend is selected from
  the DSN by `open_catalog`. See
  [`docs/deploy/duckdb-catalog.md`](docs/deploy/duckdb-catalog.md).
- **iRODS sync sidecar with push-before-commit.** Snapshot rows start
  `'pending'`, the `mesa.pending_pushes` WAL row covers crashes, the
  Parquet push is checksum-verified, and the catalog flip is the
  commit point.
- **Local Parquet cache** under `platformdirs.user_cache_dir`, with
  LRU-by-mtime eviction (`cache_cap_bytes`).
- **Configurable Parquet sub-collection** (`data_collection`), fixed
  per project at registration.
- **CLI** `mesa-ducklake record | recover | migrate`, which accepts
  Postgres or DuckDB DSNs. See [`docs/user/cli.md`](docs/user/cli.md).
- **Catalog backup**: a daily `pg_dump`-to-iRODS systemd timer, plus a
  documented recovery procedure. See
  [`docs/deploy/backup.md`](docs/deploy/backup.md).
- **iRODS rule files** in [`irods-rules/`](irods-rules/). They are
  written but not deployed (see below).
- **CI** runs the Postgres-backed tests against a service container.
- **Claude Code agents and skills** in [`.claude/`](.claude/), and a
  vendor-neutral [`AGENTS.md`](AGENTS.md).

## Open issues

- **iRODS rule deployment on production.** The rule callbacks
  (`mesa_avu_change.re` / `.py`, `mesa_enroll_policy.re`) have not been
  installed on a production iRODS server. Until they are, AVU changes
  made outside mesa-mcp are not recorded: `imeta`, `irods-mcp-server`,
  the esiil-portal UI, and ticket sessions that bypass mesa-mcp. See
  [`docs/deploy/irods-rules.md`](docs/deploy/irods-rules.md).
- **`record` does not push to iRODS.** The CLI builds its client with
  `irods_session=None`, so rule-captured snapshots are committed in
  the catalog, but their Parquet stays in the rule host's local cache.
  A read from any other host cannot pull it, and eviction on the rule
  host can delete it. This must be fixed before rule deployment is
  useful. Options include giving `record` an iRODS session, or
  writing a WAL row so that `recover` pushes the file later.
- **Security-review leftovers in the `.re` rules.** Two items were
  excluded from the 2026-05-11 review and are still unaddressed (see
  [`SECURITY_REVIEW_2026-05-11.md`](SECURITY_REVIEW_2026-05-11.md)):
  - `mesa_avu_change.re` builds the stdin JSON with raw `msiStrCat`
    and does no JSON escaping of AVU strings. A `"` or `\` in an
    attribute, value or unit corrupts or reshapes the payload.
  - `mesa_enroll_policy.re` interpolates `*parent` into a GenQuery
    string without escaping (GenQuery injection).
- **Newer surfaces not security-reviewed**: `catalog_duckdb.py`,
  `irods_sync.py`, `open_catalog`, and the CLI `recover` / `migrate`
  verbs. See
  [`docs/dev/architecture-review-2026-09.md`](docs/dev/architecture-review-2026-09.md).
- **Checksum verification can pass vacuously.** `irods_sync` treats an
  empty server checksum, or one with an unknown algorithm, as a match.
  On a zone that never computes checksums, pushes are effectively
  unverified. Enable server-side checksums until this is fixed
  (review item D4).
- **Delete/unit mismatch gotcha.** Effective AVUs are computed by
  partitioning on the full `(attribute, value, unit)` triple. A
  `delete` whose `unit` differs from the unit of the original `add`
  does not supersede that add, so the AVU still looks set. Examples
  are `""` versus an ontology CURIE, or a client that drops the unit
  on removal. This is correct per the canonical-triple contract, but
  callers (mesa-mcp, the rules) must send the exact stored unit on
  delete. Consider a warning or a lookup helper.
- **Move/rename loses AVU history continuity.** History is keyed by
  `irods_path`. When a data object or collection is moved or renamed
  in iRODS, the new path starts with an empty history, and the old
  path's history is orphaned. Needs a design: a rename event, a path
  alias table, or a rule hook on `acPostProcForObjRename`.
- **`'failed'` snapshots are visible in `list_snapshots`.** Rows that
  recovery gives up on are filtered only from AVU reads, because they
  have no file. `list_snapshots` and `latest_snapshot_id` filter only
  `'pending'`, so a `failed` snapshot can become the next snapshot's
  `parent_snapshot`.
- **Live LLM e2e runs.** The `live_e2e` / `llm_e2e` tiers under
  `tests/llm_e2e/` exist, but they need real iRODS and LLM endpoints
  and have not been run on a schedule. See
  [`docs/dev/llm-e2e-tests.md`](docs/dev/llm-e2e-tests.md).

## Next

1. Close the `record`-does-not-push gap, then fix JSON escaping and
   GenQuery escaping in the `.re` rules.
2. Validate the rules on a disposable iRODS server: install, fire
   `irule -F`, confirm the catalog row and the Parquet in iRODS. Then
   coordinate a production deployment with the iRODS admins.
3. **Snapshot compaction.** Consolidate a project's many small
   per-snapshot Parquet files. Reads cap `ensure_cached` at 1000
   snapshots per project today.
4. **Postgres WAL shipping to iRODS** (`archive_command`) for
   sub-minute catalog RPO, beyond the daily `pg_dump`.
5. A design for move/rename history continuity.
6. `mesa-ducklake fsck` to reconcile `.mesa/ducklake/` contents with
   `mesa.snapshots` after a restore (see `docs/deploy/backup.md`).
7. A regular schedule for the live and LLM e2e tiers, with results
   triaged by the `llm-e2e-triage` agent.

## Pointers

- Architecture and contracts: [`CLAUDE.md`](CLAUDE.md), and
  [`AGENTS.md`](AGENTS.md) for other agents.
- Docs entry: [`docs/README.md`](docs/README.md).
- Companion server: [idss-mesa/mesa-mcp](https://github.com/idss-mesa/mesa-mcp).
