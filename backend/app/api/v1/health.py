from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import get_engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict[str, str]:
    """Report that the API process is serving requests."""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict[str, str]:
    """Report readiness only when PostgreSQL accepts a simple query."""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        # Do not expose connection details, credentials, or driver error text.
        raise HTTPException(status_code=503, detail="database unavailable") from None
    return {"status": "ok", "database": "ok"}
