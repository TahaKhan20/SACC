"""Email-to-Triage Workflow
==========================

Thin orchestrator that connects the Email Intake Agent to the Triage Agent.

    1.  EMAIL INTAKE  – delegates to Email_Intake_Agent.run_intake() which
        fetches emails from Microsoft Graph, enriches them with full body
        content and attachments, and classifies which emails are relevant.
    2.  TRIAGE         – for each relevant email with document attachments,
        saves the attachment to a temp file and calls the Triage Agent's
        ``run_triage`` entry point, which uploads to Document AI and returns
        a TriageResult.

The workflow does NOT fetch, enrich, or classify emails itself — that logic
lives entirely in ``langgraph/Email_Intake_Agent/email_intake_agent.py``.

Prerequisites:
    - GRAPH_USER_ID and GRAPH_API_TOKEN env vars set
    - SACC API running:  python main.py  (port 8000)
    - Triage Agent configured via .env (see langgraph/TriageAgent/.env.example)

Usage:
    export GRAPH_USER_ID="user@domain.com"
    export GRAPH_API_TOKEN="eyJ0e..."

    cd langgraph/workflow
    python workflow.py
    python workflow.py --top 10
    python workflow.py --dry-run
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# ── Path setup: make sibling packages importable ──────────────────────────

_LANGGRAPH_DIR = Path(__file__).resolve().parent.parent
_TRIAGE_DIR = _LANGGRAPH_DIR / "TriageAgent"
_INTAKE_DIR = _LANGGRAPH_DIR / "Email_Intake_Agent"

for _dir in (_TRIAGE_DIR, _INTAKE_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from triage_agent import run_triage  # noqa: E402
from email_intake_agent import (  # noqa: E402
    run_intake,
    save_attachment_to_temp,
    DOCUMENT_EXTENSIONS,
)

# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("workflow")

# ── Temp: temp_input folder for saving JSON payloads ──────────────────────
# TEMP CODE: Creates a 'temp_input' folder to save JSON payloads after
# document upload. Each file is named by document_id and contains the actual
# attachment content (base64) plus triage results. These files replace the
# mock API's fixed/seeded extraction data — read from here to get real content
# based on document_id instead of querying the mock API for seeded payloads.
# Remove this section once the mock API or real SAP Document AI can return
# extraction results from the actual uploaded file content.

TEMP_INPUT_DIR = _LANGGRAPH_DIR / "temp_input"


def _ensure_temp_input_dir() -> Path:
    """Create the temp_input directory if it doesn't exist."""
    TEMP_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_INPUT_DIR


def _save_payload_to_temp(document_id: str, payload: dict[str, Any]) -> str:
    """Save a JSON payload to temp_input/{document_id}.json.

    Returns the file path where the payload was saved.
    """
    _ensure_temp_input_dir()
    file_path = TEMP_INPUT_DIR / f"{document_id}.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str, ensure_ascii=False)
    logger.info("  Saved payload to temp_input: %s", file_path.name)
    return str(file_path)


def _load_payload_from_temp(document_id: str) -> dict[str, Any] | None:
    """Load a JSON payload from temp_input/{document_id}.json.

    Returns None if the file does not exist.
    """
    file_path = TEMP_INPUT_DIR / f"{document_id}.json"
    if not file_path.exists():
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Triage Node ────────────────────────────────────────────────────────────


def triage_documents(relevant_emails: list, dry_run: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """For each relevant email with document attachments, call the Triage Agent.

    The Triage Agent's ``run_triage`` entry point:
      1. Uploads the document to Document AI (SACC API on port 8000)
      2. Gets extraction results for classification
      3. Determines document type, company, direct/intercompany, invoice type
      4. Validates extraction confidence and required fields
      5. Returns a TriageResult dict

    Returns (triage_results, errors).
    """
    if dry_run:
        logger.info("TRIAGE: DRY RUN — skipping triage (would triage %d emails)", len(relevant_emails))
        return [], []

    triage_results: list[dict[str, Any]] = []
    errors: list[str] = []

    logger.info("TRIAGE: Processing %d relevant emails …", len(relevant_emails))

    for email in relevant_emails:
        logger.info("─" * 60)
        logger.info("Email: '%s' from %s", email.subject[:80], email.sender)

        if not email.attachments:
            logger.info("  No attachments — skipping triage")
            continue

        for attachment in email.attachments:
            att_name = attachment.get("name", "unknown")
            att_lower = att_name.lower()

            # Only triage document-type attachments
            if not any(att_lower.endswith(ext) for ext in DOCUMENT_EXTENSIONS):
                logger.info("  Skipping non-document attachment: %s", att_name)
                continue

            logger.info("  Triaging attachment: %s", att_name)

            # Save attachment to temp file (from Email Intake Agent)
            temp_path = save_attachment_to_temp(attachment)
            if not temp_path:
                errors.append(f"Could not save attachment '{att_name}' from email '{email.subject}'")
                continue

            try:
                # Call Triage Agent entry point
                result = run_triage(file_path=temp_path, file_name=att_name)

                # TEMP: Save the upload payload + triage result to temp_input
                # folder, keyed by a generated document_id. This file contains
                # the actual attachment contentBytes (base64) and can be used
                # to retrieve real content by document_id instead of relying on
                # the mock API's fixed/seeded extraction data.
                _doc_id = str(uuid.uuid4())
                _payload = {
                    "document_id": _doc_id,
                    "attachment_name": att_name,
                    "attachment_contentBytes": attachment.get("contentBytes", ""),
                    "document_type": email.category,
                    "triage_result": result,
                    "email_context": {
                        "message_id": email.message_id,
                        "subject": email.subject,
                        "sender": email.sender,
                        "sender_name": email.sender_name,
                        "recipients": email.recipients,
                        "received_date": email.received_date,
                    },
                }
                _save_payload_to_temp(_doc_id, _payload)

                # Enrich result with email context
                result["email_context"] = {
                    "message_id": email.message_id,
                    "subject": email.subject,
                    "sender": email.sender,
                    "sender_name": email.sender_name,
                    "recipients": email.recipients,
                    "received_date": email.received_date,
                    "attachment_name": att_name,
                }

                triage_results.append(result)
                logger.info("  Category: %s", email.category)
                logger.info("  Triage result: type=%s company=%s direct_intercompany=%s review=%s",
                            result.get("document_type"),
                            result.get("company_classification"),
                            result.get("direct_intercompany"),
                            result.get("review_required"))

            except Exception as exc:
                logger.error("  Triage failed for '%s': %s", att_name, exc)
                errors.append(f"Triage error for '{att_name}': {exc}")
            finally:
                # Clean up temp file
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)

    logger.info("Triage complete: %d results, %d errors", len(triage_results), len(errors))
    return triage_results, errors


# ── Workflow Orchestrator ────────────────────────────────────────────────────


def run_workflow(
    top: int = 50,
    dry_run: bool = False,
    min_score: int = 20,
) -> dict[str, Any]:
    """Run the full email-to-triage workflow.

    Delegates email fetching, enrichment, and classification to the Email
    Intake Agent, then runs the Triage Agent on relevant document attachments.

    Credentials (GRAPH_USER_ID, GRAPH_API_TOKEN) are read from environment
    variables by the Email Intake Agent — not passed through this function.

    Args:
        top: Maximum number of emails to fetch from the mailbox.
        dry_run: If True, fetch and classify emails but skip triage.
        min_score: Minimum relevance score for email classification.

    Returns:
        Summary dict with email counts, triage results, and errors.
    """
    logger.info("=" * 60)
    logger.info("STARTING EMAIL-TO-TRIAGE WORKFLOW")
    logger.info("=" * 60)

    # Step 1: Email Intake Agent — fetch, enrich, classify (reads env vars)
    intake_result = run_intake(
        top=top,
        min_score=min_score,
    )

    if intake_result.get("error"):
        return {"error": intake_result["error"], "triage_results": [], "errors": [intake_result["error"]]}

    relevant_emails = intake_result["relevant_emails"]
    if not relevant_emails:
        logger.warning("No relevant emails found — nothing to triage")
        return {
            "total_emails_fetched": intake_result["total_emails"],
            "relevant_emails": 0,
            "triage_results_count": 0,
            "triage_results": [],
            "errors": intake_result.get("errors", []),
            "dry_run": dry_run,
        }

    # Step 2: Triage Agent — classify documents from relevant emails
    triage_results, triage_errors = triage_documents(relevant_emails, dry_run=dry_run)

    return {
        "total_emails_fetched": intake_result["total_emails"],
        "relevant_emails": len(relevant_emails),
        "triage_results_count": len(triage_results),
        "triage_results": triage_results,
        "errors": intake_result.get("errors", []) + triage_errors,
        "dry_run": dry_run,
    }


# ── CLI Entry Point ─────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for the workflow."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Email-to-Triage Workflow: connect Email Intake Agent to Triage Agent.",
    )
    parser.add_argument("--top", type=int, default=50, help="Max emails to fetch (default: 50)")
    parser.add_argument("--min-score", type=int, default=20, help="Min relevance score (default: 20)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and classify only, skip triage")
    args = parser.parse_args()

    summary = run_workflow(
        top=args.top,
        dry_run=args.dry_run,
        min_score=args.min_score,
    )

    print("\n" + "=" * 60)
    print("WORKFLOW SUMMARY")
    print("=" * 60)
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
