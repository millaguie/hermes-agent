# Postgres kanban backend, build + deploy + migration

branch `feat/postgres-kanban`, rebased onto the v0.17.0 release (`v2026.6.19`, base branch `prod-v0.17.0`). It carries the store/adapter layer from upstream PR #33366 plus our own integration. The point of all this: sqlite kanban corrupts under concurrent writers ("database disk image is malformed" / "wrong # of entries in index idx_events_task"), gloobalsec hits it every few minutes at ~1790 tasks/day -> dispatcher pauses -> 0 production. Postgres does row-level locking so it just doesn't have the problem.

backend is OPT-IN, default stays sqlite, the other 6 bots keep running exactly as before with the same image. If psycopg is missing the factory import is lazy so the sqlite path never breaks.

## how it's wired (and why it's cheap to rebase)

upstream won't merge this (5k+ open PRs), so the fork is built to be carried version to version with minimal friction. ~2000 of the ~2300 changed lines live in our OWN new files (`hermes_cli/kanban_store_*.py` + tests) that upstream doesn't have, so they NEVER conflict. The footprint inside upstream files is 3 ADDITIVE touches:

- `hermes_cli/kanban_db.py` — one ~16-line hunk at the top of `connect()`: the **backend chokepoint**. When a non-sqlite backend is configured it returns that backend's connection; otherwise it falls through to the unchanged sqlite path. Because every caller (worker tools, CLI, dispatcher, the #31752 heartbeat bridge) opens its connection through `connect()` / `connect_closing()`, this single hunk routes ALL of them. No per-call-site edits.
- `pyproject.toml` — the `postgres` and `web-search` extras (+12 lines).
- `Dockerfile` — `--extra postgres` on the uv sync line (1 line).

all routing logic lives in our files; `kanban_db.connect()` just calls `kanban_store_factory.maybe_runtime_connection()` (lazy import, returns `None` for sqlite so the default path is untouched and psycopg-free). The dispatcher (`gateway/kanban_watchers.py`), the worker tools (`tools/kanban_tools.py`), `hermes_cli/kanban.py`, `hermes_cli/main.py` and `agent/prompt_builder.py` are byte-for-byte upstream — they work on Postgres purely through the chokepoint. Earlier iterations patched all of those; collapsing to the chokepoint dropped the conflict surface from 7 files (incl. main.py, which upstream churns by ±5000 lines per release) to 3 additive ones.

version bumps: `./update-fork.sh [vTAG]` (rebases onto the tag with `git rerere` on, auto-regenerates uv.lock, runs the test + stress gate). See "carrying to a new version" below.

## validation (2026-06-27, rebased on v0.17.0, postgres:16 in docker, local)

ran the in-repo stress harness (`scripts/stress_pg.py` driving `hermes_cli/kanban_store_stress.py`) plus the chokepoint smoke (`scripts/kanban_chokepoint_smoke.py`) against a throwaway postgres:

- claim contract, 32 thread workers x 400 tasks x 3 rounds -> 1200/1200 claimed, 0 duplicate claims, all done, 0 open runs
- dispatch signal contract SIGTERM/SIGKILL -> both workers classified crashed, tasks back to ready, 0 open runs
- multi-process storm, 24 OS processes x 1200 tasks on ONE shared schema -> 1200 claimed exactly once, 0 dups, 0 double-completed runs
- chokepoint smoke: default backend -> sqlite3.Connection + sqlite file; backend=postgres -> rows land in the pg schema, `with kb.connect()` and `connect_closing()` route to pg, no sqlite file written
- real CLI: `hermes kanban create/list` with `HERMES_KANBAN_BACKEND=postgres` writes/reads from the pg schema

malformed=0 everywhere, this was the failure mode we were chasing. Store suite + 228 core `kanban_db` tests pass (sqlite default path intact); the postgres-local contract test runs green against a real server with `HERMES_TEST_POSTGRES_DSN` set.

## build (Orin Nano)

olimpo does not build the image (kernel without EXT4_FS_SECURITY), build on the Orin:

```bash
ssh orin
git clone https://github.com/millaguie/hermes-agent.git && cd hermes-agent   # or git fetch in the existing checkout
git checkout feat/postgres-kanban
docker build -t tea.millaguie.net/millaguie/hermes-agent-evilio:postgres-kanban .
docker login tea.millaguie.net   # creds in pass: tea.millaguie.net/millaguie
docker push tea.millaguie.net/millaguie/hermes-agent-evilio:postgres-kanban
```

do NOT push to the tag the 7 bots track until the gloobalsec canary is done, watchtower would roll everyone at once. Push `:postgres-kanban` first, point only gloobalsec at it.

psycopg is baked into the image (`--extra postgres` in the Dockerfile uv sync), bots that stay on sqlite just don't load it.

## provision postgres (production)

NOT the newsbot-postgres on TrueNAS 192.168.1.4, that one is production for the news pipeline. New instance, e.g. another app/container on TrueNAS or wherever:

```bash
docker run -d --name hermes-kanban-pg \
  -e POSTGRES_USER=kanban \
  -e POSTGRES_PASSWORD_FILE=/run/secrets/kanban_pg_pass \
  -e POSTGRES_DB=hermes_kanban \
  -v hermes-kanban-pgdata:/var/lib/postgresql/data \
  -p <host>:5432:5432 postgres:16
```

password goes in `pass` (e.g. `pass insert infra/hermes-kanban-pg`), never in compose files or git.

one schema per bot, the adapter does NOT separate boards inside a schema (sqlite had one file per board, postgres collapses all boards of a bot into one schema). Our bots are single-board so it's fine, but don't share a schema between bots:

```sql
CREATE SCHEMA gloobalsec;
-- later, one per migrated bot: CREATE SCHEMA evilio; etc.
```

## per-bot config

two ways, env wins over config.yaml:

```yaml
# config.yaml of the bot
kanban:
  storage:
    backend: postgres        # default sqlite, omit on the other 6 bots
```

DSN always via env in the bot's compose/env file, schema selected in the DSN:

```bash
HERMES_KANBAN_POSTGRES_DSN="postgresql://kanban:<pass>@<host>:5432/hermes_kanban?options=-csearch_path%3Dgloobalsec"
# or HERMES_KANBAN_BACKEND=postgres instead of the yaml key
# or HERMES_KANBAN_POSTGRES_SCHEMA=gloobalsec instead of the options trick
```

schema/tables are created automatically on first connect (init_db runs the DDL, idempotent).

## migration sqlite -> postgres

recommendation: **clean start** for gloobalsec. The kanban is a work queue, ~1790 tasks/day flow through it, done/archived rows are history not state. Keep the old `kanban.db` file as archive, start postgres empty, the pipeline refills it in minutes. Less moving parts than a data migration and we don't drag possibly-corrupt rows into the new backend.

if some bot really needs its open tasks carried over: stop the bot, then copy the live rows (`status IN ('todo','ready','running','blocked','scheduled')` from tasks plus their task_links / task_events / task_comments / task_runs / kanban_notify_subs), sqlite ids are TEXT so they move as-is, SERIAL sequences only matter for new rows. Easiest is a 30-line python with sqlite3 + psycopg doing INSERT ... ON CONFLICT DO NOTHING per table, then `SELECT setval(...)` for the serial pks. Write it when we actually need it, gloobalsec doesn't.

## rollout plan

1. provision prod postgres + `CREATE SCHEMA gloobalsec`
2. build + push `:postgres-kanban` from the Orin
3. point ONLY gloobalsec at the new tag, add the DSN env + backend config, stop bot, mv kanban.db kanban.db.pre-postgres, start bot
4. check logs for `kanban: storage backend=postgres` (logged once on the first runtime pg connection), then let it run 48-72h, watch for tick_failed / malformed (should be literally zero now) and that throughput holds ~1790 tasks/day
5. if ok -> retag for the rest or migrate bots one by one, each with its own schema. The other 6 don't need postgres at all unless they grow concurrent writers, sqlite default keeps working
6. rollback = point the bot back at the old tag + restore the kanban.db file, nothing else changed

## carrying to a new upstream version

upstream won't merge this, so we carry the fork forward ourselves. Designed to be a near-one-command bump:

```bash
git checkout feat/postgres-kanban
./update-fork.sh                 # newest upstream release tag
# or: ./update-fork.sh v2026.7.3
export HERMES_TEST_POSTGRES_DSN=postgresql://...   # optional: also run the pg gate
```

what it does: enables `git rerere` (records + replays your conflict resolutions across bumps), rebases `--onto` the new tag, auto-regenerates `uv.lock` instead of hand-merging the hash blob, then runs `py_compile` + the store/contract tests + (if DSN set) the postgres-local contract + `scripts/stress_pg.py`. A backup ref `feat/postgres-kanban-backup-<sha>` is created before it touches anything.

expected conflict surface per bump: only the 3 additive touches (chokepoint hunk in `kanban_db.py`, `pyproject.toml`, `Dockerfile`), and uv.lock which the script regenerates. The chokepoint sits at the top of `connect()`, a stable function, so it usually applies clean; rerere remembers it if it doesn't. If upstream ever renames/moves `connect()` or changes the kanban schema, the gate will fail loudly — re-check the chokepoint and re-sync the postgres DDL in `kanban_store_postgres.py` (`POSTGRES_SCHEMA_SQL`) against the new `SCHEMA_SQL` / additive migrations in `kanban_db.py`.

after a green bump: `git push --force-with-lease origin feat/postgres-kanban`, push a `prod-vX.Y.Z` base branch for a clean PR diff (see note below), and delete the backup ref.

> note: `vYYYY.M.D` upstream tags are ANNOTATED — you can't point a branch at the tag-object SHA (git/API return 422). Use the peeled commit: `git push origin 'vTAG^{commit}':refs/heads/prod-vX.Y.Z` or push an ancestor of the rebased branch.

## upstream

PR #33366 (NousResearch/hermes-agent, draft, issue #33267 DB-ADAPTER-006 is closed) is the source of the store/adapter layer. We commented twice with testing results + the integration fixes it's missing (the worker tools / CLI / heartbeat bridge still hit sqlite in the raw PR -> "phantom task"); no maintainer response. Treat the fork as ours to maintain.
