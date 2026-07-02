"""dbt manifest.json lineage extractor.

dbt models are Jinja templates that graphify's tree-sitter-sql extractor cannot
resolve - Jinja is not valid SQL syntax, and dbt only adds the CREATE TABLE/VIEW
wrapper at run time, never in the source .sql file. dbt itself already resolves
all `ref()`/`source()` lineage on every compile/run/parse into
`target/manifest.json`'s `depends_on.nodes` per model, so this extractor reads
that already-resolved lineage directly instead of re-deriving it from raw SQL.

Mirrors manifest_ingest.py's package-manifest pattern structurally: one
canonical node per dbt model/seed/snapshot/source, keyed by bare NAME (not the
unique_id, which embeds resource type and package name) via make_id - so a
model referenced from its own definition and from a downstream model's
depends_on collapses to one node. This is the same convention
graphify/extractors/lookml.py uses for sql_table_name table nodes, so a LookML
view reading `schema.prod_orders` and a dbt model named `prod_orders` resolve
to the SAME graph node once both graphs are combined with `graphify
merge-graphs` - that shared node is what makes source-to-dashboard lineage
traceable in one graph.

Excludes `test` and `analysis` resource types deliberately - they are
assertions/ad-hoc queries, not part of the physical data lineage, and a typical
project has far more tests than models, which would swamp the lineage view if
included.

Recognized by CONTENT, not filename alone: `manifest.json` is also the
standard PWA/web-app-manifest filename, so filename matching alone (the
approach manifest_ingest.py uses for unambiguous names like `go.mod`) would
misclassify unrelated files. This module reads only the first 4KB to sniff for
the `dbt_schema_version` marker key - never loads the whole (potentially
10MB+) file just to check.
"""
from __future__ import annotations

import json
from pathlib import Path

from graphify.ids import make_id

__all__ = ["is_dbt_manifest_path", "extract_dbt_manifest"]

_DBT_SCHEMA_MARKER = "dbt_schema_version"
_LINEAGE_RESOURCE_TYPES = frozenset({"model", "seed", "snapshot", "source"})


def is_dbt_manifest_path(path: Path) -> bool:
    """True only for an actual dbt manifest.json - filename AND a cheap content
    sniff (read the first 4KB, don't parse the whole 10MB+ file just to check)."""
    if path.name != "manifest.json":
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    return _DBT_SCHEMA_MARKER in head


def _node_id(name: str) -> str:
    return make_id(name)


def extract_dbt_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {"nodes": [], "edges": []}
    except json.JSONDecodeError as e:
        return {"nodes": [], "edges": [], "error": f"manifest parse error: {e}"}

    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(nid: str, label: str, extra: dict) -> None:
        if nid not in seen:
            seen.add(nid)
            node = {"id": nid, "label": label, "file_type": "code",
                     "source_file": str_path, "source_location": None}
            node.update(extra)
            nodes.append(node)

    all_entries: dict[str, dict] = dict(data.get("nodes", {}))
    all_entries.update(data.get("sources", {}))

    for unique_id, entry in all_entries.items():
        rtype = entry.get("resource_type")
        name = entry.get("name")
        if rtype not in _LINEAGE_RESOURCE_TYPES or not name:
            continue
        add_node(_node_id(name), name, {
            "type": rtype,
            "schema": entry.get("schema"),
            "database": entry.get("database"),
        })

    for unique_id, entry in data.get("nodes", {}).items():
        name = entry.get("name")
        if entry.get("resource_type") not in _LINEAGE_RESOURCE_TYPES or not name:
            continue
        nid = _node_id(name)
        for dep_unique_id in entry.get("depends_on", {}).get("nodes", []):
            dep_entry = all_entries.get(dep_unique_id)
            if not dep_entry or not dep_entry.get("name"):
                continue
            dep_nid = _node_id(dep_entry["name"])
            if dep_nid == nid:
                continue
            edges.append({
                "source": nid, "target": dep_nid, "relation": "depends_on",
                "confidence": "EXTRACTED", "source_file": str_path,
                "source_location": None, "weight": 1.0,
            })

    return {"nodes": nodes, "edges": edges}
