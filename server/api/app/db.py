"""SQLAlchemy catalog: recordings + stages.

Worker writes stage transitions directly (plan T3); API reads.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Enum, Float, ForeignKey, String, Text, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import TEXT
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.graph_edit_model import EditOp, EditStatus, EditTarget


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


STAGE_KINDS = ("chunk", "separate", "transcribe", "diarize", "merge_speakers", "summarize", "enrich")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(Text, default="")
    # Normalized knowledge-graph tags (trim + lowercase + dedupe, order preserved).
    # Postgres stores TEXT[]; SQLite (tests, local dev) gets a JSON variant so
    # the same python-side `list[str]` default works in both dialects.
    tags: Mapped[list[str]] = mapped_column(
        # Dialect-specific postgresql ARRAY: the generic sqlalchemy.ARRAY
        # lacks .contains() (@>) — keep both packages on the same type.
        postgresql.ARRAY(TEXT, as_tuple=False).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )
    state: Mapped[RecordingState] = mapped_column(
        Enum(RecordingState, name="recording_state"), default=RecordingState.uploading
    )
    # Resumable upload bookkeeping
    committed_bytes: Mapped[int] = mapped_column(default=0)
    total_bytes: Mapped[int | None] = mapped_column(default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_sec: Mapped[float | None] = mapped_column(Float, default=None)
    # Phase 0 type/freehand-tag split: `type` is the system recording-type
    # slug (^[a-z0-9][a-z0-9-]{0,31}$) that routes the pipeline (profile
    # match); NULL → built-in default pipeline. `recorded_at` is when the
    # audio actually happened (import backdate); NULL → client displays
    # coalesce(recorded_at, created_at). Both nullable: pre-Phase-0 rows
    # predate them (columns are added idempotently in app.main startup).
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
    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, name="stage_status"), default=StageStatus.pending
    )
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    recording: Mapped[Recording] = relationship(back_populates="stages")


class GraphEdit(Base):
    """Edit-store row (Phase A graph editing). The API creates the table
    (create_all) and inserts rows for every accepted edit; the worker's
    edit activities read them for overlay/audit. ``before``/``after``
    carry the op payload; ``anchor`` the fuzzy re-anchor context for
    event edits; ``feedback_text`` the NL instruction for enrich
    prompts (Phase B). Mirrors worker.db.GraphEdit exactly — change the
    two IN SYNC."""

    __tablename__ = "graph_edits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tag: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    target: Mapped[EditTarget] = mapped_column(
        Enum(EditTarget, name="edit_target"), nullable=False
    )
    op: Mapped[EditOp] = mapped_column(Enum(EditOp, name="edit_op"), nullable=False)
    obj_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    anchor: Mapped[dict] = mapped_column(JSON, default=dict)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    feedback_text: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str] = mapped_column(String(16), default="user")
    status: Mapped[EditStatus] = mapped_column(
        Enum(EditStatus, name="edit_status"), default=EditStatus.applied
    )
    applied_namespaces: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(TEXT, as_tuple=False).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

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
