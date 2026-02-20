"""
Provisioning Celery tasks — SSH into hosting server to create/teardown WordPress sites.
"""
import json
import logging
import os
import re
import uuid

from src.db.models import Site
from src.tasks.base import celery_app, get_db_session

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc")
HOSTING_SERVER_IP = os.getenv("HOSTING_SERVER_IP", "69.10.55.138")
HOSTING_SSH_USER = os.getenv("HOSTING_SSH_USER", "sitedoc")
HOSTING_SSH_KEY_PATH = os.getenv("HOSTING_SSH_KEY_PATH", "")
PROVISION_SCRIPT = os.getenv("HOSTING_PROVISION_SCRIPT", "/opt/sitedoc-infra/scripts/provision-site.sh")
TEARDOWN_SCRIPT = os.getenv("HOSTING_TEARDOWN_SCRIPT", "/opt/sitedoc-infra/scripts/teardown-site.sh")


def _ssh_exec(command: str, timeout: int = 120) -> str:
    """Execute a command on the hosting server via SSH using paramiko."""
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs = {
        "hostname": HOSTING_SERVER_IP,
        "username": HOSTING_SSH_USER,
        "timeout": 30,
    }
    if HOSTING_SSH_KEY_PATH:
        connect_kwargs["key_filename"] = HOSTING_SSH_KEY_PATH
    # Falls back to SSH agent or default keys if no key path specified

    try:
        client.connect(**connect_kwargs)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace")
        errors = stderr.read().decode("utf-8", errors="replace")

        if exit_code != 0:
            raise RuntimeError(f"SSH command failed (exit {exit_code}):\n{errors}\n{output}")

        return output
    finally:
        client.close()


def _extract_provision_json(output: str) -> dict:
    """Extract the JSON block from provision-site.sh output."""
    match = re.search(r'---JSON---\s*(\{.*?\})', output, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return {}


@celery_app.task(name="src.tasks.provision.provision_site")
def provision_site(site_id: str, slug: str, site_name: str, site_token: str, db_password: str) -> None:
    """
    SSH into the hosting server and run the provisioning script.
    Updates the Site record with provisioning results.
    """
    logger.info("[provision] Starting provisioning for site %s (slug=%s)", site_id, slug)

    try:
        # Run provision script
        cmd = f"sudo {PROVISION_SCRIPT} {slug} '{site_name}' {site_token} {db_password}"
        output = _ssh_exec(cmd, timeout=180)
        logger.info("[provision] Provisioning output for %s:\n%s", slug, output[-500:])

        # Extract JSON result
        result = _extract_provision_json(output)

        # Update Site record
        from datetime import datetime, timezone
        with get_db_session(DB_URL) as session:
            site = session.get(Site, uuid.UUID(site_id))
            if site:
                from src.db.models import SiteStatus
                site.provisioned_at = datetime.now(timezone.utc)
                site.status = SiteStatus.active

                # Update WP admin credential with actual password
                if result.get("admin_password"):
                    from src.db.models import SiteCredential
                    from cryptography.fernet import Fernet
                    import base64

                    raw_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "changeme32byteskeyplaceholder123").encode()
                    key = base64.urlsafe_b64encode(raw_key.ljust(32)[:32])
                    fernet = Fernet(key)

                    # Find and update the wp_admin credential
                    wp_cred = (
                        session.query(SiteCredential)
                        .filter(
                            SiteCredential.site_id == uuid.UUID(site_id),
                            SiteCredential.credential_type == "wp_admin",
                        )
                        .first()
                    )
                    if wp_cred:
                        new_value = json.dumps({
                            "url": result.get("admin_url", f"https://{slug}.sitedoc.site/wp-admin"),
                            "username": result.get("admin_user", "sitedoc"),
                            "password": result["admin_password"],
                        })
                        wp_cred.encrypted_value = fernet.encrypt(new_value.encode()).decode()

        logger.info("[provision] Site %s provisioned successfully at https://%s.sitedoc.site", site_id, slug)

    except Exception as e:
        logger.exception("[provision] Failed to provision site %s: %s", site_id, e)
        # Mark site as error
        try:
            with get_db_session(DB_URL) as session:
                site = session.get(Site, uuid.UUID(site_id))
                if site:
                    from src.db.models import SiteStatus
                    site.status = SiteStatus.error
        except Exception:
            pass


@celery_app.task(name="src.tasks.provision.teardown_site")
def teardown_site(site_id: str, slug: str) -> None:
    """SSH into the hosting server and run the teardown script."""
    logger.info("[provision] Starting teardown for site %s (slug=%s)", site_id, slug)

    try:
        cmd = f"sudo {TEARDOWN_SCRIPT} {slug}"
        output = _ssh_exec(cmd, timeout=120)
        logger.info("[provision] Teardown output for %s:\n%s", slug, output[-500:])

        # Update site record
        with get_db_session(DB_URL) as session:
            site = session.get(Site, uuid.UUID(site_id))
            if site:
                from src.db.models import SiteStatus
                site.status = SiteStatus.inactive

        logger.info("[provision] Site %s torn down successfully", site_id)

    except Exception as e:
        logger.exception("[provision] Failed to teardown site %s: %s", site_id, e)


@celery_app.task(name="src.tasks.provision.add_custom_domain")
def add_custom_domain(site_id: str, slug: str, domain: str) -> None:
    """Add a custom domain Caddy config for a managed site."""
    logger.info("[provision] Adding custom domain %s for site %s", domain, slug)

    try:
        # Create Caddy config file for the custom domain
        caddy_config = f"""# Custom domain for {slug}.sitedoc.site
{domain} {{
    root * /var/www/sites/{slug}.sitedoc.site
    php_fastcgi unix//run/php/php8.2-fpm.sock
    file_server
    try_files {{path}} {{path}}/ /index.php?{{query}}
}}
"""
        # Write config and reload Caddy
        cmd = f"""cat > /etc/caddy/sites/{slug}.caddy << 'CADDYEOF'
{caddy_config}
CADDYEOF
sudo systemctl reload caddy"""
        output = _ssh_exec(cmd, timeout=30)
        logger.info("[provision] Custom domain %s configured for %s", domain, slug)

    except Exception as e:
        logger.exception("[provision] Failed to add custom domain %s for %s: %s", domain, slug, e)
