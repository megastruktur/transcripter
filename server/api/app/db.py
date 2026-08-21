"""SQLAlchemy catalog: recordings + stages.

Worker writes stage transitions directly (plan T3); API reads.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Enum, Float, ForeignKey, String, Text, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
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


STAGE_KINDS = ("transcribe", "diarize", "merge_speakers", "summarize")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[RecordingState] = mapped_column(
        Enum(RecordingState, name="recording_state"), default=RecordingState.uploading
    )
    # Resumable upload bookkeeping
    committed_bytes: Mapped[int] = mapped_column(default=0)
    total_bytes: Mapped[int | None] = mapped_column(default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_sec: Mapped[float | None] = mapped_column(Float, default=None)
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
    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, name="stage_status"), default=StageStatus.pending
    )
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


def engine():
    assert _engine is not None, "init_engine() must be called first"
    return _engine


def get_session():
    assert _SessionLocal is not None, "init_engine() must be called first"
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()
