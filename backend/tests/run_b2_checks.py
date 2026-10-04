"""Run only against the disposable B2 database; never the user's database."""
import os
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uuid import uuid4
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url

target = make_url(os.environ["DATABASE_URL"]).set(database="workbench_b2_test")
assert target.database == "workbench_b2_test"
url = target.render_as_string(hide_password=False)
os.environ["DATABASE_URL"] = url
os.environ["WORKBENCH_TEST_DATABASE_URL"] = url
os.environ["SILICONFLOW_API_KEY"] = ""
os.environ["DATA_DIR"] = "/tmp/workbench-b2-tests"
subprocess.run(["alembic", "upgrade", "head"], check=True)
from app.documents.models import IndexGeneration
from sqlalchemy.orm import Session
with Session(create_engine(url)) as session:
    if session.scalar(select(IndexGeneration.id).where(IndexGeneration.status == "active")) is None:
        session.add(IndexGeneration(id=uuid4(), provider="siliconflow", model="Qwen/Qwen3-Embedding-4B", dimensions=2560, config_hash="test-profile", status="active"))
        session.commit()
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]]))
