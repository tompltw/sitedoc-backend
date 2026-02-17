"""
Secure credential handler.

Called AFTER Haiku extraction detects a credential (password=[DETECTED]).
This is a separate, hardened code path:
  - Never logged
  - Raw credential value is read from message, encrypted immediately
  - Stored in site_credentials (Fernet-encrypted)
  - conversation_memory row updated with vault_ref (never the raw value)

Haiku's job: detect + classify (outputs [DETECTED] for sensitive fields)
This handler's job: extract actual value + encrypt + store
"""
import base64
import json
import logging
import os
import re
from typing import Optional
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Patterns to extract credentials from raw message text
# Ordered from most specific to least specific
CREDENTIAL_PATTERNS = {
    "password": [
        r"password[:\s]+['\"]?([^\s'\"]+)['\"]?",
        r"pass[:\s]+['\"]?([^\s'\"]+)['\"]?",
        r"pwd[:\s]+['\"]?([^\s'\"]+)['\"]?",
    ],
    "token": [
        r"token[:\s]+['\"]?([^\s'\"]+)['\"]?",
        r"api[_\s]?key[:\s]+['\"]?([^\s'\"]+)['\"]?",
        r"access[_\s]?key[:\s]+['\"]?([^\s'\"]+)['\"]?",
        r"secret[:\s]+['\"]?([^\s'\"]+)['\"]?",
    ],
    "ssh_key": [
        r"(-----BEGIN[^-]+PRIVATE KEY-----[\s\S]+?-----END[^-]+PRIVATE KEY-----)",
    ],
}


def _get_fernet() -> Fernet:
    # NOTE: Fernet symmetric encryption is fine for MVP.
    # Production: swap to HashiCorp Vault Transit Engine for envelope encryption
    # + automatic key rotation without re-encrypting all stored credentials.
    # See: https://developer.hashicorp.com/vault/docs/secrets/transit
    key_raw = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "placeholder-change-in-prod")
    key = base64.urlsafe_b64encode(key_raw.encode().ljust(32)[:32])
    return Fernet(key)


def _encrypt(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def _extract_sensitive_from_message(raw_message: str, field: str) -> Optional[str]:
    """
    Extract a sensitive value from raw message text using regex.
    Returns None if not found.
    NOTE: This function must NOT log the extracted value.
    """
    patterns = CREDENTIAL_PATTERNS.get(field, [])
    for pattern in patterns:
        match = re.search(pattern, raw_message, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


async def handle_detected_credential(
    db: AsyncSession,
    raw_message: str,
    memory_row_id: UUID,
    site_id: UUID,
    customer_id: UUID,
    haiku_payload: dict,
) -> dict:
    """
    Process a credential that Haiku detected ([DETECTED] fields).

    1. Extract actual sensitive value from raw message (regex, never logged)
    2. Encrypt with Fernet
    3. Store in site_credentials
    4. Update conversation_memory row with vault_ref
    5. Return vault_ref (no raw value ever returned)
    """
    cred_type = haiku_payload.get("type", "other")
    host = haiku_payload.get("host", "")
    username = haiku_payload.get("username", "")

    stored_fields = {}
    vault_ref = f"customer/{customer_id}/site/{site_id}/cred/{cred_type}"

    # Determine which fields to look for based on credential type
    fields_to_extract = ["password"]
    if cred_type in ("api_key", "other"):
        fields_to_extract = ["token", "password"]
    elif cred_type == "ssh":
        fields_to_extract = ["password", "ssh_key"]

    for field in fields_to_extract:
        raw_value = _extract_sensitive_from_message(raw_message, field)
        if raw_value and raw_value != "[DETECTED]":
            encrypted = _encrypt(raw_value)

            # Map credential type to DB enum
            db_cred_type = _map_cred_type(cred_type)

            # Build the credential value payload (host + username + encrypted secret)
            cred_payload = json.dumps({
                "type": cred_type,
                "host": host,
                "username": username,
                "field": field,
                "vault_ref": vault_ref,
            })

            # Store in site_credentials (encrypted_value = encrypted secret)
            result = await db.execute(
                text("""
                    INSERT INTO site_credentials
                        (site_id, credential_type, encrypted_value)
                    VALUES
                        (:site_id, :credential_type, :encrypted_value)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                """),
                {
                    "site_id": str(site_id),
                    "credential_type": db_cred_type,
                    "encrypted_value": encrypted,
                }
            )
            cred_id = result.scalar_one_or_none()
            stored_fields[field] = bool(cred_id)
            # Raw value goes out of scope here — never returned or logged

    # Update conversation_memory with vault_ref (no raw value)
    if stored_fields:
        updated_payload = {**haiku_payload}
        updated_payload.pop("password", None)
        updated_payload.pop("token", None)
        updated_payload.pop("key", None)
        updated_payload["vault_ref"] = vault_ref
        updated_payload["secured"] = True

        await db.execute(
            text("""
                UPDATE conversation_memory
                SET payload = :payload::jsonb,
                    updated_at = now()
                WHERE id = :id
            """),
            {
                "payload": json.dumps(updated_payload),
                "id": str(memory_row_id),
            }
        )
        await db.commit()
        logger.info(
            "Credential secured for site %s type=%s fields=%s",
            site_id, cred_type, list(stored_fields.keys())
        )
    else:
        logger.warning(
            "Credential detected by Haiku but could not extract value from message "
            "(site=%s type=%s) — may need manual entry",
            site_id, cred_type
        )

    return {
        "vault_ref": vault_ref,
        "secured": bool(stored_fields),
        "fields_stored": list(stored_fields.keys()),
    }


def _map_cred_type(cred_type: str) -> str:
    """Map Haiku's credential type to DB enum."""
    mapping = {
        "wordpress": "wp_admin",
        "wp_admin": "wp_admin",
        "ssh": "ssh",
        "ftp": "ftp",
        "api_key": "api_key",
        "other": "api_key",
    }
    return mapping.get(cred_type, "api_key")
