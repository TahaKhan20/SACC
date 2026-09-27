# Email Intake Agent

Fetches, enriches, and classifies emails from Microsoft Graph API (or a mock API). Identifies relevant emails with invoice/document attachments for downstream triage.

## Folder Structure

```
Email_Intake_Agent/
├── email_intake_agent.py           # Main script: fetch, enrich, classify
├── requirements.txt                 # Dependencies
├── .env.example                     # Environment variable template
└── README.md                        # This file
```

## Quick Start

```bash
pip install -r requirements.txt

# Copy .env.example to .env and set your credentials
cp .env.example .env
# Edit .env to set GRAPH_USER_ID, GRAPH_API_TOKEN, etc.

# Run the intake agent
python email_intake_agent.py --top 10
```

## Endpoints

### Messages

| Method | Path | Description
| --- | --- | ---
| GET | `/v1.0/me/messages` | List messages (supports $top, $skip, $filter, $select, $orderby, $count)
| GET | `/v1.0/me/messages/{id}` | Get single message — returns subject, body, sender, from, toRecipients, date/time, importance, isRead, etc.

### Attachments

| Method | Path | Description
| --- | --- | ---
| GET | `/v1.0/me/messages/{id}/attachments` | List attachments for a message

### Mail Folders

| Method | Path | Description
| --- | --- | ---
| GET | `/v1.0/me/mailFolders` | List folders
| GET | `/v1.0/me/mailFolders/{id}` | Get folder
| GET | `/v1.0/me/mailFolders/{id}/messages` | List messages in folder

### Admin

| Method | Path | Description
| --- | --- | ---
| GET | `/health` | Health check (counts of loaded data)
| POST | `/api/reload` | Reload JSON templates into memory

All endpoints also support `/v1.0/users/{user_id}/...` variants.

## OData Query Parameters

| Param | Example | Description
| --- | --- | ---
| `$top` | `$top=10` | Page size (max 1000)
| `$skip` | `$skip=20` | Skip N records
| `$filter` | `$filter=importance eq 'high'` | Filter by field (eq, ne, contains, startswith)
| `$select` | `$select=id,subject,isRead` | Project specific fields
| `$orderby` | `$orderby=receivedDateTime desc` | Sort by field (asc/desc)
| `$count` | `$count=true` | Include total count in response

## Response Format

All list responses use the Graph `@odata.context` envelope:

```json
{
  "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users('me')/messages",
  "value": [ ... ]
}
```

Single-item responses use the `$entity` suffix:

```json
{
  "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users('me')/messages/$entity",
  "id": "AAMkAGI2TG93ExLn",
  "subject": "Welcome to Saudi Cargo - Onboarding Details",
  ...
}
```

## Environment Variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `GRAPH_USER_ID` | Yes | - | User ID or UPN (e.g., `user@domain.com`) |
| `GRAPH_API_TOKEN` | Yes | - | API access token |
| `GRAPH_API_BASE_URL` | No | `https://graph.microsoft.com` | Base URL for Graph API |

**Authentication:** Token is sent as `?api_token=<token>` query parameter on all requests.

### Example Configuration

```bash
GRAPH_USER_ID=sacc.ap.invoice@addo.ai
GRAPH_API_TOKEN=12346789abcdefgh
GRAPH_API_BASE_URL=http://0.0.0.0:8002
```

## CLI Arguments

| Argument | Default | Description |
| --- | --- | --- |
| `--top` | 50 | Maximum number of emails to fetch |
| `--min-score` | 20 | Minimum relevance score (0-100) for classification |

## Notes

* **Authentication**: Token is sent as `?api_token=<token>` query parameter on all Graph API requests.
* **Attachment saving**: Document attachments (PDF, images, Office docs) are base64-decoded and can be saved to temp files for processing.
* **Standalone**: Can be imported as a module (`from email_intake_agent import run_intake`) or run as a CLI script.