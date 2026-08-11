from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os
from pathlib import Path

from app.database import engine, Base, ensure_schema
from app.core.config import settings
from app.api.checklists import router as checklists_router
from app.services.checklists import PHOTO_ROOT, ensure_photo_root

# Create tables on startup (for demo; use Alembic in production)
ensure_schema()
ensure_photo_root()

app = FastAPI(
    title=settings.APP_NAME,
    description="Craftable-style hospitality back-office API",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(checklists_router)

# Serve stored checklist photos so iOS/web can display them by URL
photo_root = ensure_photo_root()
app.mount(
    "/media/checklists",
    StaticFiles(directory=str(photo_root / "checklists")),
    name="checklist_media",
)


@app.get("/")
def root():
    return {
        "message": "Craftable Replica API",
        "docs": "/docs",
        "checklists": "/api/checklists/templates",
        "status": "ready",
    }


@app.get("/health")
def health():
    return {"status": "ok"}
