"""Factory tests for selecting the Kanban storage backend."""

from __future__ import annotations

import pytest


def test_default_kanban_store_is_sqlite_when_backend_unset(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)

    from hermes_cli.kanban_store_factory import get_default_kanban_store
    from hermes_cli.kanban_store_sqlite import SQLiteKanbanStore

    store = get_default_kanban_store()

    assert isinstance(store, SQLiteKanbanStore)
    assert store.capabilities.backend == "sqlite"


def test_default_kanban_store_accepts_explicit_sqlite_backend(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "sqlite")

    from hermes_cli.kanban_store_factory import get_default_kanban_store
    from hermes_cli.kanban_store_sqlite import SQLiteKanbanStore

    assert isinstance(get_default_kanban_store(), SQLiteKanbanStore)


def test_default_kanban_store_reads_config_backend_when_env_unset(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)

    import hermes_cli.kanban_store_factory as factory
    from hermes_cli.kanban_store_postgres import PostgresKanbanStore

    monkeypatch.setattr(factory, "_load_config_backend", lambda: "postgres")

    assert isinstance(factory.get_default_kanban_store(), PostgresKanbanStore)


def test_default_kanban_store_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "mysql")

    from hermes_cli.kanban_store_factory import get_default_kanban_store

    with pytest.raises(ValueError, match="Unsupported Kanban store backend"):
        get_default_kanban_store()


def test_runtime_connection_seam_is_sqlite_by_default(monkeypatch):
    """The chokepoint seam returns None for the default backend, so
    ``kanban_db.connect`` falls through to the unchanged SQLite path."""
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)

    import hermes_cli.kanban_store_factory as factory

    monkeypatch.setattr(factory, "_config_backend_cache", (False, None))
    monkeypatch.setattr(factory, "_load_config_backend", lambda: None)

    assert factory.resolve_backend_name() == "sqlite"
    assert factory.maybe_runtime_connection(board="default") is None


def test_runtime_connection_seam_routes_to_configured_backend(monkeypatch):
    """With a non-sqlite backend configured, the seam returns that backend's
    live connection, which is what ``kanban_db.connect`` hands every call site
    (worker tools, CLI, dispatcher, heartbeat bridge) without per-site edits."""
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")

    import hermes_cli.kanban_store_factory as factory

    sentinel = object()
    monkeypatch.setattr(
        "hermes_cli.kanban_store_postgres.open_runtime_connection",
        lambda: sentinel,
    )

    assert factory.resolve_backend_name() == "postgres"
    assert factory.maybe_runtime_connection(board="default") is sentinel
