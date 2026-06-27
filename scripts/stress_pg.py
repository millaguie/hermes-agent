"""Stress-validate the Postgres kanban store at high concurrency.

Usage:
    HERMES_TEST_POSTGRES_DSN=postgresql://... uv run python stress_pg.py

Each phase/round runs in its own fresh schema so counts never accumulate.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from pathlib import Path

BASE_DSN = os.environ["HERMES_TEST_POSTGRES_DSN"]

failures: list[str] = []
created_schemas: list[str] = []


def fresh_schema(tag: str) -> str:
    import psycopg
    schema = f"stress_{tag}_" + uuid.uuid4().hex[:8]
    with psycopg.connect(BASE_DSN, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in BASE_DSN else "?"
    os.environ["HERMES_KANBAN_POSTGRES_DSN"] = f"{BASE_DSN}{sep}options=-csearch_path%3D{schema}"
    created_schemas.append(schema)
    return schema


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        failures.append(f"{name}: {detail}")


def phase_threaded_claims() -> None:
    from hermes_cli.kanban_store_stress import StressHarnessConfig, run_claim_stress_contract

    for rnd in range(1, 4):
        fresh_schema(f"thr{rnd}")
        with tempfile.TemporaryDirectory(prefix="pgstress_") as tmp:
            r = run_claim_stress_contract(
                StressHarnessConfig(task_count=400, claim_workers=32, base_dir=tmp, backend="postgres")
            )
            print(f"round {rnd}: backend={r.backend} claimed={len(r.claimed_task_ids)} "
                  f"dups={len(r.duplicate_claims)} statuses={r.status_counts} open_runs={r.open_run_count}")
            check(f"r{rnd} backend", r.backend == "postgres")
            check(f"r{rnd} all claimed", len(r.claimed_task_ids) == 400)
            check(f"r{rnd} no duplicate claims", r.duplicate_claims == [])
            check(f"r{rnd} all done", r.status_counts == {"done": 400})
            check(f"r{rnd} no open runs", r.open_run_count == 0)


def phase_signal_contract() -> None:
    from hermes_cli.kanban_store_stress import StressHarnessConfig, run_dispatch_signal_contract

    fresh_schema("sig")
    with tempfile.TemporaryDirectory(prefix="pgstress_") as tmp:
        r = run_dispatch_signal_contract(
            StressHarnessConfig(task_count=2, claim_workers=2, base_dir=tmp, backend="postgres"),
            signals=(signal.SIGTERM, signal.SIGKILL),
        )
        print(f"signal: spawned={r.spawned_task_ids} crashed={r.crashed_task_ids} "
              f"statuses={r.status_counts} crash_events={r.crash_event_count} unexpected={r.unexpected_failures}")
        check("signal backend", r.backend == "postgres")
        check("signal crashed==spawned", sorted(r.crashed_task_ids) == sorted(r.spawned_task_ids))
        check("signal back to ready", r.status_counts == {"ready": 2})
        check("signal no open runs", r.open_run_count == 0)
        check("signal no unexpected", r.unexpected_failures == [])


WORKER_SRC = r'''
import json, os, sys, time
from hermes_cli.kanban_store_factory import create_kanban_store

idx = int(sys.argv[1])
store = create_kanban_store("postgres")
claimed = []
idle = 0
with store.connect() as conn:
    while idle < 5:
        row = conn.execute(
            "SELECT id FROM tasks WHERE status='ready' AND claim_lock IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            idle += 1
            time.sleep(0.02)
            continue
        idle = 0
        c = store.claim_task(conn, row["id"], ttl_seconds=60, claimer=f"proc-{idx}")
        if c is None:
            continue
        claimed.append(c.id)
        store.complete_task(conn, c.id, result="ok", summary=f"proc {idx}",
                            expected_run_id=c.current_run_id)
print(json.dumps(claimed))
'''


def phase_multiprocess() -> None:
    n_procs, n_tasks = 24, 1200
    home = tempfile.mkdtemp(prefix="pgstress_home_")
    env = dict(os.environ)
    env.update({
        "HERMES_HOME": home, "HOME": home,
        "HERMES_KANBAN_BACKEND": "postgres",
        "HERMES_KANBAN_BOARD": "pg-mp-stress",
    })
    os.environ.update(env)
    fresh_schema("mp")
    env["HERMES_KANBAN_POSTGRES_DSN"] = os.environ["HERMES_KANBAN_POSTGRES_DSN"]

    from hermes_cli.kanban_store_factory import create_kanban_store
    store = create_kanban_store("postgres")
    store.init_db()
    with store.connect() as conn:
        for i in range(n_tasks):
            store.create_task(conn, title=f"mp-{i}", assignee="default",
                              created_by="mp-stress", tenant="mp-stress")

    script = Path(home) / "worker.py"
    script.write_text(WORKER_SRC)
    procs = [
        subprocess.Popen([sys.executable, str(script), str(i)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        for i in range(n_procs)
    ]
    all_claimed: list[str] = []
    for p in procs:
        out, err = p.communicate(timeout=600)
        if p.returncode != 0:
            check(f"proc rc={p.returncode}", False, err.strip()[-300:])
            continue
        all_claimed.extend(json.loads(out.strip().splitlines()[-1]))

    counts = Counter(all_claimed)
    dups = [t for t, c in counts.items() if c > 1]
    with store.connect() as conn:
        statuses = {r["status"]: int(r["n"]) for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()}
        open_runs = int(conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE ended_at IS NULL").fetchone()[0])
        multi_done = int(conn.execute(
            "SELECT COUNT(*) FROM (SELECT task_id FROM task_runs WHERE outcome='completed' "
            "GROUP BY task_id HAVING COUNT(*) > 1) AS x").fetchone()[0])
    print(f"multiproc: procs={n_procs} tasks={n_tasks} claimed={len(all_claimed)} "
          f"dups={len(dups)} statuses={statuses} open_runs={open_runs} multi_done={multi_done}")
    check("mp all tasks claimed exactly once", len(all_claimed) == n_tasks and not dups,
          f"claimed={len(all_claimed)} dups={dups[:5]}")
    check("mp all done", statuses == {"done": n_tasks}, str(statuses))
    check("mp no open runs", open_runs == 0)
    check("mp no double-completed runs", multi_done == 0)


def main() -> None:
    import psycopg
    try:
        print("== phase 1: threaded claim contract (32 workers x 400 tasks x 3 rounds) ==")
        phase_threaded_claims()
        print("== phase 2: dispatch signal contract (SIGTERM/SIGKILL) ==")
        phase_signal_contract()
        print("== phase 3: multi-process claim storm (24 procs x 1200 tasks) ==")
        phase_multiprocess()
    finally:
        with psycopg.connect(BASE_DSN, autocommit=True) as c:
            for schema in created_schemas:
                c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    print("\n" + ("ALL CHECKS PASSED — malformed=0, duplicates=0" if not failures
                  else f"{len(failures)} FAILURES:\n" + "\n".join(failures)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
