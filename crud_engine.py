"""Part 3 – Generic CRUD Operations Engine

Provides a FastAPI router with proxy endpoints that forward CRUD
operations to any given API endpoint.  Intended for testing purposes:
the UI’s “CRUD Tester” tab posts requests here, and this module
forwards them to the target service URL.

This keeps the CRUD testing logic server-side so the static UI never
needs to worry about CORS or complex request assembly.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Query


router = APIRouter(prefix="/crud", tags=["CRUD Tester"])


# ── Proxy helpers ─────────────────────────────────────────────────────────

async def _forward(
    method: str, url: str, json_body: Any | None = None
) -> Any:
    """Forward a request to an external URL and return the JSON response."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.request(
                method,
                url,
                json=json_body,
                headers={"Content-Type": "application/json"},
            )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to reach target: {exc}",
            ) from exc

    if response.status_code == 204:
        return {"status": "deleted", "code": 204}

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=data if isinstance(data, (dict, list)) else {"raw": str(data)},
        )
    return data


# ── List (GET) ────────────────────────────────────────────────────────────

@router.post("/list")
async def crud_list(
    endpoint: str = Body(..., description="Full URL to the entity list endpoint"),
    filters: dict[str, Any] = Body(default_factory=dict, description="Key-value filter params"),
    top: int = Body(100, description="$top parameter"),
    skip: int = Body(0, description="$skip parameter"),
) -> Any:
    """List records from any API endpoint."""
    params = [f"$top={top}", f"$skip={skip}"]
    for key, value in filters.items():
        if value is not None and str(value).strip():
            params.append(f"{key}={value}")
    url = f"{endpoint}?{'&'.join(params)}"
    return await _forward("GET", url)


# ── Get Single (GET) ─────────────────────────────────────────────────────

@router.post("/get")
async def crud_get(
    endpoint: str = Body(..., description="Full URL to the entity list endpoint"),
    keys: dict[str, Any] = Body(..., description="Key-value pairs for lookup"),
) -> Any:
    """Get a single record by key values."""
    params = "&".join(f"{k}={v}" for k, v in keys.items())
    url = f"{endpoint}/item?{params}"
    return await _forward("GET", url)


# ── Create (POST) ────────────────────────────────────────────────────────

@router.post("/create")
async def crud_create(
    endpoint: str = Body(..., description="Full URL to the entity list endpoint"),
    payload: dict[str, Any] = Body(..., description="Record data"),
) -> Any:
    """Create a new record via POST."""
    return await _forward("POST", endpoint, json_body=payload)


# ── Update (PATCH) ───────────────────────────────────────────────────────

@router.post("/update")
async def crud_update(
    endpoint: str = Body(..., description="Full URL to the entity list endpoint"),
    keys: dict[str, Any] = Body(..., description="Key-value pairs identifying the record"),
    payload: dict[str, Any] = Body(..., description="Fields to update"),
) -> Any:
    """Update a record via PATCH."""
    params = "&".join(f"{k}={v}" for k, v in keys.items())
    url = f"{endpoint}/item?{params}"
    return await _forward("PATCH", url, json_body=payload)


# ── Delete (DELETE) ──────────────────────────────────────────────────────

@router.post("/delete")
async def crud_delete(
    endpoint: str = Body(..., description="Full URL to the entity list endpoint"),
    keys: dict[str, Any] = Body(..., description="Key-value pairs identifying the record"),
) -> Any:
    """Delete a record via DELETE."""
    params = "&".join(f"{k}={v}" for k, v in keys.items())
    url = f"{endpoint}/item?{params}"
    return await _forward("DELETE", url)
