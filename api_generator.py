"""Part 2b – Portable API Router Generator

Reads parsed EDMX metadata and creates a FastAPI APIRouter for each service.
Each router is self-contained and can be mounted on any FastAPI app.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from edmx_parser import (
    EntityMeta,
    ServiceMeta,
    coerce_value,
    default_scalar_value,
    editable_properties,
    get_python_type,
    property_dict,
    sample_record_for_entity,
    service_to_dict,
)


# ── In-memory data store (shared across all routers) ─────────────────────

DATA_STORE: dict[str, dict[str, list[dict[str, Any]]]] = {}


def init_data_for_service(service: ServiceMeta) -> None:
    """Seed a single sample record per entity set."""
    DATA_STORE[service.name] = {}
    for entity in service.entity_sets:
        DATA_STORE[service.name][entity.entity_set] = [
            sample_record_for_entity(entity)
        ]


def reset_all_data(services: dict[str, ServiceMeta]) -> None:
    """Re-seed data for every registered service."""
    DATA_STORE.clear()
    for service in services.values():
        init_data_for_service(service)


# ── Internal helpers ───────────────────────────────────────────────────────

def _get_entity_or_404(service: ServiceMeta, entity_set: str) -> EntityMeta:
    for entity in service.entity_sets:
        if entity.entity_set == entity_set:
            return entity
    raise HTTPException(
        status_code=404,
        detail=f"Entity set '{entity_set}' not found in service '{service.name}'",
    )


def _normalize_payload(
    entity: EntityMeta, payload: dict[str, Any], partial: bool = False
) -> dict[str, Any]:
    allowed = property_dict(entity)
    unknown_fields = sorted(set(payload).difference(allowed))
    if unknown_fields:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown field(s): {', '.join(unknown_fields)}",
        )

    normalized: dict[str, Any] = {}
    for prop in editable_properties(entity):
        if prop.name in payload:
            try:
                normalized[prop.name] = coerce_value(payload[prop.name], prop)
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid value for field '{prop.name}': {exc}",
                ) from exc
        elif not partial:
            if prop.name in entity.keys or not prop.nullable:
                normalized[prop.name] = default_scalar_value(prop.edm_type, prop.name)

    for prop in entity.properties:
        if prop.is_collection and prop.name in payload:
            normalized[prop.name] = payload[prop.name]
        elif prop.is_collection and not partial:
            normalized[prop.name] = []
    return normalized


def _extract_key_values(entity: EntityMeta, request: Request) -> dict[str, Any]:
    keys: dict[str, Any] = {}
    prop_map = property_dict(entity)
    missing: list[str] = []
    for key in entity.keys:
        raw = request.query_params.get(key)
        if raw is None:
            missing.append(key)
            continue
        keys[key] = coerce_value(raw, prop_map[key])

    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing key query parameter(s): {', '.join(missing)}",
        )
    return keys


def _find_record(
    service_name: str, entity: EntityMeta, key_values: dict[str, Any]
) -> dict[str, Any]:
    for record in DATA_STORE[service_name][entity.entity_set]:
        if all(
            str(record.get(key)) == str(value)
            for key, value in key_values.items()
        ):
            return record
    raise HTTPException(
        status_code=404,
        detail=f"Record not found in '{entity.entity_set}' for keys {key_values}",
    )


# ── Router Factory ───────────────────────────────────────────────────────

def create_service_router(service: ServiceMeta) -> APIRouter:
    """Create a portable FastAPI router for a single EDMX service.

    The returned router is self-contained and can be mounted on any
    FastAPI application under any prefix.

    Usage:
        router = create_service_router(my_service)
        app.include_router(router, prefix="/api/services/my_service")
    """
    router = APIRouter(tags=[service.name])
    svc_name = service.name

    # -- Metadata ----------------------------------------------------------

    @router.get("/metadata")
    def get_metadata() -> dict[str, Any]:
        return service_to_dict(service)

    @router.get("/inspect")
    def inspect() -> dict[str, Any]:
        return {
            "service": service.name,
            "file_name": service.file_name,
            "namespace": service.namespace,
            "version": service.version,
            "entity_sets": [
                {
                    "entity_set": entity.entity_set,
                    "keys": entity.keys,
                    "property_count": len(entity.properties),
                    "navigation_count": len(entity.navigation_properties),
                    "capabilities": entity.capabilities,
                    "field_names": [prop.name for prop in entity.properties],
                }
                for entity in service.entity_sets
            ],
        }

    # -- List Records ------------------------------------------------------

    @router.get("/entities/{entity_set}")
    def list_records(
        entity_set: str,
        request: Request,
        top: int = Query(100, alias="$top", ge=1, le=1000),
        skip: int = Query(0, alias="$skip", ge=0),
    ):
        entity = _get_entity_or_404(service, entity_set)
        records = DATA_STORE[svc_name][entity_set]
        filtered = records
        prop_map = property_dict(entity)

        if entity.capabilities.get("filterable", True):
            reserved = {"$top", "$skip"}
            query_filters = {
                key: value
                for key, value in request.query_params.items()
                if key not in reserved and key in prop_map
            }
            for field_name, raw_value in query_filters.items():
                prop = prop_map[field_name]
                expected = str(coerce_value(raw_value, prop)).lower()
                if get_python_type(prop.edm_type) is str:
                    filtered = [
                        r for r in filtered
                        if expected in str(r.get(field_name, "")).lower()
                    ]
                else:
                    filtered = [
                        r for r in filtered
                        if str(r.get(field_name)) == str(coerce_value(raw_value, prop))
                    ]

        return filtered[skip : skip + top]

    # -- Get Single Record -------------------------------------------------

    @router.get("/entities/{entity_set}/item")
    def get_record(
        entity_set: str, request: Request
    ) -> dict[str, Any]:
        entity = _get_entity_or_404(service, entity_set)
        key_values = _extract_key_values(entity, request)
        return _find_record(svc_name, entity, key_values)

    # -- Create Record -----------------------------------------------------

    @router.post("/entities/{entity_set}")
    def create_record(
        entity_set: str,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        entity = _get_entity_or_404(service, entity_set)
        record = _normalize_payload(entity, payload, partial=False)
        if entity.keys:
            key_signature = {key: record.get(key) for key in entity.keys}
            try:
                _find_record(svc_name, entity, key_signature)
                raise HTTPException(
                    status_code=409,
                    detail=f"Record already exists for keys {key_signature}",
                )
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise

        DATA_STORE[svc_name][entity_set].append(record)
        return record

    # -- Update Record -----------------------------------------------------

    @router.patch("/entities/{entity_set}/item")
    def update_record(
        entity_set: str,
        request: Request,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        entity = _get_entity_or_404(service, entity_set)
        key_values = _extract_key_values(entity, request)
        record = _find_record(svc_name, entity, key_values)
        updates = _normalize_payload(entity, payload, partial=True)
        record.update(updates)
        return record

    # -- Delete Record -----------------------------------------------------

    @router.delete("/entities/{entity_set}/item", status_code=204)
    def delete_record(entity_set: str, request: Request):
        entity = _get_entity_or_404(service, entity_set)
        key_values = _extract_key_values(entity, request)
        record = _find_record(svc_name, entity, key_values)
        DATA_STORE[svc_name][entity_set].remove(record)
        return None

    return router
