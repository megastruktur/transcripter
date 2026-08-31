"""Deterministic write-path probe for the enrich stage (e2e smoke, GRAPH=1).

The LLM extraction on the speech fixture legitimately returns EMPTY lists
(the ttrpg profile demands RPG facts), so the smoke cannot assert on
model output. This probe exercises worker.enrich.write_to_graph end-to-end
instead: write synthetic nodes under a probe origin, rewrite with different
content (DETACH DELETE + MERGE idempotency), then clean up. Runs INSIDE the
worker container (bolt://neo4j:7687 is not published to the host by design):

    docker compose cp scripts/graph_probe.py worker:/tmp/graph_probe.py
    docker compose exec -T -w /app/worker worker .venv/bin/python /tmp/graph_probe.py
"""

import os
import sys

from neo4j import GraphDatabase

from worker.config import load_config
from worker.enrich import (
    ExtractedEntity,
    ExtractedEvent,
    ExtractedGraph,
    write_to_graph,
)
from worker.profiles import load_profiles

ORIGIN = "e2e-graph-probe"


def main() -> int:
    c = load_config()
    if not c.graph.enabled:
        print("graph not configured (graph.uri empty) — run with --profile graph", file=sys.stderr)
        return 1
    pw = os.environ.get(c.graph.password_env, "")
    labels = next(p.enrich.node_labels for p in load_profiles(c.profiles.path) if p.enrich)

    args = (c.graph.uri, c.graph.user, pw, c.graph.database)

    g1 = ExtractedGraph(
        events=[ExtractedEvent(ts="t0", kind="probe", summary="Probe Alpha meets Probe Beta")],
        entities=[
            ExtractedEntity(slug="probe-alpha", label="Probe Alpha", type="character"),
            ExtractedEntity(slug="probe-beta", label="Probe Beta", type="npc"),
        ],
        relations=[],
    )
    n1 = write_to_graph(ORIGIN, "e2e", g1, labels, *args)
    assert n1 == 2, f"first write: {n1} entities, want 2"

    # Rewrite with different content: DETACH DELETE must replace the subgraph.
    g2 = ExtractedGraph(
        events=[],
        entities=[ExtractedEntity(slug="probe-gamma", label="Probe Gamma", type="item")],
        relations=[],
    )
    n2 = write_to_graph(ORIGIN, "e2e", g2, labels, *args)
    assert n2 == 1, f"rewrite: {n2} entities, want 1"

    drv = GraphDatabase.driver(c.graph.uri, auth=(c.graph.user, pw))
    try:
        with drv.session(database=c.graph.database) as s:
            rows = sorted(s.run("MATCH (n {origin_recording_id: $o}) RETURN n.slug AS slug", o=ORIGIN).value())
        assert rows == ["probe-gamma"], f"after rewrite: {rows}, want ['probe-gamma']"

        # Cleanup: empty extraction = DETACH DELETE everything for the origin.
        write_to_graph(ORIGIN, "e2e", ExtractedGraph(events=[], entities=[], relations=[]), labels, *args)
        with drv.session(database=c.graph.database) as s:
            left = s.run("MATCH (n {origin_recording_id: $o}) RETURN count(n) AS c", o=ORIGIN).single()["c"]
        assert left == 0, f"cleanup left {left} nodes"
    finally:
        drv.close()

    print(f"graph probe OK (wrote {n1}, rewrote to {n2}, cleaned up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
