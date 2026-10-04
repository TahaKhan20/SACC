import os, sys, json, time, glob, base64, requests

try:
    import fitz  # PyMuPDF for PDF-to-image conversion
except ImportError:
    print("PyMuPDF is required for PDF processing. Install with: pip install pymupdf")
    sys.exit(1)

_dir = os.getcwd()

with open(os.path.join(_dir, "ai_core_cred.json")) as f:
    cred = json.load(f)

uaa_url = cred["url"]
client_id = cred["clientid"]
client_secret = cred["clientsecret"]
ai_api_url = cred["serviceurls"]["AI_API_URL"]

tok = requests.post(f'{uaa_url}/oauth/token',
                    data={"grant_type": "client_credentials"},
                    auth=(client_id, client_secret))
tok.raise_for_status()
H = {"Authorization": f'Bearer {tok.json()["access_token"]}',
     "AI-Resource-Group": "default"}

# Find the foundation-models deployment (GPT-5.4)
_deps = requests.get(f"{ai_api_url}/v2/lm/deployments", headers=H).json()
_dep = next((d for d in _deps.get("resources", [])
             if d.get("scenarioId") == "foundation-models" and d.get("status") == "RUNNING"), None)
if not _dep:
    print("No RUNNING foundation-models deployment found"); sys.exit(1)
dep_url = _dep["deploymentUrl"]
print(f"using deployment: {_dep['id']} model={_dep.get('details', {}).get('resources', {}).get('backend_details', {}).get('backendDetails', {}).get('model', {}).get('name', 'unknown')}")

EXTRACT_PROMPT = """You are a document classification and extraction assistant. Analyze this document page and return ONLY valid JSON (no markdown, no explanation):
{
  "document_type": "",
  "invoice_subtype": null,
  "invoice_number": null,
  "po_number": null,
  "vendor_number": null,
  "confidence": 0.0,
  "reason": ""
}

Rules:
1. Classify the document into exactly one of these document_type values:
   - "invoice"
   - "credit_note"
   - "supporting_document"
   - "reconciliation"
   - "statement_of_account"
   - "purchase_order_list"
   - "general_correspondence"

2. If document_type is "invoice", also set invoice_subtype to either "fuel" or "cargo" based on the invoice content.
   Set invoice_subtype to null for all other document types.

3. Extract these fields if present on this page (use null if not found):
   - invoice_number: The invoice/document number
   - po_number: The purchase order number
   - vendor_number: The vendor/supplier number

4. Set confidence to a value between 0.0 and 1.0 reflecting how certain you are.
5. Provide a brief reason explaining your classification."""

folder = os.path.join(_dir, "invoice_samples")
files = [f for f in glob.glob(os.path.join(folder, "*")) if f.lower().endswith((".pdf", ".png", ".jpg", ".jpeg"))]
# Only use sys.argv if the args look like real file paths (not IPython/REPL flags like -f)
if len(sys.argv) > 1 and all(os.path.isfile(a) for a in sys.argv[1:]):
    files = sys.argv[1:]

out_dir = os.path.join(os.path.dirname(folder), "output")
os.makedirs(out_dir, exist_ok=True)

# Also capture console output to a log file
log_path = os.path.join(out_dir, "run_log.txt")
_log_fh = open(log_path, "w", encoding="utf-8")
def _log(*args):
    line = " ".join(str(a) for a in args)
    print(line)
    _log_fh.write(line + "\n")
    _log_fh.flush()

def _call_gpt(image_b64, mime="image/png"):
    """Send a single page image to GPT-5.4 and return parsed JSON."""
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": EXTRACT_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}}
        ]
    }
    r = requests.post(f"{dep_url}/v1/chat/completions",
                      headers={**H, "Content-Type": "application/json"},
                      json={"messages": [msg], "max_completion_tokens": 2000})
    if not r.ok:
        return {"_error": r.status_code, "_error_text": r.text[:500]}
    content = r.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = "\n".join(content.split("\n")[1:-1]) if content.endswith("```") else "\n".join(content.split("\n")[1:])
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"raw_content": content}

_log("files to process:", len(files))
for path in files:
    name = os.path.basename(path)
    _log("processing:", name)
    ext = os.path.splitext(name)[1].lower().lstrip(".")

    # Build list of (image_b64, mime) tuples — 1 per page for PDFs, 1 for images
    page_images = []
    if ext == "pdf":
        doc = fitz.open(path)
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            page_images.append((base64.b64encode(pix.tobytes("png")).decode(), "image/png"))
        doc.close()
        _log(f"  {len(page_images)} PDF page(s) — processing each separately")
    else:
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "image/jpeg")
        with open(path, "rb") as fh:
            page_images.append((base64.b64encode(fh.read()).decode(), mime))

    # Process each page as a separate API call
    page_results = []
    for i, (img_b64, mime) in enumerate(page_images):
        result = _call_gpt(img_b64, mime)
        result["page"] = i + 1
        page_results.append(result)
        if "_error" in result:
            _log(f"  page {i+1}/{len(page_images)}: ERROR {result['_error']}: {result['_error_text'][:200]}")
        else:
            _log(f"  page {i+1}/{len(page_images)}: type={result.get('document_type', '?')} "
                 f"subtype={result.get('invoice_subtype')} "
                 f"inv={result.get('invoice_number')} "
                 f"po={result.get('po_number')} "
                 f"vendor={result.get('vendor_number')} "
                 f"conf={result.get('confidence', '?')}")

    # Build combined summary — first non-null value wins for each field
    def _first_field(field):
        for r in page_results:
            if "_error" not in r and r.get(field):
                return r[field]
        return None

    doc_types = [r.get("document_type") for r in page_results if "_error" not in r]
    unique_types = list(dict.fromkeys(doc_types))
    combined = {
        "_source_file": name,
        "_model": _dep.get("details", {}).get("resources", {}).get("backend_details", {}).get("backendDetails", {}).get("model", {}).get("name", "unknown"),
        "_total_pages": len(page_images),
        "page_results": page_results,
        "document_type": unique_types[0] if unique_types else None,
        "document_types_found": unique_types,
        "invoice_subtype": _first_field("invoice_subtype"),
        "invoice_number": _first_field("invoice_number"),
        "po_number": _first_field("po_number"),
        "vendor_number": _first_field("vendor_number"),
    }

    with open(os.path.join(out_dir, os.path.splitext(name)[0] + ".json"), "w", encoding="utf-8") as fh:
        json.dump(combined, fh, indent=2)

    _log(f"  ── summary for {name} ──")
    _log(f"  document_type: {combined['document_type']}")
    _log(f"  document_types_found: {combined['document_types_found']}")
    _log(f"  invoice_subtype: {combined['invoice_subtype']}")
    _log(f"  invoice_number: {combined['invoice_number']}")
    _log(f"  po_number: {combined['po_number']}")
    _log(f"  vendor_number: {combined['vendor_number']}")
_log("done. output saved to", out_dir)
_log_fh.close()
 