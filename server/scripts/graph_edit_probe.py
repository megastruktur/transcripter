"""Deterministic Phase A edit seed for the e2e smoke (GRAPH=1).

The LLM extraction on the speech fixture legitimately returns EMPTY (the ttrpg
profile demands RPG facts), so the smoke recording ends its pipeline with zero
graph events — nothing to PATCH. This probe seeds the smoke recording's OWN
event/entity graph deterministically through the SAME write path the enrich
stage uses (worker.enrich.write_to_graph + write_events_json), writing:

- Neo4j: two entities + one event (with event_key) + one relation for tag
  "e2e" under origin = the smoke recording id.
- events.json: storage meta + the vault mirror (.transcripter/meta), so the
  API's timeline read (which NEVER touches Neo4j) sees the seeded events too.

Then the smoke's 9e section runs the real API→Temporal→worker edit flow
(PATCH event) against this seeded state — a true e2e of the edit path with no
dependence on LLM output.

Run INSIDE the worker container (bolt://neo4j:7687 is not published):

    docker compose cp scripts/graph_edit_probe.py worker:/tmp/graph_edit_probe.py
    docker compose exec -T -w /app/worker -e PYTHONPATH=/app/worker \
        -e PROBE_RID=<recording-uuid> -e PROBE_VAULT_META=<container-path> \
        worker .venv/bin/python /tmp/graph_edit_probe.py

Env: PROBE_RID (smoke recording id), PROBE_VAULT_META (container path to the
recording's vault mirror meta dir, e.g.
/transcripts/YYYY/MM/<folder>/.transcripter/meta), PROBE_TAG (default "e2e").
"""

import os
import sys
from pathlib import Path

from worker.config import load_config
from worker.enrich import (
    ExtractedEntity,
    ExtractedEvent,
    ExtractedGraph,
    ExtractedRelation,
    write_events_json,
    write_to_graph,
)
from worker.profiles import load_profiles

RID = os.environ.get("PROBE_RID", "")
TAG = os.environ.get("PROBE_TAG", "e2e")
VAULT_META = os.environ.get("PROBE_VAULT_META", "")

EVENT_TS = "00:00:01"
EVENT_KIND = "milestone"
EVENT_SUMMARY = "Probe seed: e2e editable event for Phase A smoke."


def main() -> int:
    if not RID:
        print("PROBE_RID is required", file=sys.stderr)
        return 1
    c = load_config()
    if not c.graph.enabled:
        print("graph not configured (graph.uri empty) — run with --profile graph", file=sys.stderr)
        return 1
    pw = os.environ.get(c.graph.password_env, "")
    labels = next(p.enrich.node_labels for p in load_profiles(c.profiles.path) if p.enrich)
    args = (c.graph.uri, c.graph.user, pw, c.graph.database)

    g = ExtractedGraph(
        events=[ExtractedEvent(ts=EVENT_TS, kind=EVENT_KIND, summary=EVENT_SUMMARY)],
        entities=[
            ExtractedEntity(slug="probe-alpha", label="Probe Alpha", type="character"),
            ExtractedEntity(slug="probe-beta", label="Probe Beta", type="location"),
        ],
        relations=[ExtractedRelation(from_slug="probe-alpha", to_slug="probe-beta", type="visited")],
    )
    n = write_to_graph(
        RID, TAG, g, labels, *args,
        recording_date="2026-09-02T00:00:00Z",
        recording_title="e2e-smoke",
    )
    assert n == 2, f"seed write: {n} entities, want 2"

    # events.json — BOTH copies (storage meta + vault mirror), same shape as
    # enrich writes. The API timeline reads these files (never Neo4j), so the
    # seeded event is visible to the PATCH endpoint's existence check.
    meta_kw = dict(
        recording_id=RID,
        recording_date="2026-09-02T00:00:00Z",
        recording_title="e2e-smoke",
        profile_id="ttrpg-session-log",
        namespaces=[TAG],
    )
    storage_meta = Path("/storage/recordings") / RID / "meta" / "events.json"
    write_events_json(storage_meta, resolved=g, **meta_kw)
    if VAULT_META:
        write_events_json(Path(VAULT_META) / "events.json", resolved=g, **meta_kw)

    print(f"edit probe OK (seeded {n} entities + 1 event + 1 relation for {RID}@{TAG})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
