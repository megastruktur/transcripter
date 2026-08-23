"""set_stage semantics: error clearing per transition."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker.db import Base, Recording, RecordingState, Stage, StageStatus, set_stage


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    with Session() as s:
        rec = Recording(id="r1", state=RecordingState.processing, title="t")
        s.add(rec)
        s.add(Stage(recording_id="r1", kind="diarize", status=StageStatus.pending))
        s.commit()
    return Session


def _get(Session) -> Stage:
    with Session() as s:
        return s.query(Stage).filter_by(recording_id="r1", kind="diarize").one()


def test_skipped_clears_last_error(db):
    set_stage("r1", "diarize", StageStatus.failed, error="boom")
    assert _get(db).last_error == "boom"
    set_stage("r1", "diarize", StageStatus.skipped)
    st = _get(db)
    assert st.status == StageStatus.skipped
    assert st.last_error is None


def test_running_clears_last_error(db):
    set_stage("r1", "diarize", StageStatus.failed, error="boom")
    set_stage("r1", "diarize", StageStatus.running, inc_attempts=True)
    assert _get(db).last_error is None


def test_done_preserves_error_until_next_run(db):
    set_stage("r1", "diarize", StageStatus.failed, error="boom")
    set_stage("r1", "diarize", StageStatus.done, details={"speakers": []})
    st = _get(db)
    assert st.last_error == "boom"  # cleared on next running, not on done
    assert st.details == {"speakers": []}


def test_details_replaced_on_skip(db):
    set_stage("r1", "diarize", StageStatus.done, details={"speakers": ["a"]})
    set_stage("r1", "diarize", StageStatus.skipped, details={})
    assert _get(db).details == {}
