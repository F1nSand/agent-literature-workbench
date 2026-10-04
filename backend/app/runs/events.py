from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.runs.models import Run, RunEvent


MAX_RUN_EVENTS = 256


def append_run_event(session: Session, run_id: UUID, event_type: str, payload: dict) -> RunEvent:
    """Append one ordered event and retain a bounded replay window for this run."""
    session.scalar(select(Run.id).where(Run.id == run_id).with_for_update())
    sequence_number = (session.scalar(
        select(func.max(RunEvent.sequence_number)).where(RunEvent.run_id == run_id)
    ) or 0) + 1
    event = RunEvent(
        run_id=run_id,
        sequence_number=sequence_number,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    session.flush()
    if sequence_number > MAX_RUN_EVENTS:
        session.execute(delete(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.sequence_number <= sequence_number - MAX_RUN_EVENTS,
        ))
    return event
