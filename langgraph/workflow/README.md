# Email-to-Triage Workflow

Orchestrates the full pipeline: fetch emails from Microsoft Graph → pre-classify by email metadata → triage document attachments via the Triage Agent.

## Pipeline (4 nodes)

```
Node 1: Fetch   →  Node 2: Enrich   →  Node 3: Pre-Class  →  Node 4: Triage
 Emails             Emails              Emails               Documents
 Graph API          Full body +         Score sender,        Triage Agent
 port 8002          attachments         subject, content,    uploads to
                    per message         attachments           Document AI
                                                              port 8000
```

### Node 1: Fetch Emails
Calls `GET /v1.0/me/messages` on the Graph API (graph_mock, port 8002) to list all messages.

### Node 2: Enrich Emails
For each message, fetches the full body content and attachment metadata.

### Node 3: Pre-Classify Emails
Examines each email's sender, receiver, subject, content, and attachments to score relevance (0-100). Emails scoring >= 20 are considered relevant for triage.

Scoring criteria:
* Subject keywords (invoice, credit note, statement, fuel, charter, etc.): up to 30 points
* Body content keywords: up to 25 points
* Document attachment present (PDF, image, Office): 30 points
* Any attachment present: 10 points
* Business sender domain: 5 points

### Node 4: Triage Documents
For each relevant email with document attachments:
1. Saves the attachment (base64-decoded) to a temp file
2. Calls `triage_agent.run_triage(file_path, file_name)` which uploads to Document AI and classifies
3. Enriches the TriageResult with email context (sender, subject, recipients, etc.)
4. Cleans up temp files

## Prerequisites

```bash
pip install -r requirements.txt  # from SACC root

cp langgraph/graph_mock/graph_config.example.json langgraph/graph_mock/graph_config.json  # fill in Azure AD creds
cp langgraph/TriageAgent/.env.example langgraph/TriageAgent/.env  # fill in SACC API URL + company config
```

## Running

Start all three services in separate terminals:

```bash
# Terminal 1: SACC API (port 8000)
cd SACC && python main.py

# Terminal 2: Graph API (port 8002)
cd SACC/langgraph/graph_mock && python graph_mail_real.py

# Terminal 3: Run the workflow
cd SACC/langgraph/workflow
python workflow.py                # fetch + triage all emails
python workflow.py --top 10      # limit to 10 emails
python workflow.py --dry-run     # fetch + classify only, no triage
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GRAPH_API_BASE_URL` | `http://localhost:8002` | Graph API (graph_mock) URL |
| `SACC_API_BASE_URL` | `http://localhost:8000` | SACC API URL (used by Triage Agent) |

## Output

Returns a summary dict with total emails fetched, relevant emails, triage results (each containing TriageAgent output + email context), and any errors.