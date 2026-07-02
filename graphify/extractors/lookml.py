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
                explore_nid = _explore_id(explore_name)
                add_node(explore_nid, f"explore: {explore_name}")
                add_edge(el_nid, explore_nid, "uses")

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
