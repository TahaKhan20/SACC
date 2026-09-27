"""Microsoft Graph Mail API — Real Account Connector

Standalone FastAPI application that connects to a REAL Microsoft Graph
account, fetches live emails, and reads their contents.

Configuration (via environment variables — no config file needed):
    GRAPH_USER_ID      Target user ID or UPN (e.g. user@domain.com)
    GRAPH_API_TOKEN    Pre-obtained bearer token for Microsoft Graph
    GRAPH_PORT          Port to run on (default: 8002)

Optional (for MSAL-based token acquisition as fallback):
    GRAPH_AUTH_MODE         "client_credentials" or "device_code"
    GRAPH_TENANT_ID         Azure AD tenant ID
    GRAPH_CLIENT_ID         Azure AD app client ID
    GRAPH_CLIENT_SECRET     Azure AD app client secret

If GRAPH_API_TOKEN is set, it is used directly and MSAL is not required.
All endpoints use /v1.0/users/{user_id}/... when GRAPH_USER_ID is set.

Supported endpoints (all under /v1.0, READ-ONLY):
  - Messages:    list / get single
  - Attachments: list
  - Folders:     list / messages-by-folder
  - Save emails: POST /api/save-emails  (writes .eml + .json to ./saved_emails/)

Usage:
  export GRAPH_USER_ID="user@domain.com"
  export GRAPH_API_TOKEN="eyJ0e..."
  python graph_mail_real.py            (or: uvicorn graph_mail_real:app --port 8002)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# MSAL is optional at import time — the app will still start and report
# a clear error if you try to use a protected endpoint without it.
try:
    import msal
    HAS_MSAL = True
except ImportError:
    HAS_MSAL = False


BASE_DIR = Path(__file__).resolve().parent
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_VERSION = "v1.0"
CONFIG_PATH = BASE_DIR / "graph_config.json"
SAVED_DIR = BASE_DIR / "saved_emails"

# ── App -------------------------------------------------------------------

app = FastAPI(
    title="Microsoft Graph Mail — Real Account Connector",
    description=(
        "Connects to a real Microsoft Graph account to fetch and read live emails. "
        "Configure credentials in graph_config.json."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config & Token Management --------------------------------------------


class GraphConfig:
    """Load Graph credentials from environment variables (with optional config file fallback)."""

    def __init__(self) -> None:
        raw: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)

        # Primary: environment variables for direct token mode
        self.user_id: str = os.getenv("GRAPH_USER_ID", raw.get("user_id", ""))
        self.api_token: str = os.getenv("GRAPH_API_TOKEN", "")

        # Fallback: MSAL-based config (from config file or env vars)
        self.auth_mode: str = os.getenv("GRAPH_AUTH_MODE", raw.get("auth_mode", "client_credentials"))
        self.tenant_id: str = os.getenv("GRAPH_TENANT_ID", raw.get("tenant_id", ""))
        self.client_id: str = os.getenv("GRAPH_CLIENT_ID", raw.get("client_id", ""))
        self.client_secret: str = os.getenv("GRAPH_CLIENT_SECRET", raw.get("client_secret", ""))
        self.scopes: list[str] = raw.get("scopes", ["https://graph.microsoft.com/.default"])
        self.redirect_port: int = int(os.getenv("GRAPH_PORT", raw.get("port", 8002)))

    @property
    def use_direct_token(self) -> bool:
        """True when a pre-obtained API token is provided (no MSAL needed)."""
        return bool(self.api_token and self.user_id)

    @property
    def is_configured(self) -> bool:
        if self.use_direct_token:
            return True
        if not self.client_id:
            return False
        if self.auth_mode == "client_credentials":
            return bool(self.tenant_id and self.client_secret)
        return True  # device_code only needs client_id

    def summary(self) -> dict[str, Any]:
        return {
            "auth_mode": "direct_token" if self.use_direct_token else self.auth_mode,
            "user_id": self.user_id or "(not set)",
            "token_set": bool(self.api_token),
            "tenant_id": self.tenant_id[:8] + "..." if self.tenant_id else "(not set)",
            "client_id": self.client_id[:8] + "..." if self.client_id else "(not set)",
            "is_configured": self.is_configured,
            "msal_available": HAS_MSAL,
        }


config = GraphConfig()

# Token cache (in-memory)
_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0}


def _get_token_client_credentials() -> str:
    """Get an access token using client-credentials flow (app-only)."""
    if not HAS_MSAL:
        raise HTTPException(500, detail="msal package not installed. Run: pip install msal")
    authority = f"https://login.microsoftonline.com/{config.tenant_id}"
    app_obj = msal.ConfidentialClientApplication(
        config.client_id,
        authority=authority,
        client_credential=config.client_secret,
    )
    result = app_obj.acquire_token_for_client(scopes=config.scopes)
    if "access_token" not in result:
        raise HTTPException(401, detail=f"Failed to get token: {result.get('error_description', result)}")
    return result["access_token"]


def _get_token_device_code() -> str:
    """Get an access token using device-code flow (interactive).

    First call returns instructions for the user to visit a URL and enter a code.
    The app polls until the user completes authentication.
    """
    if not HAS_MSAL:
        raise HTTPException(500, detail="msal package not installed. Run: pip install msal")
    authority = "https://login.microsoftonline.com/common"
    app_obj = msal.PublicClientApplication(
        config.client_id,
        authority=authority,
    )
    flow = app_obj.initiate_device_flow(scopes=["Mail.Read", "Mail.ReadBasic", "User.Read"])
    if "user_code" not in flow:
        raise HTTPException(500, detail=f"Failed to create device flow: {flow}")

    # Print instructions for the user
    print("\n" + "=" * 60)
    print(flow["message"])
    print("=" * 60 + "\n")

    # Poll for completion (blocking — user must complete in browser)
    result = app_obj.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise HTTPException(401, detail=f"Authentication failed: {result.get('error_description', result)}")
    return result["access_token"]


def get_access_token() -> str:
    """Get a valid access token.

    If a direct API token is provided via GRAPH_API_TOKEN env var, use it directly.
    Otherwise, acquire one via MSAL (client_credentials or device_code flow).
    """
    # Direct token mode — no MSAL needed
    if config.use_direct_token:
        return config.api_token

    # MSAL-based token acquisition
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    if config.auth_mode == "client_credentials":
        token = _get_token_client_credentials()
    else:
        token = _get_token_device_code()

    # Cache for 50 minutes (tokens last 60 min)
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = time.time() + 3000
    return token


# ── Graph API Helper ------------------------------------------------------


def graph_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Make an authenticated GET request to Microsoft Graph."""
    token = get_access_token()
    url = f"{GRAPH_BASE}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # In direct-token mode, always use /users/{user_id}/ prefix
    # In MSAL client_credentials mode, replace /me/ with /users/{user_id}/
    if config.use_direct_token and config.user_id:
        url = url.replace("/me/", f"/users/{config.user_id}/")
    elif config.auth_mode == "client_credentials" and config.user_id:
        url = url.replace("/me/", f"/users/{config.user_id}/")

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers, params=params)

    if resp.status_code == 401:
        # Token may have expired — clear cache and retry once
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0
        token = get_access_token()
        headers["Authorization"] = f"Bearer {token}"
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=headers, params=params)

    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, detail=resp.json())
    return resp.json()


def _passthrough_query_params(request: Request) -> dict[str, Any]:
    """Forward OData query parameters ($top, $filter, $select, etc.) to Graph."""
    params: dict[str, Any] = {}
    for key, value in request.query_params.items():
        params[key] = value
    return params


# ── Startup ---------------------------------------------------------------


@app.on_event("startup")
def startup_event() -> None:
    SAVED_DIR.mkdir(parents=True, exist_ok=True)


# ── Health / Auth ---------------------------------------------------------


@app.get("/health")
def healthcheck() -> dict[str, Any]:
    return {"status": "ok", **config.summary()}


@app.post("/api/auth/token")
def authenticate() -> dict[str, Any]:
    """Trigger authentication and cache the token."""
    try:
        token = get_access_token()
        return {"status": "authenticated", "token_preview": token[:20] + "..."}
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ── Messages: List ---------------------------------------------------------


@app.get("/v1.0/me/messages")
@app.get("/v1.0/users/{user_id}/messages")
def list_messages(
    request: Request,
    user_id: str | None = None,
    top: int | None = Query(default=None, alias="$top", ge=1, le=1000),
    skip: int | None = Query(default=None, alias="$skip", ge=0),
    count: bool | None = Query(default=None, alias="$count"),
):
    """List real messages from Microsoft Graph."""
    params = _passthrough_query_params(request)
    path = f"users/{user_id}/messages" if user_id else "me/messages"
    return graph_get(path, params)


# ── Messages: Get single --------------------------------------------------


@app.get("/v1.0/me/messages/{message_id}")
@app.get("/v1.0/users/{user_id}/messages/{message_id}")
def get_message(
    request: Request,
    message_id: str,
    user_id: str | None = None,
):
    """Get a single real message by ID (includes full body content)."""
    params = _passthrough_query_params(request)
    path = f"users/{user_id}/messages/{message_id}" if user_id else f"me/messages/{message_id}"
    return graph_get(path, params)


# ── Attachments -----------------------------------------------------------


@app.get("/v1.0/me/messages/{message_id}/attachments")
@app.get("/v1.0/users/{user_id}/messages/{message_id}/attachments")
def list_attachments(
    request: Request,
    message_id: str,
    user_id: str | None = None,
):
    """List attachments for a real message."""
    path = f"users/{user_id}/messages/{message_id}/attachments" if user_id else f"me/messages/{message_id}/attachments"
    return graph_get(path)


# ── Mail Folders ----------------------------------------------------------


@app.get("/v1.0/me/mailFolders")
@app.get("/v1.0/users/{user_id}/mailFolders")
def list_folders(
    request: Request,
    user_id: str | None = None,
    top: int | None = Query(default=None, alias="$top", ge=1, le=1000),
    skip: int | None = Query(default=None, alias="$skip", ge=0),
):
    """List real mail folders."""
    params = _passthrough_query_params(request)
    path = f"users/{user_id}/mailFolders" if user_id else "me/mailFolders"
    return graph_get(path, params)


@app.get("/v1.0/me/mailFolders/{folder_id}/messages")
@app.get("/v1.0/users/{user_id}/mailFolders/{folder_id}/messages")
def list_messages_in_folder(
    request: Request,
    folder_id: str,
    user_id: str | None = None,
    top: int | None = Query(default=None, alias="$top", ge=1, le=1000),
    skip: int | None = Query(default=None, alias="$skip", ge=0),
    count: bool | None = Query(default=None, alias="$count"),
):
    """List real messages within a specific folder."""
    params = _passthrough_query_params(request)
    path = f"users/{user_id}/mailFolders/{folder_id}/messages" if user_id else f"me/mailFolders/{folder_id}/messages"
    return graph_get(path, params)


# ── Save Emails to Disk ---------------------------------------------------


@app.post("/api/save-emails")
def save_emails(
    folder_id: str | None = None,
    top: int = 10,
) -> dict[str, Any]:
    """Fetch real emails and save their contents to ./saved_emails/ as JSON files.

    Query params:
      folder_id  (optional) — fetch from a specific folder (default: inbox)
      top       — max number of emails to save (default: 10)
    """
    SAVED_DIR.mkdir(parents=True, exist_ok=True)

    # Determine the API path
    if folder_id:
        path = f"me/mailFolders/{folder_id}/messages"
    else:
        path = "me/messages"

    params = {"$top": top, "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,body,hasAttachments"}
    data = graph_get(path, params)

    messages = data.get("value", [])
    saved_files: list[str] = []

    for msg in messages:
        msg_id = msg.get("id", "unknown")
        safe_id = msg_id.replace("/", "_").replace("=", "-")[:60]
        subject = msg.get("subject", "no_subject")[:60].replace(" ", "_").replace("/", "_")
        filename = f"{safe_id}_{subject}.json"
        filepath = SAVED_DIR / filename

        # If message has attachments, fetch them too
        if msg.get("hasAttachments"):
            try:
                attach_data = graph_get(f"me/messages/{msg_id}/attachments")
                msg["_attachments"] = attach_data.get("value", [])
            except Exception:
                msg["_attachments"] = []

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(msg, f, indent=2, ensure_ascii=False)
        saved_files.append(filename)

    return {
        "status": "saved",
        "count": len(saved_files),
        "folder": SAVED_DIR.name,
        "files": saved_files,
    }


@app.get("/api/saved-emails")
def list_saved_emails() -> dict[str, Any]:
    """List emails that have been saved to disk."""
    if not SAVED_DIR.exists():
        return {"files": [], "count": 0}
    files = sorted(SAVED_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "files": [f.name for f in files],
        "count": len(files),
        "directory": str(SAVED_DIR),
    }


@app.get("/api/saved-emails/{filename}")
def get_saved_email(filename: str) -> dict[str, Any]:
    """Read a single saved email from disk."""
    filepath = SAVED_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, detail=f"Saved email '{filename}' not found")
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Entrypoint -------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn

    print("\nMicrosoft Graph Mail — Real Account Connector")
    if config.use_direct_token:
        print(f"Mode: direct token (GRAPH_API_TOKEN set)")
        print(f"User: {config.user_id}")
    else:
        print(f"Config: {CONFIG_PATH}")
        print(f"Auth mode: {config.auth_mode}")
    print(f"Configured: {config.is_configured}")
    print(f"MSAL available: {HAS_MSAL}")
    if not config.is_configured:
        print("\n⚠  Not configured! Set GRAPH_USER_ID and GRAPH_API_TOKEN env vars,")
        print("   or copy graph_config.example.json → graph_config.json and fill in credentials.")
    print()
    uvicorn.run(app, host="0.0.0.0", port=config.redirect_port)
