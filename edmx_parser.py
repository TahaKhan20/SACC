"""Part 2a – Pure EDMX Parsing Library

Parses SAP OData **v2 and v4** EDMX files into typed metadata objects.
No FastAPI dependency – this module is a standalone, portable library.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Optional
import uuid
import xml.etree.ElementTree as ET


# ── Namespace Detection ─────────────────────────────────────────────────────

_V4_EDM = "http://docs.oasis-open.org/odata/ns/edm"
_V4_EDMX = "http://docs.oasis-open.org/odata/ns/edmx"
_SAP_NS = "http://www.sap.com/Protocols/SAPData"

# Fallback v4 namespace dict (used by parse_annotation_value for v4)
EDMX_NS: dict[str, str] = {"edmx": _V4_EDMX, "edm": _V4_EDM}


def _detect_namespaces(root: ET.Element) -> tuple[dict[str, str], int]:
    """Auto-detect EDMX/EDM namespaces from the XML root.

    Returns ``(ns_dict, odata_version)`` where *odata_version* is 2 or 4.
    """
    root_ns = root.tag[1:root.tag.index("}")] if root.tag.startswith("{") else ""

    if root_ns == _V4_EDMX:
        return {"edmx": _V4_EDMX, "edm": _V4_EDM}, 4

    # v2/v3 – discover the EDM namespace from the first Schema element
    edmx_ns = root_ns or "http://schemas.microsoft.com/ado/2007/06/edmx"
    edm_ns = ""
    for elem in root.iter():
        local = elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag
        if local == "Schema":
            if elem.tag.startswith("{"):
                edm_ns = elem.tag[1:elem.tag.index("}")]
            break

    if not edm_ns:
        edm_ns = "http://schemas.microsoft.com/ado/2008/09/edm"

    version = 4 if edm_ns == _V4_EDM else 2
    return {"edmx": edmx_ns, "edm": edm_ns}, version


# ── Data Models ─────────────────────────────────────────────────────────────

@dataclass
class PropertyMeta:
    name: str
    edm_type: str
    nullable: bool = True
    max_length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    is_collection: bool = False
    is_navigation: bool = False
    target_type: Optional[str] = None
    editable: bool = True


@dataclass
class ReferentialConstraintMeta:
    property: str
    referenced_property: str


@dataclass
class NavigationMeta:
    name: str
    target_type: str
    is_collection: bool
    nullable: bool
    partner: Optional[str]
    referential_constraints: list[ReferentialConstraintMeta]


@dataclass
class EntityMeta:
    entity_set: str
    entity_type: str
    keys: list[str]
    properties: list[PropertyMeta]
    navigation_properties: list[NavigationMeta]
    capabilities: dict[str, Any]
    annotations: dict[str, Any]


@dataclass
class ServiceMeta:
    name: str
    namespace: str
    alias: Optional[str]
    version: Optional[str]
    file_name: str
    file_path: str
    entity_sets: list[EntityMeta]


# ── Helpers ─────────────────────────────────────────────────────────────────

def strip_namespace(name: Optional[str]) -> str:
    if not name:
        return ""
    return name.split(".")[-1]


def is_collection_type(type_name: str) -> bool:
    return type_name.startswith("Collection(") and type_name.endswith(")")


def unwrap_collection_type(type_name: str) -> str:
    return type_name[len("Collection("):-1] if is_collection_type(type_name) else type_name


def get_python_type(edm_type: str) -> type:
    scalar = unwrap_collection_type(edm_type)
    mapping = {
        "Edm.String": str,
        "Edm.Guid": str,
        "Edm.Boolean": bool,
        "Edm.Byte": int,
        "Edm.SByte": int,
        "Edm.Int16": int,
        "Edm.Int32": int,
        "Edm.Int64": int,
        "Edm.Decimal": float,
        "Edm.Double": float,
        "Edm.Single": float,
        "Edm.Date": str,
        "Edm.DateTimeOffset": str,
        "Edm.TimeOfDay": str,
        "Edm.Binary": str,
        # OData v2 types
        "Edm.DateTime": str,
        "Edm.Time": str,
    }
    return mapping.get(scalar, str)


def parse_annotation_value(element: ET.Element, ns: dict[str, str] | None = None) -> Any:
    """Parse an OData v4 Annotation element value."""
    _ns = ns or EDMX_NS

    bool_value = element.attrib.get("Bool")
    if bool_value is not None:
        return bool_value.lower() == "true"

    string_value = element.attrib.get("String")
    if string_value is not None:
        return string_value

    int_value = element.attrib.get("Int")
    if int_value is not None:
        return int(int_value)

    if element.tag.endswith("Collection"):
        return [parse_annotation_value(child, _ns) for child in list(element)]

    if element.tag.endswith("Record"):
        record: dict[str, Any] = {}
        for child in element.findall("edm:PropertyValue", _ns):
            record[child.attrib.get("Property", "")] = parse_annotation_value(child, _ns)
        return record

    if list(element):
        if len(list(element)) == 1:
            return parse_annotation_value(list(element)[0], _ns)
        return [parse_annotation_value(child, _ns) for child in list(element)]

    return element.text


def default_scalar_value(edm_type: str, name: str = "") -> Any:
    scalar = unwrap_collection_type(edm_type)
    lower_name = name.lower()

    if scalar == "Edm.Boolean":
        return False
    if scalar in {"Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64"}:
        return 1
    if scalar in {"Edm.Decimal", "Edm.Double", "Edm.Single"}:
        return 0.0
    if scalar == "Edm.Date":
        return date.today().isoformat()
    if scalar in {"Edm.DateTimeOffset", "Edm.DateTime"}:
        return datetime.utcnow().isoformat()
    if scalar in {"Edm.TimeOfDay", "Edm.Time"}:
        return time(8, 0, 0).isoformat()
    if scalar == "Edm.Guid":
        return str(uuid.uuid4())

    if "item" in lower_name:
        return "00010"
    if "date" in lower_name:
        return date.today().isoformat()
    if "time" in lower_name:
        return time(8, 0, 0).isoformat()
    return name[:12] or "value"


def coerce_value(value: Any, prop: PropertyMeta) -> Any:
    if value is None:
        return None

    target_type = unwrap_collection_type(prop.edm_type)
    if prop.is_collection:
        if not isinstance(value, list):
            raise ValueError(f"{prop.name} must be a list")
        return value

    if target_type == "Edm.Boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "y"}
        return bool(value)

    if target_type in {"Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64"}:
        return int(value)

    if target_type in {"Edm.Decimal", "Edm.Double", "Edm.Single"}:
        return float(value)

    if target_type in {"Edm.Date"}:
        if isinstance(value, date):
            return value.isoformat()
        return str(value)

    if target_type in {"Edm.DateTimeOffset", "Edm.DateTime"}:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    if target_type in {"Edm.TimeOfDay", "Edm.Time"}:
        if isinstance(value, time):
            return value.isoformat()
        return str(value)

    return str(value)


# ── Capabilities ────────────────────────────────────────────────────────────

def parse_capabilities(annotations: dict[str, Any]) -> dict[str, Any]:
    """Derive capabilities from OData v4 annotation terms."""
    def restriction(term_suffix: str, key: str, fallback: bool = True) -> bool:
        record = annotations.get(term_suffix)
        if isinstance(record, dict):
            value = record.get(key)
            if isinstance(value, bool):
                return value
        return fallback

    return {
        "readable": not annotations.get("ReadRestrictions", {}).get("Readable") is False,
        "insertable": restriction("InsertRestrictions", "Insertable", True),
        "updatable": restriction("UpdateRestrictions", "Updatable", True),
        "deletable": restriction("DeleteRestrictions", "Deletable", True),
        "top_supported": annotations.get("TopSupported", True),
        "skip_supported": annotations.get("SkipSupported", True),
        "filterable": not annotations.get("FilterRestrictions", {}).get("Filterable") is False,
    }


def _sap_attr(element: ET.Element, attr: str, default: str | None = None) -> str | None:
    """Read a ``sap:attr`` value (OData v2 SAP extensions)."""
    return element.attrib.get(f"{{{_SAP_NS}}}{attr}", default)


def _sap_bool(element: ET.Element, attr: str, default: bool = True) -> bool:
    val = _sap_attr(element, attr)
    if val is None:
        return default
    return val.lower() == "true"


def parse_capabilities_v2(entity_set_elem: ET.Element) -> dict[str, Any]:
    """Derive capabilities from ``sap:`` attributes on an EntitySet (v2)."""
    return {
        "readable": True,
        "insertable": _sap_bool(entity_set_elem, "creatable", True),
        "updatable": _sap_bool(entity_set_elem, "updatable", True),
        "deletable": _sap_bool(entity_set_elem, "deletable", True),
        "top_supported": _sap_bool(entity_set_elem, "pageable", True),
        "skip_supported": _sap_bool(entity_set_elem, "pageable", True),
        "filterable": _sap_bool(entity_set_elem, "searchable", True),
    }


def _sap_annotations_v2(entity_set_elem: ET.Element) -> dict[str, Any]:
    """Collect all ``sap:`` attributes on an EntitySet as an annotation dict."""
    prefix = f"{{{_SAP_NS}}}"
    return {
        k.replace(prefix, "sap:"): v
        for k, v in entity_set_elem.attrib.items()
        if k.startswith(prefix)
    }


# ── Entity Utilities ────────────────────────────────────────────────────────

def property_dict(entity: EntityMeta) -> dict[str, PropertyMeta]:
    return {prop.name: prop for prop in entity.properties}


def editable_properties(entity: EntityMeta) -> list[PropertyMeta]:
    return [
        prop
        for prop in entity.properties
        if prop.editable and not prop.is_collection and prop.name not in {"SAP__Messages"}
    ]


def sample_record_for_entity(entity: EntityMeta, record_number: int = 1) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for prop in editable_properties(entity):
        value = default_scalar_value(prop.edm_type, prop.name)
        if prop.name in entity.keys and isinstance(value, str):
            if prop.max_length and prop.max_length <= 5 and value.isdigit():
                value = str(record_number).zfill(prop.max_length)
            elif prop.max_length and prop.max_length <= 10 and "order" in prop.name.lower():
                value = str(record_number).zfill(prop.max_length)
            else:
                value = f"{prop.name[:4].upper()}{record_number}"[:prop.max_length or 20]
        record[prop.name] = value

    for prop in entity.properties:
        if prop.is_collection:
            record[prop.name] = []
    return record


def service_to_dict(service: ServiceMeta) -> dict[str, Any]:
    """Serialize a ServiceMeta to a plain dict for JSON responses."""
    return {
        "name": service.name,
        "namespace": service.namespace,
        "alias": service.alias,
        "version": service.version,
        "file_name": service.file_name,
        "file_path": service.file_path,
        "entity_sets": [
            {
                "entity_set": entity.entity_set,
                "entity_type": entity.entity_type,
                "keys": entity.keys,
                "capabilities": entity.capabilities,
                "properties": [asdict(prop) for prop in entity.properties],
                "navigation_properties": [
                    {
                        **asdict(nav),
                        "referential_constraints": [
                            asdict(rc) for rc in nav.referential_constraints
                        ],
                    }
                    for nav in entity.navigation_properties
                ],
                "annotations": entity.annotations,
            }
            for entity in service.entity_sets
        ],
    }


# ── OData v2 Association Resolver ───────────────────────────────────────────

def _build_association_map(
    schema: ET.Element, ns: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Build a lookup from Association name → end-role metadata (v2)."""
    assoc_map: dict[str, dict[str, Any]] = {}
    edm = ns["edm"]
    for assoc in schema.findall(f"{{{edm}}}Association"):
        assoc_name = assoc.attrib.get("Name", "")
        ends: dict[str, dict[str, str]] = {}
        for end in assoc.findall(f"{{{edm}}}End"):
            role = end.attrib.get("Role", "")
            ends[role] = {
                "type": strip_namespace(end.attrib.get("Type", "")),
                "multiplicity": end.attrib.get("Multiplicity", "1"),
            }
        # Referential constraints (optional)
        rc_elem = assoc.find(f"{{{edm}}}ReferentialConstraint")
        rc_list: list[dict[str, str]] = []
        if rc_elem is not None:
            principal = rc_elem.find(f"{{{edm}}}Principal")
            dependent = rc_elem.find(f"{{{edm}}}Dependent")
            if principal is not None and dependent is not None:
                p_refs = [pr.attrib.get("Name", "") for pr in principal.findall(f"{{{edm}}}PropertyRef")]
                d_refs = [pr.attrib.get("Name", "") for pr in dependent.findall(f"{{{edm}}}PropertyRef")]
                for p, d in zip(p_refs, d_refs):
                    rc_list.append({"property": d, "referenced_property": p})
        assoc_map[assoc_name] = {"ends": ends, "referential_constraints": rc_list}
    return assoc_map


def _resolve_nav_v2(
    nav_elem: ET.Element,
    namespace: str,
    assoc_map: dict[str, dict[str, Any]],
) -> NavigationMeta:
    """Convert an OData v2 NavigationProperty into our NavigationMeta."""
    name = nav_elem.attrib.get("Name", "")
    relationship = strip_namespace(nav_elem.attrib.get("Relationship", ""))
    to_role = nav_elem.attrib.get("ToRole", "")

    target_type = ""
    is_collection = False
    rc_list: list[ReferentialConstraintMeta] = []

    assoc = assoc_map.get(relationship)
    if assoc:
        end = assoc["ends"].get(to_role, {})
        target_type = end.get("type", "")
        is_collection = end.get("multiplicity", "") == "*"
        rc_list = [
            ReferentialConstraintMeta(
                property=rc["property"],
                referenced_property=rc["referenced_property"],
            )
            for rc in assoc.get("referential_constraints", [])
        ]

    return NavigationMeta(
        name=name,
        target_type=target_type,
        is_collection=is_collection,
        nullable=True,
        partner=None,
        referential_constraints=rc_list,
    )


# ── Main Parser ─────────────────────────────────────────────────────────────

def parse_edmx_file(file_path: Path) -> ServiceMeta:
    """Parse a single OData v2 or v4 EDMX file and return a ServiceMeta."""
    tree = ET.parse(file_path)
    root = tree.getroot()
    ns, odata_version = _detect_namespaces(root)
    edm = ns["edm"]

    # ── Locate Schema ────────────────────────────────────────────────────
    schema = root.find(f".//{{{edm}}}Schema")
    if schema is None:
        raise ValueError(f"No Schema node found in {file_path.name}")

    namespace = schema.attrib.get("Namespace", file_path.stem)
    alias = schema.attrib.get("Alias")

    # ── Schema-level annotations / version (v4 only) ────────────────────
    schema_annotations: dict[str, Any] = {}
    if odata_version == 4:
        for annotation in schema.findall(f"{{{edm}}}Annotation"):
            term = strip_namespace(annotation.attrib.get("Term"))
            schema_annotations[term] = parse_annotation_value(annotation, ns)
    version = schema_annotations.get("SchemaVersion") or _sap_attr(schema, "schema-version")

    # ── Association map (v2 only) ────────────────────────────────────────
    assoc_map: dict[str, dict[str, Any]] = {}
    if odata_version == 2:
        assoc_map = _build_association_map(schema, ns)

    # ── Entity Types ────────────────────────────────────────────────────
    entity_types_by_name: dict[str, dict[str, Any]] = {}
    for entity_type in schema.findall(f"{{{edm}}}EntityType"):
        entity_type_name = entity_type.attrib["Name"]
        keys = [
            ref.attrib["Name"]
            for ref in entity_type.findall(f"{{{edm}}}Key/{{{edm}}}PropertyRef")
        ]

        properties: list[PropertyMeta] = []
        for prop in entity_type.findall(f"{{{edm}}}Property"):
            edm_type = prop.attrib.get("Type", "Edm.String")
            # For mock/testing purposes all properties are editable.
            # SAP v2 sap:updatable / sap:creatable hints are informational
            # about the real backend – they should not limit the mock UI.
            prop_editable = not prop.attrib.get("Name", "").startswith("SAP__")

            properties.append(
                PropertyMeta(
                    name=prop.attrib["Name"],
                    edm_type=edm_type,
                    nullable=prop.attrib.get("Nullable", "true").lower() != "false",
                    max_length=int(prop.attrib["MaxLength"]) if prop.attrib.get("MaxLength", "").isdigit() else None,
                    precision=int(prop.attrib["Precision"]) if prop.attrib.get("Precision", "").isdigit() else None,
                    scale=int(prop.attrib["Scale"]) if prop.attrib.get("Scale", "").isdigit() else None,
                    is_collection=is_collection_type(edm_type),
                    editable=prop_editable,
                )
            )

        navigation_properties: list[NavigationMeta] = []
        for nav in entity_type.findall(f"{{{edm}}}NavigationProperty"):
            if odata_version == 2:
                navigation_properties.append(
                    _resolve_nav_v2(nav, namespace, assoc_map)
                )
            else:
                nav_type = nav.attrib.get("Type", "")
                navigation_properties.append(
                    NavigationMeta(
                        name=nav.attrib["Name"],
                        target_type=strip_namespace(unwrap_collection_type(nav_type)),
                        is_collection=is_collection_type(nav_type),
                        nullable=nav.attrib.get("Nullable", "true").lower() != "false",
                        partner=nav.attrib.get("Partner"),
                        referential_constraints=[
                            ReferentialConstraintMeta(
                                property=rc.attrib.get("Property", ""),
                                referenced_property=rc.attrib.get("ReferencedProperty", ""),
                            )
                            for rc in nav.findall(f"{{{edm}}}ReferentialConstraint")
                        ],
                    )
                )

        entity_types_by_name[entity_type_name] = {
            "keys": keys,
            "properties": properties,
            "navigation_properties": navigation_properties,
        }

    # ── Entity Container ────────────────────────────────────────────────
    container = schema.find(f"{{{edm}}}EntityContainer")
    if container is None:
        raise ValueError(f"No EntityContainer found in {file_path.name}")

    # ── v4 Annotations blocks ───────────────────────────────────────────
    raw_annotations_by_target: dict[str, dict[str, Any]] = {}
    if odata_version == 4:
        for annotations_block in schema.findall(f"{{{edm}}}Annotations"):
            target = annotations_block.attrib.get("Target", "")
            target_name = target.split("/")[-1]
            raw_annotations_by_target.setdefault(target_name, {})
            for annotation in annotations_block.findall(f"{{{edm}}}Annotation"):
                term = strip_namespace(annotation.attrib.get("Term"))
                raw_annotations_by_target[target_name][term] = parse_annotation_value(annotation, ns)

    # ── Entity Sets ─────────────────────────────────────────────────────
    entity_sets: list[EntityMeta] = []
    for entity_set in container.findall(f"{{{edm}}}EntitySet"):
        entity_set_name = entity_set.attrib["Name"]
        entity_type_name = strip_namespace(entity_set.attrib.get("EntityType", ""))
        type_meta = entity_types_by_name.get(entity_type_name)
        if not type_meta:
            continue

        if odata_version == 2:
            capabilities = parse_capabilities_v2(entity_set)
            annotations = _sap_annotations_v2(entity_set)
        else:
            annotations = raw_annotations_by_target.get(entity_set_name, {})
            capabilities = parse_capabilities(annotations)

        entity_sets.append(
            EntityMeta(
                entity_set=entity_set_name,
                entity_type=entity_type_name,
                keys=type_meta["keys"],
                properties=type_meta["properties"],
                navigation_properties=type_meta["navigation_properties"],
                capabilities=capabilities,
                annotations=annotations,
            )
        )

    return ServiceMeta(
        name=file_path.stem,
        namespace=namespace,
        alias=alias,
        version=version,
        file_name=file_path.name,
        file_path=str(file_path),
        entity_sets=entity_sets,
    )
