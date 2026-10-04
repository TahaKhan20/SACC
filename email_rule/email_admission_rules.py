"""Email Admission Rules — Production-Ready Engine
===================================================

Pre-processing gate that evaluates incoming emails before they enter
the classification or triage pipeline.  Five rules decide whether an
email is admitted and how it is routed.

Rules
-----
1. ``is_auto_reply(headers, config)``
       Detect auto-replies from ``Auto-Submitted`` and ``Precedence``
       headers, using configurable accepted values.  ``X-Auto-Response-
       Suppress`` is **never** treated as an auto-reply signal.

2. ``find_thread_case(envelope, case_store)``
       Match an incoming email to an existing case using
       ``conversationId`` first, then ``In-Reply-To`` and
       ``References`` (with message-ID normalization).  A match to a
       **closed** case is raised for AP — the closed case is **not**
       reopened.

3. ``check_known_file(attachment, file_store)``
       A byte-identical file already received is linked, not extracted
       again (155 of 339 PDF rows in the mailbox export were re-sends).

4. ``count_processable_attachments(attachments, config)``
       After quarantined-file removal, inline-logo removal, and
       attachment-type filtering, branch into 3a (none), 3b (one) or
       3c (several).

5. ``body_invoice_check(body, classifier)``
       Whether an email body contains an invoice.  Three detection paths:
       (a) inline attachments (is_inline=True), (b) <img> tags in HTML,
       (c) text pattern matching (only when no processable attachments).

Routing Precedence (``_decide_route``)
--------------------------------------
The orchestrator maps rule evaluation results to routing decisions
according to the intake workflow table. Rules are evaluated independently;
routing logic combines their results.

**Workflow Mapping** (highest priority first):

    **Step 6** — Closed-case match → ``RAISE_AP``
        A reply to a closed case (even an auto-reply) is raised for AP.
        The closed case is **not** reopened.

    **Auto-reply handling** (no closed-case match):
        • Auto-reply + processable attachment → ``TRIAGE``
        • Auto-reply + body invoice → ``TRIAGE``
        • Auto-reply (no invoice) → ``AUTO_CLOSE``

    **Step 3** — Thread match + all files known → ``LINK_TO_CASE``
        Link the submission to the matched case as additional evidence.
        No new case is created.

    **Step 5** — Thread match + new distinct files → ``TRIAGE``
        Despite the thread match, new/distinct evidence requires a
        **new case**. The thread association is retained as evidence.

    **Step 4** — All attachments are duplicates (no thread match) → ``LINK_FILES``
        All processable attachments are byte-identical to previously
        received files. Link them, do not re-extract.

    **Step 5** — Has new processable attachments → ``TRIAGE``
        New distinct documents require triage and case creation.

    **Step 7** — No processable attachments, body has invoice → ``BODY_INVOICE``
        Invoice content is in the email body (inline attachment or text
        pattern match), not as a processable file attachment.

    **Step 8** — Nothing processable → ``NO_ACTION``
        No invoice content found.

Architecture: Rules → Routing Separation
-----------------------------------------
The design strictly separates **rule evaluation** from **routing decisions**
to ensure each workflow step can be updated independently.

**Rules (independent evaluation functions)**:
    Each of the 5 rules is a **pure function** that accepts normalized data
    and returns a result object. Rules do NOT make routing decisions:

    1. ``is_auto_reply(headers, config)`` → AutoReplyResult
    2. ``find_thread_case(envelope, case_store)`` → Optional[CaseMatch]
    3. ``check_known_file(attachment, file_store)`` → FileDedupResult
    4. ``count_processable_attachments(attachments, config)`` → AttachmentCountResult
    5. ``body_invoice_check(body, classifier)`` → bool

**Orchestrator** (``evaluate_admission``):
    Calls each rule in sequence, collecting results into an AdmissionResult.
    Does NOT make routing decisions.

**Routing Logic** (``_decide_route``):
    Maps the accumulated rule outputs to a single routing decision
    (RAISE_AP, MANUAL_REVIEW, AUTO_CLOSE, LINK_TO_CASE, LINK_FILES,
    TRIAGE, BODY_INVOICE, NO_ACTION) according to the intake workflow table.

    This function encapsulates ALL routing precedence. To update a workflow
    step (e.g., change when a new case is created vs linked), modify ONLY
    this function. The rules remain unchanged.

**Benefits**:
    * Each rule can be tested independently
    * Routing precedence is explicit and centralized
    * Workflow changes require modifying only ``_decide_route``
    * Rules are reusable across different routing strategies

Design
------
* Every rule accepts **normalised** data (dataclasses), not raw Graph
  API JSON, so the same rules work with any email source.
* ``graph_*`` helper functions convert MS Graph API responses into the
  normalised shapes.
* Custom APIs provide their own conversion — the rules themselves are
  source-agnostic.
* ``CaseStore`` and ``FileStore`` are abstract base classes.  Callers
  implement them for their specific backend (database, S3, etc.).
* ``BodyInvoiceClassifier`` is an abstract base class.  The default
  ``PatternBasedClassifier`` uses configurable regex patterns; callers
  may substitute an ML-based or rule-based classifier.
* All configurable values live in :class:`AdmissionConfig`.

Usage (MS Graph API)::

    from email_admission_rules_v2 import (
        graph_evaluate_admission, AdmissionConfig,
        InMemoryCaseStore, InMemoryFileStore,
    )

    cfg = AdmissionConfig()
    case_store = InMemoryCaseStore()
    file_store = InMemoryFileStore()
    # ... populate stores ...

    result = graph_evaluate_admission(
        message=graph_message_json,
        attachments=graph_attachments_json,
        case_store=case_store,
        file_store=file_store,
        config=cfg,
    )
    print(result.route, result.route_reason)

Usage (custom API)::

    from email_admission_rules_v2 import (
        EmailHeaders, EmailEnvelope, AttachmentInfo,
        evaluate_admission, AdmissionConfig, CaseStore, FileStore,
    )

    cfg = AdmissionConfig()
    headers = EmailHeaders(auto_submitted="auto-replied", precedence="auto_reply")
    envelope = EmailEnvelope(conversation_id="abc123", subject="RE: Invoice")
    attachments = [AttachmentInfo(name="invoice.pdf", content_bytes="...")]

    result = evaluate_admission(
        headers=headers, envelope=envelope, attachments=attachments,
        body_content="<html>...</html>", body_content_type="html",
        case_store=my_case_store, file_store=my_file_store,
        config=cfg,
    )
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import logging
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence

__all__ = [
    # Data models
    "EmailHeaders", "EmailEnvelope", "AttachmentInfo",
    "CaseMatch", "FileRecord",
    # Enums
    "AdmissionRoute", "AttachmentBranch",
    # Rule results
    "AutoReplyResult", "FileDedupResult", "AttachmentCountResult",
    "AdmissionResult",
    # Config
    "AdmissionConfig", "InvoicePatternSpec",
    # Abstract stores
    "CaseStore", "FileStore",
    # In-memory stores
    "InMemoryCaseStore", "InMemoryFileStore",
    # Classifier
    "BodyInvoiceClassifier", "PatternBasedClassifier",
    # Rule 1
    "is_auto_reply", "graph_fetch_internet_headers",
    # Rule 2
    "find_thread_case",
    # Rule 3
    "compute_sha256", "check_known_file", "record_new_files",
    # Rule 4
    "is_inline_image", "is_quarantined", "is_processable_attachment",
    "count_processable_attachments",
    # Rule 5
    "has_images_in_html", "strip_html", "body_invoice_check",
    # Utilities
    "normalize_message_id", "safe_b64decode",
    # Orchestrator
    "evaluate_admission",
    # Graph API helpers
    "graph_headers_to_dict", "graph_message_to_headers",
    "graph_message_to_envelope", "graph_attachment_to_info",
    "graph_attachments_to_info_list", "graph_evaluate_admission",
    # Constants
    "DEFAULT_DOCUMENT_EXTENSIONS", "DEFAULT_DOCUMENT_CONTENT_TYPES",
    "DEFAULT_QUARANTINE_PATTERNS", "DEFAULT_INVOICE_PATTERNS",
    "DEFAULT_AUTO_SUBMITTED_VALUES", "DEFAULT_AUTO_PRECEDENCE_VALUES",
    "DEFAULT_AUTO_SUBMITTED_REJECT",
]

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

# Document file extensions considered processable (Rule 4)
DEFAULT_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    ".doc", ".docx", ".xls", ".xlsx",
})

# Document content-type prefixes considered processable (Rule 4)
DEFAULT_DOCUMENT_CONTENT_TYPES: tuple[str, ...] = (
    "application/pdf",
    "image/",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml",
)

# Image content-type prefix for inline-image detection (Rule 4)
_IMAGE_CONTENT_PREFIXES: tuple[str, ...] = ("image/",)

# Auto-Submitted header values that indicate auto-reply (RFC 3834)
# Any non-empty value other than "no" is treated as auto-reply by default.
DEFAULT_AUTO_SUBMITTED_VALUES: frozenset[str] = frozenset({
    "auto-replied", "auto-generated", "auto-notified",
})

# Precedence header values that indicate auto-reply
DEFAULT_AUTO_PRECEDENCE_VALUES: frozenset[str] = frozenset({
    "auto_reply", "bulk", "junk",
})

# Auto-Submitted value that explicitly means NOT auto-reply (RFC 3834)
DEFAULT_AUTO_SUBMITTED_REJECT: frozenset[str] = frozenset({"no"})

# Graph attachment types that are processable (file attachments only)
_PROCESSABLE_ATTACHMENT_TYPES: frozenset[str] = frozenset({"fileAttachment"})

# Default patterns indicating a quarantined attachment (Rule 4)
DEFAULT_QUARANTINE_PATTERNS: tuple[str, ...] = (
    "quarantine",
    "blocked",
    "malware",
    "virus",
    "threat detected",
    "security scan",
    "attachment removed",
    "file deleted",
)

@dataclass(frozen=True)
class InvoicePatternSpec:
    """A weighted invoice-detection pattern for body_invoice_check (Rule 5).

    Each pattern is a regex searched case-insensitively in the plain-text
    body.  Patterns carry a ``weight`` (importance) so that multiple weak
    matches can be required or a single strong match can suffice.

    Attributes:
        pattern: Regex string (searched with ``re.IGNORECASE``).
        weight: Importance score contributed when the pattern matches.
            Strong patterns (3): invoice number, amount due. Medium (2):
            payment terms, due date. Weak (1): bill to, subtotal.
        name: Human-readable label for logging and debugging.
    """
    pattern: str
    weight: int = 1
    name: str = ""


# Default invoice indicator patterns for body_invoice_check (Rule 5)
DEFAULT_INVOICE_PATTERNS: tuple[InvoicePatternSpec, ...] = (
    # --- Strong patterns (score 3): highly specific invoice terms ---
    InvoicePatternSpec(r"invoice\s+(number|#|id|no\.?)", weight=3, name="invoice_number"),
    InvoicePatternSpec(r"invoice\s*#\s*\w+", weight=3, name="invoice_hash"),
    InvoicePatternSpec(r"amount\s+due", weight=3, name="amount_due"),
    InvoicePatternSpec(r"total\s+due", weight=3, name="total_due"),
    # --- Medium patterns (score 2): payment-related terms ---
    InvoicePatternSpec(r"payment\s+(due|terms)", weight=2, name="payment_terms"),
    InvoicePatternSpec(r"net\s+\d+\s+days", weight=2, name="net_days"),
    InvoicePatternSpec(r"due\s+date", weight=2, name="due_date"),
    InvoicePatternSpec(r"remit\s+(to|payment)", weight=2, name="remit"),
    InvoicePatternSpec(r"tax\s+(id|vat|gst)", weight=2, name="tax_id"),
    # --- Weak patterns (score 1): generic billing terms ---
    InvoicePatternSpec(r"bill\s+to", weight=1, name="bill_to"),
    InvoicePatternSpec(r"sub[-]?total", weight=1, name="subtotal"),
    InvoicePatternSpec(r"po\s+number", weight=1, name="po_number"),
)

# Minimum score for body_invoice_check to return True
DEFAULT_INVOICE_THRESHOLD: int = 2


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class AdmissionConfig:
    """Central configuration for all admission rules.

    Every ambiguous or environment-specific value lives here so callers
    can tune behaviour without modifying code.  Defaults match the
    original fuel-mailbox analysis.

    Attributes:
        document_extensions: File extensions considered processable (Rule 4).
        document_content_type_prefixes: Content-type prefixes considered
            processable (Rule 4).  If non-empty, an attachment must match
            either an extension OR a content-type prefix to be processable.
            If empty, extension matching alone is used.
        quarantine_patterns: Substrings matched (case-insensitive) against
            attachment name and content-type to detect quarantined files.
        auto_submitted_values: ``Auto-Submitted`` header values that indicate
            auto-reply.  Any non-empty, non-reject value also triggers if
            ``treat_unknown_auto_submitted_as_auto`` is ``True``.
        auto_submitted_reject: ``Auto-Submitted`` value(s) that explicitly
            mean NOT auto-reply (RFC 3834 ``"no"``).
        auto_precedence_values: ``Precedence`` header values indicating auto-reply.
        treat_unknown_auto_submitted_as_auto: When ``True``, any non-empty
            ``Auto-Submitted`` value not in ``auto_submitted_reject`` is treated
            as auto-reply (per RFC 3834).  When ``False``, only values in
            ``auto_submitted_values`` trigger.
        invoice_patterns: Weighted patterns for :class:`PatternBasedClassifier`.
        invoice_threshold: Minimum score for the pattern classifier to flag
            a body as containing an invoice.
        processable_attachment_types: Graph ``@odata.type`` suffixes that are
            considered processable file attachments.  ``itemAttachment`` and
            ``referenceAttachment`` are excluded by default because they do
            not carry file bytes.
        closed_case_statuses: Case status values treated as "closed".
    """
    document_extensions: frozenset[str] = field(
        default_factory=lambda: DEFAULT_DOCUMENT_EXTENSIONS,
    )
    document_content_type_prefixes: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_DOCUMENT_CONTENT_TYPES,
    )
    quarantine_patterns: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_QUARANTINE_PATTERNS,
    )
    auto_submitted_values: frozenset[str] = field(
        default_factory=lambda: DEFAULT_AUTO_SUBMITTED_VALUES,
    )
    auto_submitted_reject: frozenset[str] = field(
        default_factory=lambda: DEFAULT_AUTO_SUBMITTED_REJECT,
    )
    auto_precedence_values: frozenset[str] = field(
        default_factory=lambda: DEFAULT_AUTO_PRECEDENCE_VALUES,
    )
    treat_unknown_auto_submitted_as_auto: bool = True
    invoice_patterns: tuple[InvoicePatternSpec, ...] = field(
        default_factory=lambda: DEFAULT_INVOICE_PATTERNS,
    )
    invoice_threshold: int = DEFAULT_INVOICE_THRESHOLD
    processable_attachment_types: frozenset[str] = field(
        default_factory=lambda: _PROCESSABLE_ATTACHMENT_TYPES,
    )
    closed_case_statuses: frozenset[str] = field(
        default_factory=lambda: frozenset({"closed"}),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════════════════════

class AdmissionRoute(str, Enum):
    """Recommended routing decision after admission evaluation."""
    AUTO_CLOSE = "auto_close"
    MANUAL_REVIEW = "manual_review"
    RAISE_AP = "raise_ap"
    LINK_TO_CASE = "link_to_case"
    LINK_FILES = "link_files"
    TRIAGE = "triage"
    BODY_INVOICE = "body_invoice"
    NO_ACTION = "no_action"


class AttachmentBranch(str, Enum):
    """Attachment count branch from Rule 4."""
    NONE = "3a"     # no processable attachments
    ONE = "3b"      # exactly one
    SEVERAL = "3c"  # two or more


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EmailHeaders:
    """Normalised internet message headers for auto-reply detection (Rule 1).

    All values are case-insensitive at evaluation time.
    """
    auto_submitted: Optional[str] = None
    precedence: Optional[str] = None
    x_auto_response_suppress: Optional[str] = None


@dataclass
class EmailEnvelope:
    """Normalised email envelope for thread/case matching (Rule 2).

    ``conversation_id`` is Graph's thread grouping ID (not an RFC 2822
    message ID).  ``internet_message_id``, ``in_reply_to``, and
    ``references`` are RFC 2822 message IDs (with or without angle
    brackets — they are normalised at lookup time).
    """
    conversation_id: Optional[str] = None
    internet_message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: list[str] = field(default_factory=list)
    subject: str = ""
    sender: str = ""


@dataclass
class AttachmentInfo:
    """Normalised attachment metadata (Rules 3 & 4).

    ``content_bytes`` is the base64-encoded file content as returned
    by Graph API's ``contentBytes`` field.  ``attachment_type`` is
    ``"fileAttachment"`` by default; Graph may also return
    ``"itemAttachment"`` or ``"referenceAttachment"`` (which are not
    processable file attachments).
    """
    id: str = ""
    name: str = ""
    content_type: str = ""
    size: int = 0
    is_inline: bool = False
    content_id: Optional[str] = None
    content_bytes: Optional[str] = None
    attachment_type: str = "fileAttachment"


@dataclass
class CaseMatch:
    """Result of a successful thread-case lookup (Rule 2)."""
    case_id: str = ""
    status: str = ""
    matched_by: str = ""       # "conversation_id" | "in_reply_to" | "references"
    matched_value: str = ""


@dataclass
class FileRecord:
    """Record of a previously seen file (Rule 3)."""
    sha256: str = ""
    name: str = ""
    case_id: Optional[str] = None
    first_seen: Optional[str] = None   # ISO timestamp


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_message_id(message_id: str) -> str:
    """Normalise an RFC 2822 message ID for case-store lookup.

    Message IDs may or may not be wrapped in angle brackets
    (``<abc@example.com>`` vs ``abc@example.com``).  This function
    strips surrounding whitespace and angle brackets and lowercases
    the result so that ``<ABC@Example.com>`` and ``abc@example.com``
    match the same case.

    Args:
        message_id: Raw message ID (with or without angle brackets).

    Returns:
        Normalised message ID (no angle brackets, lowercased, stripped).
    """
    if not message_id:
        return ""
    mid = message_id.strip()
    # Strip angle brackets
    if mid.startswith("<") and mid.endswith(">"):
        mid = mid[1:-1]
    return mid.lower()


def safe_b64decode(content: str) -> Optional[bytes]:
    """Safely base64-decode a string, returning ``None`` on failure.

    Graph API ``contentBytes`` is standard base64.  Some sources may
    return URL-safe base64 (``-`` and ``_`` instead of ``+`` and ``/``).
    This function tries standard base64 first, then URL-safe, and
    also handles padding issues.

    Args:
        content: Base64-encoded string.

    Returns:
        Decoded bytes, or ``None`` if the input is empty or invalid.
    """
    if not content:
        return None
    raw = content.strip()
    if not raw:
        return None
    # Remove any internal whitespace (some sources add newlines)
    cleaned = re.sub(r'\s+', '', raw)
    if not cleaned:
        return None
    # Try standard base64 with strict validation (rejects invalid chars)
    try:
        return base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        pass
    # Try URL-safe base64 (- and _ instead of + and /)
    try:
        return base64.urlsafe_b64decode(cleaned)
    except (binascii.Error, ValueError):
        pass
    # Try adding padding and decoding with validation
    try:
        padded = cleaned + "=" * (-len(cleaned) % 4)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("Failed to base64-decode attachment content (%d chars)", len(raw))
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ABSTRACT STORES — Callers implement these for their backend
# ═══════════════════════════════════════════════════════════════════════════════

class CaseStore(ABC):
    """Abstract case store for thread/case lookup (Rule 2).

    Implement this for your specific backend (SQL database, REST API,
    ticketing system, etc.).  Message IDs passed to
    :meth:`find_by_message_id` are already normalised via
    :func:`normalize_message_id` (angle brackets stripped, lowercased).
    """

    @abstractmethod
    def find_by_conversation_id(self, conversation_id: str) -> Optional[CaseMatch]:
        """Find a case by Graph conversationId."""
        ...

    @abstractmethod
    def find_by_message_id(self, message_id: str) -> Optional[CaseMatch]:
        """Find a case by normalised RFC 2822 message ID."""
        ...


class FileStore(ABC):
    """Abstract file store for SHA-256 deduplication (Rule 3).

    Implement this for your specific backend (S3 + metadata table,
    database, filesystem index, etc.).
    """

    @abstractmethod
    def has_file(self, sha256: str) -> Optional[FileRecord]:
        """Return the FileRecord if this hash was seen before, else ``None``."""
        ...

    @abstractmethod
    def record_file(self, sha256: str, name: str, case_id: Optional[str] = None) -> None:
        """Record a newly seen file hash."""
        ...


# ═══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY STORES — For testing and prototyping
# ═══════════════════════════════════════════════════════════════════════════════

class InMemoryCaseStore(CaseStore):
    """Simple in-memory case store for testing and prototyping."""

    def __init__(self) -> None:
        self._by_conversation: dict[str, CaseMatch] = {}
        self._by_msg_id: dict[str, CaseMatch] = {}

    def add_case(
        self,
        case_id: str,
        status: str,
        *,
        conversation_id: str = "",
        message_ids: Optional[list[str]] = None,
    ) -> None:
        """Register a case for later lookup.

        Message IDs are normalised via :func:`normalize_message_id` before
        storage, so callers may pass IDs with or without angle brackets.
        """
        match = CaseMatch(case_id=case_id, status=status)
        if conversation_id:
            self._by_conversation[conversation_id] = match
        for mid in (message_ids or []):
            self._by_msg_id[normalize_message_id(mid)] = match

    def find_by_conversation_id(self, conversation_id: str) -> Optional[CaseMatch]:
        m = self._by_conversation.get(conversation_id)
        if m:
            return CaseMatch(
                case_id=m.case_id, status=m.status,
                matched_by="conversation_id", matched_value=conversation_id,
            )
        return None

    def find_by_message_id(self, message_id: str) -> Optional[CaseMatch]:
        norm = normalize_message_id(message_id)
        m = self._by_msg_id.get(norm)
        if m:
            return CaseMatch(
                case_id=m.case_id, status=m.status,
                matched_by="message_id", matched_value=message_id,
            )
        return None


class InMemoryFileStore(FileStore):
    """Simple in-memory file store for testing and prototyping."""

    def __init__(self) -> None:
        self._files: dict[str, FileRecord] = {}

    def has_file(self, sha256: str) -> Optional[FileRecord]:
        return self._files.get(sha256)

    def record_file(self, sha256: str, name: str, case_id: Optional[str] = None) -> None:
        self._files[sha256] = FileRecord(sha256=sha256, name=name, case_id=case_id)


# ═══════════════════════════════════════════════════════════════════════════════
# RULE 1: is_auto_reply
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AutoReplyResult:
    """Result of is_auto_reply check (Rule 1)."""
    is_auto_reply: bool = False
    reasons: list[str] = field(default_factory=list)


def graph_fetch_internet_headers(
    base_url: str,
    user_id: str,
    message_id: str,
    *,
    access_token: Optional[str] = None,
    timeout: int = 30,
) -> EmailHeaders:
    """Fetch ``internetMessageHeaders`` from a Graph API endpoint.

    Makes an HTTP GET request to::

        {base_url}/v1.0/users/{user_id}/messages/{message_id}
            ?$select=internetMessageHeaders

    and extracts the three headers relevant to Rule 1
    (``Auto-Submitted``, ``Precedence``, ``X-Auto-Response-Suppress``)
    from the response.

    Uses the standard-library ``urllib`` so no third-party dependency
    (e.g. ``requests``) is required.

    Args:
        base_url: Base URL of the Graph API endpoint
            (e.g. ``"http://localhost:8002"``).
        user_id: The mailbox / user principal name
            (e.g. ``"sacc.ap.invoice@domain.com"``).
        message_id: The Graph message ID whose headers should be fetched.
        access_token: Optional bearer token for authentication.
        timeout: Request timeout in seconds (default 30).

    Returns:
        EmailHeaders populated with ``Auto-Submitted``, ``Precedence``,
        and ``X-Auto-Response-Suppress``.  Missing headers are ``None``.
        On any request or parse failure an empty ``EmailHeaders`` is
        returned (all fields ``None``) and the error is logged.
    """
    url = (
        f"{base_url.rstrip('/')}/v1.0/users/{user_id}"
        f"/messages/{message_id}?$select=internetMessageHeaders"
    )

    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if access_token:
        request.add_header("Authorization", f"Bearer {access_token}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            data = json.loads(body)
    except urllib.error.URLError as exc:
        logger.error("Failed to fetch internetMessageHeaders from %s: %s", url, exc)
        return EmailHeaders()
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON response from %s: %s", url, exc)
        return EmailHeaders()
    except Exception:
        logger.exception("Unexpected error fetching headers from %s", url)
        return EmailHeaders()

    # Parse internetMessageHeaders into a flat, lowercased dict
    headers_map: dict[str, str] = {}
    raw_headers = data.get("internetMessageHeaders", [])
    if isinstance(raw_headers, list):
        for h in raw_headers:
            if not isinstance(h, dict):
                continue
            name = h.get("name", "")
            value = h.get("value", "")
            if name and value:
                headers_map[name.lower()] = value

    return EmailHeaders(
        auto_submitted=headers_map.get("auto-submitted"),
        precedence=headers_map.get("precedence"),
        x_auto_response_suppress=headers_map.get("x-auto-response-suppress"),
    )


def is_auto_reply(
    headers: Optional[EmailHeaders] = None,
    config: AdmissionConfig = AdmissionConfig(),
    *,
    message_id: Optional[str] = None,
    graph_base_url: str = "http://localhost:8002",
    graph_user_id: Optional[str] = None,
    access_token: Optional[str] = None,
    timeout: int = 30,
) -> AutoReplyResult:
    """Detect whether an email is an auto-reply based on its headers.

    Checks the ``Auto-Submitted`` and ``Precedence`` headers against
    configurable accepted values.

    ``X-Auto-Response-Suppress`` is **never** treated as proof of
    auto-reply — it appears on 123 of 124 fuel-mailbox messages, including
    real invoices.  It is a suppression header that Exchange adds to
    prevent OOF loops; its presence does not mean the message itself
    is automated.

    **Two modes of operation:**

    1. **Pre-normalised headers** (default, backward compatible) —
       pass ``headers`` directly::

           result = is_auto_reply(headers, config)

    2. **Fetch from Graph API** — omit ``headers`` and provide
       ``message_id`` + ``graph_user_id`` instead.  The function
       calls :func:`graph_fetch_internet_headers` to retrieve
       ``internetMessageHeaders`` from the Graph endpoint, then
       performs the same checks::

           result = is_auto_reply(
               message_id="AAMkAGI2TG93AAA=",
               graph_user_id="sacc.ap.invoice@domain.com",
               graph_base_url="http://localhost:8002",
           )

    Args:
        headers: Normalised email headers.  Pass ``None`` to fetch
            headers from the Graph API (requires ``message_id`` and
            ``graph_user_id``).
        config: Admission configuration with accepted/reject values.
        message_id: Graph message ID (required when ``headers`` is
            ``None``).
        graph_base_url: Base URL of the Graph API endpoint
            (default ``"http://localhost:8002"``).
        graph_user_id: Mailbox / user principal name (required when
            ``headers`` is ``None``).
        access_token: Optional bearer token for Graph API auth.
        timeout: Request timeout in seconds for the Graph API call.

    Returns:
        AutoReplyResult with ``is_auto_reply`` flag and matching reasons.

    Raises:
        ValueError: If ``headers`` is ``None`` and either ``message_id``
            or ``graph_user_id`` is not provided.
    """
    # Fetch headers from Graph API when not provided directly
    if headers is None:
        if not message_id or not graph_user_id:
            raise ValueError(
                "Either 'headers' must be provided, or both 'message_id' "
                "and 'graph_user_id' must be given to fetch from the Graph API."
            )
        headers = graph_fetch_internet_headers(
            base_url=graph_base_url,
            user_id=graph_user_id,
            message_id=message_id,
            access_token=access_token,
            timeout=timeout,
        )

    reasons: list[str] = []

    # Auto-Submitted header (RFC 3834)
    auto_submitted = (headers.auto_submitted or "").strip().lower()
    if auto_submitted:
        if auto_submitted in config.auto_submitted_reject:
            pass  # Explicitly NOT an auto-reply
        elif (
            auto_submitted in config.auto_submitted_values
            or config.treat_unknown_auto_submitted_as_auto
        ):
            reasons.append(f"Auto-Submitted: {auto_submitted}")

    # Precedence header
    precedence = (headers.precedence or "").strip().lower()
    if precedence and precedence in config.auto_precedence_values:
        reasons.append(f"Precedence: {precedence}")

    return AutoReplyResult(
        is_auto_reply=bool(reasons),
        reasons=reasons,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# RULE 2: find_thread_case
# ═══════════════════════════════════════════════════════════════════════════════

def find_thread_case(
    envelope: EmailEnvelope,
    case_store: CaseStore,
) -> Optional[CaseMatch]:
    """Find an existing case for this email thread.

    Lookup order (first match wins):

    1. ``conversationId`` — Graph's thread grouping ID (strongest signal).
    2. ``In-Reply-To`` — the message ID this email directly replies to
       (normalised via :func:`normalize_message_id`).
    3. ``References`` — prior message IDs in the thread, checked
       most-recent-first (last entry first, per RFC 5322 ordering).

    A match to a **closed** case is returned as-is; the caller
    (:func:`evaluate_admission`) raises it for AP rather than
    reopening the case.

    Args:
        envelope: Normalised email envelope with thread identifiers.
        case_store: Backend that implements case lookup.

    Returns:
        CaseMatch if a case is found, ``None`` otherwise.
    """
    # 1. conversationId (strongest signal — Graph groups by this)
    if envelope.conversation_id:
        match = case_store.find_by_conversation_id(envelope.conversation_id)
        if match:
            return match

    # 2. In-Reply-To (direct parent message)
    if envelope.in_reply_to:
        match = case_store.find_by_message_id(envelope.in_reply_to)
        if match:
            match.matched_by = "in_reply_to"
            return match

    # 3. References (walk thread history, most recent first)
    if envelope.references:
        for ref_id in reversed(envelope.references):
            match = case_store.find_by_message_id(ref_id)
            if match:
                match.matched_by = "references"
                return match

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# RULE 3: is_known_file (SHA-256 deduplication)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_sha256(content_bytes_base64: str) -> Optional[str]:
    """Compute the SHA-256 hash of an attachment's content.

    Uses :func:`safe_b64decode` for robust base64 handling (standard,
    URL-safe, and padding-fix variants).

    Args:
        content_bytes_base64: Base64-encoded file content.

    Returns:
        Hex-encoded SHA-256 digest, or ``None`` if the input is
        empty or cannot be decoded.
    """
    raw = safe_b64decode(content_bytes_base64)
    if raw is None or not raw:
        return None
    return hashlib.sha256(raw).hexdigest()


@dataclass
class FileDedupResult:
    """Result of deduplication check for a single attachment (Rule 3)."""
    sha256: Optional[str] = None
    is_known: bool = False
    existing_record: Optional[FileRecord] = None
    attachment_name: str = ""


def check_known_file(
    attachment: AttachmentInfo,
    file_store: FileStore,
) -> FileDedupResult:
    """Check if a single attachment's content has been seen before.

    Args:
        attachment: Normalised attachment with ``content_bytes`` (base64).
        file_store: Backend that implements SHA-256 lookup.

    Returns:
        FileDedupResult with hash, ``is_known`` flag, and existing record.
    """
    sha = compute_sha256(attachment.content_bytes or "")
    if sha is None:
        return FileDedupResult(attachment_name=attachment.name)

    existing = file_store.has_file(sha)
    return FileDedupResult(
        sha256=sha,
        is_known=existing is not None,
        existing_record=existing,
        attachment_name=attachment.name,
    )


def record_new_files(
    result: AdmissionResult,
    file_store: FileStore,
    case_id: Optional[str] = None,
) -> int:
    """Persist new file hashes from an AdmissionResult into the FileStore.

    After :func:`evaluate_admission` identifies new files (in
    ``result.new_files``), call this to record them so future emails
    with the same content are deduplicated.

    Args:
        result: The AdmissionResult from ``evaluate_admission``.
        file_store: The FileStore to record into.
        case_id: Optional case ID to associate with the file records.

    Returns:
        Number of files recorded.
    """
    count = 0
    for sha, name in result.new_files.items():
        try:
            file_store.record_file(sha, name, case_id)
            count += 1
        except Exception:
            logger.exception("Failed to record file hash %s (%s)", sha[:12], name)
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# RULE 4: count_processable_attachments
# ═══════════════════════════════════════════════════════════════════════════════

def is_inline_image(attachment: AttachmentInfo) -> bool:
    """Check if an attachment is an inline image (e.g., signature logo).

    Inline images are embedded in the HTML body via ``cid:`` references.
    They are NOT documents — they are logos, signature images, and
    decorative elements that should be excluded from attachment counts.
    """
    return (
        attachment.is_inline
        and any(
            attachment.content_type.lower().startswith(prefix)
            for prefix in _IMAGE_CONTENT_PREFIXES
        )
    )


def is_quarantined(
    attachment: AttachmentInfo,
    quarantine_patterns: Sequence[str] = DEFAULT_QUARANTINE_PATTERNS,
) -> bool:
    """Check if an attachment appears to be a quarantine notification.

    Email security systems may replace malicious attachments with a
    notification text file.  Checks attachment name and content type
    against known quarantine indicator patterns.
    """
    name_lower = (attachment.name or "").lower()
    ct_lower = (attachment.content_type or "").lower()
    for pattern in quarantine_patterns:
        if pattern in name_lower or pattern in ct_lower:
            return True
    return False


def is_processable_attachment(
    attachment: AttachmentInfo,
    config: AdmissionConfig = AdmissionConfig(),
) -> bool:
    """Check if an attachment is a processable document.

    An attachment is processable when:
    1. Its ``attachment_type`` is in ``config.processable_attachment_types``
       (default: ``fileAttachment`` only; ``itemAttachment`` and
       ``referenceAttachment`` are excluded because they do not carry
       file bytes).
    2. It has a recognised document extension OR a recognised document
       content-type prefix (if ``document_content_type_prefixes`` is
       non-empty).  If both lists are empty, all file attachments are
       considered processable.

    Args:
        attachment: Normalised attachment.
        config: Admission configuration.

    Returns:
        ``True`` if the attachment is a processable document.
    """
    # Must be a processable attachment type
    if attachment.attachment_type not in config.processable_attachment_types:
        return False

    name_lower = (attachment.name or "").lower()
    ct_lower = (attachment.content_type or "").lower()

    ext_match = any(name_lower.endswith(ext) for ext in config.document_extensions)
    ct_match = any(ct_lower.startswith(pfx) for pfx in config.document_content_type_prefixes)

    if not config.document_extensions and not config.document_content_type_prefixes:
        return True  # No filter configured → accept all

    return ext_match or ct_match


@dataclass
class AttachmentCountResult:
    """Result of processable attachment count (Rule 4)."""
    processable: list[AttachmentInfo] = field(default_factory=list)
    filtered_inline: list[AttachmentInfo] = field(default_factory=list)
    filtered_quarantine: list[AttachmentInfo] = field(default_factory=list)
    filtered_unsupported: list[AttachmentInfo] = field(default_factory=list)
    count: int = 0
    branch: AttachmentBranch = AttachmentBranch.NONE


def count_processable_attachments(
    attachments: Sequence[AttachmentInfo],
    config: AdmissionConfig = AdmissionConfig(),
) -> AttachmentCountResult:
    """Count processable attachments after filtering.

    Removes three categories before counting:

    * **Inline images** — ``isInline: true`` with an ``image/*``
      content type (signature logos, decorative images).
    * **Quarantined files** — attachments whose name or content type
      matches a quarantine indicator pattern.
    * **Unsupported attachment types** — ``itemAttachment``,
      ``referenceAttachment``, or other non-file types that do not carry
      file bytes.  Also filters out attachments that don't match any
      configured document extension or content-type.

    After filtering, the count determines the branch:

    * ``3a`` — no processable attachments
    * ``3b`` — exactly one
    * ``3c`` — two or more

    Args:
        attachments: All attachments from the email.
        config: Admission configuration.

    Returns:
        AttachmentCountResult with processable list, filtered lists,
        count, and branch.
    """
    processable: list[AttachmentInfo] = []
    filtered_inline: list[AttachmentInfo] = []
    filtered_quarantine: list[AttachmentInfo] = []
    filtered_unsupported: list[AttachmentInfo] = []

    for att in attachments:
        if is_inline_image(att):
            filtered_inline.append(att)
            continue
        if is_quarantined(att, config.quarantine_patterns):
            filtered_quarantine.append(att)
            continue
        if not is_processable_attachment(att, config):
            filtered_unsupported.append(att)
            continue
        processable.append(att)

    count = len(processable)
    branch = (
        AttachmentBranch.NONE if count == 0
        else AttachmentBranch.ONE if count == 1
        else AttachmentBranch.SEVERAL
    )

    return AttachmentCountResult(
        processable=processable,
        filtered_inline=filtered_inline,
        filtered_quarantine=filtered_quarantine,
        filtered_unsupported=filtered_unsupported,
        count=count,
        branch=branch,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# RULE 5: body_invoice_check
# ═══════════════════════════════════════════════════════════════════════════════

def has_images_in_html(html_content: str) -> bool:
    """Check if HTML body contains any <img> tags.

    Detects images embedded in the body via:
    - Base64 data URIs: ``<img src="data:image/...;base64,...">``
    - CID references: ``<img src="cid:...">``
    - External URLs: ``<img src="http://...">``

    Used by Rule 5 to detect invoice images that don't appear as
    separate attachments in the Graph API response.

    Args:
        html_content: Raw HTML body content.

    Returns:
        ``True`` if any ``<img>`` tags are found.
    """
    if not html_content:
        return False
    # Case-insensitive search for <img tags
    return bool(re.search(r'<img\s+[^>]*>', html_content, re.IGNORECASE))


def strip_html(html_content: str) -> str:
    """Convert HTML body content to plain text.

    Removes ``<script>`` and ``<style>`` blocks, converts ``<br>``
    and block-level tags to newlines, strips remaining tags, and
    unescapes HTML entities (``&amp;`` → ``&``, etc.).
    """
    if not html_content:
        return ""
    # Remove script and style content
    text = re.sub(
        r'<(script|style)[^>]*>.*?</\1>',
        '',
        html_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Convert line-break tags to newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # Convert block-level tags to newlines
    text = re.sub(
        r'</?(p|div|tr|li|h[1-6]|table)[^>]*>',
        '\n',
        text,
        flags=re.IGNORECASE,
    )
    # Convert table cells to spaces
    text = re.sub(r'</?td[^>]*>', ' ', text, flags=re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Unescape HTML entities
    text = html.unescape(text)
    # Collapse excessive whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


class BodyInvoiceClassifier(ABC):
    """Abstract classifier for body invoice detection (Rule 5).

    Implementations decide whether an email body contains an invoice.
    The default :class:`PatternBasedClassifier` uses weighted regex
    patterns; callers may substitute an ML-based classifier.
    """

    @abstractmethod
    def classify(self, body_text: str) -> bool:
        """Return ``True`` if the body text contains an invoice."""
        ...


class PatternBasedClassifier(BodyInvoiceClassifier):
    """Weighted-regex body invoice classifier.

    Searches the plain-text body for weighted invoice indicator patterns.
    If the total score of matched patterns reaches ``threshold``, the
    body is flagged as containing an invoice.

    This improves on a simple any-match approach by requiring either:
    - One strong pattern (score >= threshold), or
    - Multiple weaker patterns that together reach the threshold.

    Args:
        patterns: Weighted pattern specs.
        threshold: Minimum total score to flag as invoice.
    """

    def __init__(
        self,
        patterns: Sequence[InvoicePatternSpec] = DEFAULT_INVOICE_PATTERNS,
        threshold: int = DEFAULT_INVOICE_THRESHOLD,
    ) -> None:
        self._patterns = list(patterns)
        self._threshold = threshold

    def classify(self, body_text: str) -> bool:
        if not body_text:
            return False
        total = 0
        matched: list[str] = []
        for spec in self._patterns:
            if re.search(spec.pattern, body_text, re.IGNORECASE):
                total += spec.weight
                matched.append(spec.name or spec.pattern)
        if total >= self._threshold:
            if matched:
                logger.debug("Body invoice matched: %s (score=%d)", ", ".join(matched), total)
            return True
        return False


def body_invoice_check(
    body_content: str,
    body_content_type: str = "html",
    classifier: Optional[BodyInvoiceClassifier] = None,
    invoice_patterns: Optional[Sequence[str]] = None,
) -> bool:
    """Check whether an email body contains an invoice (Rule 5).

    Used when an email has **no** processable attachment — the invoice
    may be embedded in the body text rather than attached as a file.

    If a ``classifier`` is provided, it is used directly.  Otherwise, if
    ``invoice_patterns`` (plain regex strings) are provided, a
    :class:`PatternBasedClassifier` is created with weight 1 per pattern
    and threshold 1 (any-match mode, preserving backward compatibility).
    If neither is provided, the default :class:`PatternBasedClassifier`
    with :data:`DEFAULT_INVOICE_PATTERNS` is used.

    Args:
        body_content: Raw body content (HTML or plain text).
        body_content_type: ``"html"`` or ``"text"``.
        classifier: Pluggable classifier instance.
        invoice_patterns: Legacy plain regex patterns (weight=1, any-match).

    Returns:
        ``True`` if the body is classified as containing an invoice.
    """
    if not body_content:
        return False

    # Convert to plain text if HTML
    if body_content_type.lower() == "html":
        text = strip_html(body_content)
    else:
        text = body_content

    if classifier is not None:
        return classifier.classify(text)

    if invoice_patterns is not None:
        # Legacy mode: plain regex patterns, any-match (backward compatible)
        for pattern in invoice_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    # Default: weighted pattern classifier
    default_classifier = PatternBasedClassifier()
    return default_classifier.classify(text)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMISSION RESULT
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AdmissionResult:
    """Complete result of all five admission rule evaluations."""

    # Rule 1 — auto-reply detection
    is_auto_reply: bool = False
    auto_reply_reasons: list[str] = field(default_factory=list)

    # Rule 2 — thread/case matching
    thread_case: Optional[CaseMatch] = None

    # Rule 3 — file deduplication
    file_dedup_results: list[FileDedupResult] = field(default_factory=list)
    known_files: dict[str, str] = field(default_factory=dict)
    new_files: dict[str, str] = field(default_factory=dict)

    # Rule 4 — processable attachment count
    processable_attachments: list[AttachmentInfo] = field(default_factory=list)
    filtered_inline: list[AttachmentInfo] = field(default_factory=list)
    filtered_quarantine: list[AttachmentInfo] = field(default_factory=list)
    filtered_unsupported: list[AttachmentInfo] = field(default_factory=list)
    attachment_count: int = 0
    attachment_branch: AttachmentBranch = AttachmentBranch.NONE

    # Rule 5 — body invoice check
    body_has_invoice: bool = False

    # Final decision
    route: AdmissionRoute = AdmissionRoute.NO_ACTION
    route_reason: str = ""

    def get_summary(self) -> dict[str, Any]:
        """Return a structured summary of key admission rule results.

        Returns a dictionary with the five key fields requested for each
        invoice evaluation:

        Returns:
            dict with keys:
                - is_auto_reply: bool
                - case_id: Optional[str] (None if no thread match)
                - has_known_files: bool (True if any attachments are duplicates)
                - attachment_count: int
                - body_has_invoice: bool
                - route: str (the routing decision)
                - route_reason: str
        """
        return {
            "is_auto_reply": self.is_auto_reply,
            "case_id": self.thread_case.case_id if self.thread_case else None,
            "has_known_files": bool(self.known_files),
            "attachment_count": self.attachment_count,
            "body_has_invoice": self.body_has_invoice,
            "route": self.route.value,
            "route_reason": self.route_reason,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTING LOGIC — Maps rule outputs to workflow steps
# ═══════════════════════════════════════════════════════════════════════════════
# Rules (above) are independent evaluators that return facts about the email.
# Routing logic (below) orchestrates workflow decisions based on those facts.
# This separation ensures rules remain reusable and routing remains centralized.
# ═══════════════════════════════════════════════════════════════════════════════

def _decide_route(
    result: AdmissionResult,
    config: AdmissionConfig = AdmissionConfig(),
) -> tuple[AdmissionRoute, str]:
    """Produce a recommended routing decision from admission results.

    Maps the outputs of **Rules 1-5** (evaluated independently) to routing
    decisions according to the intake workflow. Rules do not make routing
    decisions; this orchestrator does.

    See module docstring for the complete workflow mapping.

    Args:
        result: The AdmissionResult from rule evaluation.
        config: Admission configuration (for closed_case_statuses).

    Returns:
        Tuple of (AdmissionRoute, reason string).
    """
    # ═══════════════════════════════════════════════════════════════════
    # Computed signals from rule outputs
    # ═══════════════════════════════════════════════════════════════════
    has_processable = result.attachment_count > 0
    has_new_files = bool(result.new_files)
    dedup_performed = bool(result.file_dedup_results)
    all_known = has_processable and dedup_performed and not has_new_files
    has_thread_match = result.thread_case is not None
    is_closed_case = (
        has_thread_match
        and result.thread_case.status.lower() in config.closed_case_statuses
    )

    # ═══════════════════════════════════════════════════════════════════
    # ROUTING LOGIC — maps rule outputs to workflow steps
    # ═══════════════════════════════════════════════════════════════════

    # ── Workflow Step 6: Closed-case match ─────────────────────────────
    # Takes ABSOLUTE precedence — even over auto-reply. A reply to a
    # closed case (even an OOF) must be raised for AP. The case is NOT
    # reopened.
    if is_closed_case:
        return (
            AdmissionRoute.RAISE_AP,
            f"[Step 6] Thread matches closed case '{result.thread_case.case_id}' "
            f"(via {result.thread_case.matched_by}) — "
            "raise for AP review, do not reopen",
        )

    # ── Auto-reply handling (no closed-case match) ─────────────────────
    # Not explicitly a workflow step, but critical for preventing
    # auto-closure of emails with invoice content.
    if result.is_auto_reply:
        if has_processable:
            return (
                AdmissionRoute.TRIAGE,
                f"[Auto-reply + Step 5] Processable attachment(s) present "
                f"({result.attachment_count}) — send to triage for invoice "
                "extraction, do not auto-close",
            )
        if result.body_has_invoice:
            return (
                AdmissionRoute.TRIAGE,
                "[Auto-reply + Step 7] Invoice detected in body — "
                "send to triage, do not auto-close",
            )
        return (
            AdmissionRoute.AUTO_CLOSE,
            f"[Auto-reply] No invoice content ({', '.join(result.auto_reply_reasons)})",
        )

    # ── Workflow Step 3 vs Step 5: Thread match — decision depends on ──
    # whether there are new/distinct files.
    #
    # Step 3: Thread match + all files known → LINK_TO_CASE
    #     Link the submission to the existing case as additional evidence.
    #     No new case is created.
    #
    # Step 5: Thread match + new distinct files → TRIAGE (new case)
    #     Despite the thread association, distinct evidence requires a
    #     NEW case. The thread match is retained as evidence linking the
    #     two cases.
    if has_thread_match:
        # If there are new files, fall through to TRIAGE (Step 5)
        # despite the thread match
        if has_new_files:
            # Fall through — will be handled by TRIAGE logic below
            pass
        else:
            # Step 3: No new files, link to existing case
            return (
                AdmissionRoute.LINK_TO_CASE,
                f"[Step 3] Thread matches open case '{result.thread_case.case_id}' "
                f"(via {result.thread_case.matched_by}), no new distinct files — "
                "link as additional evidence",
            )

    # ── Workflow Step 4: All attachments are duplicates ────────────────
    # (no thread match). All processable attachments are byte-identical
    # to previously received files.
    if all_known:
        return (
            AdmissionRoute.LINK_FILES,
            f"[Step 4] All {len(result.known_files)} processable attachment(s) "
            "are byte-identical to previously received files — "
            "link files, do not re-extract",
        )

    # ── Workflow Step 5: New distinct documents ────────────────────────
    # Covers two scenarios:
    #   (a) Thread match + new files (fell through from above)
    #   (b) No thread match + new files
    # Both require triage and a new case.
    if has_processable:
        if has_thread_match:
            # Scenario (a): thread match but new files present
            return (
                AdmissionRoute.TRIAGE,
                f"[Step 5] Thread matches case '{result.thread_case.case_id}' "
                f"but {len(result.new_files)} new/distinct file(s) present — "
                "create new case, retain thread association as evidence",
            )
        else:
            # Scenario (b): no thread match, new files
            return (
                AdmissionRoute.TRIAGE,
                f"[Step 5] Branch {result.attachment_branch.value}: "
                f"{result.attachment_count} processable attachment(s), "
                f"{len(result.new_files)} new — create case and send to triage",
            )

    # ── Workflow Step 7: Invoice in body (no attachments) ──────────────
    if result.body_has_invoice:
        return (
            AdmissionRoute.BODY_INVOICE,
            "[Step 7] No processable attachment but invoice detected in body",
        )

    # ── Workflow Step 8: Nothing processable ───────────────────────────
    return (
        AdmissionRoute.NO_ACTION,
        "[Step 8] No processable content found (no attachments, no body invoice)",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_admission(
    headers: EmailHeaders,
    envelope: EmailEnvelope,
    attachments: Sequence[AttachmentInfo],
    body_content: str,
    body_content_type: str,
    case_store: Optional[CaseStore] = None,
    file_store: Optional[FileStore] = None,
    *,
    config: AdmissionConfig = AdmissionConfig(),
    classifier: Optional[BodyInvoiceClassifier] = None,
    invoice_patterns: Optional[Sequence[str]] = None,
) -> AdmissionResult:
    """Run all five admission rules and produce a routing decision.

    This is the main entry point.  Pass normalised data (not raw Graph
    API JSON) and optional store implementations; the function evaluates
    every rule, combines the results, and returns a recommended route.

    ``case_store`` and ``file_store`` are optional — if ``None``, the
    corresponding rule (thread matching / file dedup) is skipped.

    Args:
        headers: Normalised headers for auto-reply detection (Rule 1).
        envelope: Normalised envelope for thread matching (Rule 2).
        attachments: All attachments from the email (Rules 3 & 4).
        body_content: Full body content of the email (Rule 5).
        body_content_type: ``"html"`` or ``"text"`` (Rule 5).
        case_store: Case lookup backend (Rule 2).  Pass ``None`` to skip.
        file_store: File deduplication backend (Rule 3).  Pass ``None`` to skip.
        config: Central configuration for all rules.
        classifier: Pluggable body invoice classifier (Rule 5).
            If ``None``, a default :class:`PatternBasedClassifier` is used.
        invoice_patterns: Legacy plain regex patterns for backward
            compatibility.  Ignored if ``classifier`` is provided.

    Returns:
        AdmissionResult with all rule outputs and a recommended route.
    """
    result = AdmissionResult()

    # ── Rule 1: is_auto_reply ──────────────────────────────────────────────
    try:
        auto = is_auto_reply(headers, config)
        result.is_auto_reply = auto.is_auto_reply
        result.auto_reply_reasons = auto.reasons
    except Exception:
        logger.exception("Rule 1 (is_auto_reply) failed — treating as not auto-reply")

    # ── Rule 2: find_thread_case ───────────────────────────────────────────
    if case_store is not None:
        try:
            result.thread_case = find_thread_case(envelope, case_store)
        except Exception:
            logger.exception("Rule 2 (find_thread_case) failed — no case match")

    # ── Rule 4: count_processable_attachments ──────────────────────────────
    try:
        att_result = count_processable_attachments(attachments, config)
        result.processable_attachments = att_result.processable
        result.filtered_inline = att_result.filtered_inline
        result.filtered_quarantine = att_result.filtered_quarantine
        result.filtered_unsupported = att_result.filtered_unsupported
        result.attachment_count = att_result.count
        result.attachment_branch = att_result.branch
    except Exception:
        logger.exception("Rule 4 (count_processable_attachments) failed")

    # ── Rule 3: is_known_file (per processable attachment) ─────────────────
    if file_store is not None:
        for att in result.processable_attachments:
            try:
                dedup = check_known_file(att, file_store)
                result.file_dedup_results.append(dedup)
                if dedup.sha256:
                    if dedup.is_known:
                        result.known_files[dedup.sha256] = att.name
                    else:
                        result.new_files[dedup.sha256] = att.name
            except Exception:
                logger.exception("Rule 3 (check_known_file) failed for '%s'", att.name)

    # ── Rule 5: body_invoice_check ──────────────────────────────────────────
    # Three detection paths (in order of specificity):
    # 1. Inline attachments (separate attachment with is_inline=True)
    # 2. Images embedded directly in HTML body (<img> tags)
    # 3. Text pattern matching (only when no attachments)
    if result.filtered_inline:
        result.body_has_invoice = True
    elif body_content_type.lower() == "html" and has_images_in_html(body_content):
        result.body_has_invoice = True
    elif result.attachment_branch == AttachmentBranch.NONE:
        try:
            result.body_has_invoice = body_invoice_check(
                body_content, body_content_type, classifier, invoice_patterns,
            )
        except Exception:
            logger.exception("Rule 5 (body_invoice_check) failed")

    # ── Decision ──────────────────────────────────────────────────────────
    result.route, result.route_reason = _decide_route(result, config)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# MS GRAPH API CONVERSION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def graph_headers_to_dict(message: dict[str, Any]) -> dict[str, str]:
    """Extract ``internetMessageHeaders`` from a Graph message into a flat dict.

    Graph returns headers as::

        "internetMessageHeaders": [
            {"name": "Auto-Submitted", "value": "auto-replied"},
            ...
        ]

    Keys are lowercased for case-insensitive lookup.  Malformed entries
    (missing name or value) are skipped with a debug log.
    """
    headers_map: dict[str, str] = {}
    raw_headers = message.get("internetMessageHeaders", [])
    if not isinstance(raw_headers, list):
        logger.warning("internetMessageHeaders is not a list: %r", type(raw_headers))
        return headers_map
    for h in raw_headers:
        if not isinstance(h, dict):
            continue
        name = h.get("name", "")
        value = h.get("value", "")
        if name and value:
            headers_map[name.lower()] = value
    return headers_map


def graph_message_to_headers(message: dict[str, Any]) -> EmailHeaders:
    """Convert a Graph API message to normalised :class:`EmailHeaders` (Rule 1)."""
    h = graph_headers_to_dict(message)
    return EmailHeaders(
        auto_submitted=h.get("auto-submitted"),
        precedence=h.get("precedence"),
        x_auto_response_suppress=h.get("x-auto-response-suppress"),
    )


def graph_message_to_envelope(message: dict[str, Any]) -> EmailEnvelope:
    """Convert a Graph API message to normalised :class:`EmailEnvelope` (Rule 2).

    Requires the Graph message to include ``conversationId`` and
    ``internetMessageHeaders`` in its ``$select`` parameter.
    """
    h = graph_headers_to_dict(message)

    # Parse References — space-separated <msg-id> tokens
    references: list[str] = []
    refs_raw = h.get("references", "")
    if refs_raw:
        references = re.findall(r'<[^>]+>', refs_raw)

    # In-Reply-To — single <msg-id>
    in_reply_to = h.get("in-reply-to", "").strip() or None

    # Sender email
    sender = ""
    from_obj = message.get("from")
    if isinstance(from_obj, dict):
        sender = from_obj.get("emailAddress", {}).get("address", "")

    return EmailEnvelope(
        conversation_id=message.get("conversationId"),
        internet_message_id=message.get("internetMessageId"),
        in_reply_to=in_reply_to,
        references=references,
        subject=message.get("subject", ""),
        sender=sender,
    )


def graph_attachment_to_info(attachment: dict[str, Any]) -> AttachmentInfo:
    """Convert a Graph API attachment object to :class:`AttachmentInfo`.

    Handles missing or malformed fields gracefully:
    - ``@odata.type`` defaults to ``"fileAttachment"`` if absent.
    - ``size`` defaults to ``0`` if not an integer.
    - ``isInline`` defaults to ``False``.
    """
    # Detect attachment type from @odata.type
    odata_type = attachment.get("@odata.type", "")
    if isinstance(odata_type, str) and odata_type:
        att_type = odata_type.split(".")[-1]
    else:
        att_type = "fileAttachment"

    # Size: validate it's an integer
    raw_size = attachment.get("size", 0)
    size = raw_size if isinstance(raw_size, int) else 0

    # isInline: validate it's a boolean
    raw_inline = attachment.get("isInline", False)
    is_inline = bool(raw_inline) if isinstance(raw_inline, (bool, int)) else False

    return AttachmentInfo(
        id=str(attachment.get("id", "")),
        name=attachment.get("name", "") or "",
        content_type=attachment.get("contentType", "") or "",
        size=size,
        is_inline=is_inline,
        content_id=attachment.get("contentId"),
        content_bytes=attachment.get("contentBytes"),
        attachment_type=att_type,
    )


def graph_attachments_to_info_list(
    attachments: list[dict[str, Any]],
) -> list[AttachmentInfo]:
    """Convert a list of Graph API attachment objects to ``AttachmentInfo`` list.

    Malformed entries (non-dict items) are skipped with a warning.
    """
    result: list[AttachmentInfo] = []
    if not isinstance(attachments, list):
        logger.warning("attachments is not a list: %r", type(attachments))
        return result
    for a in attachments:
        if not isinstance(a, dict):
            logger.warning("Skipping non-dict attachment: %r", type(a))
            continue
        try:
            result.append(graph_attachment_to_info(a))
        except Exception:
            logger.exception("Failed to convert attachment to AttachmentInfo")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE: Full Graph API evaluation in one call
# ═══════════════════════════════════════════════════════════════════════════════

def graph_evaluate_admission(
    message: dict[str, Any],
    attachments: list[dict[str, Any]],
    case_store: Optional[CaseStore] = None,
    file_store: Optional[FileStore] = None,
    *,
    config: AdmissionConfig = AdmissionConfig(),
    classifier: Optional[BodyInvoiceClassifier] = None,
    invoice_patterns: Optional[Sequence[str]] = None,
) -> AdmissionResult:
    """Evaluate admission rules directly from MS Graph API JSON.

    Convenience wrapper that converts Graph API message and attachment
    JSON to normalised data, then calls :func:`evaluate_admission`.

    The Graph message must include ``internetMessageHeaders``,
    ``conversationId``, ``internetMessageId``, and ``body`` in its
    ``$select`` parameter::

        $select=id,subject,from,conversationId,internetMessageId,
                internetMessageHeaders,body,hasAttachments

    Attachments come from::

        GET /v1.0/users/{user_id}/messages/{message_id}/attachments

    Args:
        message: Raw Graph API message object.
        attachments: Raw Graph API attachment objects (list of dicts).
        case_store: Case lookup backend.  Pass ``None`` to skip Rule 2.
        file_store: File deduplication backend.  Pass ``None`` to skip Rule 3.
        config: Central configuration for all rules.
        classifier: Pluggable body invoice classifier (Rule 5).
        invoice_patterns: Legacy plain regex patterns (backward compat).

    Returns:
        AdmissionResult with all rule outputs and recommended route.
    """
    headers = graph_message_to_headers(message)
    envelope = graph_message_to_envelope(message)
    att_infos = graph_attachments_to_info_list(attachments)

    body_obj = message.get("body", {})
    body_content = body_obj.get("content", "") if isinstance(body_obj, dict) else ""
    body_type = body_obj.get("contentType", "html") if isinstance(body_obj, dict) else "html"

    return evaluate_admission(
        headers=headers,
        envelope=envelope,
        attachments=att_infos,
        body_content=body_content,
        body_content_type=body_type,
        case_store=case_store,
        file_store=file_store,
        config=config,
        classifier=classifier,
        invoice_patterns=invoice_patterns,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST / DEMO: Proper store population for all three scenarios
# ═══════════════════════════════════════════════════════════════════════════════

def _run_scenario(label: str, result: AdmissionResult) -> None:
    """Print a summary of an AdmissionResult for the demo."""
    summary = result.get_summary()
    
    print(f"\n{'=' * 70}")
    print(f"  SCENARIO: {label}")
    print(f"{'=' * 70}")
    print("\n  KEY RESULTS (for each invoice):")
    print(f"  1. is_auto_reply              : {summary['is_auto_reply']}")
    print(f"  2. find_thread_case (case_id) : {summary['case_id']}")
    print(f"  3. is_known_file (has dupes)  : {summary['has_known_files']}")
    print(f"  4. count_processable_attach   : {summary['attachment_count']}")
    print(f"  5. body_invoice_check         : {summary['body_has_invoice']}")
    print(f"\n  ROUTING DECISION:")
    print(f"  → Route  : {summary['route']}")
    print(f"  → Reason : {summary['route_reason']}")
    
    # Detailed breakdown
    print(f"\n  DETAILED BREAKDOWN:")
    if result.auto_reply_reasons:
        print(f"  • Auto-reply reasons: {result.auto_reply_reasons}")
    if result.thread_case:
        print(f"  • Thread match: case '{result.thread_case.case_id}' (via {result.thread_case.matched_by})")
    if result.known_files:
        print(f"  • Known files ({len(result.known_files)}): {list(result.known_files.values())}")
    if result.new_files:
        print(f"  • New files ({len(result.new_files)}): {list(result.new_files.values())}")
    print(f"  • Attachment branch: {result.attachment_branch.value}")
    if result.filtered_inline:
        print(f"  • Inline images: {len(result.filtered_inline)}")
    if result.filtered_quarantine:
        print(f"  • Quarantined: {len(result.filtered_quarantine)}")
    if result.filtered_unsupported:
        print(f"  • Unsupported: {len(result.filtered_unsupported)}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    # ── Shared stores and config ────────────────────────────────────────────
    cfg = AdmissionConfig()
    case_store = InMemoryCaseStore()
    file_store = InMemoryFileStore()

    # ── Simulate a PDF invoice attachment (base64-encoded minimal PDF) ─────
    # A tiny valid PDF so compute_sha256 produces a real hash.
    fake_pdf_bytes = b"%PDF-1.4\n%test invoice\n%%EOF"
    fake_pdf_b64 = base64.b64encode(fake_pdf_bytes).decode()
    invoice_attachment = AttachmentInfo(
        name="invoice_001.pdf",
        content_type="application/pdf",
        size=len(fake_pdf_bytes),
        is_inline=False,
        content_bytes=fake_pdf_b64,
        attachment_type="fileAttachment",
    )

    # ── Scenario 1: Direct email (no reply, no attachment) ─────────────────
    # Body contains invoice text → should route to BODY_INVOICE
    direct_headers = EmailHeaders()
    direct_envelope = EmailEnvelope(
        conversation_id="conv-direct-001",
        internet_message_id="<direct-001@example.com>",
        subject="Invoice for March 2025",
    )
    result1 = evaluate_admission(
        headers=direct_headers,
        envelope=direct_envelope,
        attachments=[],
        body_content="Invoice #INV-123 Amount due: $500 Due date: 2025-04-15",
        body_content_type="text",
        case_store=case_store,
        file_store=file_store,
        config=cfg,
    )
    _run_scenario("1. Direct email (no attachment, invoice in body)", result1)

    # After processing: register the case and record the body invoice file
    # (no file to record here, but we register the case for Rule 2 later)
    case_store.add_case(
        case_id="CASE-001",
        status="open",
        conversation_id="conv-direct-001",
        message_ids=["<direct-001@example.com>"],
    )

    # ── Scenario 2: Reply to existing email (no attachment) ────────────────
    # Should trigger Rule 2 (find_thread_case) → LINK_TO_CASE
    reply_headers = EmailHeaders()
    reply_envelope = EmailEnvelope(
        conversation_id="conv-direct-001",          # same thread
        internet_message_id="<reply-001@example.com>",
        in_reply_to="<direct-001@example.com>",     # replies to original
        references=["<direct-001@example.com>"],
        subject="RE: Invoice for March 2025",
    )
    result2 = evaluate_admission(
        headers=reply_headers,
        envelope=reply_envelope,
        attachments=[],
        body_content="Please find attached the revised invoice.",
        body_content_type="text",
        case_store=case_store,
        file_store=file_store,
        config=cfg,
    )
    _run_scenario("2. Reply to existing email (triggers Rule 2 → LINK_TO_CASE)", result2)

    # ── Scenario 3: Reply with same invoice attached ───────────────────────
    # First, we need the file to be "known" — so we process an email with
    # the attachment first, then record it in file_store, then send the
    # reply with the identical attachment.

    # Step 3a: Original email WITH the invoice attachment (new file)
    orig3_headers = EmailHeaders()
    orig3_envelope = EmailEnvelope(
        conversation_id="conv-attach-001",
        internet_message_id="<orig-attach-001@example.com>",
        subject="Invoice attached",
    )
    result3a = evaluate_admission(
        headers=orig3_headers,
        envelope=orig3_envelope,
        attachments=[invoice_attachment],
        body_content="<html>Invoice attached</html>",
        body_content_type="html",
        case_store=case_store,
        file_store=file_store,
        config=cfg,
    )
    _run_scenario("3a. Original email with invoice PDF (new file → TRIAGE)", result3a)

    # Record the new file and register the case
    record_new_files(result3a, file_store, case_id="CASE-002")
    case_store.add_case(
        case_id="CASE-002",
        status="open",
        conversation_id="conv-attach-001",
        message_ids=["<orig-attach-001@example.com>"],
    )

    # Step 3b: Reply with the SAME invoice attached — should trigger BOTH:
    #   Rule 2 (thread match → LINK_TO_CASE, since case is open)
    #   Rule 3 (file dedup → file is known)
    # Rule 2 takes precedence (precedence item 5 before item 6).
    reply3_headers = EmailHeaders()
    reply3_envelope = EmailEnvelope(
        conversation_id="conv-attach-001",          # same thread
        internet_message_id="<reply-attach-001@example.com>",
        in_reply_to="<orig-attach-001@example.com>",
        references=["<orig-attach-001@example.com>"],
        subject="RE: Invoice attached",
    )
    result3b = evaluate_admission(
        headers=reply3_headers,
        envelope=reply3_envelope,
        attachments=[invoice_attachment],   # identical bytes
        body_content="<html>Resending the same invoice</html>",
        body_content_type="html",
        case_store=case_store,
        file_store=file_store,
        config=cfg,
    )
    _run_scenario(
        "3b. Reply with SAME invoice (Rule 2 → LINK_TO_CASE + Rule 3 → known file)",
        result3b,
    )

    # ── Scenario 4: Direct email with same invoice (no case match) ─────────
    # Should trigger Rule 3 only → LINK_FILES (all known, no thread match)
    direct4_headers = EmailHeaders()
    direct4_envelope = EmailEnvelope(
        conversation_id="conv-different-999",     # different thread
        internet_message_id="<direct-004@example.com>",
        subject="Here is the invoice again",
    )
    result4 = evaluate_admission(
        headers=direct4_headers,
        envelope=direct4_envelope,
        attachments=[invoice_attachment],   # identical bytes
        body_content="<html>Sending the same invoice</html>",
        body_content_type="html",
        case_store=case_store,
        file_store=file_store,
        config=cfg,
    )
    _run_scenario(
        "4. Direct email with same invoice (no case match → Rule 3 → LINK_FILES)",
        result4,
    )

    # ── Scenario 5: Direct email with inline image in body ─────────────────
    # An inline image (is_inline=True, content_type=image/png) embedded in
    # the body via cid: reference.  No processable attachments.
    # Should trigger Rule 5 via filtered_inline → BODY_INVOICE.
    inline_image = AttachmentInfo(
        name="invoice_screenshot.png",
        content_type="image/png",
        size=1024,
        is_inline=True,
        content_id="invoice_screenshot",
        content_bytes=base64.b64encode(b"fake-png-data").decode(),
        attachment_type="fileAttachment",
    )
    inline5_headers = EmailHeaders()
    inline5_envelope = EmailEnvelope(
        conversation_id="conv-inline-001",
        internet_message_id="<inline-001@example.com>",
        subject="Invoice screenshot",
    )
    result5 = evaluate_admission(
        headers=inline5_headers,
        envelope=inline5_envelope,
        attachments=[inline_image],
        body_content="<html><img src='cid:invoice_screenshot'></html>",
        body_content_type="html",
        case_store=case_store,
        file_store=file_store,
        config=cfg,
    )
    _run_scenario(
        "5. Direct email with inline image (Rule 5 via filtered_inline → BODY_INVOICE)",
        result5,
    )

    # ── Scenario 6: Invoice HTML with embedded image (no attachment) ────────
    # Real-world case: invoice image embedded directly in HTML via base64 or URL.
    # The image doesn't appear as a separate attachment in Graph API.
    # Should trigger Rule 5 via has_images_in_html() → BODY_INVOICE.
    invoice_html = """
    <html>
    <head><title>Tax Invoice</title></head>
    <body>
        <h1>Testing inline-body invoice:</h1>
        <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==">
        <img src="https://example.com/invoice-logo.png">
        <div>Invoice # 001125</div>
        <div>Amount Due: 12,420.00 SAR</div>
    </body>
    </html>
    """
    html6_headers = EmailHeaders()
    html6_envelope = EmailEnvelope(
        conversation_id="conv-html-001",
        internet_message_id="<html-001@example.com>",
        subject="Invoice 001125",
    )
    result6 = evaluate_admission(
        headers=html6_headers,
        envelope=html6_envelope,
        attachments=[],  # No attachments - image is in HTML
        body_content=invoice_html,
        body_content_type="html",
        case_store=case_store,
        file_store=file_store,
        config=cfg,
    )
    _run_scenario(
        "6. Invoice HTML with embedded images (Rule 5 via has_images_in_html → BODY_INVOICE)",
        result6,
    )