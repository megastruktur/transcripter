#!/usr/bin/env bash
# Backup transcripter stateful services (Postgres + Neo4j).
#
# Captures a consistent snapshot of every stateful service the worker
# depends on, mirroring the `backups/docker-compose.yml.YYYYMMDD-<topic>`
# convention used for config snapshots. Two artifacts per run:
#
#   postgres.YYYYMMDD-HHMMSS.sql       pg_dump --format=custom (-Fc), compresses
#                                       inside the file; restored with
#                                       `pg_restore --format=custom`.
#   neo4j.YYYYMMDD-HHMMSS.dump          neo4j-admin database dump of the
#                                       `neo4j` DB (default DB name). Restored
#                                       with `neo4j-admin database load` into a
#                                       stopped neo4j container (see RUNBOOK).
#
# Behavior:
#   - Postgres dump always runs (the base stack needs it).
#   - Neo4j dump runs iff `docker compose --profile graph ps neo4j` reports a
#     running container; otherwise it's skipped with an explanatory note.
#   - Service stopped = the script exits non-zero (don't paper over drift).
#   - Output dir defaults to ./backups (matches the existing pre-update
#     snapshot convention in that dir).
#
# Usage:
#   bash server/scripts/backup.sh                  # -> ./backups/postgres.*.sql
#   bash server/scripts/backup.sh /path/to/dir     # -> /path/to/dir/postgres.*.sql
#   bash server/scripts/backup.sh --neo4j-only     # skip postgres
#   bash server/scripts/backup.sh --pg-only        # skip neo4j (default if neo4j
#                                                  # container is not running)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Argument parsing: mutually exclusive flags --neo4j-only / --pg-only, then
# optional OUT_DIR. Flag must come first so an OUT_DIR that starts with `--`
# can never collide.
DO_PG=1
DO_NEO4J=1
case "${1:-}" in
  --neo4j-only) DO_PG=0; shift ;;
  --pg-only)    DO_NEO4J=0; shift ;;
esac
OUT_DIR="${1:-$SERVER_DIR/backups}"

mkdir -p "$OUT_DIR"
TS="$(date -u +%Y%m%d-%H%M%S)"

cd "$SERVER_DIR"

log() { printf '[backup %s] %s\n' "$TS" "$*"; }

# --- Postgres ---------------------------------------------------------------
run_pg_dump() {
  local out="$OUT_DIR/postgres.$TS.sql"
  log "postgres: pg_dump -Fc -> $out"
  # pg_dump inside the postgres container. --format=custom is the compressed
  # archive form — restored with `pg_restore` (see RUNBOOK below). We don't
  # need pg_dumpall because transcripter uses one logical DB; createuser /
  # createdb (if needed) live in the compose file, not in the data dir.
  #
  # We must explicitly capture and propagate the exit code: when this
  # function is invoked under `run_pg_dump || fail=1`, bash suspends
  # `set -e` for the function body, so a silent failure would otherwise
  # leave an empty $out and exit 0.
  if ! docker compose exec -T postgres \
      pg_dump -U transcripter -d transcripter --format=custom \
      > "$out"; then
    log "postgres: pg_dump FAILED — see docker compose output above"
    return 1
  fi
  log "postgres: $(wc -c < "$out") bytes written"
}
# --- Neo4j ------------------------------------------------------------------
neo4j_running() {
  # `docker compose ps --status running` filters at the source; service-name
  # argument is honored even when the service is behind an unselected profile.
  # Exit 0 iff the container is listed and running.
  #
  # We swallow compose's interpolation error (NEO4J_PASSWORD unset when the
  # user never enabled --profile graph) and just treat the service as not
  # running — the script falls through to the "skip" branch.
  if ! docker compose --profile graph ps --status running neo4j 2>/dev/null; then
    return 1
  fi
  grep -qE '^\S+\s+neo4j\s'
}

run_neo4j_dump() {
  local out="$OUT_DIR/neo4j.$TS.dump"
  log "neo4j: neo4j-admin database dump -> $out"
  # neo4j-admin lives in /var/lib/neo4j/bin inside the official image.
  # `database dump` produces a self-contained archive per DB; --to-path is
  # the directory where the .dump file lands. We pull the resulting file out
  # of the container via `docker cp` to keep the host artifact path stable.
  local container
  if ! container="$(docker compose --profile graph ps -q neo4j)"; then
    log "neo4j: container lookup failed"
    return 1
  fi
  if [ -z "$container" ]; then
    log "neo4j: container not found (profile graph not running); skipping"
    return 1
  fi
  # neo4j-admin refuses to run while the DB is open for writes; the
  # `database dump` subcommand itself is online-safe (it uses a consistent
  # snapshot) but the binary still asks for confirmation via --yes.
  if ! docker compose --profile graph exec -T neo4j \
      /var/lib/neo4j/bin/neo4j-admin database dump neo4j \
        --to-path=/tmp --yes; then
    log "neo4j: neo4j-admin database dump FAILED — see docker compose output above"
    return 1
  fi
  if ! docker cp "$container:/tmp/neo4j.dump" "$out"; then
    log "neo4j: docker cp to $out FAILED"
    return 1
  fi
  docker compose --profile graph exec -T neo4j rm -f /tmp/neo4j.dump >/dev/null 2>&1 || true
  log "neo4j: $(wc -c < "$out") bytes written"
}

# --- Drive ------------------------------------------------------------------
fail=0
if [ "$DO_PG" = 1 ]; then
  run_pg_dump || fail=1
fi
if [ "$DO_NEO4J" = 1 ]; then
  if neo4j_running; then
    run_neo4j_dump || fail=1
  else
    log "neo4j: --profile graph not running; nothing to dump (use --pg-only to suppress this message)"
  fi
fi

if [ "$fail" -ne 0 ]; then
  log "one or more dumps failed"
  exit 1
fi
log "done"
