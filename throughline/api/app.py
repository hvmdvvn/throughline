"""Minimal FastAPI application."""

from fastapi import FastAPI

from throughline.api.routes import admin
from throughline.config import settings

app = FastAPI(title=settings.app_name)
app.include_router(admin.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
