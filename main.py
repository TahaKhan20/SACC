"""Main Application Entrypoint

Slim orchestrator that wires together the three parts:
  - Part 2a: edmx_parser   (pure EDMX parsing library)
  - Part 2b: api_generator  (portable FastAPI router per service)
  - Part 3:  crud_engine    (generic CRUD testing proxy)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from edmx_parser import ServiceMeta, parse_edmx_file
from api_generator import create_service_router, reset_all_data, DATA_STORE
from crud_engine import router as crud_router
from generate_apis import generate_all_apis


BASE_DIR = Path(__file__).resolve().parent
APIS_DIR = BASE_DIR / "apis"
DEFAULT_SERVICE_ENV = "EDMX_FILE"

# -- Service Registry (populated on startup) --------------------------------

SERVICE_REGISTRY: dict[str, ServiceMeta] = {}


# -- App Setup --------------------------------------------------------------

app = FastAPI(
    title="Generic SAP EDMX API Generator",
    description=(
        "Parses SAP OData EDMX files, inspects fields and capabilities, and "
        "creates generic in-memory CRUD APIs plus a metadata-driven UI."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- Service Loading --------------------------------------------------------

def load_services() -> None:
    """Discover EDMX files, parse them, and mount a router per service."""
    APIS_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_REGISTRY.clear()

    edmx_files = sorted(APIS_DIR.glob("*.edmx"))
    env_file = os.getenv(DEFAULT_SERVICE_ENV)
    if env_file:
        candidate = APIS_DIR / env_file
        if candidate.exists() and candidate.suffix.lower() == ".edmx":
            edmx_files = [candidate]

    for file_path in edmx_files:
        try:
            service = parse_edmx_file(file_path)
            SERVICE_REGISTRY[service.name] = service
        except Exception as exc:
            print(f"Skipping {file_path.name}: {exc}")

    reset_all_data(SERVICE_REGISTRY)
    _mount_service_routers()


def _mount_service_routers() -> None:
    """Create and mount a FastAPI router for each parsed service."""
    # Remove previously mounted per-service routers (on reload)
    # Keep static routes like /api/services/reload
    _static_paths = {"/api/services/reload"}
    app.router.routes = [
        route for route in app.router.routes
        if not getattr(route, "path", "").startswith("/api/services/")
        or getattr(route, "path", "") in _static_paths
    ]

    for service_name, service in SERVICE_REGISTRY.items():
        router = create_service_router(service)
        prefix = f"/api/services/{service_name}"
        app.include_router(router, prefix=prefix)


# -- Mount CRUD Tester (Part 3) ---------------------------------------------

app.include_router(crud_router)


# -- Core Routes ------------------------------------------------------------

@app.on_event("startup")
def startup_event() -> None:
    load_services()
    # Generate custom API definition files under custom_apis/
    summary = generate_all_apis()
    if summary.get("total_files_created"):
        print(f"Generated {summary['total_files_created']} API definition files in custom_apis/")


@app.get("/", include_in_schema=False)
def serve_ui():
    html_path = BASE_DIR / "index.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html")
    return {"message": "Place index.html in the same directory as main.py"}


@app.get("/health")
def healthcheck() -> dict[str, Any]:
    return {
        "status": "ok",
        "services_loaded": len(SERVICE_REGISTRY),
        "services": sorted(SERVICE_REGISTRY),
    }


@app.get("/api/services")
def list_services() -> list[dict[str, Any]]:
    return [
        {
            "name": service.name,
            "file_name": service.file_name,
            "namespace": service.namespace,
            "version": service.version,
            "entity_set_count": len(service.entity_sets),
        }
        for service in SERVICE_REGISTRY.values()
    ]


@app.post("/api/services/reload")
def reload_services() -> dict[str, Any]:
    load_services()
    return {
        "status": "reloaded",
        "services": sorted(SERVICE_REGISTRY),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
