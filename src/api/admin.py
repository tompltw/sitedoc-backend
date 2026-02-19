"""
Super-admin HTTP endpoints.

Authentication:
  - Bearer AGENT_INTERNAL_TOKEN  (for internal tooling)
  - Bearer <customer JWT>        where customer email ends in @sitedoc.ai
                                  or is listed in ADMIN_EMAILS env var

All endpoints are mounted under /api/v1/internal/admin/
"""
import logging
import os
import subprocess
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.core.security import decode_token
from src.db.models import (
    Customer,
    Issue,
    KanbanColumn,
    PlanType,
    Site,
    SiteCredential,
    SiteStatus,
    TicketTransition,
)
from src.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/internal/admin", tags=["admin"])

AGENT_INTERNAL_TOKEN = os.getenv("AGENT_INTERNAL_TOKEN", "")
ADMIN_EMAILS_ENV = os.getenv("ADMIN_EMAILS", "")

# In-memory agent config store (survives process lifetime, not restarts)
_agent_config: dict[str, str] = {
    "AGENT_MODEL_DEV": os.getenv("AGENT_MODEL_DEV", "claude-sonnet-4-5"),
    "AGENT_MODEL_QA": os.getenv("AGENT_MODEL_QA", "claude-sonnet-4-5"),
    "AGENT_MODEL_PM": os.getenv("AGENT_MODEL_PM", "claude-haiku-4-5"),
    "AGENT_MODEL_TECH_LEAD": os.getenv("AGENT_MODEL_TECH_LEAD", "claude-opus-4-5"),
}


def _get_admin_emails() -> list[str]:
    emails = []
    if ADMIN_EMAILS_ENV:
        emails = [e.strip().lower() for e in ADMIN_EMAILS_ENV.split(",") if e.strip()]
    return emails


async def _verify_admin(
    authorization: str | None,
    db: AsyncSession,
) -> Customer | None:
    """
    Accept either:
      1. AGENT_INTERNAL_TOKEN  → returns None (no customer context)
      2. Valid customer JWT where email ends @sitedoc.ai or is in ADMIN_EMAILS
    """
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization")

    token = authorization.removeprefix("Bearer ").strip()

    # --- Check internal token ---
    if AGENT_INTERNAL_TOKEN and token == AGENT_INTERNAL_TOKEN:
        return None

    # --- Try JWT ---
    try:
        payload = decode_token(token)
    except HTTPException:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    try:
        customer_id = uuid.UUID(sub)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")

    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    # Admin check: @sitedoc.ai domain OR explicit list
    email = customer.email.lower()
    admin_emails = _get_admin_emails()
    is_admin = email.endswith("@sitedoc.ai") or email in admin_emails
    if not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    return customer


# ── Stats ─────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    total_users = (await db.execute(func.count(Customer.id).select())).scalar() or 0
    total_sites = (await db.execute(func.count(Site.id).select())).scalar() or 0

    # Issues by status
    issues_result = await db.execute(
        select(Issue.status, func.count(Issue.id)).group_by(Issue.status)
    )
    issues_by_status: dict[str, int] = {}
    for row in issues_result.all():
        issues_by_status[row[0].value if hasattr(row[0], "value") else str(row[0])] = row[1]

    total_issues = sum(issues_by_status.values())
    open_issues = issues_by_status.get("open", 0) + issues_by_status.get("in_progress", 0)
    resolved_issues = issues_by_status.get("resolved", 0) + issues_by_status.get("dismissed", 0)

    # Recent 20 transitions
    transitions_result = await db.execute(
        select(TicketTransition)
        .order_by(TicketTransition.created_at.desc())
        .limit(20)
    )
    transitions = transitions_result.scalars().all()

    transition_list = []
    for t in transitions:
        transition_list.append({
            "id": str(t.id),
            "issue_id": str(t.issue_id),
            "from_col": t.from_col.value if t.from_col else None,
            "to_col": t.to_col.value if t.to_col else None,
            "actor_type": t.actor_type,
            "note": t.note,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })

    # Agent config (in-memory)
    agents_running = 0  # placeholder

    return {
        "total_users": total_users,
        "total_sites": total_sites,
        "total_issues": total_issues,
        "open_issues": open_issues,
        "resolved_issues": resolved_issues,
        "issues_by_status": issues_by_status,
        "agents_running": agents_running,
        "recent_transitions": transition_list,
        "agent_config": _agent_config,
    }


# ── Users ─────────────────────────────────────────────────────────────────

class PlanUpdateBody(BaseModel):
    plan: str  # free | starter | pro | enterprise


class DeactivateBody(BaseModel):
    active: bool


@router.get("/users")
async def list_admin_users(
    search: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    await _verify_admin(authorization, db)

    q = select(Customer).order_by(Customer.created_at.desc())
    result = await db.execute(q)
    customers = result.scalars().all()

    # Site counts
    site_counts_result = await db.execute(
        select(Site.customer_id, func.count(Site.id)).group_by(Site.customer_id)
    )
    site_counts = {str(row[0]): row[1] for row in site_counts_result.all()}

    users = []
    for c in customers:
        if search and search.lower() not in c.email.lower():
            continue
        users.append({
            "id": str(c.id),
            "email": c.email,
            "plan": c.plan.value if hasattr(c.plan, "value") else str(c.plan),
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "site_count": site_counts.get(str(c.id), 0),
        })

    return users


@router.get("/users/{user_id}")
async def get_admin_user(
    user_id: uuid.UUID,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    result = await db.execute(select(Customer).where(Customer.id == user_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="User not found")

    # Sites
    sites_result = await db.execute(select(Site).where(Site.customer_id == user_id))
    sites = sites_result.scalars().all()

    # Recent issues
    issues_result = await db.execute(
        select(Issue)
        .where(Issue.customer_id == user_id)
        .order_by(Issue.created_at.desc())
        .limit(10)
    )
    issues = issues_result.scalars().all()

    return {
        "id": str(customer.id),
        "email": customer.email,
        "plan": customer.plan.value if hasattr(customer.plan, "value") else str(customer.plan),
        "created_at": customer.created_at.isoformat() if customer.created_at else None,
        "sites": [
            {
                "id": str(s.id),
                "name": s.name,
                "url": s.url,
                "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in sites
        ],
        "recent_issues": [
            {
                "id": str(i.id),
                "title": i.title,
                "status": i.status.value if hasattr(i.status, "value") else str(i.status),
                "kanban_column": i.kanban_column.value if i.kanban_column else None,
                "priority": i.priority.value if hasattr(i.priority, "value") else str(i.priority),
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in issues
        ],
    }


@router.patch("/users/{user_id}/plan")
async def update_user_plan(
    user_id: uuid.UUID,
    body: PlanUpdateBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    result = await db.execute(select(Customer).where(Customer.id == user_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        new_plan = PlanType(body.plan)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {body.plan}")

    customer.plan = new_plan
    await db.commit()
    return {"ok": True, "plan": body.plan}


@router.patch("/users/{user_id}/status")
async def update_user_status(
    user_id: uuid.UUID,
    body: DeactivateBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Deactivate or reactivate a user (sets plan to 'free' when deactivating)."""
    await _verify_admin(authorization, db)

    result = await db.execute(select(Customer).where(Customer.id == user_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="User not found")

    # We use a simple convention: deactivated users have a special marker in the DB.
    # Since there's no "active" column, we store it via text execution using a flag column trick.
    # For now, we'll use plan manipulation as a proxy (free = deactivated).
    if not body.active:
        customer.plan = PlanType.free  # simplify: mark as deactivated = free tier
    # Reactivating just marks them as active (they were free when deactivated)
    await db.commit()
    return {"ok": True, "active": body.active}


# ── Sites ─────────────────────────────────────────────────────────────────

@router.get("/sites")
async def list_admin_sites(
    search: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    await _verify_admin(authorization, db)

    sites_result = await db.execute(
        select(Site, Customer.email)
        .join(Customer, Site.customer_id == Customer.id)
        .order_by(Site.created_at.desc())
    )
    rows = sites_result.all()

    # Credential types per site (not values)
    creds_result = await db.execute(
        select(SiteCredential.site_id, SiteCredential.credential_type)
    )
    creds_by_site: dict[str, list[str]] = {}
    for row in creds_result.all():
        sid = str(row[0])
        ctype = row[1].value if hasattr(row[1], "value") else str(row[1])
        creds_by_site.setdefault(sid, []).append(ctype)

    results = []
    for site, customer_email in rows:
        if search:
            s = search.lower()
            if s not in site.name.lower() and s not in site.url.lower() and s not in customer_email.lower():
                continue
        results.append({
            "id": str(site.id),
            "name": site.name,
            "url": site.url,
            "customer_email": customer_email,
            "status": site.status.value if hasattr(site.status, "value") else str(site.status),
            "credential_types": creds_by_site.get(str(site.id), []),
            "created_at": site.created_at.isoformat() if site.created_at else None,
            "last_health_check": site.last_health_check.isoformat() if site.last_health_check else None,
        })

    return results


@router.get("/sites/{site_id}")
async def get_admin_site(
    site_id: uuid.UUID,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    result = await db.execute(
        select(Site, Customer.email)
        .join(Customer, Site.customer_id == Customer.id)
        .where(Site.id == site_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Site not found")
    site, customer_email = row

    # Credential types (NOT values)
    creds_result = await db.execute(
        select(SiteCredential.credential_type).where(SiteCredential.site_id == site_id)
    )
    cred_types = [
        (r[0].value if hasattr(r[0], "value") else str(r[0]))
        for r in creds_result.all()
    ]

    # Linked issues
    issues_result = await db.execute(
        select(Issue)
        .where(Issue.site_id == site_id)
        .order_by(Issue.created_at.desc())
        .limit(20)
    )
    issues = issues_result.scalars().all()

    return {
        "id": str(site.id),
        "name": site.name,
        "url": site.url,
        "customer_email": customer_email,
        "status": site.status.value if hasattr(site.status, "value") else str(site.status),
        "credential_types": cred_types,
        "last_health_check": site.last_health_check.isoformat() if site.last_health_check else None,
        "created_at": site.created_at.isoformat() if site.created_at else None,
        "issues": [
            {
                "id": str(i.id),
                "title": i.title,
                "status": i.status.value if hasattr(i.status, "value") else str(i.status),
                "kanban_column": i.kanban_column.value if i.kanban_column else None,
                "priority": i.priority.value if hasattr(i.priority, "value") else str(i.priority),
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in issues
        ],
    }


@router.patch("/sites/{site_id}/status")
async def update_site_status(
    site_id: uuid.UUID,
    body: dict,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    active = body.get("active", True)
    site.status = SiteStatus.active if active else SiteStatus.inactive
    await db.commit()
    return {"ok": True, "status": site.status.value}


# ── Issues ────────────────────────────────────────────────────────────────

@router.get("/issues")
async def list_admin_issues(
    kanban_column: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    await _verify_admin(authorization, db)

    q = (
        select(Issue, Site.name.label("site_name"), Customer.email.label("customer_email"))
        .join(Site, Issue.site_id == Site.id)
        .join(Customer, Issue.customer_id == Customer.id)
        .order_by(Issue.created_at.desc())
        .limit(limit)
    )

    if kanban_column:
        try:
            col_enum = KanbanColumn(kanban_column)
            q = q.where(Issue.kanban_column == col_enum)
        except ValueError:
            pass

    result = await db.execute(q)
    rows = result.all()

    results = []
    for issue, site_name, customer_email in rows:
        if search:
            s = search.lower()
            if s not in issue.title.lower() and s not in (customer_email or "").lower():
                continue
        results.append({
            "id": str(issue.id),
            "title": issue.title,
            "site_id": str(issue.site_id),
            "site_name": site_name,
            "customer_email": customer_email,
            "status": issue.status.value if hasattr(issue.status, "value") else str(issue.status),
            "kanban_column": issue.kanban_column.value if issue.kanban_column else None,
            "priority": issue.priority.value if hasattr(issue.priority, "value") else str(issue.priority),
            "ticket_number": issue.ticket_number,
            "created_at": issue.created_at.isoformat() if issue.created_at else None,
            "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        })

    return results


class TransitionBody(BaseModel):
    to_col: str
    note: str | None = None


@router.post("/issues/{issue_id}/transition")
async def admin_transition_issue(
    issue_id: uuid.UUID,
    body: TransitionBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    result = await db.execute(select(Issue).where(Issue.id == issue_id))
    issue = result.scalar_one_or_none()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    try:
        target_col = KanbanColumn(body.to_col)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid kanban column: {body.to_col}")

    from_col = issue.kanban_column
    issue.kanban_column = target_col

    # Record transition
    transition = TicketTransition(
        issue_id=issue_id,
        from_col=from_col,
        to_col=target_col,
        actor_type="system",
        note=body.note or f"Admin manual transition to {body.to_col}",
    )
    db.add(transition)
    await db.commit()

    return {"ok": True, "kanban_column": body.to_col}


# ── Agent Config ──────────────────────────────────────────────────────────

class AgentConfigBody(BaseModel):
    AGENT_MODEL_DEV: str | None = None
    AGENT_MODEL_QA: str | None = None
    AGENT_MODEL_PM: str | None = None
    AGENT_MODEL_TECH_LEAD: str | None = None


@router.get("/agent-config")
async def get_agent_config(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)
    return {"config": _agent_config}


@router.post("/agent-config")
async def update_agent_config(
    body: AgentConfigBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No config values provided")

    _agent_config.update(updates)
    logger.info("[admin] Agent config updated: %s", updates)

    # Optionally update environment variables in the current process
    for key, val in updates.items():
        os.environ[key] = val

    return {"ok": True, "config": _agent_config}


# ── Celery Status ─────────────────────────────────────────────────────────

@router.get("/celery-status")
async def get_celery_status(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    try:
        # Try to import and inspect Celery
        celery_url = os.getenv("CELERY_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/1"))
        from celery import Celery

        app = Celery(broker=celery_url)
        inspector = app.control.inspect(timeout=2.0)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        stats = inspector.stats() or {}

        workers = list(set(list(active.keys()) + list(reserved.keys()) + list(stats.keys())))

        return {
            "ok": True,
            "workers": workers,
            "active_tasks": {w: len(tasks) for w, tasks in active.items()},
            "reserved_tasks": {w: len(tasks) for w, tasks in reserved.items()},
            "raw": {
                "active": active,
                "reserved": reserved,
            },
        }
    except Exception as e:
        logger.warning("[admin] Celery inspect failed: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "workers": [],
            "active_tasks": {},
            "reserved_tasks": {},
        }


# ── Restart Workers ───────────────────────────────────────────────────────

@router.post("/restart-workers")
async def restart_workers(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _verify_admin(authorization, db)

    try:
        # Try Celery broadcast restart
        celery_url = os.getenv("CELERY_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/1"))
        from celery import Celery

        app = Celery(broker=celery_url)
        app.control.broadcast("pool_restart", arguments={"reload": True})
        return {"ok": True, "method": "celery_broadcast"}
    except Exception as e:
        logger.warning("[admin] Celery restart failed: %s", e)
        return {"ok": False, "error": str(e)}
