"""
Sites and credentials routes.
"""
import base64
import uuid

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, status
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
