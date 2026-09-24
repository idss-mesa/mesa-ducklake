"""``mesa-ducklake`` command-line entry point.

Three verbs:

* ``record`` — the rule-engine callback path. Reads a single JSON
  object from stdin describing one AVU change and records it. See
  ``irods-rules/mesa_avu_change.re`` for the rule that drives it.
* ``recover`` — drain the ``mesa.pending_pushes`` WAL. Used by ops on
  service start or after an iRODS outage to re-push any in-flight
  Parquet snapshots that didn't commit. Needs an iRODS session,
  constructed from ``~/.irods/`` (or ``IRODS_ENVIRONMENT_FILE``).
* ``migrate [--target N]`` — apply pending Postgres migrations. On a
  DuckDB-file catalog there is nothing to migrate (that backend creates
  its schema on open), so it bootstraps the file and reports
  ``applied: 0``.

``MESA_DUCKLAKE_DSN`` selects the catalog for every verb: a
``postgresql://`` URI or libpq keyword DSN, or ``duckdb:///abs/path.duckdb``.

This CLI is the **only** sanctioned non-Python interface to
mesa-ducklake. Everything else uses :class:`DuckLakeClient`.

``record`` wire contract
------------------------

Input — single JSON object on stdin::

    {
      "project_id"?:    "<uuid>",      # optional; resolved by path walk if absent
      "irods_path":     "/iplant/...", # path the AVU is bound to
      "target_type":    "data_object" | "collection" | "resource" | "user",
      "attribute":      "...",
      "value":          "...",
      "unit":           "...",         # may be ""
      "op":             "add" | "delete",
      "actor":          "<irods_user>",
      "ts"?:            "<RFC3339>",   # default: now()
      "source":         "irods-rule:<event>",
      "via_ticket"?:    "<ticket id>",
      "rule_invocation"?: "<rule name>"
    }

Output (on success) — single JSON object on stdout::

    {"snapshot_id": <int>, "parquet_file": "<path>"}

``recover`` wire contract
-------------------------

No stdin. Optional flags consumed before the verb is parsed.

Output (on success) — single JSON object on stdout summarising the
counts: ``{"pushed": N, "committed": N, "failed": N, "orphaned": N,
"missing_local": N}``.

Exit codes
----------

* ``0`` — success (record committed, or recovery completed)
* ``1`` — generic error (catalog/lake failure, malformed JSON, etc.)
* ``2`` — path is not within any MESA-enabled project (``record``
  only; the rule may silently skip this one)
  — or, for ``migrate``, the migration failed
* ``3`` — ``MESA_DUCKLAKE_DSN`` is unset
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import IO, Any
from uuid import UUID

from mesa_ducklake.client import DuckLakeClient
from mesa_ducklake.models import AvuChange


def _stderr_json(stream: IO[str], obj: dict[str, Any]) -> None:
    """Emit a structured error envelope on stderr.

    Single-line JSON keeps the rule-engine log readable while still being
    machine-parseable. The rule may need to switch on the ``code`` field.
    """
    json.dump(obj, stream)
    stream.write("\n")


def _walk_to_project(
    client: DuckLakeClient,
    irods_path: str,
) -> Any | None:
    """Walk up ``irods_path``'s parents looking for a known project.

    Returns the first ``Project`` whose ``irods_path`` is a prefix of
    the supplied path, or ``None``. We can't reach iRODS from the rule
    callback (it's a fork+exec context), so we rely on the catalog
    being authoritative: a project must have been registered via
    ``mesa_ducklake_init_project`` for the path to resolve.
    """
    current = irods_path
    while current and current != "/":
        project = client.find_project_by_path(current)
        if project is not None:
            return project
        parent = current.rsplit("/", 1)[0]
        if not parent or parent == current:
            break
        current = parent
    return None


def _parse_ts(raw: Any) -> datetime | None:
    """Parse an RFC3339-ish timestamp into a tz-aware UTC datetime.

    Returns ``None`` when ``raw`` is missing or empty so the model's
    ``default_factory`` (``now(tz=UTC)``) takes effect.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def main(
    argv: list[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the CLI.

    Returns an integer exit code so unit tests can call ``main(argv=[])``
    in-process and assert on the result. The default ``argparse``-style
    main wraps :func:`main` with ``sys.exit`` (see :func:`_console_main`).
    """
    argv = argv if argv is not None else sys.argv[1:]
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    # Subcommand routing. ``record`` is the default for back-compat
    # with the iRODS rule callback that just runs ``mesa-ducklake``
    # with no verb.
    if not argv:
        verb = "record"
    else:
        verb = argv[0]
    if verb not in {"record", "recover", "migrate"}:
        _stderr_json(
            stderr,
            {"code": "unknown_verb", "message": f"unknown verb {verb!r}"},
        )
        return 1

    dsn = os.environ.get("MESA_DUCKLAKE_DSN")
    if not dsn:
        _stderr_json(
            stderr,
            {
                "code": "missing_dsn",
                "message": (
                    "MESA_DUCKLAKE_DSN environment variable is unset. "
                    "Set it to the mesa-ducklake catalog DSN: "
                    "postgresql://… (or a libpq keyword DSN) or "
                    "duckdb:///abs/path.duckdb."
                ),
            },
        )
        return 3

    if verb == "recover":
        return _recover(dsn, stdout, stderr)

    if verb == "migrate":
        return _migrate(dsn, argv[1:], stdout, stderr)

    try:
        raw = stdin.read()
        if not raw.strip():
            raise ValueError("stdin was empty; expected a JSON object")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("stdin JSON must be an object")
    except (ValueError, json.JSONDecodeError) as exc:
        _stderr_json(
            stderr,
            {"code": "invalid_input", "message": str(exc)},
        )
        return 1

    return _record(payload, dsn, stdout, stderr)


def _migrate(
    dsn: str, args: list[str], stdout: IO[str], stderr: IO[str]
) -> int:
    """Apply pending Postgres migrations.

    :func:`mesa_ducklake.schema.apply_migrations` is documented as the
    only code path that mutates the live schema, but nothing outside the
    test suite called it — an operator had no supported way to create the
    ``mesa`` schema before first use. This verb is that way.

    Accepts an optional ``--target N`` to stop at a specific migration
    version, which is what makes a staged rollout or a bisect possible.
    Idempotent: re-running applies nothing and reports ``applied: 0``.
    """
    from mesa_ducklake.catalog import catalog_backend, open_catalog
    from mesa_ducklake.schema import apply_migrations

    target: int | None = None
    if args:
        if args[0] != "--target" or len(args) < 2:
            _stderr_json(
                stderr,
                {
                    "code": "invalid_input",
                    "message": "usage: mesa-ducklake migrate [--target N]",
                },
            )
            return 1
        try:
            target = int(args[1])
        except ValueError:
            _stderr_json(
                stderr,
                {
                    "code": "invalid_input",
                    "message": f"--target must be an integer, got {args[1]!r}",
                },
            )
            return 1

    try:
        backend = catalog_backend(dsn)
        if backend == "duckdb":
            # The DuckDB catalog has no migration history: opening it runs
            # its idempotent schema bootstrap. Doing that here still gives
            # operators one command that prepares either backend.
            open_catalog(dsn).close()
            applied = 0
        else:
            applied = apply_migrations(dsn, target=target)
    except Exception as exc:
        _stderr_json(
            stderr,
            {"code": "migration_failed", "message": f"{type(exc).__name__}: {exc}"},
        )
        return 2

    json.dump({"applied": applied, "target": target, "backend": backend}, stdout)
    stdout.write("\n")
    return 0


def _recover(dsn: str, stdout: IO[str], stderr: IO[str]) -> int:
    """Workhorse for the ``recover`` verb.

    Opens an iRODS session from the standard env files (``~/.irods/``
    by default; overridable via ``IRODS_ENVIRONMENT_FILE``), then
    drains the WAL via :meth:`DuckLakeClient.recover_pending_pushes`.
    """
    from irods.session import iRODSSession

    from mesa_ducklake.client import DuckLakeClient

    env_file = os.environ.get(
        "IRODS_ENVIRONMENT_FILE",
        os.path.expanduser("~/.irods/irods_environment.json"),
    )

    try:
        with iRODSSession(irods_env_file=env_file) as session:
            with DuckLakeClient(
                catalog_dsn=dsn, irods_session=session
            ) as client:
                summary = client.recover_pending_pushes()
    except FileNotFoundError as exc:
        _stderr_json(
            stderr,
            {
                "code": "irods_env_missing",
                "message": (
                    f"iRODS environment file not readable ({exc}); "
                    "set IRODS_ENVIRONMENT_FILE or run iinit"
                ),
            },
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - surface anything else uniformly
        _stderr_json(
            stderr,
            {"code": "recover_failed", "message": str(exc)},
        )
        return 1

    json.dump(summary, stdout)
    stdout.write("\n")
    return 0


def _record(
    payload: dict[str, Any],
    dsn: str,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    """Workhorse for the ``record`` verb. Split out for unit-test clarity."""
    try:
        irods_path = str(payload["irods_path"])
        target_type = str(payload["target_type"])
        attribute = str(payload["attribute"])
        value = str(payload["value"])
        unit = str(payload.get("unit") or "")
        op = str(payload["op"])
        actor = str(payload["actor"])
        source = str(payload.get("source") or "mesa-ducklake-cli")
    except KeyError as exc:
        _stderr_json(
            stderr,
            {"code": "invalid_input", "message": f"missing required field: {exc.args[0]!r}"},
        )
        return 1

    via_ticket = payload.get("via_ticket")
    rule_invocation = payload.get("rule_invocation")

    try:
        ts = _parse_ts(payload.get("ts"))
    except ValueError as exc:
        _stderr_json(
            stderr,
            {"code": "invalid_input", "message": f"bad ts: {exc}"},
        )
        return 1

    try:
        client = DuckLakeClient(catalog_dsn=dsn, irods_session=None)
    except Exception as exc:  # noqa: BLE001 - surface anything from psycopg
        _stderr_json(
            stderr,
            {"code": "catalog_unreachable", "message": str(exc)},
        )
        return 1

    try:
        # Resolve the project.
        if payload.get("project_id"):
            try:
                project_id = UUID(str(payload["project_id"]))
            except ValueError as exc:
                _stderr_json(
                    stderr,
                    {"code": "invalid_input", "message": f"bad project_id: {exc}"},
                )
                return 1
            try:
                project = client.get_project(project_id)
            except KeyError:
                _stderr_json(
                    stderr,
                    {
                        "code": "unknown_project",
                        "message": f"project {project_id} not found in catalog",
                    },
                )
                return 1
        else:
            project = _walk_to_project(client, irods_path)
            if project is None:
                # Distinct exit code so the rule can decide whether to log
                # this as "not MESA-enabled" (expected) vs. a real error.
                _stderr_json(
                    stderr,
                    {
                        "code": "not_mesa_enabled",
                        "message": (
                            f"no MESA project covers {irods_path!r}; nothing "
                            "to record (run mesa_ducklake_init_project to "
                            "enroll the project root)"
                        ),
                        "irods_path": irods_path,
                    },
                )
                return 2

        # Build the AvuChange.
        change_kwargs: dict[str, Any] = {
            "irods_path": irods_path,
            "target_type": target_type,
            "attribute": attribute,
            "value": value,
            "unit": unit,
            "op": op,
            "actor": actor,
            "source": source,
            "via_ticket": via_ticket if via_ticket else None,
            "rule_invocation": rule_invocation if rule_invocation else None,
        }
        if ts is not None:
            change_kwargs["ts"] = ts

        try:
            change = AvuChange(**change_kwargs)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            _stderr_json(
                stderr,
                {"code": "invalid_input", "message": str(exc)},
            )
            return 1

        # Record the change.
        try:
            snapshot = client.record_changes(
                project_id=project.project_id,
                actor=actor,
                changes=[change],
                note=f"{op} AVU via {source}",
            )
        except Exception as exc:  # noqa: BLE001
            _stderr_json(
                stderr,
                {"code": "record_failed", "message": str(exc)},
            )
            return 1

        json.dump(
            {
                "snapshot_id": snapshot.snapshot_id,
                "parquet_file": snapshot.parquet_file,
                "project_id": str(project.project_id),
            },
            stdout,
        )
        stdout.write("\n")
        return 0
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - never let teardown crash exit code
            pass


def _console_main() -> None:  # pragma: no cover - thin wrapper
    """Entry point for ``[project.scripts]``. Exits with the CLI's status."""
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    _console_main()
