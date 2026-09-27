# Microsoft Graph Mail API Mock (Read-Only)

Standalone FastAPI application that mocks Microsoft Graph v1.0 Mail **read-only** endpoints for development and testing.

## Folder Structure

```
graph_mock/
├── graph_mail_mock.py              # FastAPI app — read-only mock endpoints
├── messages.json                   # Seed data — 5 mock messages (inbox, drafts, sent)
└── README.md                       # This file
```

## Quick Start

```bash
pip install fastapi uvicorn[standard] httpx

# Run from inside the graph_mock folder
cd graph_mock
uvicorn graph_mail_mock:app --reload --port 8001
```

API docs (Swagger UI): `http://localhost:8001/docs`

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

## Notes

* **No auth** — open mock, just like the SAP EDMX project
* **In-memory** — all data is lost on restart; reload from JSON with `POST /api/reload`
* **Port 8001** — avoids conflict with the SAP EDMX mock on port 8000
* **Read-only** — no POST/PATCH/DELETE endpoints; data is seeded from `messages.json` at startup
* **Standalone** — does not depend on or modify the SAP EDMX files (`main.py`, `edmx_parser.py`, `api_generator.py`, `crud_engine.py`)