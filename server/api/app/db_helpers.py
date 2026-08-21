"""Recording state helper used by async failure paths."""

from typing import cast

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db import Recording, RecordingState, get_session


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
