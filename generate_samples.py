"""Generate Sample Payloads for EDMX-derived APIs

Reads all EDMX files from the apis/ folder, parses them with edmx_parser,
and generates per-entity-set sample request files under samples/.

Folder structure:
    samples/<ServiceName>/<EntitySet>/
        read_list.json      – GET list request
        read_single.json    – GET single-record request
        create.json         – POST create request

Each JSON file contains a full request example:
    { url, method, headers, query_params, body }
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure the project root is importable
_PROJECT_DIR = Path(__file__).resolve().parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from edmx_parser import (
    EntityMeta,
    ServiceMeta,
    parse_edmx_file,
    sample_record_for_entity,
    property_dict,
    default_scalar_value,
)


# ── Configuration ───────────────────────────────────────────────────────────

APIS_DIR = _PROJECT_DIR / "apis"
SAMPLES_DIR = _PROJECT_DIR / "samples"
BASE_URL = "http://localhost:8000"  # Default dev server


# ── Payload Builders ────────────────────────────────────────────────────────

def _common_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def build_read_list_sample(
    service: ServiceMeta, entity: EntityMeta
) -> dict[str, Any]:
    """Build a sample GET-list request for an entity set."""
    url = f"{BASE_URL}/api/services/{service.name}/entities/{entity.entity_set}"
    query_params: dict[str, Any] = {"$top": 100, "$skip": 0}

    # Add an example filter on the first string key if available
    prop_map = property_dict(entity)
    for key in entity.keys:
        prop = prop_map.get(key)
        if prop and prop.edm_type == "Edm.String":
            query_params[key] = default_scalar_value(prop.edm_type, prop.name)
            break

    return {
        "description": f"List records from {entity.entity_set}",
        "method": "GET",
        "url": url,
        "headers": _common_headers(),
        "query_params": query_params,
        "body": None,
        "example_curl": _build_curl("GET", url, query_params=query_params),
    }


def build_read_single_sample(
    service: ServiceMeta, entity: EntityMeta
) -> dict[str, Any]:
    """Build a sample GET-single request for an entity set."""
    url = f"{BASE_URL}/api/services/{service.name}/entities/{entity.entity_set}/item"
    prop_map = property_dict(entity)

    key_params: dict[str, Any] = {}
    for key in entity.keys:
        prop = prop_map.get(key)
        if prop:
            key_params[key] = default_scalar_value(prop.edm_type, prop.name)

    return {
        "description": f"Get a single {entity.entity_type} record by key",
        "method": "GET",
        "url": url,
        "headers": _common_headers(),
        "query_params": key_params,
        "body": None,
        "keys": list(entity.keys),
        "example_curl": _build_curl("GET", url, query_params=key_params),
    }


def build_create_sample(
    service: ServiceMeta, entity: EntityMeta
) -> dict[str, Any]:
    """Build a sample POST-create request for an entity set."""
    url = f"{BASE_URL}/api/services/{service.name}/entities/{entity.entity_set}"
    body = sample_record_for_entity(entity, record_number=1)

    return {
        "description": f"Create a new {entity.entity_type} record",
        "method": "POST",
        "url": url,
        "headers": _common_headers(),
        "query_params": None,
        "body": body,
        "example_curl": _build_curl("POST", url, body=body),
    }


# ── Curl Helper ─────────────────────────────────────────────────────────────

def _build_curl(
    method: str,
    url: str,
    query_params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    """Build an example curl command string."""
    if query_params:
        qs = "&".join(f"{k}={v}" for k, v in query_params.items())
        full_url = f"{url}?{qs}"
    else:
        full_url = url

    parts = [f"curl -X {method}"]
    parts.append(f'  "{full_url}"')
    parts.append('  -H "Content-Type: application/json"')
    parts.append('  -H "Accept: application/json"')

    if body is not None:
        compact_body = json.dumps(body, ensure_ascii=False)
        parts.append(f"  -d '{compact_body}'")

    return " \\\n".join(parts)


# ── Folder / File Writer ────────────────────────────────────────────────────

def write_sample_file(folder: Path, filename: str, payload: dict[str, Any]) -> Path:
    """Write a single sample JSON file, creating directories as needed."""
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / filename
    with open(file_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    return file_path


def generate_samples_for_service(service: ServiceMeta) -> list[str]:
    """Generate all sample payloads for a single service.

    Returns a list of created file paths (relative to SAMPLES_DIR).
    """
    created: list[str] = []

    for entity in service.entity_sets:
        entity_folder = SAMPLES_DIR / service.name / entity.entity_set

        # Read – list
        read_list = build_read_list_sample(service, entity)
        p = write_sample_file(entity_folder, "read_list.json", read_list)
        created.append(str(p.relative_to(SAMPLES_DIR)))

        # Read – single
        read_single = build_read_single_sample(service, entity)
        p = write_sample_file(entity_folder, "read_single.json", read_single)
        created.append(str(p.relative_to(SAMPLES_DIR)))

        # Create
        create_payload = build_create_sample(service, entity)
        p = write_sample_file(entity_folder, "create.json", create_payload)
        created.append(str(p.relative_to(SAMPLES_DIR)))

    return created


# ── Main Entry Point ────────────────────────────────────────────────────────

def generate_all_samples(
    apis_dir: Path | None = None,
    samples_dir: Path | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Parse every EDMX file in *apis_dir* and generate sample payloads.

    Parameters
    ----------
    apis_dir : Path, optional
        Directory containing .edmx files.  Defaults to ``./apis``.
    samples_dir : Path, optional
        Output root for generated samples.  Defaults to ``./samples``.
    base_url : str, optional
        Base URL prefix for sample requests.  Defaults to ``http://localhost:8000``.

    Returns
    -------
    dict with summary: services processed, files created, any errors.
    """
    global SAMPLES_DIR, BASE_URL

    _apis = apis_dir or APIS_DIR
    SAMPLES_DIR = samples_dir or SAMPLES_DIR
    BASE_URL = base_url or BASE_URL

    _apis.mkdir(parents=True, exist_ok=True)
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

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
            created = generate_samples_for_service(service)
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
        description="Generate sample API payloads from EDMX files",
    )
    parser.add_argument(
        "--apis-dir", type=Path, default=None,
        help="Path to directory containing .edmx files (default: ./apis)",
    )
    parser.add_argument(
        "--samples-dir", type=Path, default=None,
        help="Output directory for generated samples (default: ./samples)",
    )
    parser.add_argument(
        "--base-url", type=str, default=None,
        help="Base URL for sample requests (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    summary = generate_all_samples(
        apis_dir=args.apis_dir,
        samples_dir=args.samples_dir,
        base_url=args.base_url,
    )
    print(json.dumps(summary, indent=2, default=str))
