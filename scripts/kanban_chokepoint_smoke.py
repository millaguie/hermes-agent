"""Prove the kanban_db.connect() chokepoint routes to the configured backend.

This is the test the store-level stress harness does NOT cover: it exercises the
*upstream call path* (kb.connect / kb.create_task / `with kb.connect()`), which
is what worker tools, the CLI and the dispatcher use after the refactor.
"""
from __future__ import annotations

import os
import tempfile
import uuid

BASE_DSN = os.environ["HERMES_TEST_POSTGRES_DSN"]
fails = []


def check(name, ok, detail=""):
    print(f"  [{'OK' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        fails.append(name)


# ---- 1. default (no backend) -> kb.connect() returns a real sqlite3.Connection
import sqlite3

home = tempfile.mkdtemp(prefix="choke_sqlite_")
os.environ["HERMES_HOME"] = home
os.environ["HOME"] = home
os.environ.pop("HERMES_KANBAN_BACKEND", None)
os.environ.pop("HERMES_KANBAN_POSTGRES_DSN", None)
os.environ["HERMES_KANBAN_DB"] = os.path.join(home, "kanban.db")

# import after env is set
from hermes_cli import kanban_db as kb
import hermes_cli.kanban_store_factory as factory

factory._config_backend_cache = (False, None)
conn = kb.connect()
check("default backend -> sqlite3.Connection", isinstance(conn, sqlite3.Connection),
      type(conn).__name__)
conn.close()
check("default -> sqlite file created", os.path.exists(os.environ["HERMES_KANBAN_DB"]))

# ---- 2. backend=postgres -> kb.connect() returns the pg wrapper, writes land in pg
schema = "choke_" + uuid.uuid4().hex[:8]
import psycopg
with psycopg.connect(BASE_DSN, autocommit=True) as c:
    c.execute(f'CREATE SCHEMA "{schema}"')
sep = "&" if "?" in BASE_DSN else "?"
os.environ["HERMES_KANBAN_BACKEND"] = "postgres"
os.environ["HERMES_KANBAN_POSTGRES_DSN"] = f"{BASE_DSN}{sep}options=-csearch_path%3D{schema}"
factory._config_backend_cache = (False, None)

pg_home = tempfile.mkdtemp(prefix="choke_pg_")
os.environ["HERMES_HOME"] = pg_home
os.environ["HOME"] = pg_home
os.environ["HERMES_KANBAN_DB"] = os.path.join(pg_home, "kanban.db")

try:
    # the chokepoint: plain kb.connect(), no store object in sight
    conn = kb.connect()
    check("postgres backend -> NOT sqlite3.Connection", not isinstance(conn, sqlite3.Connection),
          type(conn).__name__)
    tid = kb.create_task(conn, title="chokepoint task", assignee="qa",
                         created_by="smoke", tenant="choke")
    listed = kb.list_tasks(conn)
    check("create_task via kb.* on pg conn", any(t.id == tid for t in listed))
    conn.close()

    # the `with kb.connect() as c:` path (needs the __enter__/__exit__ we added)
    with kb.connect() as c2:
        got = kb.get_task(c2, tid)
    check("`with kb.connect()` works on pg", got is not None and got.id == tid)

    # connect_closing() inherits the chokepoint (heartbeat bridge path)
    with kb.connect_closing() as c3:
        n = len(kb.list_tasks(c3))
    check("kb.connect_closing() routed to pg", n >= 1, f"rows={n}")

    # prove the row is physically in postgres, not a sqlite file
    with psycopg.connect(BASE_DSN, autocommit=True) as c:
        c.execute(f'SET search_path TO "{schema}"')
        row = c.execute("SELECT COUNT(*) FROM tasks WHERE id = %s", (tid,)).fetchone()
    check("row physically in postgres schema", row[0] == 1, f"count={row[0]}")

    # and that we did NOT silently create/populate a sqlite kanban file
    sqlite_path = os.environ["HERMES_KANBAN_DB"]
    check("no sqlite kanban file written under pg backend", not os.path.exists(sqlite_path),
          sqlite_path)
finally:
    with psycopg.connect(BASE_DSN, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

print("\n" + ("CHOKEPOINT OK — backend routing works end to end" if not fails
              else f"FAILURES: {fails}"))
raise SystemExit(1 if fails else 0)
