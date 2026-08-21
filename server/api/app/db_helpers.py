"""Recording state helper used by async failure paths."""

from sqlalchemy import update

from app.db import Recording, RecordingState, get_session


def set_recording_failed(rec_id: str) -> None:
    with get_session() as s:
        s.execute(
            update(Recording)
            .where(Recording.id == rec_id)
            .values(state=RecordingState.failed)
        )
        s.commit()
