"""Test email_admission_rules.py against live emails via the
graph_mail_real.py FastAPI proxy server.

This script calls the local API server defined in
    /Workspace/.../SACC/langgraph/Email_Intake_Agent/graph_mail_real.py
which handles MSAL authentication and proxies to Microsoft Graph.
It fetches real emails + attachments and feeds the raw API JSON
directly into ``graph_evaluate_admission`` — no mock data.

Prerequisites
-------------
1.  The graph_mail_real.py server must be running::

        cd SACC/langgraph/Email_Intake_Agent
        python graph_mail_real.py
        # listens on http://localhost:8002 (or GRAPH_PORT)

2.  The .env file in the Email_Intake_Agent directory must contain
    valid MSAL credentials (GRAPH_TENANT_ID, GRAPH_CLIENT_ID,
    GRAPH_CLIENT_SECRET, GRAPH_USER_ID, GRAPH_API_TOKEN).

3.  requests (already available on Databricks runtime).

Run::

    python test_email_admission.py
    python test_email_admission.py --top 10
    python test_email_admission.py --folder inbox --detail
    python test_email_admission.py --message-id AAMkAGk1...
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import requests

# Ensure we import from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from email_admission_rules import (
    AdmissionConfig,
    AdmissionRoute,
    InMemoryCaseStore,
    InMemoryFileStore,
    graph_evaluate_admission,
    record_new_files,
)


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — reads from the Email_Intake_Agent .env file
# ═══════════════════════════════════════════════════════════════════════════════



import os
from dotenv import load_dotenv

# Load .env from the same directory as this Python file
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(env_path)

# Read configuration (same env vars as graph_mail_real.py)
API_TOKEN = os.environ.get("GRAPH_API_TOKEN", "")
SERVER_HOST = os.environ.get("GRAPH_HOST", "localhost")
SERVER_PORT = int(os.environ.get("GRAPH_PORT", "8002"))
USER_ID = os.environ.get("GRAPH_USER_ID", "")

# Base URL for the graph_mail_real.py FastAPI server
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

# Path to the Email_Intake_Agent directory (for error messages)
_EMAIL_AGENT_DIR = "SACC/langgraph/Email_Intake_Agent"

# Fields we need from each message (matches graph_evaluate_admission requirements)
SELECT_FIELDS = (
    "id,subject,from,conversationId,internetMessageId,"
    "internetMessageHeaders,body,hasAttachments"
)


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH MAIL PROXY CLIENT — calls the graph_mail_real.py FastAPI server
# ═══════════════════════════════════════════════════════════════════════════════

class GraphMailProxyClient:
    """Client for the graph_mail_real.py FastAPI proxy server.

    Calls the local server endpoints which handle MSAL auth and proxy
    requests to Microsoft Graph.  No MSAL/OAuth logic here — the server
    does all of that.
    """

    def __init__(
        self,
        server_url: str,
        api_token: str,
        user_id: str = "",
    ):
        self.server_url = server_url.rstrip("/")
        self.api_token = api_token
        self.user_id = user_id
        self._session = requests.Session()

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build query params with the api_token."""
        params = {"api_token": self.api_token}
        if self.user_id:
            params["user_id"] = self.user_id
        if extra:
            params.update(extra)
        return params

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Make a GET request to the proxy server."""
        url = f"{self.server_url}{path}"
        resp = self._session.get(url, params=self._params(params), timeout=60)
        resp.raise_for_status()
        return resp.json()

    # -- Health -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Check server health."""
        resp = self._session.get(
            f"{self.server_url}/health",
            params=self._params(),
            timeout=10,
        )
        return resp.json() if resp.ok else {"status": "error", "code": resp.status_code}

    # -- Messages ---------------------------------------------------------

    def list_messages(
        self,
        top: int = 20,
        folder: str | None = None,
    ) -> list[dict[str, Any]]:
        """List messages — returns raw Graph API JSON.

        Args:
            top: Max number of messages.
            folder: Well-known folder name (inbox, junkemail, etc.) or
                    a folder ID.  If None, lists from the root messages
                    endpoint.
        """
        params = {
            "$select": SELECT_FIELDS,
            "$top": top,
            "$orderby": "receivedDateTime asc",
        }

        if folder:
            path = f"/v1.0/me/mailFolders/{folder}/messages"
        else:
            path = "/v1.0/me/messages"

        data = self._get(path, params)
        messages = data.get("value", [])
        label = f"folder '{folder}'" if folder else "root"
        print(f"  [fetch] Got {len(messages)} messages from {label}")
        return messages

    def get_message(self, message_id: str) -> dict[str, Any]:
        """Get a single message by Graph API ID."""
        params = {"$select": SELECT_FIELDS}
        return self._get(f"/v1.0/me/messages/{message_id}", params)

    def get_attachments(self, message_id: str) -> list[dict[str, Any]]:
        """List attachments for a message — returns raw Graph API JSON."""
        data = self._get(f"/v1.0/me/messages/{message_id}/attachments")
        return data.get("value", [])

    def get_message_with_attachments(
        self,
        message_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch a message and its attachments in one call."""
        message = self.get_message(message_id)
        attachments = self.get_attachments(message_id) if message.get("hasAttachments") else []
        return message, attachments

    # -- Folders ----------------------------------------------------------

    def list_folders(self) -> list[dict[str, Any]]:
        """List mail folders — returns raw Graph API JSON."""
        data = self._get("/v1.0/me/mailFolders", {"$top": 50})
        return data.get("value", [])


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL ADMISSION PROCESSOR
# ═══════════════════════════════════════════════════════════════════════════════

def process_message(
    message: dict[str, Any],
    attachments: list[dict[str, Any]],
    case_store: InMemoryCaseStore | None = None,
    file_store: InMemoryFileStore | None = None,
    config: AdmissionConfig = AdmissionConfig(),
) -> dict[str, Any]:
    """Run the admission engine on a single email and return a summary dict.

    The dict includes a ``rules`` sub-dict with the individual outcome of
    each of the five rules, so callers can see exactly which rules fired
    and why — not just the final route.
    """
    result = graph_evaluate_admission(
        message=message,
        attachments=attachments,
        case_store=case_store,
        file_store=file_store,
        config=config,
    )

    # ── Populate stores so subsequent emails in the same thread match ──────
    # Register this email as a case (if it represents a new case, not a
    # link to an existing one) so replies can find it via Rule 2.
    conversation_id = message.get("conversationId", "")
    internet_message_id = message.get("internetMessageId", "")
    routes_that_create_cases = {
        AdmissionRoute.TRIAGE,
        AdmissionRoute.BODY_INVOICE,
        AdmissionRoute.MANUAL_REVIEW,
        AdmissionRoute.NO_ACTION,
    }
    if case_store is not None and result.route in routes_that_create_cases:
        case_id = f"CASE-{message.get('id', '')[:12]}"
        case_store.add_case(
            case_id=case_id,
            status="open",
            conversation_id=conversation_id,
            message_ids=[internet_message_id] if internet_message_id else None,
        )

    # Record any new file hashes so duplicate attachments are detected by Rule 3
    if file_store is not None:
        record_new_files(result, file_store, case_id=None)

    subject = message.get("subject", "(no subject)")
    sender_obj = message.get("from", {})
    sender = ""
    if isinstance(sender_obj, dict):
        sender = sender_obj.get("emailAddress", {}).get("address", "")

    # -- Per-rule breakdown ------------------------------------------------
    # Rule 1: Auto-reply detection
    if result.is_auto_reply:
        rule1_detail = f"Triggered: {'; '.join(result.auto_reply_reasons)}"
    else:
        rule1_detail = "Not an auto-reply"

    # Rule 2: Thread/case matching
    if result.thread_case:
        tc = result.thread_case
        rule2_detail = (
            f"Matched case {tc.case_id} (status={tc.status}) "
            f"via {tc.matched_by}"
        )
    elif case_store is None:
        rule2_detail = "Skipped (no case_store provided)"
    else:
        rule2_detail = "No existing case matched"

    # Rule 3: File deduplication
    total_files = len(result.processable_attachments)
    known_count = len(result.known_files)
    new_count = len(result.new_files)
    if total_files == 0:
        rule3_detail = "No processable attachments to check"
    elif file_store is None:
        rule3_detail = "Skipped (no file_store provided)"
    elif known_count == 0:
        rule3_detail = f"{new_count} new file(s), 0 known"
    elif new_count == 0:
        rule3_detail = (
            f"All {known_count} file(s) are byte-identical to "
            f"previously received files"
        )
    else:
        rule3_detail = (
            f"{known_count} known file(s), {new_count} new file(s)"
        )

    # Rule 4: Processable attachment count
    filtered_count = (
        len(result.filtered_inline)
        + len(result.filtered_quarantine)
        + len(result.filtered_unsupported)
    )
    branch_map = {
        "none": "3a (no processable attachments)",
        "one": "3b (exactly one)",
        "several": "3c (multiple)",
    }
    branch_label = branch_map.get(
        result.attachment_branch.value,
        result.attachment_branch.value,
    )
    filter_parts = []
    if result.filtered_inline:
        filter_parts.append(f"{len(result.filtered_inline)} inline image(s)")
    if result.filtered_quarantine:
        filter_parts.append(f"{len(result.filtered_quarantine)} quarantined")
    if result.filtered_unsupported:
        filter_parts.append(f"{len(result.filtered_unsupported)} unsupported type(s)")
    filter_note = f"; filtered out {', '.join(filter_parts)}" if filter_parts else ""
    rule4_detail = (
        f"{result.attachment_count} processable attachment(s) "
        f"→ branch {branch_label}{filter_note}"
    )

    # Rule 5: Body invoice check
    if result.attachment_count > 0:
        rule5_detail = "Skipped (email has processable attachments)"
    elif result.body_has_invoice:
        rule5_detail = "Invoice detected in email body"
    else:
        rule5_detail = "No invoice detected in body"

    # Get the structured summary (5 key fields for each invoice)
    structured_summary = result.get_summary()

    return {
        "message_id": message.get("id", ""),
        "subject": subject,
        "sender": sender,
        "route": result.route.value,
        "reason": result.route_reason,
        "is_auto_reply": result.is_auto_reply,
        "auto_reply_reasons": result.auto_reply_reasons,
        "thread_case": result.thread_case.case_id if result.thread_case else None,
        "thread_case_status": result.thread_case.status if result.thread_case else None,
        "attachment_count": result.attachment_count,
        "attachment_branch": result.attachment_branch.value,
        "known_files": len(result.known_files),
        "new_files": list(result.new_files.values()),
        "filtered_inline": len(result.filtered_inline),
        "filtered_quarantine": len(result.filtered_quarantine),
        "filtered_unsupported": len(result.filtered_unsupported),
        "body_has_invoice": result.body_has_invoice,
        "summary": structured_summary,
        "rules": {
            "rule1_auto_reply": {
                "triggered": result.is_auto_reply,
                "detail": rule1_detail,
            },
            "rule2_thread_case": {
                "matched": result.thread_case is not None,
                "detail": rule2_detail,
            },
            "rule3_file_dedup": {
                "has_known_files": known_count > 0,
                "has_new_files": new_count > 0,
                "detail": rule3_detail,
            },
            "rule4_attachment_count": {
                "branch": result.attachment_branch.value,
                "detail": rule4_detail,
            },
            "rule5_body_invoice": {
                "detected": result.body_has_invoice,
                "detail": rule5_detail,
            },
        },
    }


def print_summary(summary: dict[str, Any], idx: int = 0) -> None:
    """Print a single email's admission result in a readable format."""
    print(f"\n{'-'*72}")
    print(f"  #{idx+1}  {summary['subject'][:50]}")
    print(f"{'-'*72}")
    print(f"  From:        {summary['sender']}")
    print(f"  Msg ID:      {summary['message_id'][:60]}")
    
    # Display the 5 key results for each invoice (structured summary)
    structured = summary.get('summary', {})
    if structured:
        print(f"\n  KEY RESULTS (for each invoice):")
        print(f"  1. is_auto_reply              : {structured.get('is_auto_reply', False)}")
        print(f"  2. find_thread_case (case_id) : {structured.get('case_id', None)}")
        print(f"  3. is_known_file (has dupes)  : {structured.get('has_known_files', False)}")
        print(f"  4. count_processable_attach   : {structured.get('attachment_count', 0)}")
        print(f"  5. body_invoice_check         : {structured.get('body_has_invoice', False)}")
    
    print(f"\n  ROUTING DECISION:")
    print(f"  → Route  : {summary['route']}")
    print(f"  → Reason : {summary['reason']}")

    # Per-rule breakdown
    rules = summary.get("rules", {})
    print(f"\n  5-Rule Breakdown:")
    print(f"  {'─'*50}")

    # Rule 1
    r1 = rules.get("rule1_auto_reply", {})
    r1_mark = "[FIRED]" if r1.get("triggered") else "[  NO ]"
    print(f"  {r1_mark} Rule 1 — Auto-reply:  {r1.get('detail', '')}")

    # Rule 2
    r2 = rules.get("rule2_thread_case", {})
    r2_mark = "[FIRED]" if r2.get("matched") else "[  NO ]"
    print(f"  {r2_mark} Rule 2 — Thread/Case: {r2.get('detail', '')}")

    # Rule 3
    r3 = rules.get("rule3_file_dedup", {})
    r3_fired = r3.get("has_known_files", False)
    r3_mark = "[FIRED]" if r3_fired else "[  NO ]"
    print(f"  {r3_mark} Rule 3 — File dedup:   {r3.get('detail', '')}")

    # Rule 4
    r4 = rules.get("rule4_attachment_count", {})
    r4_fired = r4.get("branch", "none") != "none"
    r4_mark = "[FIRED]" if r4_fired else "[  NO ]"
    print(f"  {r4_mark} Rule 4 — Attachments:  {r4.get('detail', '')}")

    # Rule 5
    r5 = rules.get("rule5_body_invoice", {})
    r5_mark = "[FIRED]" if r5.get("detected") else "[  NO ]"
    print(f"  {r5_mark} Rule 5 — Body invoice: {r5.get('detail', '')}")

    # Detailed fields
    print(f"\n  Auto-reply:  {summary['is_auto_reply']}  {summary['auto_reply_reasons'] or ''}")
    if summary['thread_case']:
        print(f"  Thread:      {summary['thread_case']} ({summary['thread_case_status']})")
    print(f"  Attachments: {summary['attachment_count']} processable "
          f"(branch {summary['attachment_branch']})")
    if summary['known_files']:
        print(f"  Known files: {summary['known_files']}")
    if summary['new_files']:
        print(f"  New files:   {summary['new_files']}")
    if summary['filtered_inline']:
        print(f"  Filtered inline:      {summary['filtered_inline']}")
    if summary['filtered_quarantine']:
        print(f"  Filtered quarantine:  {summary['filtered_quarantine']}")
    if summary['filtered_unsupported']:
        print(f"  Filtered unsupported: {summary['filtered_unsupported']}")
    if summary['body_has_invoice']:
        print(f"  Body invoice: {summary['body_has_invoice']}")


def print_route_table(summaries: list[dict[str, Any]]) -> None:
    """Print a compact summary table of all routes."""
    print(f"\n{'='*72}")
    print("  ROUTING SUMMARY")
    print(f"{'='*72}")
    print(f"  {'#':>3}  {'Route':<15}  {'Subject':<45}")
    print(f"  {'---':>3}  {'---':<15}  {'---':<45}")
    for i, s in enumerate(summaries):
        print(f"  {i+1:>3}  {s['route']:<15}  {s['subject'][:45]}")

    counts = Counter(s["route"] for s in summaries)
    print(f"\n  Route distribution:")
    for route, count in sorted(counts.items()):
        print(f"    {route:<20} {count:>3}")


def print_route_detail(summaries: list[dict[str, Any]]) -> None:
    """Print detailed results grouped by route."""
    routes_seen: dict[str, list[dict[str, Any]]] = {}
    for s in summaries:
        routes_seen.setdefault(s["route"], []).append(s)

    print(f"\n{'='*72}")
    print("  ROUTE BREAKDOWN")
    print(f"{'='*72}")
    for route in sorted(routes_seen):
        items = routes_seen[route]
        print(f"\n  {route.upper()} ({len(items)} emails)")
        print(f"  {'-'*50}")
        for s in items:
            print(f"    - {s['subject'][:60]}")
            print(f"      From: {s['sender']}  |  {s['reason'][:60]}")
            if s['attachment_count']:
                print(f"      Attachments: {s['attachment_count']} processable, "
                      f"{len(s['new_files'])} new")
            if s['body_has_invoice']:
                print(f"      Body invoice detected: True")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Test email_admission_rules.py via the graph_mail_real.py API server.",
    )
    parser.add_argument(
        "--top", type=int, default=20,
        help="Number of emails to fetch (default: 20)",
    )
    parser.add_argument(
        "--folder", type=str, default=None,
        help="Folder name (inbox, junkemail, sentitems, ...) or folder ID",
    )
    parser.add_argument(
        "--message-id", type=str, default=None,
        help="Process a single message by Graph API ID",
    )
    parser.add_argument(
        "--detail", action="store_true",
        help="Print detailed route breakdown",
    )
    parser.add_argument(
        "--list-folders", action="store_true",
        help="List available mail folders and exit",
    )
    parser.add_argument(
        "--server-url", type=str, default=None,
        help=f"Override server URL (default: {SERVER_URL})",
    )
    args = parser.parse_args()

    server_url = args.server_url or SERVER_URL

    print("\n" + "#" * 72)
    print("#  EMAIL ADMISSION RULES -- Live API Test via graph_mail_real.py")
    print("#" * 72)
    print(f"  Server:    {server_url}")
    print(f"  User ID:   {USER_ID or '(from server config)'}")
    print(f"  API token: {'set' if API_TOKEN else '(not set)'}")
    print()

    # Check server health
    client = GraphMailProxyClient(
        server_url=server_url,
        api_token=API_TOKEN,
        user_id=USER_ID,
    )

    print("  Checking server health...")
    try:
        health = client.health()
        print(f"  [health] {health}")
        if health.get("status") != "ok":
            print("\nERROR: Server is not healthy. Is graph_mail_real.py running?")
            print(f"  Start it with:  cd {_EMAIL_AGENT_DIR} && python graph_mail_real.py")
            return 1
        if not health.get("is_configured"):
            print("\nWARNING: Server reports MSAL is not configured.")
            print("  Set GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET in .env")
    except requests.exceptions.ConnectionError:
        print(f"\nERROR: Cannot connect to {server_url}")
        print(f"  Is graph_mail_real.py running?  Start it with:")
        print(f"    cd {_EMAIL_AGENT_DIR} && python graph_mail_real.py")
        return 1
    except Exception as e:
        print(f"\nERROR: Health check failed: {e}")
        return 1

    # -- List folders mode -------------------------------------------------
    if args.list_folders:
        print("\n  Available mail folders:")
        folders = client.list_folders()
        for f in folders:
            print(f"    {f.get('displayName', '?'):20s}  "
                  f"({f.get('totalItemCount', 0)} items, id={f.get('id', '?')[:30]})")
        return 0

    # Initialise admission stores (empty — no pre-existing cases or files)
    case_store = InMemoryCaseStore()
    file_store = InMemoryFileStore()
    config = AdmissionConfig()

    # Fetch messages from the proxy server
    try:
        if args.message_id:
            print("\n  [mode] Single message by ID")
            message, attachments = client.get_message_with_attachments(args.message_id)
            messages_with_atts = [(message, attachments)]
        else:
            folder_label = args.folder or "inbox"
            print(f"\n  [mode] Fetch from mailbox (folder: {folder_label})")
            messages = client.list_messages(top=args.top, folder=args.folder)
            messages_with_atts = []
            for i, msg in enumerate(messages):
                msg_id = msg.get("id", "")
                has_atts = msg.get("hasAttachments", False)
                atts = client.get_attachments(msg_id) if has_atts else []
                messages_with_atts.append((msg, atts))
                if (i + 1) % 5 == 0:
                    print(f"  [fetch] Processed {i+1}/{len(messages)} messages...")
    except requests.exceptions.HTTPError as e:
        print(f"\nAPI ERROR: {e}")
        if e.response is not None:
            print(f"  Status: {e.response.status_code}")
            print(f"  Body:   {e.response.text[:500]}")
        return 1
    except requests.exceptions.ConnectionError as e:
        print(f"\nCONNECTION ERROR: {e}")
        return 1

    # Process each message through the admission engine
    print(f"\n  Processing {len(messages_with_atts)} emails through admission engine...")
    summaries: list[dict[str, Any]] = []
    for i, (msg, atts) in enumerate(messages_with_atts):
        summary = process_message(
            message=msg,
            attachments=atts,
            case_store=case_store,
            file_store=file_store,
            config=config,
        )
        summaries.append(summary)
        print_summary(summary, idx=i)

    # Print summary table
    print_route_table(summaries)

    if args.detail:
        print_route_detail(summaries)

    print(f"\n{'='*72}")
    print(f"  Done: {len(summaries)} emails processed")
    print(f"{'='*72}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())