"""
Sites and credentials routes.
"""
import base64
import uuid

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.api.schemas import (
    CredentialCreate,
    CredentialResponse,
    SiteCreate,
    SiteResponse,
)
from src.core.config import settings
from src.db.models import Customer, Site, SiteCredential
from src.db.session import get_db

router = APIRouter()


def _get_fernet() -> Fernet:
    """Build a Fernet instance from CREDENTIAL_ENCRYPTION_KEY."""
    raw = settings.CREDENTIAL_ENCRYPTION_KEY.encode()
    # Pad/truncate to exactly 32 bytes then base64url-encode to get a valid Fernet key
    key = base64.urlsafe_b64encode(raw.ljust(32)[:32])
    return Fernet(key)


def _get_site_or_404(site, site_id: uuid.UUID, customer_id: uuid.UUID) -> Site:
    if site is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")
    if site.customer_id != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")
    return site


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[SiteResponse])
async def list_sites(
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Site).where(Site.customer_id == current_customer.id).order_by(Site.created_at.desc())
    )
    return result.scalars().all()


@router.post("/", response_model=SiteResponse, status_code=status.HTTP_201_CREATED)
async def create_site(
    body: SiteCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    site = Site(
        customer_id=current_customer.id,
        url=body.url,
        name=body.name,
    )
    db.add(site)
    await db.flush()
    await db.refresh(site)
    return site


@router.get("/{site_id}", response_model=SiteResponse)
async def get_site(
    site_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    return _get_site_or_404(site, site_id, current_customer.id)


@router.post("/{site_id}/health-check", response_model=SiteResponse)
async def trigger_health_check(
    site_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a health check for a site (updates last_health_check timestamp)."""
    from datetime import datetime, timezone
    from sqlalchemy import text

    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    _get_site_or_404(site, site_id, current_customer.id)

    await db.execute(
        text("UPDATE sites SET last_health_check = :now WHERE id = :id"),
        {"now": datetime.now(timezone.utc), "id": str(site_id)},
    )
    await db.commit()
    await db.refresh(site)
    return site


@router.delete("/disconnect")
async def plugin_disconnect(
    x_site_token: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Clear the plugin token when a site disconnects."""
    from sqlalchemy import text
    if x_site_token:
        await db.execute(
            text("UPDATE sites SET plugin_token = NULL WHERE plugin_token = :t"),
            {"t": x_site_token},
        )
        await db.commit()
    return {"ok": True}


@router.delete("/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site(
    site_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    _get_site_or_404(site, site_id, current_customer.id)
    await db.delete(site)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@router.post("/{site_id}/credentials", response_model=CredentialResponse, status_code=status.HTTP_201_CREATED)
async def add_credential(
    site_id: uuid.UUID,
    body: CredentialCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    # Verify site ownership
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    _get_site_or_404(site, site_id, current_customer.id)

    fernet = _get_fernet()
    encrypted = fernet.encrypt(body.value.encode()).decode()

    credential = SiteCredential(
        site_id=site_id,
        credential_type=body.credential_type,
        encrypted_value=encrypted,
    )
    db.add(credential)
    await db.flush()
    await db.refresh(credential)
    return credential


@router.get("/{site_id}/credentials", response_model=list[CredentialResponse])
async def list_credentials(
    site_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    # Verify site ownership
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    _get_site_or_404(site, site_id, current_customer.id)

    cred_result = await db.execute(
        select(SiteCredential)
        .where(SiteCredential.site_id == site_id)
        .order_by(SiteCredential.created_at.desc())
    )
    return cred_result.scalars().all()


# ---------------------------------------------------------------------------
# WordPress plugin endpoints — authenticated by site token (not JWT)
# ---------------------------------------------------------------------------

import secrets
from fastapi import Header

async def _get_site_by_token(
    x_site_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> Site:
    from sqlalchemy import text
    result = await db.execute(
        text("SELECT * FROM sites WHERE plugin_token = :t"),
        {"t": x_site_token},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid site token")
    return row


@router.post("/connect", status_code=status.HTTP_201_CREATED)
async def plugin_connect(
    body: dict,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the WordPress plugin during connection.
    Creates (or updates) the site record and returns a long-lived site token.
    """
    from sqlalchemy import text

    url = body.get("url", "").rstrip("/")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    # Find existing site by URL for this customer, or create one
    result = await db.execute(
        select(Site).where(
            Site.customer_id == current_customer.id,
            Site.url == url,
        )
    )
    site = result.scalar_one_or_none()

    plugin_token = secrets.token_urlsafe(48)

    if site:
        await db.execute(
            text("UPDATE sites SET plugin_token = :t, plugin_version = :v, name = :n WHERE id = :id"),
            {
                "t": plugin_token,
                "v": body.get("plugin_version", ""),
                "n": body.get("name") or site.name,
                "id": str(site.id),
            },
        )
    else:
        site = Site(
            customer_id=current_customer.id,
            url=url,
            name=body.get("name") or url,
        )
        db.add(site)
        await db.flush()
        await db.execute(
            text("UPDATE sites SET plugin_token = :t, plugin_version = :v WHERE id = :id"),
            {"t": plugin_token, "v": body.get("plugin_version", ""), "id": str(site.id)},
        )

    await db.commit()

    return {
        "site_id":    str(site.id),
        "site_token": plugin_token,
        "message":    "Site connected successfully",
    }


@router.post("/health")
async def plugin_health_push(
    body: dict,
    x_site_token: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive a health snapshot from the WordPress plugin."""
    from sqlalchemy import text

    if not x_site_token:
        raise HTTPException(status_code=401, detail="X-Site-Token required")

    result = await db.execute(
        text("SELECT id, customer_id FROM sites WHERE plugin_token = :t"),
        {"t": x_site_token},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid site token")

    site_id, customer_id = row

    # Update last_health_check + store php_errors as issues if any
    await db.execute(
        text("UPDATE sites SET last_health_check = now() WHERE id = :id"),
        {"id": str(site_id)},
    )

    # If PHP errors in the health data, create an issue automatically
    php_errors = body.get("php_errors", [])
    if php_errors:
        error_summary = "\n".join(php_errors[-5:])
        await db.execute(
            text("""
                INSERT INTO issues (site_id, customer_id, title, description, priority, status)
                VALUES (:site_id, :customer_id, :title, :desc, 'high', 'open')
                ON CONFLICT DO NOTHING
            """),
            {
                "site_id":     str(site_id),
                "customer_id": str(customer_id),
                "title":       f"PHP errors detected ({len(php_errors)} entries)",
                "desc":        error_summary,
            },
        )

    await db.commit()
    return {"ok": True}


@router.post("/errors")
async def plugin_report_error(
    body: dict,
    x_site_token: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive a PHP error report from the WordPress plugin (fire-and-forget)."""
    from sqlalchemy import text

    if not x_site_token:
        return {"ok": True}  # Always 200 — non-blocking

    result = await db.execute(
        text("SELECT id, customer_id FROM sites WHERE plugin_token = :t"),
        {"t": x_site_token},
    )
    row = result.fetchone()
    if not row:
        return {"ok": True}

    site_id, customer_id = row
    error_type = body.get("type", "UNKNOWN")
    message    = body.get("message", "")[:500]

    # Only auto-create issues for fatal errors
    if error_type in ("E_ERROR", "E_PARSE") and message:
        await db.execute(
            text("""
                INSERT INTO issues (site_id, customer_id, title, description, priority, status)
                VALUES (:site_id, :customer_id, :title, :desc, 'high', 'open')
            """),
            {
                "site_id":     str(site_id),
                "customer_id": str(customer_id),
                "title":       f"{error_type}: {message[:100]}",
                "desc":        f"{message}\nFile: {body.get('file', '')}, Line: {body.get('line', '')}",
            },
        )
        await db.commit()

    return {"ok": True}
