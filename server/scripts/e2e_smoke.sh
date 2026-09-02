#!/usr/bin/env bash
# E2E smoke: full path — upload with interruption+resume → pipeline artifacts.
# Requires: docker compose stack up (api on :8090), jq, curl.
# Usage: bash server/scripts/e2e_smoke.sh [seconds-to-wait-for-pipeline]
#   STT=speaches  run against the speaches profile (config.yaml must route
#                 transcribe.backend=api at http://speaches:8000/v1); asserts
#                 non-empty word timestamps (diarization input) and uses the
#                 speech fixture (tones fail Speaches' VAD).

set -euo pipefail

API="${TRANSCRIPTER_API:-http://localhost:8090}"
TOKEN="${TRANSCRIPTER_TOKEN:-test-token-e2e}"
WAIT="${1:-600}"
STT="${STT:-local}"
STORAGE_DIR="${TRANSCRIPTER_STORAGE:-$(cd "$(dirname "$0")/.." && pwd)/storage}"
# Compose command for docker exec probes (speaches preload). Dev stack:
# TRANSCRIPTER_DC="docker compose -p transcripter-dev -f docker-compose.yml -f docker-compose.dev.yml"
DC="${TRANSCRIPTER_DC:-docker compose -f $(cd "$(dirname "$0")/.." && pwd)/docker-compose.yml}"
FIXTURES_DIR="$(cd "$(dirname "$0")" && pwd)/fixtures"
WORK="$(cd "$(dirname "$0")/.." && pwd)/.e2e-work"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT  # keep under project path: containers can't see host /tmp

auth() { curl -s -H "authorization: Bearer $TOKEN" "$@"; }
# Like auth, but hard-fail on HTTP >= 400 with the body shown — for
# responses that gate assertions; a silent error body must not pass.
authf() {
  local body rc
  # `if !` disables errexit for the assignment, so transport failures
  # (connection refused, timeout) reach the diagnostic instead of killing
  # the subshell silently.
  if ! body=$(curl -s -w '\n%{http_code}' -H "authorization: Bearer $TOKEN" "$@"); then
    rc=$?
    echo "authf: curl exit $rc for: $*" >&2
    exit "$rc"
  fi
  local code="${body##*$'\n'}"
  if [ "$code" -ge 400 ] 2>/dev/null; then
    echo "authf: HTTP $code from: $*" >&2
    printf '%s\n' "${body%$'\n'*}" | head -c 400 >&2
    exit 22
  fi
  printf '%s' "${body%$'\n'*}"
}

# Portable helpers: GNU coreutils are absent on macOS/BSD hosts.
fsize() { wc -c < "$1" | tr -d ' '; }
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

echo "== 1. health"
curl -sf "$API/health" | jq -e '.status == "ok"' >/dev/null && echo OK

if [ "$STT" = "speaches" ]; then
  echo "== 2. use speech fixture (speaches VAD rejects tones)"
  test -s "$FIXTURES_DIR/speech-2voices.flac" || { echo "fixture missing"; exit 1; }
  if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -y -loglevel error -i "$FIXTURES_DIR/speech-2voices.flac" "$WORK/test.flac"
  else
    docker run --rm -v "$WORK:/w" -v "$FIXTURES_DIR:/f:ro" --entrypoint ffmpeg \
      ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cpu \
      -y -loglevel error -i /f/speech-2voices.flac /w/test.flac
  fi
  echo "== 2b. wait for speaches model preload (first run downloads weights)"
  # External voice stack (config.yaml transcribe.base_url points elsewhere):
  # set SPEACHES_PROBE_URL (e.g. http://host:8010/v1/models, key via
  # SPEACHES_API_KEY) to probe IT instead of the bundled profile container.
  DEADLINE=$((SECONDS + 600))
  if [ -n "${SPEACHES_PROBE_URL:-}" ]; then
    until curl -sf -H "authorization: Bearer ${SPEACHES_API_KEY:-}" "$SPEACHES_PROBE_URL" >/dev/null 2>&1 \
        && curl -sf "$API/health" >/dev/null; do
      [ $SECONDS -ge $DEADLINE ] && { echo "external speaches probe timeout"; exit 1; }
      sleep 5
    done
  else
    until $DC exec -T speaches \
        python -c "import urllib.request,sys;urllib.request.urlopen('http://localhost:8000/v1/models',timeout=5)" 2>/dev/null \
        && curl -sf "$API/health" >/dev/null; do
      [ $SECONDS -ge $DEADLINE ] && { echo "speaches preload timeout"; exit 1; }
      sleep 5
    done
  fi
  echo "speaches ready"
else
  echo "== 2. generate test audio (2 alternating tones, 30s, 16kHz mono)"
  # Sine segments alternating every 1s — two "voices" by frequency.
  python3 - "$WORK/test.wav" <<'PY'
import math, struct, sys, wave
sr = 16000
dur = 30
n = sr * dur
frames = bytearray()
for i in range(n):
    sec = i // sr
    freq = 440 if sec % 2 == 0 else 660
    v = int(0.6 * 32767 * math.sin(2 * math.pi * freq * (i % sr) / sr))
    frames += struct.pack("<h", v)
with wave.open(sys.argv[1], "w") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(frames)
print("wav written")
PY
  # Prefer host ffmpeg; fall back to a throwaway container when absent.
  if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -y -loglevel error -i "$WORK/test.wav" "$WORK/test.flac"
  else
    docker run --rm -v "$WORK:/w" --entrypoint ffmpeg \
      ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cpu \
      -y -loglevel error -i /w/test.wav /w/test.flac
  fi
fi
test -s "$WORK/test.flac" && echo "flac: $(fsize "$WORK/test.flac") bytes"

SHA=$(sha256 "$WORK/test.flac")
SIZE=$(fsize "$WORK/test.flac")
echo "sha256=$SHA size=$SIZE"

echo "== 3. create recording (with tags)"
# Deliberately messy tags to prove server-side normalization
# (trim + lowercase + dedupe): expects ["pathfinder","e2e"].
RID=$(curl -sf -X POST -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"title":"e2e-smoke","tags":["Pathfinder"," E2E ","pathfinder"]}' "$API/recordings" | jq -r .id)
echo "id=$RID"
TAGS=$(authf "$API/recordings/$RID" | jq -c .tags)
test "$TAGS" = '["pathfinder","e2e"]' && echo "tags normalized: $TAGS" || { echo "BAD tags: $TAGS"; exit 1; }

echo "== 3b. profiles registry"
authf "$API/profiles" | jq -e '.[] | select(.id=="ttrpg-session-log")' >/dev/null \
  && echo "  ok: ttrpg-session-log listed" || { echo "MISSING ttrpg-session-log in GET /profiles"; exit 1; }
echo "== 3c. set recording type (profile match — the session-log.md assert below needs it;"
echo "      POST /recordings takes no type, PATCH is the only way to set it pre-pipeline)"
UPD=$(authf -X PATCH -H 'content-type: application/json' -d '{"type":"ttrpg"}' "$API/recordings/$RID")
echo "$UPD" | jq -e '.type == "ttrpg"' >/dev/null \
  && echo "  ok: type=ttrpg" || { echo "BAD type set: $UPD"; exit 1; }

echo "== 4. upload first half, simulate connection drop"
HALF=$((SIZE / 2))
head -c "$HALF" "$WORK/test.flac" > "$WORK/part1"
HTTP=$(curl -s -o "$WORK/ack1" -w '%{http_code}' -X PUT \
  -H "authorization: Bearer $TOKEN" -H "content-length: $HALF" \
  --data-binary "@$WORK/part1" "$API/recordings/$RID/audio?offset=0")
test "$HTTP" = "200" && echo "first half committed: $(jq -r .committed "$WORK/ack1")"

echo "== 5. resume from overlap (offset earlier than committed)"
OVERLAP=$((HALF - 1024))
tail -c +"$((OVERLAP + 1))" "$WORK/test.flac" > "$WORK/part2"
P2SIZE=$(fsize "$WORK/part2")
HTTP=$(curl -s -o "$WORK/ack2" -w '%{http_code}' -X PUT \
  -H "authorization: Bearer $TOKEN" -H "content-length: $P2SIZE" \
  --data-binary "@$WORK/part2" "$API/recordings/$RID/audio?offset=$OVERLAP")
test "$HTTP" = "200" || { cat "$WORK/ack2"; exit 1; }
COMMITTED=$(jq -r .committed "$WORK/ack2")
echo "resumed to $COMMITTED (expect $SIZE)"
test "$COMMITTED" = "$SIZE"

echo "== 6. verify server-side bytes are bit-identical"
SERVER_SHA=$(sha256 "$STORAGE_DIR/recordings/$RID/audio.flac")
test "$SERVER_SHA" = "$SHA" && echo "sha256 match"

echo "== 7. finalize"
DURATION=30
if [ "$STT" = "speaches" ]; then
  # Real fixture duration (activity timeouts scale from duration_sec).
  if command -v ffprobe >/dev/null 2>&1; then
    DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$WORK/test.flac")
  else
    DURATION=$(docker run --rm -v "$WORK:/w" --entrypoint ffprobe \
      ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cpu \
      -v error -show_entries format=duration -of csv=p=0 /w/test.flac)
  fi
  # Normalize via python float: catches '1.2.3', '.', 'N/A'; '30.' is a
  # valid float (30.0). Keeps the finalize JSON body safe from ffprobe
  # garbage. DURATION stays intact for the error message.
  D=$(python3 -c "print(round(float('$DURATION'), 3))" 2>/dev/null) \
    || { echo "unparseable fixture duration: '$DURATION'"; exit 1; }
  DURATION=$D
fi
HTTP=$(curl -s -o "$WORK/fin" -w '%{http_code}' -X POST \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "{\"sha256\":\"$SHA\",\"duration_sec\":$DURATION}" "$API/recordings/$RID/finalize")
test "$HTTP" = "200" && jq -r .state "$WORK/fin" | grep -q processing && echo finalized

echo "== 8. wait for pipeline stages (max ${WAIT}s)"
DEADLINE=$((SECONDS + WAIT))
while [ $SECONDS -lt $DEADLINE ]; do
  STATES=$(auth "$API/recordings/$RID" | jq -r '[.stages[].status] | join(",")')
  echo "  stages: $STATES"
  # all stages reached terminal state?
  # Count stages from the response itself — the pipeline gained a stage
  # (`chunk`) once already; a hardcoded count silently never breaks.
  DONE=$(echo "$STATES" | tr ',' '\n' | grep -cE 'done|failed|skipped' || true)
  TOTAL=$(echo "$STATES" | tr ',' '\n' | grep -c . || true)
  [ "$TOTAL" -gt 0 ] && [ "$DONE" = "$TOTAL" ] && break
  sleep 15
done
echo "$STATES" | tr ',' '\n' | grep -q failed && { echo "PIPELINE FAILED"; auth "$API/recordings/$RID" | jq -r '.stages[] | select(.status=="failed") | .last_error'; exit 1; }
echo "$STATES" | tr ',' '\n' | grep -qvE 'done|skipped' && { echo "TIMEOUT waiting"; exit 1; }

echo "== 9. artifacts exist"
# Vault mode: the export moves the meta tree out of /storage into the vault (.transcripter/meta/) — artifact asserts must accept EITHER location (storage meta or the vault mirror). The export runs right after the pipeline,
# so the storage copy may be gone by the time we check; the vault copy is the durable one. Discover the vault folder FIRST so every
# artifact assert can fall back to the vault mirror.
VAULT_DIR_HOST="${TRANSCRIPTER_TRANSCRIPTS:-$STORAGE_DIR/transcripts}"
FOLDER=$(find "$VAULT_DIR_HOST" -maxdepth 3 -type d -name "* ${RID:0:8}" 2>/dev/null | head -1)
meta_file() {
  local f="$1"
  for p in "$STORAGE_DIR/recordings/$RID/meta/$f" "$FOLDER/.transcripter/meta/$f"; do
    test -s "$p" && { printf '%s' "$p"; return 0; }
  done
  return 1
}
if [ "$STT" = "speaches" ]; then
  # Word timestamps are the diarization-merge input; empty words = silent
  # degradation of the whole api-backend path. Prove them non-empty.
  WORDS=$(jq '.words | length' "$(meta_file segments.json)") || { echo "  MISSING segments.json (storage + vault)"; exit 1; }
  test "$WORDS" -gt 0 && echo "  ok: segments.json words=$WORDS" || {
    echo "  FAIL: no word timestamps in segments.json (backend=api path)"; exit 1
  }
fi
for f in transcript.md segments.json; do
  test -s "$(meta_file "$f")" && echo "  ok: $f" || { echo "  MISSING: $f (storage + vault)"; exit 1; }
done
# Gate each artifact on the stage that writes it. Capture the response first:
# under pipefail a transient curl failure inside `if` would silently skip.
RESP=$(authf "$API/recordings/$RID")
if echo "$RESP" | jq -e '.stages[] | select(.kind=="diarize").status=="done"' >/dev/null; then
  test -s "$(meta_file diarization.json)"     && echo "  ok: diarization.json" || { echo "  MISSING: diarization.json"; exit 1; }
fi
if echo "$RESP" | jq -e '.stages[] | select(.kind=="merge_speakers").status=="done"' >/dev/null; then
  test -s "$(meta_file diarized-transcript.md)"     && echo "  ok: diarized-transcript.md" || { echo "  MISSING: diarized-transcript.md"; exit 1; }
fi

echo "== 9b. exported vault folder (nested YYYY/MM, audio + manifest)"
# Nested YYYY/MM layout + legacy root-level flat; exactly one folder per recording.
# (VAULT_DIR_HOST + FOLDER were already discovered in step 9 so the artifact
# asserts above could fall back to the vault mirror.)
FOLDERS=$(find "$VAULT_DIR_HOST" -maxdepth 3 -type d -name "* ${RID:0:8}" 2>/dev/null)
N=$(printf '%s\n' "$FOLDERS" | grep -c . || true)  # grep -c exits 1 on zero matches — without ||true set -e/pipefail kills the script silently
FOLDER=$(printf '%s\n' "$FOLDERS" | head -1)
test "$N" -eq 1 && test -s "$FOLDER/transcript.md" && echo "  ok: $(basename "$(dirname "$FOLDER")")/$(basename "$FOLDER")/transcript.md" || { echo "  MISSING/dup exported note folder (N=$N)"; exit 1; }
grep -q "recording_id: $RID" "$FOLDER/transcript.md" && echo "  ok: frontmatter recording_id" || { echo "  BAD frontmatter"; exit 1; }
grep -q '^tags:' "$FOLDER/transcript.md" && echo "  ok: frontmatter tags" || { echo "  BAD frontmatter: no tags"; exit 1; }
# The vault feature: the audio FLAC moved into .transcripter/ and a manifest
# landed beside it; storage's audio.flac is GONE (the move, not a copy).
test -s "$FOLDER/.transcripter/audio.flac" && echo "  ok: .transcripter/audio.flac" || { echo "  MISSING vault audio"; exit 1; }
grep -q "\"id\": \"$RID\"" "$FOLDER/.transcripter/manifest.json" && echo "  ok: manifest id" || { echo "  BAD manifest"; exit 1; }
test ! -e "$STORAGE_DIR/recordings/$RID/audio.flac" && echo "  ok: storage audio moved out" || { echo "  storage audio.flac still present"; exit 1; }
test -s "$VAULT_DIR_HOST/Dashboard.md" && echo "  ok: Dashboard.md" || { echo "  MISSING Dashboard.md"; exit 1; }
# Profile-matched summarize names its artifact per profile.output_artifact and
# carries `profile:` in frontmatter; asserted only when summarize actually ran.
if echo "$RESP" | jq -e '.stages[] | select(.kind=="summarize").status=="done"' >/dev/null; then
  test -s "$(meta_file summary.md)" && echo "  ok: meta/summary.md (canonical)" || { echo "  MISSING meta/summary.md"; exit 1; }
  test -s "$FOLDER/session-log.md" && grep -q "profile: ttrpg-session-log" "$FOLDER/session-log.md" \
    && echo "  ok: session-log.md (profile ttrpg-session-log)" || { echo "  MISSING/BAD session-log.md"; exit 1; }
else
  echo "  skip: summarize not done — session-log.md not asserted"
fi

if [ "${GRAPH:-0}" = "1" ]; then
  echo "== 9c. knowledge graph (enrich stage)"
  echo "$RESP" | jq -e '.stages[] | select(.kind=="enrich").status=="done"' >/dev/null \
    && echo "  ok: enrich done" || { echo "  enrich NOT done:"; echo "$RESP" | jq -r '.stages[] | select(.kind=="enrich")'; exit 1; }
  # The LLM extraction on the speech fixture legitimately returns EMPTY
  # (the ttrpg profile demands RPG facts), so node counts from the
  # model are informational only; the deterministic write-path probe
  # (write -> rewrite -> cleanup through worker.enrich.write_to_graph,
  # run inside the worker container because bolt is NOT published) is
  # what asserts graph correctness.
  CYPHER() { $DC exec -T neo4j \
    cypher-shell -u neo4j -p "$NEO4J_PASSWORD" --format plain "$1" 2>/dev/null; }
  NODES=$(CYPHER "MATCH (n {origin_recording_id: '$RID'}) RETURN count(n) AS c" | grep -oE '[0-9]+' | head -1)
  echo "  info: $((${NODES:-0} + 0)) LLM-extracted graph nodes for recording"
  $DC cp "$(dirname "$0")/graph_probe.py" worker:/tmp/graph_probe.py
  $DC exec -T -w /app/worker \
    -e PYTHONPATH=/app/worker worker \
    .venv/bin/python /tmp/graph_probe.py || { echo "  graph probe FAILED"; exit 1; }
  $DC exec -T worker rm -f /tmp/graph_probe.py

  echo "== 9d. enrich regenerate returns to done"
  HTTP=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
    -d '{"stage":"enrich"}' "$API/recordings/$RID/regenerate")
  test "$HTTP" = "200" || { echo "  regenerate enrich rc=$HTTP"; exit 1; }
  DEADLINE=$((SECONDS + WAIT))
  while [ $SECONDS -lt $DEADLINE ]; do
    ST=$(auth "$API/recordings/$RID" | jq -r '.stages[] | select(.kind=="enrich").status')
    [ "$ST" = "done" ] && break
    [ "$ST" = "failed" ] && { echo "  enrich regenerate FAILED"; exit 1; }
    sleep 10
  done
  test "$ST" = "done" && echo "  ok: enrich re-ran to done" || { echo "  TIMEOUT waiting for enrich regenerate"; exit 1; }
fi

if [ "${GRAPH:-0}" = "1" ]; then
  echo "== 9e. Phase A graph edits — PATCH event → events.json (both copies) + digest renewal"

  # The LLM extraction on the speech fixture legitimately returns EMPTY (ttrpg profile), so the smoke recording's
  # events.json has nothing to edit. Seed the recording's own event/entity graph via the probe (same write path as enrich: write_to_graph + write_events_json) — 9e then runs
  # the real API → Temporal → worker edit flow on deterministic data, no LLM dependency.
  $DC cp "$(dirname "$0")/graph_edit_probe.py" worker:/tmp/graph_edit_probe.py
  $DC exec -T -w /app/worker \
    -e PYTHONPATH=/app/worker -e PROBE_RID="$RID" \
    -e PROBE_VAULT_META="/transcripts/${FOLDER#\$VAULT_DIR_HOST/}/.transcripter/meta" \
    worker .venv/bin/python /tmp/graph_edit_probe.py || { echo "  edit probe FAILED"; exit 1; }
  $DC exec -T worker rm -f /tmp/graph_edit_probe.py

  TL=$(authf "$API/tags/e2e/timeline")
  EVKEY=$(echo "$TL" | jq -r --arg rid "$RID" \
    '[.sessions[] | select(.recording_id == $rid)] | .[0].events[0].event_key // empty' | head -1)
  test -n "$EVKEY" || { echo "  FAIL: no editable event for $RID (empty extraction?)"; exit 1; }
  echo "  event_key=$EVKEY"

  MARK="E2E-SMOKE-EDIT-$RANDOM"
  HTTP=$(curl -s -o "$WORK/patchack" -w '%{http_code}' -X PATCH \
    -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
    -d "{\"summary\":\"$MARK patched\"}" "$API/tags/e2e/events/$EVKEY")
  test "$HTTP" = "202" || { echo "  PATCH rc=$HTTP:"; cat "$WORK/patchack"; exit 1; }
  echo "  PATCH accepted (202)"

  # Wait for the Temporal ApplyGraphEdit workflow to land (poll the timeline,
  # which reads events.json — the artifact copy the workflow rewrites).
  DEADLINE=$((SECONDS + 120))
  APPLIED=0
  while [ $SECONDS -lt $DEADLINE ]; do
    if auth "$API/tags/e2e/timeline" | jq -e --arg rid "$RID" --arg k "$EVKEY" --arg m "$MARK" \
      '[.sessions[] | select(.recording_id==$rid) | .events[] | select(.summary | contains($m))] | length > 0' >/dev/null; then
      APPLIED=1; break
    fi
    sleep 5
  done
  test "$APPLIED" = "1" && echo "  ok: timeline reflects edit" || { echo "  edit not applied in 120s"; exit 1; }

    # The events.json copies (storage meta + vault mirror — vault mode deletes the storage copy after export; the vault mirror is the durable one.
  MF=$(meta_file events.json) || { echo "  FAIL: no events.json copy at all"; exit 1; }
  grep -q "$MARK" "$MF" \
    && echo "  ok: events.json updated ($MF)" || { echo "  events.json lacks edit"; exit 1; }

  # Digest: edit marks it stale (queued) → maintenance workflow renews ONCE
  # after graph.edit_debounce_sec of silence (20s in the dev-stack smoke config).
  ST=$(authf "$API/tags/e2e/digest/status" | jq -r .state)
  echo "  digest/status state=$ST (queued expected)"
  test "$ST" = "queued" || { echo "  digest/status not queued after edit: $ST"; exit 1; }
  DIGEST_FILE="$VAULT_DIR_HOST/digests/e2e.md"
  MTIME_BEFORE=$(stat -c %Y "$DIGEST_FILE" 2>/dev/null || stat -f %m "$DIGEST_FILE" 2>/dev/null || echo 0)
  echo "  waiting for renewal (debounce + digest LLM call)..."
  DEADLINE=$((SECONDS + 300))
  RENEWED=0
  while [ $SECONDS -lt $DEADLINE ]; do
    MT=$(stat -c %Y "$DIGEST_FILE" 2>/dev/null || stat -f %m "$DIGEST_FILE" 2>/dev/null || echo 0)
    if [ "$MT" != "$MTIME_BEFORE" ] && [ "$MT" != "0" ]; then
      STATE=$(auth "$API/tags/e2e/digest/status" | jq -r .state)
      test "$STATE" = "fresh" && RENEWED=1 && break
    fi
    sleep 10
  done
  test "$RENEWED" = "1" && echo "  ok: digest renewed once after debounce" \
    || { echo "  digest NOT renewed within 300s"; exit 1; }
fi

echo "== 10. regenerate diarize"
HTTP=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"stage":"diarize"}' "$API/recordings/$RID/regenerate")
# 409 while processing is acceptable; 200 when idle
test "$HTTP" = "200" || test "$HTTP" = "409" && echo "regenerate rc=$HTTP (processing-guard ok)"

echo
echo "E2E SMOKE PASSED (recording $RID)"
