# Backups

This directory stores **pre-update config snapshots** (timestamped copies of
`docker-compose.yml` and `config.yaml` saved before a non-trivial change) and
**stateful-service dumps** produced by `scripts/backup.sh` (`postgres`,
`neo4j`). The naming convention matches what already lives here:

| Artifact pattern                        | Producer                                     | Format        |
| --------------------------------------- | -------------------------------------------- | ------------- |
| `docker-compose.yml.YYYYMMDD-<topic>`   | manual `cp` before infra changes             | YAML          |
| `config.yaml.YYYYMMDD-<topic>`         | manual `cp` before profile/config changes    | YAML          |
| `postgres.YYYYMMDD-HHMMSS.sql`         | `scripts/backup.sh` (`pg_dump --format=custom`) | compressed pg dump |
| `neo4j.YYYYMMDD-HHMMSS.dump`           | `scripts/backup.sh` (`neo4j-admin database dump`) | Neo4j dump archive |

The dated config snapshots are operator-driven, gitignored, and kept locally
for one-step rollback after a bad compose / profile / yaml edit. The dump
files are produced by the script and rotated by you (no retention policy —
keep as many as disk allows, or pipe to your NAS).

## Backup script (`scripts/backup.sh`)

```bash
cd server
bash scripts/backup.sh                              # ./backups/postgres.*.sql
bash scripts/backup.sh /mnt/nas/backups             # custom OUT_DIR
bash scripts/backup.sh --pg-only /tmp/just-pg       # skip neo4j block
bash scripts/backup.sh --neo4j-only /tmp/just-neo4j  # skip postgres block
```

Behavior:

- Postgres dump always runs (the base stack needs it). Without
  `--profile graph` up, the script is effectively a `pg_dump` of the
  `transcripter` DB.
- Neo4j dump runs only when the neo4j container is running under
  `--profile graph`. If the profile is off, the script skips with a
  one-line note and exits 0 (use `--pg-only` to suppress the message).
- The neo4j dump is an OFFLINE operation on Community Edition (CE has no
  online backup and no `STOP DATABASE`): the script stops the neo4j
  container, dumps via a throwaway `--volumes-from` container, and starts
  it again on every exit path. The graph layer is down for a few seconds;
  `enrich`/digests are best-effort and degrade, the core pipeline is
  unaffected.
- Any service-level failure aborts the run with exit 1 and removes the
  partial artifact — empty output files are never left behind.

## Restore runbook

### Postgres (`postgres.YYYYMMDD-HHMMSS.sql`)

Custom-format dump (`-Fc`) → restored with `pg_restore`. Restoring into the
existing compose-managed DB drops and recreates schema:

```bash
# 1. Drop the existing schema (data + tables). Keep the `transcripter`
#    role/db — compose creates them at first start; pg_dump does NOT
#    include CREATE ROLE / CREATE DATABASE.
cd server
docker compose exec -T postgres \
  psql -U transcripter -d transcripter \
    -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'

# 2. Load the dump.
docker compose exec -T postgres \
  pg_restore -U transcripter -d transcripter --no-owner --role=transcripter \
    < /path/to/backups/postgres.YYYYMMDD-HHMMSS.sql
```

`--no-owner` keeps the in-container role `transcripter` as the owner
(matching the dump's `OWNER TO transcripter`). The dump was produced with
the same role, so this is just defensive.

### Neo4j (`neo4j.YYYYMMDD-HHMMSS.dump`)

`neo4j-admin database load` requires the target DB to be **offline** — the
load runs against a stopped container, then the data dir is renamed back
into place on next start. Procedurally:

```bash
# 1. Stop the neo4j container (do NOT `down -v` — that wipes neo4jdata).
cd server
docker compose --profile graph stop neo4j

# 2. Load the dump into a fresh DB dir, then swap the volume contents.
#    `neo4j-admin database load` writes its result into the named
#    `--from-path` directory; we load into a sibling and replace the
#    running volume mount.
docker compose --profile graph run --rm -v neo4j_restore:/restore neo4j \
  /var/lib/neo4j/bin/neo4j-admin database load neo4j \
    --from-path=/restore --to-data-dir=/var/lib/neo4j/data/databases \
    --overwrite-destination=true

# 3. Start neo4j. The container picks up the restored DB automatically.
docker compose --profile graph up -d neo4j
```

If you don't have a `neo4j_restore` named volume handy, do the load on the
host instead:

```bash
# Copy the dump into the running (but stopped) container.
docker cp /path/to/backups/neo4j.YYYYMMDD-HHMMSS.dump \
  "$(docker compose --profile graph ps -q neo4j)":/tmp/neo4j.dump

# Start a one-shot container with the same `neo4jdata` volume, load, exit.
docker compose --profile graph run --rm neo4j \
  /var/lib/neo4j/bin/neo4j-admin database load neo4j \
    --from-path=/tmp --to-data-dir=/var/lib/neo4j/data/databases \
    --overwrite-destination=true
docker compose --profile graph start neo4j
```

After restore: verify with a Cypher query (`MATCH (n) RETURN count(n)`)
and rotate the dump if the recording IDs in `n.origin_recording_id`
don't match the postgres side (drift usually means you loaded a dump
from a different stack instance).

## When to back up

The script is idempotent and cheap (~1 s on idle). Trigger it:

- **Before any infra change** that touches compose / profile / config
  (you'd snapshot `docker-compose.yml.YYYYMMDD-<topic>` at the same time).
- **Before any postgres schema migration** triggered by an API upgrade
  (`_migrate_stage_kind_enum` and friends).
- **Before rotating `NEO4J_PASSWORD`** in `.env`.
- **After each successful e2e smoke** if you want a known-good point.

## What is NOT backed up

- Audio FLACs live in `./storage/recordings/` (NAS via `TRANSCRIPTS_DIR`)
  and are bound-mounted — back them up at the filesystem level, not via
  this script.
- Temporal workflow history lives in the `pgdata` volume (the `temporal`
  Postgres database); the `postgres.*.sql` dump captures it as one
  logical DB (no `--schema-only` filter).
- Speaches / LinTO weights are downloaded from HuggingFace and live in
  the `speaches-hf-cache` / `models` volumes — they don't need backing
  up (re-pullable).
