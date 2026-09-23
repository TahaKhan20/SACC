# EDMX API Task

This project is a generic SAP EDMX-to-FastAPI generator.

It is designed to:

* accept any SAP OData EDMX file placed in the `apis/` folder
* parse the XML schema, entity sets, keys, properties, navigation properties, and OData capability annotations
* expose a generic CRUD-style FastAPI backend using the same field names defined in the EDMX
* generate a metadata-driven frontend that adapts to the selected EDMX service and entity set
* make it easier to create custom manual APIs and test UI flows before wiring to real SAP backends

## Project files

* [main.py](#file-176967538994416) — generic backend parser, metadata inspector, and CRUD API engine
* [index.html](#file-176967538994417) — dynamic frontend that renders forms and tables from EDMX metadata
* [apis](#folder-176967538994419) — place one or more `.edmx` files here

## How it works

1. On startup, the app scans `apis/*.edmx`.
2. Each EDMX file is parsed into:
   * service metadata
   * entity sets
   * keys
   * scalar fields and types
   * navigation properties
   * CRUD capability annotations when present
3. The backend builds generic endpoints per entity set.
4. The UI reads `/api/services/{service}/metadata` and generates the forms dynamically.
5. Records are stored in-memory by default so the structure can be tested without SAP connectivity.

## Generic endpoints

For a given service and entity set:

* `GET /api/services/{service}/metadata`
* `GET /api/services/{service}/inspect`
* `GET /api/services/{service}/entities/{entity_set}`
* `POST /api/services/{service}/entities/{entity_set}`
* `GET /api/services/{service}/entities/{entity_set}/item?Key1=...&Key2=...`
* `PATCH /api/services/{service}/entities/{entity_set}/item?Key1=...&Key2=...`
* `DELETE /api/services/{service}/entities/{entity_set}/item?Key1=...&Key2=...`

## Run locally

Use a Python environment with `fastapi` and `uvicorn` installed, then run:

`python main.py`

Open the UI at:

`http://127.0.0.1:8000`

## Use with a specific EDMX file

Place your EDMX file in [apis](#folder-176967538994419), for example:

`apis/OP_PURCHASEORDER_0001.edmx`

Then reload the app or call:

`POST /api/services/reload`

If you want to load only one EDMX file at startup, set the environment variable:

`EDMX_FILE=OP_PURCHASEORDER_0001.edmx`

## Current behavior and extension points

The current implementation is intentionally generic and backend-agnostic:

* field names and response keys follow the EDMX metadata
* capability annotations are inspected and enforced when available
* list filters are generated from EDMX properties
* records are sample in-memory records, not live SAP data

To convert this into a production SAP-compatible proxy, the next extension would be:

* replace in-memory CRUD handlers with real SAP OData calls
* preserve the same metadata-driven form generation in the frontend
* optionally generate nested navigation editors for child collections
* optionally generate exact OData-style canonical route patterns per entity set

## Notes

This project is generic across EDMX files, but SAP services vary widely. Some APIs contain very large entity models and deep navigation graphs, so the UI currently focuses on root entity-set CRUD and metadata inspection first.
