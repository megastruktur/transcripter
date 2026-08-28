"""Postgres-dialect coverage for Recording.tags containment (@>).

Skipped unless TRANSCRIPTER_TEST_PG_URL points at a scratch Postgres.
The default sqlite variant exercises JSON containment but NOT the ARRAY
@> operator production uses — regression 2026-08-28: the generic
sqlalchemy.ARRAY type raised NotImplementedError on .contains() and the
digest activity died on the first live run.

Manual run against the compose stack (scratch DB):

    docker compose exec postgres psql -U transcripter -c 'CREATE DATABASE transcripter_test'
    docker compose exec -T -e TRANSCRIPTER_TEST_PG_URL=\
postgresql+psycopg://transcripter:transcripter@postgres/transcripter_test \
      -w /app/worker worker .venv/bin/python -m pytest tests/test_tags_pg.py
    docker compose exec postgres psql -U transcripter -c 'DROP DATABASE transcripter_test'
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from worker.db import Base, Recording

PG_URL = os.environ.get("TRANSCRIPTER_TEST_PG_URL", "")


@pytest.mark.skipif(not PG_URL, reason="TRANSCRIPTER_TEST_PG_URL not set")
def test_tags_contains_on_postgres() -> None:
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    rid = str(uuid.uuid4())
    with Session() as s:
        s.add(Recording(id=rid, title="pg-tags-test", tags=["alpha", "beta"]))  # type: ignore[call-arg]
        s.commit()
        try:
            hit = s.scalars(
                select(Recording).where(Recording.tags.contains(["alpha"]))
            ).all()
            miss = s.scalars(
                select(Recording).where(Recording.tags.contains(["gamma"]))
            ).all()
            assert [r.id for r in hit] == [rid]
            assert miss == []
        finally:
            s.delete(s.get(Recording, rid))
            s.commit()
    engine.dispose()
