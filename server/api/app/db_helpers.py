"""Shared DB helpers for the API's recording routes (state transitions,
tag-registry auto-registration)."""

from collections.abc import Iterable
from typing import cast

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db import Recording, RecordingState, TagDef, get_session


def set_recording_failed(rec_id: str) -> None:
    gen = get_session()
    s = cast(Session, next(gen))
    try:
        s.execute(
            update(Recording)
            .where(Recording.id == rec_id)
            .values(state=RecordingState.failed)
        )
        s.commit()
    finally:
        gen.close()


def register_tag_defs(session: Session, tags: Iterable[str]) -> None:
    """Auto-register tags into the ``tag_defs`` registry: INSERT ...
    ON CONFLICT (name) DO NOTHING, one statement per tag inside the
    CALLER's transaction (no commit here — the create/PATCH commit
    carries it).

    Keeps the registry a superset of the tags seen on recordings: every
    recording-side tag becomes editable (vocabulary) on the Tags page
    without a manual create step, and a concurrent second upload of the
    same brand-new tag cannot IntegrityError (conflict is a no-op).

    Dialect branch is the list_tags precedent: core INSERT has no
    dialect-neutral on_conflict; both sqlite and postgresql do.
    """
    bind = session.get_bind()
    dialect = bind.dialect.name
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt_factory = (
        (lambda n: pg_insert(TagDef).values(name=n).on_conflict_do_nothing(constraint="tag_defs_pkey"))
        if dialect == "postgresql"
        else (lambda n: sqlite_insert(TagDef).values(name=n).on_conflict_do_nothing(index_elements=["name"]))
    )

    for tag in tags:
        if tag:  # _normalize_tags already dropped blanks; belt and braces
            session.execute(stmt_factory(tag))

