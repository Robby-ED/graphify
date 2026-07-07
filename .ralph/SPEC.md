# Phase 1 spec: LookML extractor for graphify

This is the self-contained implementation spec for `.ralph/prd.json`'s stories US-001–US-008.
Read this file in full before starting any story — it contains verified design decisions (bare-name
ID scoping, the `lkml` parser gap workaround, the dual dashboard-format fallback) discovered by
testing against real production LookML files in a prior session. Do not redesign from scratch;
implement exactly what's specified here.

## Why this extractor, and the constraints that shaped it

- No tree-sitter grammar exists for LookML anywhere. Use `lkml` (PyPI, pure Python, recursive-descent
  parser) instead.
- `.dashboard.lookml` has TWO incompatible grammars in real use: brace-syntax "dashboards-next"
  (parses via `lkml`) and legacy list-of-dicts YAML starting with `---` (parses via PyYAML). Try
  `lkml` first, fall back to `yaml.safe_load`.
- A real, confirmed gap in the `lkml` package itself: `sorts: [field asc|desc]` (a common, valid
  LookML construct) raises `SyntaxError` in `lkml.load()`. Strip direction suffixes with a targeted
  regex before parsing (implementation below) — this is a workaround for `lkml`, not a bug in this
  extractor.
- Read `ARCHITECTURE.md` and `graphify/extractors/MIGRATION.md` in this repo before writing any code
  — this project is mid-migration from a monolithic `graphify/extract.py` to one-module-per-language
  under `graphify/extractors/`. New languages go directly in the new location (mirror `zig.py`).

## Node ID scoping — read this before writing any `_make_id` call

- Views and tables are scoped by **bare name only** (`_make_id(name)`), not file path. Looker
  enforces globally unique view names project-wide (via `include:` globs), and this also
  deliberately makes a LookML view's `sql_table_name` table node collide/merge with a same-named
  node from other extractors in a combined graph — that's intentional, not a bug to fix.
- Explores and dashboards get an `explore_`/`dashboard_` prefix (`_make_id("explore", name)`,
  `_make_id("dashboard", name)`) specifically because an explore commonly shares a bare name with its
  base view (an explore with no `from:` defaults to its own name) and would otherwise collide into
  one node conflating two different LookML constructs. This was verified against a real explore
  (`zkipster_session_status_asof`, which has no `from:` key in its parsed dict at all).

## The full extractor implementation

Create `graphify/extractors/lookml.py`:

```python
"""LookML extractor (lkml + PyYAML - no tree-sitter grammar exists for LookML).

Parses `.view.lkml` / `.model.lkml` files with the brace-syntax `lkml` parser.
`.dashboard.lookml` has two incompatible grammars in the wild: "dashboards-next"
brace syntax (parsed by `lkml`) and legacy list-of-dicts YAML starting with `---`
(parsed by PyYAML). We try `lkml` first (with a workaround for a real `lkml`
parser gap - see `_strip_sort_directions`), then fall back to YAML, then give up
gracefully.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graphify.extractors.base import _make_id

_FIELD_REF_RE = re.compile(r"\$\{(\w+(?:\.\w+)?)\}")
_CTE_NAME_RE = re.compile(r"(?:with|,)\s+(\w+)\s+as\s*\(", re.IGNORECASE)
_FROM_JOIN_RE = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)", re.IGNORECASE)
_SORTS_RE = re.compile(r"sorts\s*:\s*\[([^\]]*)\]", re.IGNORECASE)
_SORT_DIR_RE = re.compile(r"\s+(?:asc|desc)\b", re.IGNORECASE)


def _explore_id(name: str) -> str:
    return _make_id("explore", name)


def _dashboard_id(name: str) -> str:
    return _make_id("dashboard", name)


def _strip_sort_directions(text: str) -> str:
    """lkml's grammar rejects `sorts: [field asc|desc]` direction suffixes inside
    a plain list (SyntaxError: "Unable to find a matching expression for
    '<literal>'" - a real gap in the lkml package, verified against a minimal
    repro). Strip them before parsing; harmless on view/model files, which have
    no `sorts:` parameter at all."""
    return _SORTS_RE.sub(lambda m: f"sorts: [{_SORT_DIR_RE.sub('', m.group(1))}]", text)


def _first_str(value: Any) -> str | None:
    """lkml pluralizes a single repeatable param into a list
    (`explore: orders` -> `{"explores": ["orders"]}`); PyYAML (legacy dashboard
    format) leaves it as a plain string. Normalize both to one string."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _bare_table_name(qualified: str) -> str:
    """`sql_table_name: schema.table` -> `table` (also strips a dbt-style
    `db.schema.table` triple). Always take the last dot-segment."""
    return qualified.strip().split(".")[-1]


def _scan_derived_table_refs(sql: str) -> list[str]:
    """Best-effort FROM/JOIN table scan for `derived_table: { sql: ... }` bodies.
    INFERRED confidence (regex heuristic) - excludes CTE names defined via
    `WITH x AS (` / `, x AS (` in the same body so a CTE alias is never mistaken
    for a physical table."""
    cte_names = {m.group(1).lower() for m in _CTE_NAME_RE.finditer(sql)}
    found: list[str] = []
    for m in _FROM_JOIN_RE.finditer(sql):
        bare = _bare_table_name(m.group(1))
        if bare.lower() not in cte_names and bare not in found:
            found.append(bare)
    return found


def extract_lookml(path: Path) -> dict:
    """Extract views, explores, dashboards, and the fields/tables/joins between
    them from `.view.lkml`, `.model.lkml`, and `.dashboard.lookml` files.
    See the ID-scoping notes in .ralph/SPEC.md before modifying this function.
    """
    try:
        import lkml
    except ImportError:
        return {"nodes": [], "edges": [], "error": "lkml not installed. Run: pip install lkml"}

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": []}

    str_path = str(path)
    file_nid = _make_id(str_path)
    nodes: list[dict] = [{"id": file_nid, "label": path.name, "file_type": "code",
                           "source_file": str_path, "source_location": None}]
    edges: list[dict] = []
    seen_ids: set[str] = {file_nid}

    def add_node(nid: str, label: str) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                           "source_file": str_path, "source_location": None})

    def add_edge(src: str, tgt: str, relation: str, confidence: str = "EXTRACTED") -> None:
        edges.append({"source": src, "target": tgt, "relation": relation,
                       "confidence": confidence, "source_file": str_path,
                       "source_location": None, "weight": 1.0})

    def handle_view(v: dict) -> None:
        view_name = v.get("name")
        if not view_name:
            return
        view_nid = _make_id(view_name)
        add_node(view_nid, view_name)
        add_edge(file_nid, view_nid, "contains")

        if v.get("sql_table_name"):
            table = _bare_table_name(v["sql_table_name"])
            table_nid = _make_id(table)
            add_node(table_nid, table)
            add_edge(view_nid, table_nid, "queries")

        for group in v.get("extends__all", []):
            for base in group:
                base_nid = _make_id(base)
                add_node(base_nid, base)
                add_edge(view_nid, base_nid, "extends")

        dt = v.get("derived_table")
        if dt and dt.get("sql"):
            for table in _scan_derived_table_refs(dt["sql"]):
                table_nid = _make_id(table)
                add_node(table_nid, table)
                add_edge(view_nid, table_nid, "queries", confidence="INFERRED")

        for field_key in ("dimensions", "dimension_groups", "measures", "parameters"):
            for f in v.get(field_key, []):
                fname = f.get("name")
                if not fname:
                    continue
                field_nid = _make_id(view_name, fname)
                add_node(field_nid, f"{view_name}.{fname}")
                add_edge(view_nid, field_nid, "contains")
                if f.get("sql"):
                    for m in _FIELD_REF_RE.finditer(f["sql"]):
                        token = m.group(1)
                        if token == "TABLE":
                            continue
                        ref_view, ref_field = token.split(".", 1) if "." in token else (view_name, token)
                        ref_nid = _make_id(ref_view, ref_field)
                        add_node(ref_nid, f"{ref_view}.{ref_field}")
                        add_edge(field_nid, ref_nid, "references", confidence="INFERRED")
                for filter_group in f.get("filters__all", []):
                    for filter_dict in filter_group:
                        for filt_field in filter_dict:
                            filt_nid = _make_id(view_name, filt_field)
                            add_node(filt_nid, f"{view_name}.{filt_field}")
                            add_edge(field_nid, filt_nid, "filters_on")

        for s in v.get("sets", []):
            sname = s.get("name")
            if not sname:
                continue
            set_nid = _make_id(view_name, "set", sname)
            add_node(set_nid, f"{view_name}.{sname} (set)")
            add_edge(view_nid, set_nid, "contains")
            for fname in s.get("fields", []):
                if fname.endswith("*"):
                    tgt_nid = _make_id(view_name, "set", fname[:-1])
                    add_node(tgt_nid, f"{view_name}.{fname[:-1]} (set)")
                else:
                    tgt_nid = _make_id(view_name, fname)
                    add_node(tgt_nid, f"{view_name}.{fname}")
                add_edge(set_nid, tgt_nid, "contains")

    def handle_explore(e: dict) -> None:
        explore_name = e.get("name")
        if not explore_name:
            return
        explore_nid = _explore_id(explore_name)
        add_node(explore_nid, f"explore: {explore_name}")
        add_edge(file_nid, explore_nid, "contains")

        base_view = e.get("from") or e.get("view_name")
        confidence = "EXTRACTED"
        if not base_view:
            base_view, confidence = explore_name, "INFERRED"
        base_nid = _make_id(base_view)
        add_node(base_nid, base_view)
        add_edge(explore_nid, base_nid, "from", confidence=confidence)

        for j in e.get("joins", []):
            join_view = j.get("name")
            if join_view:
                join_nid = _make_id(join_view)
                add_node(join_nid, join_view)
                add_edge(explore_nid, join_nid, "joins")

    def handle_dashboard(name: str, elements: list[dict]) -> None:
        dash_nid = _dashboard_id(name)
        add_node(dash_nid, f"dashboard: {name}")
        add_edge(file_nid, dash_nid, "contains")
        for el in elements or []:
            el_name = el.get("name") or "untitled"
            el_nid = _make_id("dashboard", name, el_name)
            add_node(el_nid, f"{name}.{el_name}")
            add_edge(dash_nid, el_nid, "contains")

            explore_name = _first_str(el.get("explore")) or _first_str(el.get("explores"))
            if explore_name:
                add_edge(el_nid, _explore_id(explore_name), "uses")

            for fname in el.get("fields", []) or []:
                if "." in fname:
                    fview, ffield = fname.split(".", 1)
                    tgt_nid = _make_id(fview, ffield)
                    add_node(tgt_nid, fname)
                    add_edge(el_nid, tgt_nid, "uses")

    def try_lkml(text: str):
        try:
            return lkml.load(text)
        except Exception:
            return None

    parsed = try_lkml(raw) or try_lkml(_strip_sort_directions(raw))

    if parsed is not None:
        for v in parsed.get("views", []) or []:
            handle_view(v)
        for e in parsed.get("explores", []) or []:
            handle_explore(e)
        dash_dicts = list(parsed.get("dashboards", []) or [])
        if isinstance(parsed.get("dashboard"), dict):
            dash_dicts.append(parsed["dashboard"])
        for d in dash_dicts:
            dname = d.get("name")
            if dname:
                handle_dashboard(dname, d.get("elements") or ([d["element"]] if d.get("element") else []))
        return {"nodes": nodes, "edges": edges}

    try:
        import yaml
    except ImportError:
        return {"nodes": nodes, "edges": edges, "error": "pyyaml not installed. Run: pip install pyyaml"}

    try:
        legacy = yaml.safe_load(raw)
    except Exception as e:
        return {"nodes": nodes, "edges": edges, "error": f"unparseable LookML: {e}"}

    if isinstance(legacy, list):
        for d in legacy:
            if isinstance(d, dict) and d.get("dashboard"):
                handle_dashboard(d["dashboard"], d.get("elements") or [])

    return {"nodes": nodes, "edges": edges}
```

## Test fixtures (create exactly as specified — used across multiple stories)

`tests/fixtures/sample.view.lkml`:

```lookml
view: orders {
  sql_table_name: analytics.orders ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: status {
    type: string
    sql: ${TABLE}.status ;;
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, week, month]
    sql: ${TABLE}.created_at ;;
  }

  measure: count {
    type: count
  }

  measure: completed_count {
    type: count
    filters: [status: "completed"]
  }

  measure: completion_rate {
    type: number
    sql: 1.0 * ${completed_count} / nullif(${count}, 0) ;;
  }

  set: detail {
    fields: [id, status, created_date]
  }
}

view: orders_summary {
  extends: [orders]

  derived_table: {
    sql:
      with recent as (
        select * from orders where created_at > dateadd('day', -30, current_date)
      )
      select customer_id, count(*) as order_count
      from recent
      join customers on recent.customer_id = customers.id
      group by 1 ;;
  }

  dimension: customer_id {
    type: number
    sql: ${TABLE}.customer_id ;;
  }

  measure: order_count {
    type: sum
    sql: ${TABLE}.order_count ;;
  }
}
```

`tests/fixtures/sample.model.lkml`:

```lookml
connection: "ecommerce_warehouse"

include: "*.view.lkml"

explore: orders {
}

explore: orders_summary {
  join: customers {
    type: left_outer
    sql_on: ${orders_summary.customer_id} = ${customers.id} ;;
    relationship: many_to_one
  }
}
```

`tests/fixtures/sample.dashboard.lookml` (brace/"dashboards-next" format — deliberately includes
`sorts: [... desc]` to exercise the workaround):

```lookml
dashboard: orders_overview {
  title: "Orders Overview"
  layout: newspaper

  element: {
    name: orders_by_status
    type: looker_grid
    model: ecommerce
    explore: orders
    fields: [orders.status, orders.count]
    sorts: [orders.count desc]
    row: 0
    col: 0
    width: 12
    height: 6
  }
}
```

`tests/fixtures/sample_legacy.dashboard.lookml` (legacy YAML format):

```lookml
---
- dashboard: orders_overview_legacy
  title: Orders Overview (Legacy)
  layout: newspaper
  preferred_viewer: dashboards-next
  filters: []
  elements:
  - name: banner
    type: text
    title_text: ''
    body_text: Orders dashboard
  - name: orders_by_status
    type: vis
    model: ecommerce
    explore: orders
    fields: [orders.status, orders.count]
```

## Tests (add to `tests/test_languages.py`)

Import `extract_lookml` from `graphify.extract` (the facade re-export), matching how every other
extractor is imported in that file. Add near the Apex section:

```python
_needs_lkml = pytest.mark.skipif(
    _ilu.find_spec("lkml") is None,
    reason="lkml not installed (optional [lookml] extra)",
)


@_needs_lkml
def test_lookml_view_and_table_extraction():
    r = extract_lookml(FIXTURES / "sample.view.lkml")
    labels = _labels(r)
    assert "orders" in labels
    assert "orders_summary" in labels

@_needs_lkml
def test_lookml_dimension_and_measure_extraction():
    r = extract_lookml(FIXTURES / "sample.view.lkml")
    labels = _labels(r)
    assert "orders.status" in labels
    assert "orders.completed_count" in labels

@_needs_lkml
def test_lookml_measure_references_other_measures():
    r = extract_lookml(FIXTURES / "sample.view.lkml")
    refs = _references(r)
    assert any(s == "orders.completion_rate" and t == "orders.completed_count" for s, t, _ in refs)
    assert any(s == "orders.completion_rate" and t == "orders.count" for s, t, _ in refs)

@_needs_lkml
def test_lookml_measure_filter_edge():
    r = extract_lookml(FIXTURES / "sample.view.lkml")
    node_by_id = {n["id"]: n["label"] for n in r["nodes"]}
    filt_edges = _edges_with_relation(r, "filters_on")
    assert any(node_by_id[e["source"]] == "orders.completed_count"
               and node_by_id[e["target"]] == "orders.status" for e in filt_edges)

@_needs_lkml
def test_lookml_extends_edge():
    r = extract_lookml(FIXTURES / "sample.view.lkml")
    assert "extends" in _relations(r)

@_needs_lkml
def test_lookml_derived_table_excludes_cte_includes_real_table():
    r = extract_lookml(FIXTURES / "sample.view.lkml")
    labels = _labels(r)
    assert "customers" in labels
    assert "recent" not in labels

@_needs_lkml
def test_lookml_set_extraction():
    r = extract_lookml(FIXTURES / "sample.view.lkml")
    assert "orders.detail (set)" in _labels(r)

@_needs_lkml
def test_lookml_missing_file_returns_empty():
    r = extract_lookml(Path("nonexistent.view.lkml"))
    assert r["nodes"] == []
    assert r["edges"] == []

@_needs_lkml
def test_lookml_explore_defaults_to_own_view():
    r = extract_lookml(FIXTURES / "sample.model.lkml")
    node_by_id = {n["id"]: n["label"] for n in r["nodes"]}
    from_edges = [e for e in _edges_with_relation(r, "from")
                  if node_by_id[e["source"]] == "explore: orders"]
    assert from_edges and node_by_id[from_edges[0]["target"]] == "orders"
    assert from_edges[0]["confidence"] == "INFERRED"

@_needs_lkml
def test_lookml_explore_join_extraction():
    r = extract_lookml(FIXTURES / "sample.model.lkml")
    assert "joins" in _relations(r)
    assert "customers" in _labels(r)

@_needs_lkml
def test_lookml_dashboard_brace_format_with_sort_direction():
    r = extract_lookml(FIXTURES / "sample.dashboard.lookml")
    assert "dashboard: orders_overview" in _labels(r)
    assert len(_edges_with_relation(r, "uses")) >= 1

@_needs_lkml
def test_lookml_legacy_yaml_dashboard_extraction():
    r = extract_lookml(FIXTURES / "sample_legacy.dashboard.lookml")
    labels = _labels(r)
    assert "dashboard: orders_overview_legacy" in labels

@_needs_lkml
def test_lookml_no_dangling_edges():
    for fixture in ("sample.view.lkml", "sample.model.lkml",
                    "sample.dashboard.lookml", "sample_legacy.dashboard.lookml"):
        r = extract_lookml(FIXTURES / fixture)
        node_ids = {n["id"] for n in r["nodes"]}
        for e in r["edges"]:
            assert e["source"] in node_ids, f"dangling source in {fixture}: {e}"
            assert e["target"] in node_ids, f"dangling target in {fixture}: {e}"
```

## Wiring (4 files — do these only after all tests above pass with the extractor called directly)

1. `graphify/extractors/__init__.py` — add `from graphify.extractors.lookml import extract_lookml`
   and `"lookml": extract_lookml` to `LANGUAGE_EXTRACTORS` (alphabetical).
2. `graphify/extract.py` — add `from graphify.extractors.lookml import extract_lookml  # noqa: F401`
   near the other `graphify.extractors` imports; add `".lkml": extract_lookml,` and
   `".lookml": extract_lookml,` to the `_DISPATCH` dict (alphabetically, near `.lfm`/`.lpk`).
3. `graphify/detect.py` — add `.lkml` and `.lookml` to the `CODE_EXTENSIONS` set literal.
4. `pyproject.toml` — add `lookml = ["lkml", "pyyaml"]` under `[project.optional-dependencies]`,
   and append `"lkml"`, `"pyyaml"` to the `all` extra's list.

Do NOT touch `graphify/watch.py` — `_WATCHED_EXTENSIONS` there is `CODE_EXTENSIONS | DOC_EXTENSIONS |
...`, a derived set, not a literal one; editing `detect.py` alone is sufficient (confirmed by reading
the source in a prior session).

## Final verification (smoke test against real production LookML)

These 4 files exist in a sibling checkout of `edna-looker-talent-acquisition` on this machine —
`~/edna-looker-talent-acquisition/views/Zkipster/prod_zkipster_session_status_history.view.lkml`,
`~/edna-looker-talent-acquisition/views/Zkipster/zkipster_session_status_asof.view.lkml`,
`~/edna-looker-talent-acquisition/models/ta_hiring_report.model.lkml`,
`~/edna-looker-talent-acquisition/dashboards/hc_data/hiring_26_27/SY26-27/Operations/ops_funnel.dashboard.lookml`.
Run `extract_lookml` against each (read-only — never modify anything in that repo) and confirm no
`error` key and nonzero nodes/edges on all four. The dashboard file specifically exercises the YAML
fallback path (it's the legacy format).
