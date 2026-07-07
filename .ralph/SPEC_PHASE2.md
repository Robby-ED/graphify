# Phase 2 spec: dbt manifest.json lineage extractor for graphify

Self-contained implementation spec for `.ralph/prd.json`'s stories US-009–US-011 on branch
`feat/dbt-manifest-extractor` (branched independently from `v8`, NOT from `feat/lookml-extractor` —
these ship as two separate upstream PRs per this project's "one language/feature per PR" convention
in `graphify/extractors/MIGRATION.md`).

## Why this extractor, and the constraints that shaped it

- dbt models are Jinja templates (`{{ ref('x') }}`, `{{ source('a','b') }}`, `{{ config(...) }}`).
  graphify's existing `extract_sql` (tree-sitter-sql) cannot parse Jinja — verified empirically in a
  prior session: running it against two real dbt model files produced 1 node / 0 edges each (just
  the bare file node), because the extractor only activates on top-level `CREATE TABLE`/`CREATE
  VIEW`/etc. statements, and dbt model files never contain those (dbt adds the wrapper at run time).
- dbt itself already resolves all of this on every `dbt compile`/`dbt run`/`dbt parse`, writing the
  resolved dependency graph into `target/manifest.json`'s `depends_on.nodes` per model. This
  extractor reads that already-resolved lineage directly — no Jinja parsing needed at all.
- `target/` is dbt's standard build directory and is gitignored by convention in dbt projects, so
  `manifest.json` will never surface via graphify's normal recursive `/graphify .` directory walk —
  it must be pointed at explicitly (e.g. `graphify path/to/target/manifest.json`).
- **Filename alone is NOT sufficient for detection.** `manifest.json` is ALSO the standard filename
  for a web-app/PWA manifest (`{"name": "My App", "short_name": "App", ...}`) — a completely
  different, unrelated JSON format that could appear in any JS/web project. Detection must sniff
  file CONTENT (the dbt-specific `dbt_schema_version` key), never filename alone.
- The existing `graphify/manifest_ingest.py` (package-manifest lineage: `pyproject.toml`/`go.mod`/
  `pom.xml`/`apm.yml` → one canonical node per package + `depends_on` edges) is the closest existing
  pattern in this codebase and should be mirrored structurally, but it is NOT reused directly — it
  dispatches by filename only (safe for its own manifest names, which have no collision risk), which
  is exactly the pattern that would misfire on `manifest.json`. This extractor needs its own content-
  sniffing gate, hence a new sibling module (`graphify/dbt_manifest_ingest.py`), not an addition to
  `manifest_ingest.py`.

## Node ID scoping — read this before writing any `make_id` call

- Every model/seed/snapshot/source node is keyed by **bare NAME only** (`make_id(name)`), matching
  the exact convention already used by `graphify/extractors/lookml.py` (already merged on
  `feat/lookml-extractor`) for its `sql_table_name`-derived table nodes. This is deliberate: it is
  what lets `graphify merge-graphs` collapse a LookML view's backing table and a dbt model of the
  same name into ONE node in a combined graph, without any special-cased cross-extractor edge type.
  Do not scope by `unique_id` (which embeds resource type + package name, e.g.
  `model.talent_acquisition.prod_orders`) — that would prevent the cross-repo merge from working.
- `test` and `analysis` resource types are excluded entirely — verified against a real 1600+-node
  project manifest: 625 `test` nodes and 232 `analysis` nodes vs. 764 `model` nodes. Including tests
  would swamp the lineage view with assertions that aren't part of physical data lineage.

## The full extractor implementation

Create `graphify/dbt_manifest_ingest.py` (top-level module, sibling to `manifest_ingest.py` and
`mcp_ingest.py` — NOT under `graphify/extractors/`, because like those two it is dispatched by
content-sniffing ahead of suffix-based dispatch, not registered as a per-suffix language extractor):

```python
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
```

## Tests (create `tests/test_dbt_manifest.py`, mirroring `tests/test_manifest_ingest.py`'s
`tmp_path`-synthetic-file convention exactly — read that file first for the pattern)

```python
from __future__ import annotations

import json
from pathlib import Path

from graphify.extract import extract_dbt_manifest
from graphify.dbt_manifest_ingest import is_dbt_manifest_path


def _write_manifest(p: Path, nodes: dict, sources: dict | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"},
        "nodes": nodes,
        "sources": sources or {},
    }), encoding="utf-8")
    return p


def _sample_nodes():
    return {
        "model.proj.prod_orders": {
            "resource_type": "model", "name": "prod_orders",
            "schema": "analytics", "database": "warehouse",
            "depends_on": {"nodes": ["model.proj.stg_orders"]},
        },
        "model.proj.stg_orders": {
            "resource_type": "model", "name": "stg_orders",
            "schema": "staging", "database": "warehouse",
            "depends_on": {"nodes": ["source.proj.raw.orders"]},
        },
        "test.proj.not_null_orders_id": {
            "resource_type": "test", "name": "not_null_orders_id",
            "depends_on": {"nodes": ["model.proj.prod_orders"]},
        },
    }


def _sample_sources():
    return {
        "source.proj.raw.orders": {
            "resource_type": "source", "name": "orders",
            "source_name": "raw", "identifier": "orders",
        },
    }


# ── routing: filename alone is NOT enough (PWA manifest.json collision) ──────

def test_dbt_manifest_recognized_by_content_not_filename_alone(tmp_path):
    dbt_path = _write_manifest(tmp_path / "manifest.json", _sample_nodes(), _sample_sources())
    assert is_dbt_manifest_path(dbt_path)

    pwa_path = tmp_path / "pwa" / "manifest.json"
    pwa_path.parent.mkdir()
    pwa_path.write_text('{"name": "My App", "short_name": "App", "start_url": "/"}', encoding="utf-8")
    assert not is_dbt_manifest_path(pwa_path)


# ── lineage extraction ────────────────────────────────────────────────────────

def test_dbt_manifest_model_and_source_nodes(tmp_path):
    p = _write_manifest(tmp_path / "manifest.json", _sample_nodes(), _sample_sources())
    r = extract_dbt_manifest(p)
    labels = [n["label"] for n in r["nodes"]]
    assert "prod_orders" in labels
    assert "stg_orders" in labels
    assert "orders" in labels  # the source, bare name

def test_dbt_manifest_depends_on_edges(tmp_path):
    p = _write_manifest(tmp_path / "manifest.json", _sample_nodes(), _sample_sources())
    r = extract_dbt_manifest(p)
    node_by_id = {n["id"]: n["label"] for n in r["nodes"]}
    deps = {(node_by_id[e["source"]], node_by_id[e["target"]])
            for e in r["edges"] if e["relation"] == "depends_on"}
    assert ("prod_orders", "stg_orders") in deps
    assert ("stg_orders", "orders") in deps

def test_dbt_manifest_excludes_tests_and_analyses(tmp_path):
    p = _write_manifest(tmp_path / "manifest.json", _sample_nodes(), _sample_sources())
    r = extract_dbt_manifest(p)
    labels = [n["label"] for n in r["nodes"]]
    assert "not_null_orders_id" not in labels

def test_dbt_manifest_missing_file_returns_empty():
    r = extract_dbt_manifest(Path("nonexistent/manifest.json"))
    assert r["nodes"] == []
    assert r["edges"] == []

def test_dbt_manifest_no_dangling_edges(tmp_path):
    p = _write_manifest(tmp_path / "manifest.json", _sample_nodes(), _sample_sources())
    r = extract_dbt_manifest(p)
    node_ids = {n["id"] for n in r["nodes"]}
    for e in r["edges"]:
        assert e["source"] in node_ids
        assert e["target"] in node_ids
```

Note: `extract_dbt_manifest` is imported from `graphify.extract` (the facade), matching how every
other extractor is imported across the test suite — this requires a facade re-export in
`graphify/extract.py` (see Wiring below) BEFORE these tests can even import successfully. `import
is_dbt_manifest_path` directly from `graphify.dbt_manifest_ingest` (no facade needed for that one —
it's an internal routing helper, not something downstream code calls directly, matching how
`is_package_manifest_path` is only ever imported from `graphify.manifest_ingest` in the existing
test suite, never re-exported).

## Wiring (`graphify/extract.py` only — one file, two small edits)

1. Add the import near the other content-sniffed ingestors, right after the existing
   `manifest_ingest` import:

```python
from .dbt_manifest_ingest import extract_dbt_manifest, is_dbt_manifest_path  # noqa: F401
```

2. Add the dispatch precedence check immediately BEFORE the existing `is_package_manifest_path`
   check (same priority tier — both are content/filename-sniffed checks that must win over generic
   suffix dispatch, and dbt's manifest.json must be checked before the generic package-manifest
   check purely for ordering clarity, though there's no actual name collision between the two check
   functions):

```python
    if is_dbt_manifest_path(path):
        return extract_dbt_manifest
    # Package manifests (apm.yml, pyproject.toml, go.mod, pom.xml) → a canonical
    # package node + depends_on edges, by filename before generic suffix dispatch
    if is_package_manifest_path(path):
        return extract_package_manifest
```

Do not touch `graphify/detect.py`, `graphify/extractors/__init__.py`, or `pyproject.toml` — unlike
Phase 1's LookML extractor, this one needs no new optional dependency (uses only `json` and
`pathlib`, both stdlib) and is not suffix-dispatched, so none of those files apply here.

## Final verification (smoke test against a real production dbt manifest)

A real dbt project's compiled manifest exists on this machine, read-only, at
`~/Documents/GitHub/edna-dbt-talent-acquisition/target/manifest.json` (do not modify anything in
that repo — read-only access only). Run `extract_dbt_manifest` against it directly and confirm:

- No `error` key.
- A specific, previously-traced real lineage edge is present: a node labeled
  `prod_zkipster_session_status_history` has a `depends_on` edge to a node labeled
  `stg_zkipster_session_status_history`.
- Node count is well under the file's total node count (confirms the test/analysis exclusion is
  actually filtering, not silently including everything) — expect roughly half or fewer of the raw
  manifest's total `nodes` dict size once `test`/`analysis` are excluded.
