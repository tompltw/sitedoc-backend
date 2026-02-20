"""
Sites and credentials routes.
"""
import base64
import json
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
    SiteProvisionRequest,
    SiteProvisionResponse,
    SiteResponse,
)
from src.core.config import settings
from src.db.models import Customer, Issue, IssueType, Site, SiteAgent, SiteCredential
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

    # Auto-create default PM (Haiku) + Dev (Sonnet) agents for this site
    db.add(SiteAgent(site_id=site.id, agent_role="pm", model="claude-haiku-4-5"))
    db.add(SiteAgent(site_id=site.id, agent_role="dev", model="claude-sonnet-4-5"))
    await db.flush()

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
    # Accept dict or string; always encrypt a JSON string
    if isinstance(body.value, dict):
        raw_value = json.dumps(body.value)
    else:
        raw_value = body.value
    encrypted = fernet.encrypt(raw_value.encode()).decode()

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


@router.delete("/{site_id}/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    site_id: uuid.UUID,
    credential_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Delete a specific credential from a site."""
    # Verify site ownership
    site_result = await db.execute(select(Site).where(Site.id == site_id))
    site = site_result.scalar_one_or_none()
    _get_site_or_404(site, site_id, current_customer.id)

    cred_result = await db.execute(
        select(SiteCredential).where(
            SiteCredential.id == credential_id,
            SiteCredential.site_id == site_id,
        )
    )
    cred = cred_result.scalar_one_or_none()
    if cred is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    await db.delete(cred)


# ---------------------------------------------------------------------------
# Site provisioning (managed hosting)
# ---------------------------------------------------------------------------

import logging
import re
import secrets

logger = logging.getLogger(__name__)


def _validate_slug(slug: str) -> str:
    """Validate and normalize a site slug."""
    slug = slug.lower().strip()
    if not re.match(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$', slug):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Slug must be 3-63 characters, lowercase alphanumeric and hyphens only, cannot start/end with hyphen.",
        )
    return slug


@router.post("/provision", response_model=SiteProvisionResponse, status_code=status.HTTP_201_CREATED)
async def provision_site(
    body: SiteProvisionRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """
    Provision a new managed WordPress site on SiteDoc hosting.

    1. Creates a Site record with is_managed=True
    2. SSHs into the hosting server and runs provision-site.sh
    3. Stores SSH/WP credentials for agent access
    4. Optionally creates a site_build Issue if description is provided
    """
    slug = _validate_slug(body.slug)

    # Check slug uniqueness
    existing = await db.execute(select(Site).where(Site.slug == slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already taken.")

    site_url = f"https://{slug}.nkcreator.com"
    site_token = secrets.token_urlsafe(48)
    db_password = secrets.token_urlsafe(24)

    # Create Site record
    site = Site(
        customer_id=current_customer.id,
        url=site_url,
        name=body.name,
        is_managed=True,
        slug=slug,
        server_ip=settings.HOSTING_SERVER_IP,
        server_path=f"/var/www/sites/{slug}.nkcreator.com",
        plugin_token=site_token,
    )
    db.add(site)
    await db.flush()
    await db.refresh(site)

    # Auto-create default agents
    db.add(SiteAgent(site_id=site.id, agent_role="pm", model="claude-haiku-4-5"))
    db.add(SiteAgent(site_id=site.id, agent_role="dev", model="claude-sonnet-4-5"))

    # Store SSH credentials for agent access (the hosting server)
    fernet = _get_fernet()
    ssh_creds = json.dumps({
        "host": settings.HOSTING_SERVER_IP,
        "user": settings.HOSTING_SSH_USER,
        "key_path": settings.HOSTING_SSH_KEY_PATH,
        "site_path": f"/var/www/sites/{slug}.nkcreator.com",
    })
    db.add(SiteCredential(
        site_id=site.id,
        credential_type="ssh",
        encrypted_value=fernet.encrypt(ssh_creds.encode()).decode(),
    ))

    # Store WP admin credentials (will be set by provisioning script)
    wp_admin_creds = json.dumps({
        "url": f"{site_url}/wp-admin",
        "username": "sitedoc",
        "password": "(set during provisioning)",
    })
    db.add(SiteCredential(
        site_id=site.id,
        credential_type="wp_admin",
        encrypted_value=fernet.encrypt(wp_admin_creds.encode()).decode(),
    ))

    await db.flush()

    # Run provisioning via SSH (async in background via Celery)
    from src.tasks.base import celery_app
    celery_app.send_task(
        "src.tasks.provision.provision_site",
        args=[str(site.id), slug, body.name, site_token, db_password],
        queue="backend",
    )

    # Create site_build issue if description provided
    issue_id = None
    if body.description:
        build_title = f"Build site: {body.name}"
        build_desc = body.description
        if body.business_type:
            build_desc = f"Business type: {body.business_type}\n\n{build_desc}"

        issue = Issue(
            site_id=site.id,
            customer_id=current_customer.id,
            title=build_title,
            description=build_desc,
            issue_type=IssueType.site_build,
        )
        db.add(issue)
        await db.flush()
        await db.refresh(issue)
        issue_id = issue.id

    return SiteProvisionResponse(
        site_id=site.id,
        issue_id=issue_id,
        url=site_url,
        admin_url=f"{site_url}/wp-admin",
        slug=slug,
        status="provisioning",
    )


@router.post("/{site_id}/custom-domain")
async def set_custom_domain(
    site_id: uuid.UUID,
    body: dict,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Set a custom domain for a managed site. Customer must add CNAME first."""
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    _get_site_or_404(site, site_id, current_customer.id)

    if not site.is_managed:
        raise HTTPException(status_code=400, detail="Custom domains only for managed sites.")

    domain = body.get("domain", "").strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="domain is required")

    site.custom_domain = domain
    await db.flush()

    # Trigger Caddy config update via Celery
    from src.tasks.base import celery_app
    celery_app.send_task(
        "src.tasks.provision.add_custom_domain",
        args=[str(site.id), site.slug, domain],
        queue="backend",
    )

    return {"ok": True, "domain": domain, "message": "Custom domain configured. SSL will be provisioned automatically."}


# ---------------------------------------------------------------------------
# WordPress plugin endpoints — authenticated by site token (not JWT)
# ---------------------------------------------------------------------------

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
