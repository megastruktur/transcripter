"""Catalog access for the worker (same schema as API).

Worker owns stage transitions while executing activities.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import JSON, Enum, ForeignKey, String, Text, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import TEXT
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class RecordingState(enum.Enum):
    uploading = "uploading"
    processing = "processing"
    done = "done"
    failed = "failed"


class StageStatus(enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"


STAGE_KINDS = ("chunk", "transcribe", "diarize", "merge_speakers", "summarize", "enrich")


class Recording(Base):
    __tablename__ = "recordings"

    # Wave A knowledge-graph tags. Postgres stores TEXT[]; SQLite (worker
    # unit tests + local dev) gets a JSON variant so the same python-side
    # ``list[str]`` default works in both dialects. Matches the API schema.
    # NOTE: the DIALECT-SPECIFIC postgresql ARRAY — the generic
    # sqlalchemy.ARRAY lacks .contains() (@>) which digest.py relies on
    # (NotImplementedError at runtime on Postgres; sqlite tests never saw it).
    tags: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(TEXT, as_tuple=False).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[RecordingState] = mapped_column(
        Enum(RecordingState, name="recording_state"), default=RecordingState.uploading
    )
    committed_bytes: Mapped[int] = mapped_column(default=0)
    total_bytes: Mapped[int | None] = mapped_column(default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_sec: Mapped[float | None] = mapped_column(default=None)
    # Phase 0 type/freehand-tag split — mirrors the API schema (app/db.py):
    # `type` routes the pipeline (profile match by recording.type), NULL →
    # built-in default pipeline; `recorded_at` is the import backdate.
    # Both nullable; the API startup migration adds them to existing
    # Postgres tables, so the worker never needs its own ALTER.
    type: Mapped[str | None] = mapped_column(Text, default=None)
    recorded_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    stages: Mapped[list["Stage"]] = relationship(
        back_populates="recording",
        cascade="all, delete-orphan",
        order_by="Stage.kind",
    )


class Stage(Base):
    __tablename__ = "stages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Enum(*STAGE_KINDS, name="stage_kind"))
    status: Mapped[StageStatus] = mapped_column(Enum(StageStatus, name="stage_status"), default=StageStatus.pending)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    recording: Mapped[Recording] = relationship(back_populates="stages")


_engine = None
_SessionLocal: sessionmaker | None = None


def init_engine(url: str) -> None:
    global _engine, _SessionLocal
    _engine = create_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def session() -> Session:
    assert _SessionLocal is not None, "init_engine() must be called first"
    return _SessionLocal()


def set_stage(
    rec_id: str,
    kind: str,
    status: StageStatus,
    error: str | None = None,
    details: dict | None = None,
    inc_attempts: bool = False,
) -> None:
    with session() as s:
        stage = s.query(Stage).filter_by(recording_id=rec_id, kind=kind).one()
        stage.status = status
        if error is not None:
            stage.last_error = error
        elif status in (StageStatus.running, StageStatus.skipped):
            # Fresh attempt or a deliberate skip: drop the previous run's
            # error so the UI does not show a stale failure on a stage that
            # is no longer even attempting.
            stage.last_error = None
        if details is not None:
            stage.details = details
        if inc_attempts:
            stage.attempts += 1
        s.commit()


def set_recording_state(rec_id: str, state: RecordingState) -> None:
    with session() as s:
        rec = s.get(Recording, rec_id)
        assert rec is not None, f"recording {rec_id} not found"
        rec.state = state
        s.commit()
