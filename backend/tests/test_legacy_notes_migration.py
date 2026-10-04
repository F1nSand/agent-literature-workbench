"""Run the additive legacy-note migration in a private schema of the Docker test DB."""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from app.notes.service import NoteServiceError, adopt_candidate


MIGRATION_PATH = Path(__file__).parents[1] / "migrations" / "versions" / "20261001_0009_legacy_notes.py"
spec = importlib.util.spec_from_file_location("legacy_notes_migration", MIGRATION_PATH)
migration = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(migration)


def _baseline(connection) -> None:
    connection.execute(sa.text("CREATE TABLE runs (id uuid PRIMARY KEY)"))
    connection.execute(sa.text("""
        CREATE TABLE artifacts (
            id uuid PRIMARY KEY, title varchar(500) NOT NULL, artifact_type varchar(32) NOT NULL,
            current_version_id uuid NULL, revision integer NOT NULL,
            created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        )
    """))
    connection.execute(sa.text("""
        CREATE TABLE artifact_versions (
            id uuid PRIMARY KEY, artifact_id uuid NOT NULL REFERENCES artifacts(id), version_number integer NOT NULL,
            parent_version_id uuid NULL, origin varchar(16) NOT NULL, status varchar(16) NOT NULL,
            body_text text NOT NULL, structured_content jsonb NULL, content_sha256 varchar(64) NOT NULL,
            source_run_id uuid NULL REFERENCES runs(id), evidence_snapshot jsonb NOT NULL,
            adopted_at timestamptz NULL, created_at timestamptz NOT NULL,
            UNIQUE (artifact_id, version_number), UNIQUE (artifact_id, id),
            FOREIGN KEY (artifact_id, parent_version_id) REFERENCES artifact_versions(artifact_id, id)
        )
    """))
    # These are the 0006 notes tables with the 0009 columns still absent.
    connection.execute(sa.text("""
        CREATE TABLE notes (
            id uuid PRIMARY KEY, title varchar(500) NOT NULL, revision integer NOT NULL,
            current_version_id uuid NULL, idempotency_key varchar(128) NULL UNIQUE,
            archived_at timestamptz NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        )
    """))
    connection.execute(sa.text("""
        CREATE TABLE note_versions (
            id uuid PRIMARY KEY, note_id uuid NOT NULL REFERENCES notes(id), version_number integer NOT NULL,
            base_revision integer NULL, parent_version_id uuid NULL, origin varchar(16) NOT NULL,
            status varchar(16) NOT NULL, content jsonb NOT NULL, plain_text text NOT NULL,
            source_run_id uuid NULL REFERENCES runs(id), idempotency_key varchar(128) NULL,
            adopted_at timestamptz NULL, created_at timestamptz NOT NULL,
            UNIQUE (note_id, version_number), UNIQUE (note_id, id), UNIQUE (note_id, idempotency_key),
            FOREIGN KEY (note_id, parent_version_id) REFERENCES note_versions(note_id, id)
        )
    """))
    connection.execute(sa.text("""
        ALTER TABLE notes ADD CONSTRAINT fk_notes_current_version_owner
        FOREIGN KEY (id, current_version_id) REFERENCES note_versions(note_id, id)
    """))


def _run_migration(connection, fn) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        fn()


def _create_private_schema(connection, schema: str) -> None:
    connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    connection.execute(sa.text(f'SET search_path TO "{schema}"'))
    _baseline(connection)


def _insert_artifact(connection, artifact_id, title, artifact_type, revision, current_version_id, now) -> None:
    connection.execute(sa.text("""
        INSERT INTO artifacts (id, title, artifact_type, revision, current_version_id, created_at, updated_at)
        VALUES (:id, :title, :kind, :revision, :current, :created, :updated)
    """), {"id": artifact_id, "title": title, "kind": artifact_type, "revision": revision,
           "current": current_version_id, "created": now, "updated": now})


def _insert_version(connection, *, version_id, artifact_id, version_number, parent_id, origin, status,
                    body, digest, source_run_id, evidence, structured, now) -> None:
    connection.execute(sa.text("""
        INSERT INTO artifact_versions
            (id, artifact_id, version_number, parent_version_id, origin, status, body_text,
             structured_content, content_sha256, source_run_id, evidence_snapshot, adopted_at, created_at)
        VALUES
            (:id, :artifact_id, :number, :parent, :origin, :status, :body,
             CAST(:structured AS jsonb), :digest, :run_id, CAST(:evidence AS jsonb), :adopted, :created)
    """), {"id": version_id, "artifact_id": artifact_id, "number": version_number,
           "parent": parent_id, "origin": origin, "status": status, "body": body,
           "structured": json.dumps(structured) if structured is not None else None,
           "digest": digest, "run_id": source_run_id, "evidence": json.dumps(evidence),
           "adopted": now if status == "current" else None, "created": now})


@pytest.mark.parametrize("with_legacy_data", [False, True])
def test_legacy_note_migration_is_additive_idempotent_and_preserves_version_history(with_legacy_data: bool) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to the isolated Docker PostgreSQL test database")
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    schema = f"legacy_note_migration_{uuid4().hex}"
    try:
        with engine.begin() as connection:
            _create_private_schema(connection, schema)
            now = datetime(2026, 10, 1, tzinfo=timezone.utc)
            if with_legacy_data:
                run_id = uuid4()
                connection.execute(sa.text("INSERT INTO runs(id) VALUES (:id)"), {"id": run_id})
                artifact_id, v1, v2, v3 = uuid4(), uuid4(), uuid4(), uuid4()
                _insert_artifact(connection, artifact_id, "Historical note", "comparison_table", 7, v2, now)
                evidence = [{"document_id": str(uuid4()), "page_number": 4, "content_sha256": "e" * 64}]
                _insert_version(connection, version_id=v1, artifact_id=artifact_id, version_number=1, parent_id=None,
                    origin="user", status="superseded", body="First draft\nsecond line", digest="a" * 64,
                    source_run_id=None, evidence=[], structured={"legacy": "first"}, now=now)
                _insert_version(connection, version_id=v2, artifact_id=artifact_id, version_number=2, parent_id=v1,
                    origin="user", status="current", body="Current body\nwith lines", digest="b" * 64,
                    source_run_id=None, evidence=evidence, structured={"columns": ["A", "B"]}, now=now)
                _insert_version(connection, version_id=v3, artifact_id=artifact_id, version_number=3, parent_id=v2,
                    origin="generated", status="candidate", body="AI candidate body", digest="c" * 64,
                    source_run_id=run_id, evidence=evidence, structured=None, now=now)
                # An artifact without any version must still become a readable note.
                empty_artifact = uuid4()
                _insert_artifact(connection, empty_artifact, "Empty historical draft", "draft", 1, None, now)

            _run_migration(connection, migration.upgrade)

            counts = connection.execute(sa.text("SELECT (SELECT count(*) FROM artifacts), (SELECT count(*) FROM artifact_versions), (SELECT count(*) FROM notes), (SELECT count(*) FROM note_versions)")).one()
            if not with_legacy_data:
                assert tuple(counts) == (0, 0, 0, 0)
            else:
                assert tuple(counts) == (2, 3, 2, 4)
                imported = connection.execute(sa.text("""
                    SELECT n.id, n.legacy_artifact_id, n.legacy_artifact_type, n.revision,
                           n.current_version_id, nv.version_number, nv.parent_version_id, nv.origin,
                           nv.status, nv.plain_text, nv.content, nv.source_run_id,
                           nv.legacy_artifact_version_id, nv.legacy_content_sha256,
                           nv.legacy_evidence_snapshot, nv.legacy_structured_content, nv.base_revision
                    FROM notes n JOIN note_versions nv ON nv.note_id = n.id
                    WHERE n.legacy_artifact_type = 'comparison_table'
                    ORDER BY nv.version_number
                """)).mappings().all()
                assert [row["version_number"] for row in imported] == [1, 2, 3]
                assert imported[0]["id"] == imported[0]["legacy_artifact_id"]
                assert imported[0]["legacy_artifact_type"] == "comparison_table"
                assert imported[0]["revision"] == 7
                assert imported[0]["current_version_id"] == v2
                assert [row["status"] for row in imported] == ["superseded", "current", "candidate"]
                assert imported[1]["parent_version_id"] == v1 and imported[2]["parent_version_id"] == v2
                assert imported[1]["plain_text"] == "Current body\nwith lines"
                assert imported[1]["legacy_content_sha256"] == "b" * 64
                assert imported[1]["legacy_evidence_snapshot"] == evidence
                assert imported[1]["legacy_structured_content"] == {"columns": ["A", "B"]}
                assert imported[2]["source_run_id"] == run_id
                # A pre-migration candidate has no safe base revision and cannot CAS-adopt.
                assert imported[2]["base_revision"] is None
                with Session(bind=connection) as session:
                    with pytest.raises(NoteServiceError) as error:
                        adopt_candidate(session, artifact_id, expected_revision=7, version_id=v3)
                    assert error.value.code == "candidate_requires_preview"
                # The converted editor JSON retains all old text and line breaks.
                doc = imported[1]["content"]
                converted_text = "".join(node.get("text", "") for node in doc["content"][0]["content"])
                assert converted_text == "Current body\nwith lines"
                empty_note = connection.execute(sa.text("""
                    SELECT n.current_version_id, v.plain_text, v.status
                    FROM notes n JOIN note_versions v ON v.id = n.current_version_id
                    WHERE n.title = 'Empty historical draft'
                """)).one()
                assert empty_note.current_version_id is not None and empty_note.plain_text == "" and empty_note.status == "current"

            # Replaying an upgrade must not duplicate either mapped source record.
            _run_migration(connection, migration.upgrade)
            after = connection.execute(sa.text("SELECT (SELECT count(*) FROM artifacts), (SELECT count(*) FROM artifact_versions), (SELECT count(*) FROM notes), (SELECT count(*) FROM note_versions)")).one()
            assert tuple(after) == tuple(counts)

            # Downgrade is deliberately additive: the original history and projected notes survive.
            _run_migration(connection, migration.downgrade)
            remaining = connection.execute(sa.text("SELECT (SELECT count(*) FROM artifacts), (SELECT count(*) FROM artifact_versions), (SELECT count(*) FROM notes), (SELECT count(*) FROM note_versions)")).one()
            assert tuple(remaining) == tuple(counts)
            assert {"artifacts", "artifact_versions", "notes", "note_versions"}.issubset(set(sa.inspect(connection).get_table_names()))
            _run_migration(connection, migration.upgrade)
            reupgraded = connection.execute(sa.text("SELECT (SELECT count(*) FROM artifacts), (SELECT count(*) FROM artifact_versions), (SELECT count(*) FROM notes), (SELECT count(*) FROM note_versions)")).one()
            assert tuple(reupgraded) == tuple(counts)
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
