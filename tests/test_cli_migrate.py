"""The ``migrate`` verb.

``schema.apply_migrations`` is documented as the only code path that
mutates the live Postgres schema, but nothing outside the test suite
called it: an operator had no supported way to create the ``mesa`` schema
before first use, short of importing the function in a Python shell.

These tests cover the argument handling and error paths, which need no
Postgres. The migration behaviour itself is covered by ``test_schema.py``
(skipped without a Postgres binary).
"""

from __future__ import annotations

import io
import json

import pytest

from mesa_ducklake.cli import main

# A DSN that cannot connect ANYWHERE, including on CI. An earlier
# version used localhost:5432, which is unreachable on a developer
# machine but *is* listening on CI (the Postgres service container) --
# so the "migration fails cleanly" assertions depended on the
# environment. Port 1 is privileged and never serves Postgres.
DSN = "postgresql://user@127.0.0.1:1/nonexistent_for_tests?connect_timeout=1"


def _run(argv, env_dsn=DSN, monkeypatch=None):
    if monkeypatch is not None:
        if env_dsn is None:
            monkeypatch.delenv("MESA_DUCKLAKE_DSN", raising=False)
        else:
            monkeypatch.setenv("MESA_DUCKLAKE_DSN", env_dsn)
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdin=io.StringIO(""), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_migrate_is_a_recognised_verb(monkeypatch):
    """Regression: before this, ``migrate`` was rejected as unknown."""
    code, _, err = _run(["migrate"], monkeypatch=monkeypatch)
    assert "unknown_verb" not in err
    # Connecting to a nonexistent database must fail cleanly, not crash.
    assert code == 2
    assert json.loads(err)["code"] == "migration_failed"


def test_migrate_requires_a_dsn(monkeypatch):
    code, _, err = _run(["migrate"], env_dsn=None, monkeypatch=monkeypatch)
    assert code == 3
    assert json.loads(err)["code"] == "missing_dsn"


@pytest.mark.parametrize("args", [["--bogus"], ["--target"], ["--target", "notanint"]])
def test_migrate_rejects_bad_arguments(args, monkeypatch):
    code, _, err = _run(["migrate", *args], monkeypatch=monkeypatch)
    assert code == 1
    assert json.loads(err)["code"] == "invalid_input"


def test_migration_failure_is_reported_as_json_not_a_traceback(monkeypatch):
    """An operator running this at deploy time needs a parseable error."""
    code, _, err = _run(["migrate"], monkeypatch=monkeypatch)
    assert code == 2
    payload = json.loads(err)
    assert payload["code"] == "migration_failed"
    assert payload["message"]


def test_other_verbs_still_route(monkeypatch):
    """Adding a verb must not disturb the existing two."""
    code, _, err = _run(["nonsense"], monkeypatch=monkeypatch)
    assert code == 1
    assert json.loads(err)["code"] == "unknown_verb"
