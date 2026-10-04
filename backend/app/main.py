from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.health import router as health_router
from app.api.v1.documents import router as documents_router
from app.api.v1.indexes import router as indexes_router
from app.api.v1.tasks import router as tasks_router
from app.api.v1.topics import router as topics_router
from app.api.v1.runs import router as runs_router
from app.api.v1.conversations import router as conversations_router
from app.memory.api import router as memories_router
from app.artifacts.api import router as artifacts_router
from app.documents.reader_api import router as reader_router
from app.notes.api import router as notes_router
from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")

LOCAL_BROWSER_ORIGINS = frozenset({
    "http://127.0.0.1:5174",
    "http://localhost:5174",
    "http://127.0.0.1:8002",
    "http://localhost:8002",
})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def validate_local_write_origin(request: Request, call_next):
    if request.method in WRITE_METHODS:
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        if (origin is not None and origin not in LOCAL_BROWSER_ORIGINS) or (
            origin is None and fetch_site == "cross-site"
        ):
            return JSONResponse(status_code=403, content={"detail": "请求来源不被允许"})
    return await call_next(request)


app.include_router(health_router, prefix="/api/v1")
app.include_router(documents_router, prefix="/api/v1")
app.include_router(indexes_router, prefix="/api/v1")
app.include_router(tasks_router, prefix="/api/v1")
app.include_router(topics_router, prefix="/api/v1")
app.include_router(runs_router, prefix="/api/v1")
app.include_router(conversations_router, prefix="/api/v1")
app.include_router(memories_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")
app.include_router(reader_router, prefix="/api/v1")
app.include_router(notes_router, prefix="/api/v1")
