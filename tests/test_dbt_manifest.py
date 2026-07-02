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
