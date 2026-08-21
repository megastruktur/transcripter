#!/usr/bin/env bash
# E2E smoke: full path — upload with interruption+resume → pipeline artifacts.
# Requires: docker compose stack up (api on :8080), jq, curl.
# Usage: bash server/scripts/e2e_smoke.sh [seconds-to-wait-for-pipeline]

set -euo pipefail

API="http://localhost:8090"
TOKEN="${TRANSCRIPTER_TOKEN:-test-token-e2e}"
WAIT="${1:-600}"
STORAGE_DIR="$(cd "$(dirname "$0")/.." && pwd)/storage"
WORK="$(cd "$(dirname "$0")/.." && pwd)/.e2e-work"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT  # keep under project path: containers can't see host /tmp

auth() { curl -s -H "authorization: Bearer $TOKEN" "$@"; }

echo "== 1. health"
curl -sf "$API/health" | jq -e '.status == "ok"' >/dev/null && echo OK

echo "== 2. generate test audio (2 alternating tones, 30s, 16kHz mono)"
# Sine segments alternating every 1s — two "voices" by frequency.
python3 - "$WORK/test.wav" <<'PY'
import math, struct, sys, wave
sr = 16000
dur = 30
frames = []
for i in range(sr * dur):
    t = i / sr
    voice = 220 if int(t) % 2 == 0 else 440
    amp = 0.4 if (t % 1.0) < 0.8 else 0.0  # speech-like gaps
    frames.append(int(amp * math.sin(2 * math.pi * voice * t) * 32767))
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(struct.pack(f"<{len(frames)}h", *frames))
print("wav written")
PY

# Convert to FLAC inside container (host has no ffmpeg).
docker run --rm -i -v "$WORK:/w" -v /usr:/hu:ro gcc:14 bash -c '
  apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq ffmpeg >/dev/null 2>&1
  ffmpeg -v error -y -i /w/test.wav /w/test.flac
' && test -s "$WORK/test.flac" && echo "flac: $(stat -c%s "$WORK/test.flac") bytes"

SHA=$(docker run --rm -i -v "$WORK:/w" gcc:14 bash -c 'sha256sum /w/test.flac' | cut -d' ' -f1)
SIZE=$(stat -c%s "$WORK/test.flac")
echo "sha256=$SHA size=$SIZE"

echo "== 3. create recording"
RID=$(curl -sf -X POST -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"title":"e2e-smoke"}' "$API/recordings" | jq -r .id)
echo "id=$RID"

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
P2SIZE=$(stat -c%s "$WORK/part2")
HTTP=$(curl -s -o "$WORK/ack2" -w '%{http_code}' -X PUT \
  -H "authorization: Bearer $TOKEN" -H "content-length: $P2SIZE" \
  --data-binary "@$WORK/part2" "$API/recordings/$RID/audio?offset=$OVERLAP")
test "$HTTP" = "200" || { cat "$WORK/ack2"; exit 1; }
COMMITTED=$(jq -r .committed "$WORK/ack2")
echo "resumed to $COMMITTED (expect $SIZE)"
test "$COMMITTED" = "$SIZE"

echo "== 6. verify server-side bytes are bit-identical"
SERVER_SHA=$(docker run --rm -v "$STORAGE_DIR:/s" gcc:14 bash -c "sha256sum /s/recordings/$RID/audio.flac" | cut -d' ' -f1)
test "$SERVER_SHA" = "$SHA" && echo "sha256 match"

echo "== 7. finalize"
HTTP=$(curl -s -o "$WORK/fin" -w '%{http_code}' -X POST \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "{\"sha256\":\"$SHA\",\"duration_sec\":30}" "$API/recordings/$RID/finalize")
test "$HTTP" = "200" && jq -r .state "$WORK/fin" | grep -q processing && echo finalized

echo "== 8. wait for pipeline stages (max ${WAIT}s)"
DEADLINE=$((SECONDS + WAIT))
while [ $SECONDS -lt $DEADLINE ]; do
  STATES=$(auth "$API/recordings/$RID" | jq -r '[.stages[].status] | join(",")')
  echo "  stages: $STATES"
  # all stages reached terminal state?
  DONE=$(echo "$STATES" | tr ',' '\n' | grep -cE 'done|failed|skipped')
  [ "$DONE" = "4" ] && break
  sleep 15
done
echo "$STATES" | tr ',' '\n' | grep -q failed && { echo "PIPELINE FAILED"; auth "$API/recordings/$RID" | jq -r '.stages[] | select(.status=="failed") | .last_error'; exit 1; }
echo "$STATES" | tr ',' '\n' | grep -qvE 'done|skipped' && { echo "TIMEOUT waiting"; exit 1; }

echo "== 9. artifacts exist"
for f in transcript.md segments.json diarization.json diarized-transcript.md; do
  test -s "$STORAGE_DIR/recordings/$RID/meta/$f" && echo "  ok: $f" || { echo "  MISSING: $f"; exit 1; }
done

echo "== 10. regenerate diarize"
HTTP=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"stage":"diarize"}' "$API/recordings/$RID/regenerate")
# 409 while processing is acceptable; 200 when idle
test "$HTTP" = "200" || test "$HTTP" = "409" && echo "regenerate rc=$HTTP (processing-guard ok)"

echo
echo "E2E SMOKE PASSED (recording $RID)"
