"""
Email notification service for SiteDoc.

Sends alerts for key issue lifecycle events:
- Diagnosis complete
- Fix applied (success/failure)
- Fix requires approval
- Health check alert

Uses SMTP (configurable via env). Gracefully no-ops if SMTP_HOST is not set.
"""
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.core.config import settings

logger = logging.getLogger(__name__)


def _send_email(to: str, subject: str, html: str, text: str) -> bool:
    """Send an email via SMTP. Returns True on success, False on failure."""
    if not settings.SMTP_HOST:
        logger.debug("SMTP not configured — skipping notification to %s", to)
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM
        msg["To"] = to
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))

        context = ssl.create_default_context()
        if settings.SMTP_TLS:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=context) as server:
                if settings.SMTP_USER:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_FROM, to, msg.as_string())
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.ehlo()
                if settings.SMTP_STARTTLS:
                    server.starttls(context=context)
                if settings.SMTP_USER:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_FROM, to, msg.as_string())

        logger.info("Email sent to %s: %s", to, subject)
        return True

    except Exception as e:
        logger.error("Failed to send email to %s: %s", to, e)
        return False


def notify_diagnosis_ready(
    to_email: str,
    customer_name: str,
    site_url: str,
    issue_title: str,
    confidence: float,
    actions_count: int,
    requires_approval: bool,
    issue_id: str,
) -> bool:
    """Send diagnosis-ready notification."""
    dashboard_url = f"{settings.APP_URL}/issues/{issue_id}"
    conf_pct = f"{confidence:.0%}"
    approval_note = (
        "<p>⚠️ <strong>This fix requires your approval</strong> before it runs.</p>"
        if requires_approval else
        "<p>✅ This fix can run automatically when you're ready.</p>"
    )

    html = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: #2563eb;">🩺 SiteDoc — Diagnosis Complete</h2>
      <p>Hi {customer_name},</p>
      <p>We've finished analyzing the issue on <strong>{site_url}</strong>.</p>
      <table style="width:100%; border-collapse:collapse; margin: 16px 0;">
        <tr>
          <td style="padding: 8px; background:#f1f5f9; border-radius:4px; font-weight:bold;">Issue</td>
          <td style="padding: 8px;">{issue_title}</td>
        </tr>
        <tr>
          <td style="padding: 8px; background:#f1f5f9; font-weight:bold;">Confidence</td>
          <td style="padding: 8px;">{conf_pct}</td>
        </tr>
        <tr>
          <td style="padding: 8px; background:#f1f5f9; font-weight:bold;">Actions planned</td>
          <td style="padding: 8px;">{actions_count}</td>
        </tr>
      </table>
      {approval_note}
      <a href="{dashboard_url}" style="display:inline-block; padding:12px 24px; background:#2563eb; color:white; border-radius:6px; text-decoration:none; margin-top:8px;">
        View Diagnosis →
      </a>
      <p style="color:#64748b; font-size:12px; margin-top:24px;">SiteDoc — AI-powered website maintenance</p>
    </div>
    """

    text = (
        f"SiteDoc — Diagnosis Complete\n\n"
        f"Hi {customer_name},\n\n"
        f"We've finished analyzing the issue on {site_url}.\n\n"
        f"Issue: {issue_title}\n"
        f"Confidence: {conf_pct}\n"
        f"Actions planned: {actions_count}\n\n"
        f"{'Approval required before fix runs.' if requires_approval else 'Fix can run automatically.'}\n\n"
        f"View: {dashboard_url}\n"
    )

    return _send_email(
        to=to_email,
        subject=f"[SiteDoc] Diagnosis ready: {issue_title}",
        html=html,
        text=text,
    )


def notify_fix_complete(
    to_email: str,
    customer_name: str,
    site_url: str,
    issue_title: str,
    success: bool,
    summary: str,
    issue_id: str,
) -> bool:
    """Send fix complete/failed notification."""
    dashboard_url = f"{settings.APP_URL}/issues/{issue_id}"
    status_icon = "✅" if success else "❌"
    status_word = "Fix Applied" if success else "Fix Failed"
    status_color = "#16a34a" if success else "#dc2626"

    html = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: {status_color};">{status_icon} SiteDoc — {status_word}</h2>
      <p>Hi {customer_name},</p>
      <p>Update on your site <strong>{site_url}</strong>:</p>
      <p><strong>{issue_title}</strong></p>
      <div style="background:#f8fafc; border-left:4px solid {status_color}; padding:12px; margin:16px 0;">
        {summary}
      </div>
      <a href="{dashboard_url}" style="display:inline-block; padding:12px 24px; background:#2563eb; color:white; border-radius:6px; text-decoration:none;">
        View Details →
      </a>
      <p style="color:#64748b; font-size:12px; margin-top:24px;">SiteDoc — AI-powered website maintenance</p>
    </div>
    """

    text = (
        f"SiteDoc — {status_word}\n\n"
        f"Hi {customer_name},\n\n"
        f"Update on {site_url}:\n"
        f"{issue_title}\n\n"
        f"{summary}\n\n"
        f"Details: {dashboard_url}\n"
    )

    return _send_email(
        to=to_email,
        subject=f"[SiteDoc] {status_icon} {issue_title} — {status_word}",
        html=html,
        text=text,
    )


def notify_approval_needed(
    to_email: str,
    customer_name: str,
    site_url: str,
    issue_title: str,
    actions_summary: list[str],
    issue_id: str,
) -> bool:
    """Alert user that a high-risk fix needs manual approval."""
    dashboard_url = f"{settings.APP_URL}/issues/{issue_id}"
    actions_html = "".join(f"<li>{a}</li>" for a in actions_summary[:5])

    html = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: #d97706;">⚠️ SiteDoc — Approval Required</h2>
      <p>Hi {customer_name},</p>
      <p>A fix for <strong>{site_url}</strong> needs your approval before it runs:</p>
      <p><strong>{issue_title}</strong></p>
      <p>Planned actions:</p>
      <ul>{actions_html}</ul>
      <p>Your site will be backed up before anything changes.</p>
      <div style="margin-top:16px;">
        <a href="{dashboard_url}?action=approve" style="display:inline-block; padding:12px 24px; background:#16a34a; color:white; border-radius:6px; text-decoration:none; margin-right:8px;">
          Approve Fix →
        </a>
        <a href="{dashboard_url}?action=reject" style="display:inline-block; padding:12px 24px; background:#dc2626; color:white; border-radius:6px; text-decoration:none;">
          Reject
        </a>
      </div>
      <p style="color:#64748b; font-size:12px; margin-top:24px;">SiteDoc — AI-powered website maintenance</p>
    </div>
    """

    text = (
        f"SiteDoc — Approval Required\n\n"
        f"Hi {customer_name},\n\n"
        f"A fix for {site_url} needs your approval:\n"
        f"{issue_title}\n\n"
        f"Planned actions:\n"
        + "\n".join(f"- {a}" for a in actions_summary[:5])
        + f"\n\nApprove or reject: {dashboard_url}\n"
    )

    return _send_email(
        to=to_email,
        subject=f"[SiteDoc] ⚠️ Approval needed: {issue_title}",
        html=html,
        text=text,
    )


def notify_health_alert(
    to_email: str,
    customer_name: str,
    site_url: str,
    alert_type: str,
    details: str,
) -> bool:
    """Send a site health alert."""
    html = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: #dc2626;">🚨 SiteDoc — Health Alert</h2>
      <p>Hi {customer_name},</p>
      <p>Your site <strong>{site_url}</strong> has an issue:</p>
      <div style="background:#fef2f2; border-left:4px solid #dc2626; padding:12px; margin:16px 0;">
        <strong>{alert_type}</strong><br>{details}
      </div>
      <p style="color:#64748b; font-size:12px; margin-top:24px;">SiteDoc — AI-powered website maintenance</p>
    </div>
    """

    text = f"SiteDoc — Health Alert\n\nSite: {site_url}\n{alert_type}: {details}\n"

    return _send_email(
        to=to_email,
        subject=f"[SiteDoc] 🚨 Health alert: {site_url}",
        html=html,
        text=text,
    )
