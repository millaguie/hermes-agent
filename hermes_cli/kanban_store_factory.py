"""Kanban storage backend selection.

The selector defaults to the existing SQLite implementation so runtime
behavior remains unchanged unless the operator explicitly selects a backend
through ``HERMES_KANBAN_BACKEND`` or ``kanban.storage.backend`` in config.yaml.
"""

from __future__ import annotations

import os
from typing import Any

from hermes_cli.kanban_store import KanbanStore
from hermes_cli.kanban_store_sqlite import SQLiteKanbanStore

_BACKEND_ENV = "HERMES_KANBAN_BACKEND"
_DEFAULT_BACKEND = "sqlite"
_PG_NAMES = {"postgres", "postgresql"}

# Cache for the config.yaml-derived backend only. The env var is always read
# live (tests flip it per-run), but the config read happens on the hot path of
# kanban_db.connect() via maybe_runtime_connection(), so we resolve it once.
# Runtime config is static for a long-lived process; tests drive the backend
# through HERMES_KANBAN_BACKEND, which bypasses this cache.
_config_backend_cache: tuple[bool, str | None] = (False, None)


def normalize_backend_name(value: str | None) -> str:
    """Normalize a configured backend name, defaulting to SQLite."""
    name = (value or _DEFAULT_BACKEND).strip().lower()
    return name or _DEFAULT_BACKEND


def _load_config_backend() -> str | None:
    """Read the optional Kanban storage backend from config.yaml."""
    try:
        from hermes_cli.config import load_config

        cfg: dict[str, Any] = load_config()
        kanban_cfg = cfg.get("kanban") or {}
        storage_cfg = kanban_cfg.get("storage") or {}
        backend = storage_cfg.get("backend") or kanban_cfg.get("storage_backend")
        return str(backend) if backend else None
    except Exception:
        return None


def _config_backend_cached() -> str | None:
    global _config_backend_cache
    resolved, value = _config_backend_cache
    if not resolved:
        value = _load_config_backend()
        _config_backend_cache = (True, value)
    return value


def resolve_backend_name() -> str:
    """Return the active backend name from env (live) then config (cached)."""
    return normalize_backend_name(os.environ.get(_BACKEND_ENV) or _config_backend_cached())


_runtime_backend_logged = False


def _log_runtime_backend_once(name: str) -> None:
    """Emit one INFO line the first time a non-sqlite backend opens a runtime
    connection, so operators can confirm the active backend without a dispatcher
    edit. Best-effort: logging must never break connection opening."""
    global _runtime_backend_logged
    if _runtime_backend_logged:
        return
    _runtime_backend_logged = True
    try:
        import logging

        logging.getLogger("hermes_cli.kanban").info("kanban: storage backend=%s", name)
    except Exception:
        pass


def maybe_runtime_connection(board: str | None = None):
    """Return a live non-sqlite kanban connection if one is configured, else None.

    Single bootstrap seam for the backend chokepoint in ``kanban_db.connect``:
    when the operator selects e.g. ``postgres``, every caller that opens a
    connection through ``connect()`` / ``connect_closing()`` (worker tools,
    CLI, dispatcher, heartbeat bridge) transparently gets that backend, so the
    fork touches one hunk in ``kanban_db`` instead of every call site.

    ``board`` is accepted for signature parity with ``kanban_db.connect``; the
    Postgres backend keys off the configured DSN/schema, not per-board files.
    Returns ``None`` for the default SQLite backend so the caller falls through
    to the unchanged SQLite path.
    """
    name = resolve_backend_name()
    if name in _PG_NAMES:
        from hermes_cli.kanban_store_postgres import open_runtime_connection

        _log_runtime_backend_once(name)
        return open_runtime_connection()
    return None


def create_kanban_store(backend: str | None = None) -> KanbanStore:
    """Create a Kanban store for ``backend``."""
    name = normalize_backend_name(backend)
    if name == "sqlite":
        return SQLiteKanbanStore()
    if name in {"postgres", "postgresql"}:
        # Imported lazily so a missing psycopg never breaks the default
        # SQLite path (this module is imported by the gateway dispatcher).
        from hermes_cli.kanban_store_postgres import PostgresKanbanStore

        return PostgresKanbanStore()
    raise ValueError(
        f"Unsupported Kanban store backend {name!r}. "
        "Supported backends: sqlite, postgres"
    )


def get_default_kanban_store() -> KanbanStore:
    """Return the store selected by env/config defaults."""
    return create_kanban_store(os.environ.get(_BACKEND_ENV) or _load_config_backend())


__all__ = [
    "create_kanban_store",
    "get_default_kanban_store",
    "maybe_runtime_connection",
    "normalize_backend_name",
    "resolve_backend_name",
]
