# sitedoc-backend

FastAPI backend for the SiteDoc platform — AI-powered website maintenance.

## Stack
- **FastAPI** + Uvicorn (async)
- **PostgreSQL 15** with Row-Level Security for multi-tenancy
- **Redis 7** for task queue / caching
- **Celery** for background workers
- **Alembic** for migrations
- **SQLAlchemy** (async) ORM

## Quick Start

```bash
cp .env.example .env
docker-compose up -d
# Run migrations
docker-compose exec api alembic upgrade head
```

API docs: http://localhost:8000/docs  
Health: http://localhost:8000/health

## Architecture Notes
- Multi-tenant: each request sets `app.current_customer_id` PostgreSQL session variable; RLS enforces data isolation automatically.
- Agents have NO direct DB access — all agent↔backend communication via Redis queue.
- Credentials stored encrypted (Fernet) in `site_credentials`.
- Conversation rolling summary: every 20 messages, agent writes a condensed summary to `conversations.summary`.
- **Always backup before any fix** — agent creates a `backups` record + S3 snapshot before touching a site.
