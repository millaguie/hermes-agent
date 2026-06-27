import os
import uuid

import pytest

from hermes_cli.kanban_store_factory import create_kanban_store


pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_TEST_POSTGRES_DSN"),
    reason="set HERMES_TEST_POSTGRES_DSN to run local PostgreSQL Kanban store tests",
)


def _dsn_with_schema(base: str, schema: str) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options=-csearch_path%3D{schema}"


def test_postgres_store_initializes_and_supports_basic_task_flow(monkeypatch):
    base_dsn = os.environ["HERMES_TEST_POSTGRES_DSN"]
    schema = "test_kanban_" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("HERMES_KANBAN_POSTGRES_DSN", _dsn_with_schema(base_dsn, schema))
    store = create_kanban_store("postgres")

    with store.connect(board="contract") as conn:
        task_id = store.create_task(
            conn,
            title="postgres contract task",
            body="created in local postgres contract test",
            assignee="qa",
            created_by="pytest",
            initial_status="running",
            board="contract",
        )
        listed = store.list_tasks(conn, status="ready")
        claimed = store.claim_task(conn, task_id, ttl_seconds=30, claimer="pytest")
        assert claimed is not None
        assert claimed.id == task_id
        assert any(task.id == task_id for task in listed)
        assert store.complete_task(conn, task_id, summary="done from postgres contract") is True
        assert store.get_task(conn, task_id).status == "done"
        stats = store.board_stats(conn)
        assert stats["done"] >= 1

    # cleanup schema explicitly
    import psycopg
    with psycopg.connect(base_dsn, autocommit=True) as cleanup:
        cleanup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_postgres_store_connect_closing_persists(monkeypatch):
    """``connect_closing`` (the CLI write primitive) must reach PostgreSQL.

    Regression for the CLI write path silently falling through to the SQLite
    ``kanban_db.connect_closing`` via ``__getattr__`` — which made
    ``hermes kanban create`` report success while writing to a local
    ``kanban.db`` instead of the configured PostgreSQL DSN.
    """
    base_dsn = os.environ["HERMES_TEST_POSTGRES_DSN"]
    schema = "test_kanban_" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("HERMES_KANBAN_POSTGRES_DSN", _dsn_with_schema(base_dsn, schema))
    store = create_kanban_store("postgres")

    with store.connect_closing(board="contract") as conn:
        task_id = store.create_task(
            conn,
            title="connect_closing task",
            assignee="qa",
            created_by="pytest",
            initial_status="running",
            board="contract",
        )

    # A *fresh* short-lived connection must see the row, proving the previous
    # connection committed before it closed.
    with store.connect_closing() as conn2:
        fetched = store.get_task(conn2, task_id)
    assert fetched is not None
    assert fetched.id == task_id

    import psycopg
    with psycopg.connect(base_dsn, autocommit=True) as cleanup:
        cleanup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_cli_kanban_create_round_trips_to_postgres(monkeypatch, tmp_path):
    """End-to-end CLI ``create`` → ``show`` round-trip on PostgreSQL.

    Also asserts no SQLite ``kanban.db`` is written, which is the observable
    symptom of the CLI write path falling through to the SQLite backend.
    """
    from hermes_cli import kanban as kanban_cli

    base_dsn = os.environ["HERMES_TEST_POSTGRES_DSN"]
    schema = "test_kanban_" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_POSTGRES_DSN", _dsn_with_schema(base_dsn, schema))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # The CLI module binds its store at import time; rebind for this backend.
    monkeypatch.setattr(kanban_cli, "kb", create_kanban_store("postgres"))

    import argparse

    root = argparse.ArgumentParser(prog="hermes")
    subparsers = root.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    args = root.parse_args(
        ["kanban", "create", "cli round trip", "--assignee", "scout"]
    )
    rc = kanban_cli.kanban_command(args)
    assert rc == 0

    import psycopg
    with psycopg.connect(_dsn_with_schema(base_dsn, schema), autocommit=True) as check:
        rows = check.execute(
            "SELECT id, title, assignee FROM tasks WHERE title = %s",
            ("cli round trip",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][2] == "scout"

    # The CLI must NOT have fallen back to a local SQLite kanban.db.
    assert not list(tmp_path.rglob("kanban.db")), "CLI wrote to SQLite instead of PostgreSQL"

    with psycopg.connect(base_dsn, autocommit=True) as cleanup:
        cleanup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
