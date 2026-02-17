from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI(
    title="SiteDoc API",
    description="AI-powered website maintenance platform",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "sitedoc-backend", "version": "0.1.0"}


@app.get("/")
async def root():
    return {"message": "SiteDoc API — see /docs for API reference"}


# Register routers
from src.api import auth, sites, issues, chat, ws, billing, pipeline, internal  # noqa: E402

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(sites.router, prefix="/api/v1/sites", tags=["sites"])
app.include_router(issues.router, prefix="/api/v1/issues", tags=["issues"])
app.include_router(chat.router, prefix="/api/v1", tags=["chat"])
app.include_router(pipeline.router, prefix="/api/v1", tags=["pipeline"])
app.include_router(billing.router, prefix="/api/v1/billing", tags=["billing"])
app.include_router(internal.router, tags=["internal"])
app.include_router(ws.router, tags=["websocket"])
