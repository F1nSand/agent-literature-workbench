import json
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.v1 import runs as run_routes
from app.main import app
from app.runs.events import MAX_RUN_EVENTS, append_run_event
from app.runs.models import Run, RunEvent
from app.topics.models import Conversation, Topic


def test_run_events_replay_and_expired_cursor_snapshot(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL event stream test")
    from sqlalchemy import create_engine

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(run_routes, "get_engine", lambda: engine)
    topic_id, conversation_id, run_id = uuid4(), uuid4(), uuid4()
    client = TestClient(app)
    try:
        with Session(engine) as session:
            session.add(Topic(id=topic_id, title="Event replay"))
            session.flush()
            session.add(Conversation(id=conversation_id, topic_id=topic_id, title="Event replay"))
            session.flush()
            session.add(Run(
                id=run_id,
                conversation_id=conversation_id,
                status="completed",
                scope_mode="topic",
                scope_snapshot={"schema_version": 1},
                scope_hash="a" * 64,
                budget_snapshot={"max_tool_calls": 12},
                idempotency_key=uuid4().hex,
            ))
            session.commit()

        with Session(engine) as session:
            with session.begin():
                for sequence in range(1, MAX_RUN_EVENTS + 4):
                    append_run_event(session, run_id, "progress", {"step": sequence})
                append_run_event(session, run_id, "completed", {"status": "completed"})
            assert session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)) == MAX_RUN_EVENTS
            assert session.scalar(select(func.min(RunEvent.sequence_number)).where(RunEvent.run_id == run_id)) == 5
            assert session.scalar(select(func.max(RunEvent.sequence_number)).where(RunEvent.run_id == run_id)) == MAX_RUN_EVENTS + 4

        replay = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(MAX_RUN_EVENTS + 2)})
        assert replay.status_code == 200
        assert replay.headers["content-type"].startswith("text/event-stream")
        assert f"id: {MAX_RUN_EVENTS + 3}\nevent: progress" in replay.text
        assert f"id: {MAX_RUN_EVENTS + 4}\nevent: completed" in replay.text
        assert "event: snapshot" not in replay.text

        expired = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "3"})
        assert expired.status_code == 200
        assert expired.text.startswith(f"id: {MAX_RUN_EVENTS + 4}\nevent: snapshot\n")
        snapshot_line = next(line[6:] for line in expired.text.splitlines() if line.startswith("data: "))
        snapshot = json.loads(snapshot_line)
        assert snapshot["sequence_number"] == MAX_RUN_EVENTS + 4
        assert snapshot["run"]["id"] == str(run_id)
        assert snapshot["run"]["status"] == "completed"

        invalid = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "not-a-number"})
        assert invalid.text.startswith(f"id: {MAX_RUN_EVENTS + 4}\nevent: snapshot\n")
        future = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "999"})
        assert future.text.startswith(f"id: {MAX_RUN_EVENTS + 4}\nevent: snapshot\n")

        async def disconnect_after_heartbeat(_request) -> bool:
            disconnect_after_heartbeat.calls += 1
            return disconnect_after_heartbeat.calls > 2

        disconnect_after_heartbeat.calls = 0
        monkeypatch.setattr(run_routes.Request, "is_disconnected", disconnect_after_heartbeat)
        with Session(engine) as session:
            with session.begin():
                run = session.get(Run, run_id)
                assert run is not None
                run.status = "queued"
                session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
                append_run_event(session, run_id, "queued", {"status": "queued"})

        active = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "1"})
        assert active.status_code == 200
        assert ": keep-alive\n\n" in active.text
    finally:
        with Session(engine) as session:
            session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
            session.execute(delete(Run).where(Run.id == run_id))
            session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            session.execute(delete(Topic).where(Topic.id == topic_id))
            session.commit()
        engine.dispose()
