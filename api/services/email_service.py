from __future__ import annotations

from email.message import EmailMessage
from datetime import UTC
from datetime import datetime
import logging
import os
import smtplib


logger = logging.getLogger(__name__)
DEFAULT_FROM_ADDRESS = "noreply@memoryos.io"

CATEGORY_LABELS = {
    "preference": "Your preferences and settings",
    "expertise": "Your skills and knowledge",
    "goal": "Your goals and plans",
    "procedure": "Your workflows and habits",
    "fact": "General facts about you",
    "relationship": "Your relationships and context",
}


class EmailService:
    async def send_otp_email(self, to_email: str, otp: str) -> bool:
        subject = "Your MemoryOS login code"
        body = f"Your code is: {otp}. Valid for 10 minutes."
        return self._send_email(to_email=to_email, subject=subject, body=body)

    async def send_grant_notification(
        self,
        to_email: str,
        agent_name: str,
        categories: list[str],
        manage_url: str,
        expires_at: datetime | None = None,
    ) -> bool:
        consent_base = str(os.getenv("CONSENT_APP_BASE_URL") or os.getenv("CONSENT_BASE_URL") or "").rstrip("/")
        subject = f"{agent_name} can now access your AI memories"
        if expires_at is None:
            expires_text = "continues until you revoke it"
        else:
            expires_text = f"expires on {expires_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}"

        bullet_lines = "\n".join(
            f"   - {CATEGORY_LABELS.get(category, category.replace('_', ' ').title())}"
            for category in categories
        ) or "   - No categories listed"
        manage_all_text = (
            f"Manage all your memory permissions:\n{consent_base}/manage\n\n"
            if consent_base
            else ""
        )
        body = (
            "Hi,\n\n"
            f"{agent_name} was just granted access to your MemoryOS memories.\n\n"
            "They can see:\n"
            f"{bullet_lines}\n\n"
            f"This access {expires_text}.\n\n"
            "Not you, or changed your mind?\n"
            f"Revoke access immediately: {manage_url}\n\n"
            f"{manage_all_text}"
            "\u2014 MemoryOS"
        )
        return self._send_email(to_email=to_email, subject=subject, body=body)

    def _send_email(self, *, to_email: str, subject: str, body: str) -> bool:
        smtp_host = os.getenv("SMTP_HOST")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER")
        smtp_pass = os.getenv("SMTP_PASS")
        smtp_from = os.getenv("SMTP_FROM") or os.getenv("EMAIL_FROM") or smtp_user or DEFAULT_FROM_ADDRESS
        if not smtp_host:
            logger.warning("SMTP host is not configured for email delivery.")
            return False

        message = EmailMessage()
        message["From"] = smtp_from
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)

        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
                smtp.starttls()
                if smtp_user and smtp_pass:
                    smtp.login(smtp_user, smtp_pass)
                smtp.send_message(message)
            return True
        except Exception:
            logger.exception("Failed to send transactional email via %s from %s.", smtp_host, smtp_from)
            return False
