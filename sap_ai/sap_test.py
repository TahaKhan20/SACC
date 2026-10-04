import os, sys, json, time, glob, requests

_dir = os.getcwd()

with open(os.path.join(_dir, "cred.json")) as f:
    cred = json.load(f)

uaa_url = cred["uaa"]["url"]
client_id = cred["uaa"]["clientid"]
client_secret = cred["uaa"]["clientsecret"]
dox_url = cred["url"]

tok = requests.post(f'{uaa_url}/oauth/token',
                    data={"grant_type": "client_credentials"},
                    auth=(client_id, client_secret))
tok.raise_for_status()
H = {"Authorization": f'Bearer {tok.json()["access_token"]}'}
base = f'{dox_url}/document-information-extraction/v1'

# pick the invoice schema
schemas = requests.get(f"{base}/schemas", headers=H, params={"clientId": "default"}).json()
schemas = schemas.get("schemas", schemas.get("payload", []))
inv = [x for x in schemas if x.get("documentType") == "invoice"]
schema_id = inv[0]["id"] if inv else None
print("schema:", inv[0].get("name") if inv else "none found", schema_id)

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

_log("files to process:", len(files))
for path in files:
    name = os.path.basename(path)
    _log("processing:", name)
    options = {"clientId": "default", "documentType": "invoice",
               "enrichment": {"sender": {"top": 5, "type": "businessEntity", "subtype": "supplier"},
                              "employee": {"type": "employee"}}}
    if schema_id:
        options["schemaId"] = schema_id
    with open(path, "rb") as fh:
        r = requests.post(f"{base}/document/jobs", headers=H,
                          files={"file": (name, fh, "application/pdf" if name.lower().endswith(".pdf") else "application/octet-stream")},
                          data={"options": json.dumps(options)})
    _log(name, "submit:", r.status_code)
    if not r.ok:
        _log(r.text); continue
    job_id = r.json()["id"]
    while True:
        j = requests.get(f"{base}/document/jobs/{job_id}", headers=H, params={"returnNullValues": "true"}).json()
        if j.get("status") in ("DONE", "FAILED"):
            break
        time.sleep(3)
    _log("status:", j["status"])
    with open(os.path.join(out_dir, os.path.splitext(name)[0] + ".json"), "w", encoding="utf-8") as fh:
        json.dump(j, fh, indent=2)
    for f in j.get("extraction", {}).get("headerFields", []):
        _log(f"  {f['name']}: {f.get('value')}  ({f.get('confidence') or 0:.2f})")
    _log("  line items:", len(j.get("extraction", {}).get("lineItems", [])))
_log("done. output saved to", out_dir)
_log_fh.close()
 