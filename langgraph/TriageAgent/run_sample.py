"""Sample Data Runner for the AP Invoice Triage Agent
===================================================

Thin runner that loads JSON sample data and passes it through the
triage_agent pipeline via the SACC mock API.

Does NOT derive or map any fields — just converts the JSON data format
to DocumentEntity format and calls triage_agent's pipeline functions.
Missing fields stay empty.

Supports:
  - sample_data.json: DocumentEntity format (pass-through)
  - doc_ai_payload.json: SAP Document AI format (headerFields -> DocumentEntity)
  - Any JSON with headerFields/lineItems

Prerequisite:
    The SACC mock API server must be running at http://localhost:8000.

Usage:
    cd /Users/mohammadtahakhan20@gmail.com/SACC/langgraph/TriageAgent
    python run_sample.py                              # uses sample_data.json
    python run_sample.py doc_ai_payload.json           # uses 1st doc from payload
    python run_sample.py doc_ai_payload.json 2         # uses 3rd doc (0-indexed)
    python run_sample.py doc_ai_payload.json all       # processes all docs
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from triage_agent import (
    TriageState,
    DocumentType,
    CompanyClassification,
    DocAIApiClient,
    upload_document,
    classify_with_document_api,
    determine_document_type,
    classify_company,
    classify_direct_intercompany,
    classify_invoice_type,
    extract_detailed_fields,
    validate_extraction,
    build_triage_result,
    review_node,
    _VALID_AP_TYPES,
)
import triage_agent

# -- Logging ----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_sample")


# -- Payload Conversion (format only, no mapping/derivation) ----------------


def _convert_header_fields(header_fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a headerFields dict to DocumentEntity list.

    Only converts the data format — does NOT map or derive any field names.
    """
    entities: list[dict[str, Any]] = []
    for name, field_data in header_fields.items():
        if isinstance(field_data, dict) and ("value" in field_data or "confidence" in field_data):
            value = field_data.get("value", "")
            confidence = float(field_data.get("confidence", 0.0) or 0.0)
        else:
            value = field_data
            confidence = 0.85

        entity: dict[str, Any] = {"name": name, "confidence": confidence}
        if isinstance(value, bool):
            entity["type"] = "string"
            entity["stringValue"] = str(value)
        elif isinstance(value, (int, float)):
            entity["type"] = "number"
            entity["numberValue"] = value
        else:
            entity["type"] = "string"
            entity["stringValue"] = str(value) if value is not None else ""
        entities.append(entity)
    return entities


def _normalize_payload(raw_data: dict[str, Any], doc_index: int = 0) -> dict[str, Any]:
    """Convert JSON payload to the format triage_agent expects.

    Does NOT derive or map any fields — just converts the data format.
    Missing fields are left empty.
    """
    # sample_data.json format — already in DocumentEntity format
    if "classification_extraction" in raw_data:
        return {
            "classification_extraction": raw_data.get("classification_extraction", []),
            "detailed_extraction": raw_data.get("detailed_extraction", []),
            "invoice_source": raw_data.get("invoice_source", {}),
            "line_items": raw_data.get("line_items", []),
        }

    # doc_ai_payload.json format — has processed[] array
    if "processed" in raw_data:
        processed = raw_data["processed"]
        if not processed:
            raise ValueError("No 'processed' documents in payload")
        if doc_index >= len(processed):
            raise ValueError(f"doc_index {doc_index} out of range (0-{len(processed) - 1})")
        doc = processed[doc_index]
    elif "headerFields" in raw_data or "header_fields" in raw_data:
        doc = raw_data
    else:
        # Generic — treat entire dict as header fields
        doc = {
            "attachment": raw_data.get("file_name", raw_data.get("attachment", "")),
            "headerFields": {
                k: v for k, v in raw_data.items()
                if k not in ("lineItems", "line_items") and isinstance(v, (str, int, float, dict))
            },
            "lineItems": raw_data.get("lineItems", raw_data.get("line_items", [])),
        }

    header_fields = doc.get("headerFields", doc.get("header_fields", {}))
    line_items = doc.get("lineItems", doc.get("line_items", []))

    entities = _convert_header_fields(header_fields)

    # Build invoice_source from what's in the payload (no derivation)
    values: dict[str, str] = {}
    for e in entities:
        val = e.get("stringValue") or str(e.get("numberValue", "") or "") or ""
        if val:
            values[e["name"]] = str(val)

    invoice_source = {
        "file_name": doc.get("attachment", ""),
    }
    # Check both naming conventions for common fields (no derivation, just lookup)
    for key, alt_keys in {
        "invoice_number": ["invoice_number", "documentNumber"],
        "invoice_date": ["invoice_date", "documentDate"],
        "supplier_name": ["supplier_name", "senderName"],
        "company_name": ["company_name", "receiverName"],
        "company_code": ["company_code"],
        "total_amount": ["total_amount", "grossAmount"],
        "currency": ["currency", "currencyCode"],
        "net_amount": ["net_amount", "netAmount"],
        "tax_amount": ["tax_amount"],
        "po_number": ["po_number", "purchaseOrderNumber"],
        "payment_terms": ["payment_terms", "paymentTerms"],
        "keywords": ["keywords"],
    }.items():
        for alt in alt_keys:
            if alt in values:
                invoice_source[key] = values[alt]
                break
        else:
            invoice_source[key] = ""

    return {
        "classification_extraction": entities,
        "detailed_extraction": entities,
        "invoice_source": invoice_source,
        "line_items": line_items,
    }


# -- Sample Data API Client (extends real DocAIApiClient) --------------------


class SampleDataApiClient(DocAIApiClient):
    """Extends the real DocAIApiClient to push normalised data through the API.

    All HTTP calls go to the real SACC mock server at localhost:8000.
    After trigger_processing creates a DocumentVersion, this client POSTs
    the normalised DocumentEntities to the server so that
    get_extraction_results() can GET them back.
    """

    def __init__(
        self,
        base_url: str | None = None,
        classification_extraction: list[dict[str, Any]] | None = None,
        detailed_extraction: list[dict[str, Any]] | None = None,
    ):
        super().__init__(base_url or triage_agent.API_BASE_URL)
        self._processing_call_count = 0
        self._classification_extraction = classification_extraction or []
        self._detailed_extraction = detailed_extraction or []

    def set_extraction_data(
        self,
        classification_extraction: list[dict[str, Any]],
        detailed_extraction: list[dict[str, Any]],
    ) -> None:
        """Update the extraction data (for reusing the client across documents)."""
        self._classification_extraction = classification_extraction
        self._detailed_extraction = detailed_extraction
        self._processing_call_count = 0

    def trigger_processing(self, document_id: str) -> dict[str, Any]:
        """Trigger processing, then POST normalised DocumentEntities to the server.

        First call  -> POSTs classification extraction data (Step 2)
        Second call -> POSTs detailed extraction data (Step 9)
        """
        result = super().trigger_processing(document_id)
        version_id = result.get("document_version_id", str(uuid.uuid4()))

        self._processing_call_count += 1
        if self._processing_call_count == 1:
            sample_entities = self._classification_extraction
            logger.info(
                "  [SAMPLE] POSTing %d classification entities to server for version %s",
                len(sample_entities), version_id,
            )
        else:
            sample_entities = self._detailed_extraction
            logger.info(
                "  [SAMPLE] POSTing %d detailed entities to server for version %s",
                len(sample_entities), version_id,
            )

        for entity in sample_entities:
            payload = {
                "ID": str(uuid.uuid4()),
                "documentVersion_ID": version_id,
                **entity,
            }
            try:
                self.create_document_entity(payload)
            except Exception as e:
                logger.warning("  [SAMPLE] Failed to POST entity %s: %s", entity.get("name"), e)

        return result


# -- Inject the sample data client into triage_agent -------------------------


_sample_client = SampleDataApiClient()
triage_agent._doc_ai_client = _sample_client


def _patched_get_doc_ai_client():
    return _sample_client


triage_agent._get_doc_ai_client = _patched_get_doc_ai_client


# -- Run the pipeline --------------------------------------------------------


def run_sample_triage(normalized_data: dict[str, Any]) -> dict[str, Any]:
    """Run the triage_agent pipeline with normalised data via the real API."""
    invoice_source = normalized_data["invoice_source"]
    classification_extraction = normalized_data["classification_extraction"]
    detailed_extraction = normalized_data["detailed_extraction"]
    actual_line_items = normalized_data.get("line_items", [])

    # Update the sample client with this document's extraction data
    _sample_client.set_extraction_data(classification_extraction, detailed_extraction)

    print("\n" + "=" * 80)
    print("  AP INVOICE TRIAGE AGENT -- SAMPLE DATA RUN (via SACC mock API)")
    print("  API: %s" % triage_agent.API_BASE_URL)
    print(
        "  Invoice: %s from %s"
        % (invoice_source.get("invoice_number", "?"), invoice_source.get("supplier_name", "?"))
    )
    print(
        "  Total: %s %s | Company: %s"
        % (
            invoice_source.get("total_amount", "?"),
            invoice_source.get("currency", "?"),
            invoice_source.get("company_name", "?"),
        )
    )
    print("=" * 80)

    # -- Initialise state (same as run_triage) --------------------------------
    state: TriageState = TriageState(
        file_path="",
        file_name=invoice_source.get("file_name", "unknown.pdf"),
        file_content=None,
        errors=[],
        evidence=[],
        header_fields={},
        line_items=[],
        field_confidences={},
        missing_required_fields=[],
        low_confidence_fields=[],
        review_required=False,
        review_reason=None,
        confidence=0.0,
    )

    # -- Steps 1-3: Upload & classify (always run) --------------------
    print("\n--- Steps 1-3: Upload & Classification ---")
    state = upload_document(state)
    state = classify_with_document_api(state)
    state = determine_document_type(state)

    _print_step_summary("Step 3 -- Document Type", state, ["document_type"])

    # -- Step 4: Branch on document type -----------------------------------
    if state.get("document_type", DocumentType.OTHER.value) not in _VALID_AP_TYPES:
        print("\n--- Step 4: Document type not valid AP -> REVIEW ---")
        state = review_node(state)
        state = build_triage_result(state)
        return state.get("triage_result", {})

    print("\n--- Step 4: Document type is valid AP -> continuing ---")

    # -- Step 5: Classify company ------------------------------------------
    print("\n--- Step 5: Company Classification ---")
    state = classify_company(state)
    _print_step_summary("Step 5 -- Company Classification", state, ["company_classification"])

    # -- Step 6: Branch on company classification ---------------------------
    if state.get("company_classification") == CompanyClassification.NO_MATCH.value:
        print("\n--- Step 6: Company NO MATCH -> REVIEW ---")
        state = review_node(state)
        state = build_triage_result(state)
        return state.get("triage_result", {})

    print("\n--- Step 6: Company match/uncertain -> continuing ---")

    # -- Steps 7-8, 9-10: Extraction pipeline for valid AP documents ----
    print("\n--- Step 7: Direct vs Intercompany ---")
    state = classify_direct_intercompany(state)
    _print_step_summary("Step 7 -- Direct/Intercompany", state, ["direct_intercompany"])

    print("\n--- Step 8: Invoice Type ---")
    state = classify_invoice_type(state)
    _print_step_summary("Step 8 -- Invoice Type", state, ["invoice_type"])

    print("\n--- Step 9: Extract Detailed Fields ---")
    state = extract_detailed_fields(state)
    _print_step_summary("Step 9 -- Header Fields", state, ["header_fields", "line_items"])

    # Inject actual line items from source data (replaces simulated ones)
    if actual_line_items:
        state["line_items"] = actual_line_items
        logger.info("Injected %d actual line items from source data", len(actual_line_items))

    print("\n--- Step 10: Validate Extraction ---")
    state = validate_extraction(state)
    _print_step_summary(
        "Step 10 -- Validation", state, ["confidence", "review_required", "review_reason"]
    )

    # -- Step 11: Build final result ---------------------------------------
    print("\n--- Step 11: Build Triage Result ---")
    state = build_triage_result(state)

    return state.get("triage_result", {})


def _print_step_summary(label: str, state: TriageState, keys: list[str]):
    """Print a summary of key state values after a step."""
    print(f"  {label}:")
    for key in keys:
        val = state.get(key)
        if isinstance(val, dict):
            print(f"    {key}:")
            for k, v in val.items():
                print(f"      {k}: {v}")
        elif isinstance(val, list):
            print(f"    {key}: [{len(val)} items]")
            for i, item in enumerate(val):
                if isinstance(item, dict):
                    print(f"      [{i}] {item}")
                else:
                    print(f"      [{i}] {item}")
        else:
            print(f"    {key}: {val}")


def _print_result(result: dict[str, Any], doc_label: str = "") -> None:
    """Print the triage result, evidence trail, and API call summary."""
    print("\n" + "=" * 80)
    print(f"  FINAL TRIAGE RESULT (JSON){doc_label}")
    print("=" * 80)
    print(json.dumps(result, indent=2, default=str))

    print("\n" + "=" * 80)
    print("  EVIDENCE TRAIL")
    print("=" * 80)
    evidence = result.get("evidence", [])
    for i, e in enumerate(evidence, 1):
        print(f"  {i:2d}. {e}")

    print("\n" + "=" * 80)
    print("  API CALL SUMMARY (real HTTP calls to %s)" % triage_agent.API_BASE_URL)
    print("=" * 80)
    print(f"  Processing triggers:   {_sample_client._processing_call_count}")
    print(f"  (All document/entity calls went through the real API)")
    print()


def _load_json_file(file_path: str) -> dict[str, Any]:
    """Load a JSON file, trying absolute and relative paths."""
    p = Path(file_path)
    if not p.is_absolute():
        try:
            script_dir = Path(__file__).parent
        except NameError:
            script_dir = Path.cwd()
        relative = script_dir / file_path
        if relative.exists():
            p = relative
        else:
            workspace_p = Path("/Workspace/Users/mohammadtahakhan20@gmail.com/SACC/dev") / file_path
            if workspace_p.exists():
                p = workspace_p
    if not p.exists():
        raise FileNotFoundError(f"JSON file not found: {file_path}")
    with open(p) as f:
        return json.load(f)


def _get_doc_count(raw_data: dict[str, Any]) -> int:
    """Return the number of documents in the JSON, or 1 for single-doc formats."""
    if "processed" in raw_data:
        return len(raw_data["processed"])
    return 1


def main():
    """CLI entry point.

    Usage:
        python run_sample.py [json_file] [doc_index|all]
    """
    if len(sys.argv) > 1:
        json_file = sys.argv[1]
    else:
        json_file = "sample_data.json"

    try:
        raw_data = _load_json_file(json_file)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {json_file}: {e}")
        sys.exit(1)

    doc_count = _get_doc_count(raw_data)

    if len(sys.argv) > 2:
        arg = sys.argv[2]
        if arg == "all":
            indices = list(range(doc_count))
        else:
            try:
                idx = int(arg)
                if idx < 0 or idx >= doc_count:
                    print(f"ERROR: doc_index {idx} out of range (0-{doc_count - 1})")
                    sys.exit(1)
                indices = [idx]
            except ValueError:
                print(f"ERROR: Invalid doc_index '{arg}' -- use an integer or 'all'")
                sys.exit(1)
    else:
        indices = [0]

    print(f"\n  Source: {json_file} ({doc_count} document(s) available)")
    print(f"  Processing: {len(indices)} document(s) at index(es): {indices}")

    for i, doc_idx in enumerate(indices):
        if len(indices) > 1:
            print(f"\n{'#' * 80}")
            print(f"#  DOCUMENT {i + 1}/{len(indices)}  (index {doc_idx})")
            print(f"{'#' * 80}")

        try:
            normalized = _normalize_payload(raw_data, doc_idx)
        except (ValueError, KeyError) as e:
            print(f"ERROR normalising document {doc_idx}: {e}")
            continue

        result = run_sample_triage(normalized)
        _print_result(result, f"  [doc {doc_idx}]" if len(indices) > 1 else "")


if __name__ == "__main__":
    main()