# Security Review — 2026-05-11

> **Status as of 2026-09-24.** This is a historical, point-in-time
> review. The two items listed below under *Items observed but
> excluded* are **still unaddressed**:
>
> - JSON escaping in `irods-rules/mesa_avu_change.re`;
> - GenQuery interpolation in `irods-rules/mesa_enroll_policy.re`.
>
> Both are tracked in [`NEXT_STEPS.md`](NEXT_STEPS.md). Surfaces added
> since this review have **not** been security-reviewed:
>
> - `src/mesa_ducklake/catalog_duckdb.py` (DuckDB-file catalog);
> - `src/mesa_ducklake/irods_sync.py` (push/pull and recovery);
> - `open_catalog` DSN dispatch in `src/mesa_ducklake/catalog.py`;
> - the `recover` and `migrate` CLI verbs.
>
> See [`docs/dev/architecture-review-2026-09.md`](docs/dev/architecture-review-2026-09.md)
> for the current architecture assessment. The commit and repository
> names below (`cyverse/…`) are as they were at review time. The
> repositories now live under `idss-mesa/`.

Point-in-time review of the initial build of mesa-ducklake and the
sibling mesa-mcp. Covers commits `cyverse/mesa-mcp@5b7a5da` and
`cyverse/mesa-ducklake@896dce3`.

The full review lives in
[`../mesa-mcp/SECURITY_REVIEW_2026-05-11.md`](../mesa-mcp/SECURITY_REVIEW_2026-05-11.md).
This copy is kept here so anyone reading mesa-ducklake first finds the
review without a cross-repo hop.

**Result: zero findings at confidence ≥ 8.**

## Surfaces specifically reviewed in this repo

| Surface | File(s) | Verdict |
|---|---|---|
| SQL injection (Postgres + DuckDB) | `src/mesa_ducklake/{catalog,lake,queries,schema}.py` | All Postgres queries use `%s` placeholders; DuckDB uses `?` placeholders; sole f-string into DuckDB (`lake.py:213-217`) escapes `'`→`''` over a fixed column tuple; migration runner only consumes numbered SQL files in the repo (trusted input). |
| subprocess / shell exec | `irods-rules/mesa_avu_change.py:34` | Fixed argv, no `shell=True`, payload via `input=` kwarg. Server-side admin-installed code. |
| Pickle / unsafe deserialization | repo-wide | None. `json.loads` only. |
| CLI JSON contract | `src/mesa_ducklake/cli.py` | JSON via `json.loads`, body through Pydantic, SQL parameterized; not a remote interface. |

## Items observed but excluded

- **JSON-payload spoofing via AVU values in `irods-rules/mesa_avu_change.re`.**
  Raw `msiStrCat` of user-controlled AVU strings into the JSON sent to
  `mesa-ducklake record`. Legitimate `actor`/`source` fields are written
  *after* user-controlled keys; `json.loads` is last-key-wins, so the
  actor/source can't be spoofed. Worst case: corrupted payload, CLI
  rejects with non-zero exit, rule ignores. Excluded as log spoofing.
- **`irods-rules/mesa_enroll_policy.re` GenQuery interpolation of
  `*parent`.** A collection name containing `'` could perturb the
  auto-enroll check; effect bounded to the user's own newly-created
  collection, a privilege they already have. No boundary crossed.

## Conclusion

mesa-ducklake's threat-relevant surfaces (catalog SQL, DuckDB lake
writes, the `mesa-ducklake record` CLI, the iRODS rule callbacks) are
implemented carefully. No newly-introduced exploitable vulnerability
meets the confidence-≥-8 bar.

See [`../mesa-mcp/SECURITY_REVIEW_2026-05-11.md`](../mesa-mcp/SECURITY_REVIEW_2026-05-11.md)
for the full attack-surface map and methodology.
