"""Verify background-worker imports register every FK target needed by note tools."""
import os
import subprocess
import sys
from pathlib import Path


def test_agent_worker_import_registers_legacy_artifact_tables() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    env = {**os.environ, "SILICONFLOW_API_KEY": ""}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.agent.tools; "
            "from app.db.base import Base; "
            "assert {'artifacts', 'artifact_versions'} <= set(Base.metadata.tables); "
            "Base.metadata.sorted_tables",
        ],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
