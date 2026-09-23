"""Generate Custom API Definitions from EDMX metadata

Reads all EDMX files from the apis/ folder, parses them with edmx_parser,
and generates per-entity-set API definition files under custom_apis/.

Folder structure:
    custom_apis/<ServiceName>/<EntitySet>/
        list.json         – GET list endpoint definition
        get.json          – GET single-record endpoint definition
        create.json       – POST create endpoint definition
        update.json       – PATCH update endpoint definition
        delete.json       – DELETE endpoint definition
        metadata.json     – entity metadata, keys, capabilities, fields

Each JSON file contains a complete API definition (method, path, query
params, body schema, response shape) that can be used to reconstruct,
document, or import the generated FastAPI routes.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Ensure the project root is importable
_PROJECT_DIR = Path(__file__).resolve().parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from edmx_parser import (
    EntityMeta,
    PropertyMeta,
    ServiceMeta,
    editable_properties,
    get_python_type,
    parse_edmx_file,
    property_dict,
    service_to_dict,
)


# ── Configuration ───────────────────────────────────────────────────────────

APIS_DIR = _PROJECT_DIR / "apis"
CUSTOM_APIS_DIR = _PROJECT_DIR / "custom_apis"
BASE_URL = "http://localhost:8000"  # Default dev server


# ── Schema Helpers ──────────────────────────────────────────────────────────

def _field_schema(prop: PropertyMeta) -> dict[str, Any]:
    """Build a JSON-schema-style field descriptor for a single property."""
    py_type = get_python_type(prop.edm_type)
    return {
        "name": prop.name,
        "edm_type": prop.edm_type,
        "python_type": py_type.__name__,
        "nullable": prop.nullable,
        "max_length": prop.max_length,
        "precision": prop.precision,
        "scale": prop.scale,
        "is_collection": prop.is_collection,
        "is_navigation": prop.is_navigation,
        "editable": prop.editable,
    }


def _body_schema(entity: EntityMeta) -> dict[str, Any]:
    """Build a request body schema from editable properties."""
    return {
        "type": "object",
        "properties": {
            prop.name: _field_schema(prop)
            for prop in editable_properties(entity)
        },
        "required": [
            prop.name
            for prop in entity.properties
            if not prop.nullable and prop.name not in entity.keys
        ],
    }


def _key_params(entity: EntityMeta) -> list[dict[str, Any]]:
    """Build query-parameter descriptors for key fields."""
    prop_map = property_dict(entity)
    return [
        {
            "name": key,
            "type": "query",
            "required": True,
            "schema": _field_schema(prop_map[key]) if key in prop_map else None,
        }
        for key in entity.keys
    ]


# ── Endpoint Builders ─────────────────────────────────────────────────────

def build_list_endpoint(service: ServiceMeta, entity: EntityMeta) -> dict[str, Any]:
    """Build the GET list endpoint definition for an entity set."""
    path = f"/api/services/{service.name}/entities/{entity.entity_set}"
    return {
        "description": f"List records from {entity.entity_set}",
        "method": "GET",
        "path": path,
        "url": f"{BASE_URL}{path}",
        "query_params": [
            {"name": "$top", "type": "query", "required": False, "default": 100, "schema": {"python_type": "int"}},
            {"name": "$skip", "type": "query", "required": False, "default": 0, "schema": {"python_type": "int"}},
        ],
        "body": None,
        "response": {
            "type": "array",
            "items": _body_schema(entity),
        },
        "capabilities": entity.capabilities,
    }


def build_get_endpoint(service: ServiceMeta, entity: EntityMeta) -> dict[str, Any]:
    """Build the GET single-record endpoint definition for an entity set."""
    path = f"/api/services/{service.name}/entities/{entity.entity_set}/item"
    return {
        "description": f"Get a single {entity.entity_type} record by key",
        "method": "GET",
        "path": path,
        "url": f"{BASE_URL}{path}",
        "query_params": _key_params(entity),
        "body": None,
        "response": _body_schema(entity),
    }


def build_create_endpoint(service: ServiceMeta, entity: EntityMeta) -> dict[str, Any]:
    """Build the POST create endpoint definition for an entity set."""
    path = f"/api/services/{service.name}/entities/{entity.entity_set}"
    return {
        "description": f"Create a new {entity.entity_type} record",
        "method": "POST",
        "path": path,
        "url": f"{BASE_URL}{path}",
        "query_params": None,
        "body": _body_schema(entity),
        "response": _body_schema(entity),
        "capabilities": {"insertable": entity.capabilities.get("insertable", True)},
    }


def build_update_endpoint(service: ServiceMeta, entity: EntityMeta) -> dict[str, Any]:
    """Build the PATCH update endpoint definition for an entity set."""
    path = f"/api/services/{service.name}/entities/{entity.entity_set}/item"
    return {
        "description": f"Update an existing {entity.entity_type} record by key",
        "method": "PATCH",
        "path": path,
        "url": f"{BASE_URL}{path}",
        "query_params": _key_params(entity),
        "body": {
            "type": "object",
            "properties": {
                prop.name: _field_schema(prop)
                for prop in editable_properties(entity)
            },
            "required": [],
        },
        "response": _body_schema(entity),
        "capabilities": {"updatable": entity.capabilities.get("updatable", True)},
    }


def build_delete_endpoint(service: ServiceMeta, entity: EntityMeta) -> dict[str, Any]:
    """Build the DELETE endpoint definition for an entity set."""
    path = f"/api/services/{service.name}/entities/{entity.entity_set}/item"
    return {
        "description": f"Delete a {entity.entity_type} record by key",
        "method": "DELETE",
        "path": path,
        "url": f"{BASE_URL}{path}",
        "query_params": _key_params(entity),
        "body": None,
        "response": None,
        "status_code": 204,
        "capabilities": {"deletable": entity.capabilities.get("deletable", True)},
    }


def build_metadata_file(service: ServiceMeta, entity: EntityMeta) -> dict[str, Any]:
    """Build a metadata summary for an entity set."""
    return {
        "service": service.name,
        "entity_set": entity.entity_set,
        "entity_type": entity.entity_type,
        "keys": entity.keys,
        "capabilities": entity.capabilities,
        "fields": [_field_schema(prop) for prop in entity.properties],
        "navigation_properties": [
            asdict(nav) for nav in entity.navigation_properties
        ],
    }


# ── File Writer ────────────────────────────────────────────────────────────

def _write_json(folder: Path, filename: str, payload: dict[str, Any]) -> Path:
    """Write a single JSON file, creating directories as needed."""
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / filename
    with open(file_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    return file_path


def generate_apis_for_service(service: ServiceMeta) -> list[str]:
    """Generate all API definition files for a single service.

    Returns a list of created file paths (relative to CUSTOM_APIS_DIR).
    """
    created: list[str] = []

    for entity in service.entity_sets:
        entity_folder = CUSTOM_APIS_DIR / service.name / entity.entity_set

        # GET list
        p = _write_json(entity_folder, "list.json", build_list_endpoint(service, entity))
        created.append(str(p.relative_to(CUSTOM_APIS_DIR)))

        # GET single
        p = _write_json(entity_folder, "get.json", build_get_endpoint(service, entity))
        created.append(str(p.relative_to(CUSTOM_APIS_DIR)))

        # POST create
        p = _write_json(entity_folder, "create.json", build_create_endpoint(service, entity))
        created.append(str(p.relative_to(CUSTOM_APIS_DIR)))

        # PATCH update
        p = _write_json(entity_folder, "update.json", build_update_endpoint(service, entity))
        created.append(str(p.relative_to(CUSTOM_APIS_DIR)))

        # DELETE
        p = _write_json(entity_folder, "delete.json", build_delete_endpoint(service, entity))
        created.append(str(p.relative_to(CUSTOM_APIS_DIR)))

        # Metadata
        p = _write_json(entity_folder, "metadata.json", build_metadata_file(service, entity))
        created.append(str(p.relative_to(CUSTOM_APIS_DIR)))

    return created


# ── Main Entry Point ───────────────────────────────────────────────────────

def generate_all_apis(
    apis_dir: Path | None = None,
    custom_apis_dir: Path | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Parse every EDMX file in *apis_dir* and generate API definitions.

    Parameters
    ----------
    apis_dir : Path, optional
        Directory containing .edmx files.  Defaults to ``./apis``.
    custom_apis_dir : Path, optional
        Output root for generated API definitions.  Defaults to ``./custom_apis``.
    base_url : str, optional
        Base URL prefix for endpoint paths.  Defaults to ``http://localhost:8000``.

    Returns
    -------
    dict with summary: services processed, files created, any errors.
    """
    global CUSTOM_APIS_DIR, BASE_URL

    _apis = apis_dir or APIS_DIR
    CUSTOM_APIS_DIR = custom_apis_dir or CUSTOM_APIS_DIR
    BASE_URL = base_url or BASE_URL

    _apis.mkdir(parents=True, exist_ok=True)
    CUSTOM_APIS_DIR.mkdir(parents=True, exist_ok=True)

    edmx_files = sorted(_apis.glob("*.edmx"))
    if not edmx_files:
        return {"status": "no_edmx_files", "apis_dir": str(_apis), "files_created": []}

    results: dict[str, Any] = {
        "status": "ok",
        "services": [],
        "total_files_created": 0,
        "errors": [],
    }

    for edmx_path in edmx_files:
        try:
            service = parse_edmx_file(edmx_path)
            created = generate_apis_for_service(service)
            results["services"].append({
                "name": service.name,
                "entity_sets": len(service.entity_sets),
                "files_created": len(created),
                "files": created,
            })
            results["total_files_created"] += len(created)
        except Exception as exc:
            results["errors"].append({"file": edmx_path.name, "error": str(exc)})

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate custom API definitions from EDMX files",
    )
    parser.add_argument(
        "--apis-dir", type=Path, default=None,
        help="Path to directory containing .edmx files (default: ./apis)",
    )
    parser.add_argument(
        "--custom-apis-dir", type=Path, default=None,
        help="Output directory for generated API definitions (default: ./custom_apis)",
    )
    parser.add_argument(
        "--base-url", type=str, default=None,
        help="Base URL for endpoint paths (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    summary = generate_all_apis(
        apis_dir=args.apis_dir,
        custom_apis_dir=args.custom_apis_dir,
        base_url=args.base_url,
    )
    print(json.dumps(summary, indent=2, default=str))
