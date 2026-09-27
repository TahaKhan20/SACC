"""Email Intake Agent
====================

Self-contained agent that fetches, enriches, and classifies emails from
Microsoft Graph.  This module is designed to be imported by the workflow
orchestrator but can also be run standalone for testing.

Three nodes:
    1.  FETCH     – list messages from /v1.0/users/{user_id}/messages
    2.  ENRICH    – fetch full body + attachment metadata per message
    3.  CLASSIFY  – score each email by sender / subject / content /
                   attachments; return relevant emails with their
                   document attachments ready for downstream triage

Configuration (env vars):
    GRAPH_USER_ID      Target user ID or UPN  (required)
    GRAPH_API_TOKEN    Bearer token           (required)
    GRAPH_API_BASE_URL  Graph endpoint         (default: https://graph.microsoft.com)

Credentials are read ONLY from environment variables — no CLI args.

Usage (standalone):
    export GRAPH_USER_ID="user@domain.com"
    export GRAPH_API_TOKEN="eyJ0e..."
    python email_intake_agent.py
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("email_intake_agent")

# ── Configuration ─────────────────────────────────────────────────────────

GRAPH_API_BASE_URL = os.getenv("GRAPH_API_BASE_URL", "https://graph.microsoft.com")
GRAPH_USER_ID = os.getenv("GRAPH_USER_ID", "")
GRAPH_API_TOKEN = os.getenv("GRAPH_API_TOKEN", "")

_INVOICE_KEYWORDS = [
    "invoice", "rechnung", "factura", "credit note", "gutschrift",
    "statement", "account summary", "kontoauszug",
    "bill", "payment", "supplier", "vendor", "purchase order",
    "fuel", "charter", "maintenance", "ground handling",
    "sap", "document ai", "attachment",
]

_DOCUMENT_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    ".doc", ".docx", ".xls", ".xlsx",
}

DOCUMENT_EXTENSIONS = _DOCUMENT_EXTENSIONS


# ── Data Models ───────────────────────────────────────────────────────────


@dataclass
class EmailMessage:
    """Normalised email representation."""
    message_id: str
    subject: str
    sender: str
    sender_name: str = ""
    recipients: list[str] = field(default_factory=list)
    received_date: str = ""
    body_preview: str = ""
    body_content: str = ""
    has_attachments: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)
    importance: str = "normal"
    is_read: bool = False


# ── Node 1: Fetch Emails ───────────────────────────────────────────────────


def fetch_emails(user_id: str, api_token: str, top: int = 50) -> list[EmailMessage]:
    """List messages from the mailbox via GET /v1.0/users/{user_id}/messages."""
    logger.info("Node 1: Fetching emails from Graph API for user %s …", user_id)

    url = f"{GRAPH_API_BASE_URL}/v1.0/users/{user_id}/messages"
    headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
    params = {
        "$top": top,
        "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,"
                    "hasAttachments,importance,isRead",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error("Error fetching emails: %s", exc)
        return []

    messages = data.get("value", [])
    logger.info("Fetched %d messages", len(messages))

    emails: list[EmailMessage] = []
    for msg in messages:
        sender_obj = msg.get("from", {})
        sender_email = sender_obj.get("emailAddress", {}).get("address", "")
        sender_name = sender_obj.get("emailAddress", {}).get("name", "")
        recipients = [
            r.get("emailAddress", {}).get("address", "")
            for r in msg.get("toRecipients", [])
        ]
        emails.append(EmailMessage(
            message_id=msg.get("id", ""),
            subject=msg.get("subject", ""),
            sender=sender_email,
            sender_name=sender_name,
            recipients=recipients,
            received_date=msg.get("receivedDateTime", ""),
            body_preview=msg.get("bodyPreview", ""),
            has_attachments=msg.get("hasAttachments", False),
            importance=msg.get("importance", "normal"),
            is_read=msg.get("isRead", False),
        ))

    return emails


# ── Node 2: Enrich Emails ──────────────────────────────────────────────────


def enrich_emails(emails: list[EmailMessage], user_id: str, api_token: str) -> list[EmailMessage]:
    """Fetch full body content and attachment metadata for each email."""
    logger.info("Node 2: Enriching %d emails with full body and attachments …", len(emails))

    headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
    base = f"{GRAPH_API_BASE_URL}/v1.0/users/{user_id}/messages"

    with httpx.Client(timeout=30.0) as client:
        for email in emails:
            try:
                resp = client.get(
                    f"{base}/{email.message_id}",
                    headers=headers,
                    params={"$select": "id,subject,body,from,toRecipients"},
                )
                resp.raise_for_status()
                full_msg = resp.json()
                body_obj = full_msg.get("body", {})
                email.body_content = body_obj.get("content", "")
            except Exception as exc:
                logger.warning("Could not fetch body for message %s: %s", email.message_id[:20], exc)

            if email.has_attachments:
                try:
                    resp = client.get(f"{base}/{email.message_id}/attachments", headers=headers)
                    resp.raise_for_status()
                    att_data = resp.json()
                    email.attachments = att_data.get("value", [])
                except Exception as exc:
                    logger.warning("Could not fetch attachments for %s: %s", email.message_id[:20], exc)

    logger.info(
        "Enrichment complete: %d emails with body content, %d with attachments",
        sum(1 for e in emails if e.body_content),
        sum(1 for e in emails if e.attachments),
    )
    return emails


# ── Node 3: Classify Emails ─────────────────────────────────────────────────


def _has_document_attachment(email: EmailMessage) -> bool:
    for att in email.attachments:
        name = att.get("name", "").lower()
        if any(name.endswith(ext) for ext in _DOCUMENT_EXTENSIONS):
            return True
    return False


def _score_email_relevance(email: EmailMessage) -> int:
    """Score how likely this email contains a document for triage (0-100)."""
    score = 0
    combined_text = " ".join([
        email.subject.lower(),
        email.body_preview.lower(),
        email.body_content.lower(),
    ])

    subject_lower = email.subject.lower()
    matched_subject = [kw for kw in _INVOICE_KEYWORDS if kw in subject_lower]
    if matched_subject:
        score += min(30, len(matched_subject) * 10)

    matched_body = [kw for kw in _INVOICE_KEYWORDS if kw in combined_text]
    if matched_body:
        score += min(25, len(matched_body) * 5)

    if _has_document_attachment(email):
        score += 30

    if email.has_attachments:
        score += 10

    if email.sender and not email.sender.endswith("@outlook.com"):
        if any(domain in email.sender.lower() for domain in [".com", ".net", ".org", ".sa", ".ae"]):
            score += 5

    return min(100, score)


def classify_emails(emails: list[EmailMessage], min_score: int = 20) -> list[EmailMessage]:
    """Classify emails by sender, subject, content, and attachments.

    Returns the subset of emails whose relevance score >= min_score.
    """
    logger.info("Node 3: Classifying %d emails (min_score=%d) …", len(emails), min_score)

    relevant: list[EmailMessage] = []
    for email in emails:
        score = _score_email_relevance(email)
        if score >= min_score:
            relevant.append(email)
            logger.info("  RELEVANT  score=%3d  subject='%s'  from=%s  attachments=%d",
                        score, email.subject[:60], email.sender, len(email.attachments))
        else:
            logger.debug("  SKIP       score=%3d  subject='%s'", score, email.subject[:60])

    logger.info("Classification: %d relevant emails out of %d total", len(relevant), len(emails))
    return relevant


# ── Attachment Helper ────────────────────────────────────────────────────────


def save_attachment_to_temp(attachment: dict[str, Any]) -> Optional[str]:
    """Save a Graph API attachment (base64) to a temp file and return the path."""
    att_name = attachment.get("name", "attachment")
    content_bytes = attachment.get("contentBytes", "")

    if not content_bytes:
        logger.warning("Attachment '%s' has no contentBytes", att_name)
        return None

    try:
        decoded = base64.b64decode(content_bytes)
    except Exception as exc:
        logger.warning("Could not decode attachment '%s': %s", att_name, exc)
        return None

    suffix = Path(att_name).suffix or ".pdf"
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="triage_")
    with os.fdopen(fd, "wb") as f:
        f.write(decoded)

    logger.info("Saved attachment '%s' to temp file: %s (%d bytes)", att_name, temp_path, len(decoded))
    return temp_path


# ── Orchestrator ────────────────────────────────────────────────────────────


def run_intake(
    top: int = 50,
    min_score: int = 20,
) -> dict[str, Any]:
    """Run all three intake nodes: fetch → enrich → classify.

    Credentials are read from module-level env vars GRAPH_USER_ID and
    GRAPH_API_TOKEN.  They are NOT accepted as function parameters.

    Returns dict with total_emails, relevant_emails (list of EmailMessage),
    and any errors.
    """
    if not GRAPH_USER_ID or not GRAPH_API_TOKEN:
        return {"error": "Missing GRAPH_USER_ID or GRAPH_API_TOKEN env var", "total_emails": 0, "relevant_emails": []}

    logger.info("=" * 60)
    logger.info("STARTING EMAIL INTAKE AGENT")
    logger.info("=" * 60)

    emails = fetch_emails(GRAPH_USER_ID, GRAPH_API_TOKEN, top=top)
    if not emails:
        logger.warning("No emails fetched")
        return {"total_emails": 0, "relevant_emails": [], "errors": ["No emails fetched"]}

    emails = enrich_emails(emails, GRAPH_USER_ID, GRAPH_API_TOKEN)
    relevant = classify_emails(emails, min_score=min_score)

    return {"total_emails": len(emails), "relevant_emails": relevant, "errors": []}


# ── CLI Entry Point ─────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point — fetch and classify emails (no triage)."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Email Intake Agent: fetch and classify emails from Microsoft Graph.",
    )
    parser.add_argument("--top", type=int, default=50, help="Max emails to fetch")
    parser.add_argument("--min-score", type=int, default=20, help="Min relevance score")
    args = parser.parse_args()

    result = run_intake(top=args.top, min_score=args.min_score)

    print("\n" + "=" * 60)
    print("EMAIL INTAKE RESULTS")
    print("=" * 60)
    print(json.dumps({
        "total_emails": result["total_emails"],
        "relevant_emails": len(result["relevant_emails"]),
        "errors": result.get("errors", []),
    }, indent=2))

    for email in result["relevant_emails"]:
        print(f"  [{email.subject[:60]}] from {email.sender} — {len(email.attachments)} attachments")


if __name__ == "__main__":
    main()