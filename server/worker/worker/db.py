"""Catalog access for the worker (same schema as API).

Worker owns stage transitions while executing activities.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import JSON, Enum, ForeignKey, String, Text, create_engine
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


class RecordingState(str, enum.Enum):
    uploading = "uploading"
    processing = "processing"
    done = "done"
    failed = "failed"


class StageStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"


STAGE_KINDS = ("transcribe", "diarize", "merge_speakers", "summarize")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[RecordingState] = mapped_column(
        Enum(RecordingState), default=RecordingState.uploading
    )
    committed_bytes: Mapped[int] = mapped_column(default=0)
    total_bytes: Mapped[int | None] = mapped_column(default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_sec: Mapped[float | None] = mapped_column(default=None)
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
    kind: Mapped[str] = mapped_column(Enum(*STAGE_KINDS))
    status: Mapped[StageStatus] = mapped_column(Enum(StageStatus), default=StageStatus.pending)
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
