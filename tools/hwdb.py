#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import subprocess
import sys
import tomllib
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    key TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    title TEXT,
    created TEXT,
    modified TEXT,
    extracted_text_path TEXT
);

CREATE TABLE IF NOT EXISTS systems (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT,
    detector TEXT,
    pid_prefix TEXT,
    description TEXT,
    source_key TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS subsystems (
    key TEXT PRIMARY KEY,
    system_key TEXT NOT NULL REFERENCES systems(key) ON DELETE CASCADE,
    subsystem_id TEXT,
    name TEXT NOT NULL,
    description TEXT,
    source_key TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS component_types (
    key TEXT PRIMARY KEY,
    system_key TEXT NOT NULL REFERENCES systems(key) ON DELETE CASCADE,
    subsystem_key TEXT REFERENCES subsystems(key) ON DELETE SET NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    is_batch INTEGER NOT NULL DEFAULT 0,
    has_exec_summary INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    source_key TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_key TEXT NOT NULL REFERENCES component_types(key) ON DELETE CASCADE,
    name TEXT NOT NULL,
    data_type TEXT NOT NULL,
    is_qc INTEGER NOT NULL DEFAULT 0,
    description TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_key TEXT NOT NULL REFERENCES component_types(key) ON DELETE CASCADE,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system_key TEXT NOT NULL REFERENCES systems(key) ON DELETE CASCADE,
    source_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    cardinality TEXT,
    phase TEXT,
    tags TEXT,
    description TEXT,
    source_key TEXT
);
"""


DOCX_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}

PPTX_TEXT_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and render hardware-database design graphs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract-sources", help="Extract DOCX and PPTX files into text.")
    extract_parser.add_argument("--source-dir", default="source-material", help="Directory containing local source documents.")
    extract_parser.add_argument("--output-dir", default="build/sources", help="Directory for extracted text.")

    build_parser = subparsers.add_parser("build-db", help="Build the SQLite database from specs.")
    build_parser.add_argument("--spec-dir", default="specs/systems", help="Directory containing TOML specs.")
    build_parser.add_argument("--source-index", default="build/sources/index.json", help="Source index JSON.")
    build_parser.add_argument("--db", default="build/hardware.db", help="Output SQLite database.")

    list_parser = subparsers.add_parser("list", help="List systems loaded in the SQLite database.")
    list_parser.add_argument("--db", default="build/hardware.db", help="SQLite database.")

    graph_parser = subparsers.add_parser("graph", help="Render a Mermaid graph for one system.")
    graph_parser.add_argument("--db", default="build/hardware.db", help="SQLite database.")
    graph_parser.add_argument("--system", required=True, help="System key to render.")
    graph_parser.add_argument(
        "--view",
        default="hierarchy",
        choices=["hierarchy", "dependency", "cabling", "overview", "all"],
        help="Graph view to render.",
    )
    graph_parser.add_argument("--output", help="Output .mmd file. Defaults to build/diagrams/<system>_<view>.mmd")

    render_all_parser = subparsers.add_parser("render-all", help="Render all seeded graphs.")
    render_all_parser.add_argument("--db", default="build/hardware.db", help="SQLite database.")
    render_all_parser.add_argument("--output-dir", default="build/diagrams", help="Directory for Mermaid files.")
    render_all_parser.add_argument(
        "--views",
        default="overview,hierarchy,dependency,cabling",
        help="Comma-separated list of views to render.",
    )

    svg_parser = subparsers.add_parser("render-svg", help="Render a styled Graphviz SVG for one system.")
    svg_parser.add_argument("--db", default="build/hardware.db", help="SQLite database.")
    svg_parser.add_argument("--system", required=True, help="System key to render.")
    svg_parser.add_argument(
        "--view",
        default="hierarchy",
        choices=["hierarchy", "dependency", "cabling", "overview", "all"],
        help="Graph view to render.",
    )
    svg_parser.add_argument("--output", help="Output .svg file. Defaults to build/publish/diagrams/<system>_<view>.svg")
    svg_parser.add_argument("--dot-output", help="Optional .dot output for debugging or manual editing.")
    svg_parser.add_argument("--pdf-output", help="Optional .pdf output for print-ready distribution.")

    publish_parser = subparsers.add_parser("publish", help="Build a styled publication site with SVG/PDF diagrams.")
    publish_parser.add_argument("--db", default="build/hardware.db", help="SQLite database.")
    publish_parser.add_argument("--output-dir", default="build/publish", help="Directory for the published report.")
    publish_parser.add_argument(
        "--views",
        default="overview,hierarchy,dependency,cabling",
        help="Comma-separated list of views to publish.",
    )

    args = parser.parse_args()
    command = args.command

    if command == "extract-sources":
        extract_sources(Path(args.source_dir), Path(args.output_dir))
        return 0
    if command == "build-db":
        build_db(Path(args.spec_dir), Path(args.source_index), Path(args.db))
        return 0
    if command == "list":
        list_systems(Path(args.db))
        return 0
    if command == "graph":
        output = Path(args.output) if args.output else default_graph_path(Path("build/diagrams"), args.system, args.view)
        render_graph(Path(args.db), args.system, args.view, output)
        return 0
    if command == "render-all":
        render_all(Path(args.db), Path(args.output_dir), [view.strip() for view in args.views.split(",") if view.strip()])
        return 0
    if command == "render-svg":
        output = Path(args.output) if args.output else default_svg_path(Path("build/publish/diagrams"), args.system, args.view)
        dot_output = Path(args.dot_output) if args.dot_output else None
        pdf_output = Path(args.pdf_output) if args.pdf_output else None
        render_svg(Path(args.db), args.system, args.view, output, dot_output=dot_output, pdf_output=pdf_output)
        return 0
    if command == "publish":
        publish_report(Path(args.db), Path(args.output_dir), [view.strip() for view in args.views.split(",") if view.strip()])
        return 0
    return 1


def extract_sources(source_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for path in sorted(source_dir.iterdir()):
        if path.suffix.lower() not in {".docx", ".pptx"}:
            continue
        record = extract_source(path, output_dir)
        records.append(record)
        print(f"extracted {path.name}")
    index_path = output_dir / "index.json"
    index_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"wrote {index_path}")


def extract_source(path: Path, output_dir: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".docx":
        metadata, text = parse_docx(path)
        file_type = "docx"
    elif path.suffix.lower() == ".pptx":
        metadata, text = parse_pptx(path)
        file_type = "pptx"
    else:
        raise ValueError(f"Unsupported source type: {path}")

    text_path = output_dir / f"{path.stem}.txt"
    text_path.write_text(text, encoding="utf-8")
    return {
        "key": path.name,
        "filename": path.name,
        "path": str(path.resolve()),
        "file_type": file_type,
        "title": metadata.get("title"),
        "created": metadata.get("created"),
        "modified": metadata.get("modified"),
        "extracted_text_path": str(text_path.resolve()),
    }


def parse_docx(path: Path) -> tuple[dict[str, Any], str]:
    with ZipFile(path) as archive:
        metadata = parse_core_properties(archive, DOCX_NS)
        root = ET.fromstring(archive.read("word/document.xml"))
        paragraphs: list[str] = []
        for paragraph in root.findall(".//w:p", DOCX_NS):
            pieces = [text.text for text in paragraph.findall(".//w:t", DOCX_NS) if text.text]
            line = "".join(pieces).strip()
            if line:
                paragraphs.append(line)
        return metadata, "\n\n".join(paragraphs)


def parse_pptx(path: Path) -> tuple[dict[str, Any], str]:
    with ZipFile(path) as archive:
        metadata = parse_core_properties(archive, PPTX_TEXT_NS)
        slide_names = sorted(
            [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)],
            key=slide_sort_key,
        )
        sections: list[str] = []
        for slide_name in slide_names:
            root = ET.fromstring(archive.read(slide_name))
            texts = [node.text.strip() for node in root.findall(".//a:t", PPTX_TEXT_NS) if node.text and node.text.strip()]
            if texts:
                sections.append(f"[{slide_name}]\n" + "\n".join(texts))
        return metadata, "\n\n".join(sections)


def parse_core_properties(archive: ZipFile, namespaces: dict[str, str]) -> dict[str, Any]:
    if "docProps/core.xml" not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read("docProps/core.xml"))
    title = root.findtext("dc:title", default="", namespaces=namespaces) or None
    created = root.findtext("dcterms:created", default="", namespaces=namespaces) or None
    modified = root.findtext("dcterms:modified", default="", namespaces=namespaces) or None
    return {"title": title, "created": created, "modified": modified}


def slide_sort_key(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml", name)
    return int(match.group(1)) if match else 0


def build_db(spec_dir: Path, source_index_path: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    source_index = load_source_index(source_index_path)
    for source in source_index:
        conn.execute(
            """
            INSERT INTO sources (key, filename, path, file_type, title, created, modified, extracted_text_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source["key"],
                source["filename"],
                source["path"],
                source["file_type"],
                source.get("title"),
                source.get("created"),
                source.get("modified"),
                source.get("extracted_text_path"),
            ),
        )

    specs = load_specs(spec_dir)
    spec_mappings: list[tuple[dict[str, Any], dict[str, str]]] = []

    for spec in specs:
        system = spec["system"]
        system_source = system.get("source_document")
        local_to_global: dict[str, str] = {system["key"]: system["key"]}
        conn.execute(
            """
            INSERT INTO systems (key, name, short_name, detector, pid_prefix, description, source_key, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                system["key"],
                system["name"],
                system.get("short_name"),
                system.get("detector"),
                system.get("pid_prefix"),
                system.get("description"),
                system_source,
                system.get("notes"),
            ),
        )
        for subsystem in spec.get("subsystems", []):
            global_key = qualify_key(system["key"], subsystem["key"])
            local_to_global[subsystem["key"]] = global_key
            conn.execute(
                """
                INSERT INTO subsystems (key, system_key, subsystem_id, name, description, source_key, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    global_key,
                    system["key"],
                    subsystem.get("id"),
                    subsystem["name"],
                    subsystem.get("description"),
                    subsystem.get("source_document", system_source),
                    subsystem.get("notes"),
                ),
            )
        for component in spec.get("components", []):
            global_key = qualify_key(system["key"], component["key"])
            local_to_global[component["key"]] = global_key
            subsystem_local_key = component.get("subsystem")
            subsystem_key = local_to_global.get(subsystem_local_key) if subsystem_local_key else None
            if subsystem_local_key and subsystem_key is None:
                raise ValueError(f"Unknown subsystem {subsystem_local_key!r} in {spec['__path__']}")
            conn.execute(
                """
                INSERT INTO component_types
                    (key, system_key, subsystem_key, name, category, is_batch, has_exec_summary, description, source_key, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    global_key,
                    system["key"],
                    subsystem_key,
                    component["name"],
                    component.get("category", "hardware"),
                    int(bool(component.get("is_batch", False))),
                    int(bool(component.get("has_exec_summary", False))),
                    component.get("description"),
                    component.get("source_document", system_source),
                    component.get("notes"),
                ),
            )

            for field in component.get("fields", []):
                conn.execute(
                    """
                    INSERT INTO fields (component_key, name, data_type, is_qc, description)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        global_key,
                        field["name"],
                        field.get("data_type", "text"),
                        int(bool(field.get("is_qc", False))),
                        field.get("description"),
                    ),
                )

            for artifact in component.get("artifacts", []):
                conn.execute(
                    """
                    INSERT INTO artifacts (component_key, name, artifact_type, description)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        global_key,
                        artifact["name"],
                        artifact.get("artifact_type", "file"),
                        artifact.get("description"),
                    ),
                )

        spec_mappings.append((spec, local_to_global))

    for spec, local_to_global in spec_mappings:
        system_key = spec["system"]["key"]
        for relation in spec.get("relations", []):
            source_key = local_to_global.get(relation["source"])
            target_key = local_to_global.get(relation["target"])
            if source_key is None:
                raise ValueError(f"Unknown relation source {relation['source']!r} in {spec['__path__']}")
            if target_key is None:
                raise ValueError(f"Unknown relation target {relation['target']!r} in {spec['__path__']}")
            conn.execute(
                """
                INSERT INTO relations
                    (system_key, source_node, target_node, relation_type, cardinality, phase, tags, description, source_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    system_key,
                    source_key,
                    target_key,
                    relation["type"],
                    relation.get("cardinality"),
                    relation.get("phase"),
                    ",".join(relation.get("tags", [])),
                    relation.get("description"),
                    relation.get("source_document", spec["system"].get("source_document")),
                ),
            )

    conn.commit()
    conn.close()
    print(f"wrote {db_path}")


def qualify_key(system_key: str, local_key: str) -> str:
    if local_key == system_key:
        return local_key
    return f"{system_key}.{local_key}"


def load_source_index(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def load_specs(spec_dir: Path) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for path in sorted(spec_dir.rglob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if "system" not in data:
            raise ValueError(f"Missing [system] table in {path}")
        data["__path__"] = str(path)
        specs.append(data)
    return specs


def list_systems(db_path: Path) -> None:
    conn = open_db(db_path)
    rows = conn.execute(
        """
        SELECT key, name, detector, COALESCE(pid_prefix, '-') AS pid_prefix, COALESCE(source_key, '-') AS source_key
        FROM systems
        ORDER BY detector, name
        """
    ).fetchall()
    for row in rows:
        print(f"{row['key']}: {row['name']} | detector={row['detector'] or '-'} | pid_prefix={row['pid_prefix']} | source={row['source_key']}")


def render_all(db_path: Path, output_dir: Path, views: list[str]) -> None:
    conn = open_db(db_path)
    systems = [row["key"] for row in conn.execute("SELECT key FROM systems ORDER BY key").fetchall()]
    output_dir.mkdir(parents=True, exist_ok=True)
    for system_key in systems:
        for view in views:
            path = default_graph_path(output_dir, system_key, view)
            try:
                render_graph(db_path, system_key, view, path, quiet=True)
            except SystemExit as exc:
                print(f"skipped {system_key} {view}: {exc}")
                continue
            print(f"wrote {path}")


def render_graph(db_path: Path, system_key: str, view: str, output_path: Path, quiet: bool = False) -> None:
    conn = open_db(db_path)
    system_row, nodes, filtered_relations = load_view_model(conn, system_key, view)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = to_mermaid(system_row, nodes, filtered_relations)
    output_path.write_text(content, encoding="utf-8")
    if not quiet:
        print(f"wrote {output_path}")


def render_svg(
    db_path: Path,
    system_key: str,
    view: str,
    output_path: Path,
    dot_output: Path | None = None,
    pdf_output: Path | None = None,
    quiet: bool = False,
) -> None:
    conn = open_db(db_path)
    system_row, nodes, filtered_relations = load_view_model(conn, system_key, view)
    dot_text = to_graphviz(system_row, nodes, filtered_relations, view)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_dot(dot_text, "svg"), encoding="utf-8")

    if dot_output is not None:
        dot_output.parent.mkdir(parents=True, exist_ok=True)
        dot_output.write_text(dot_text, encoding="utf-8")

    if pdf_output is not None:
        pdf_output.parent.mkdir(parents=True, exist_ok=True)
        pdf_output.write_bytes(render_dot(dot_text, "pdf", text_output=False))

    if not quiet:
        print(f"wrote {output_path}")


def load_view_model(
    conn: sqlite3.Connection,
    system_key: str,
    view: str,
) -> tuple[sqlite3.Row, dict[str, dict[str, Any]], list[sqlite3.Row]]:
    system_row = conn.execute("SELECT * FROM systems WHERE key = ?", (system_key,)).fetchone()
    if system_row is None:
        raise SystemExit(f"Unknown system: {system_key}")

    nodes = load_graph_nodes(conn, system_key)
    relations = conn.execute("SELECT * FROM relations WHERE system_key = ? ORDER BY id", (system_key,)).fetchall()
    filtered_relations = [row for row in relations if relation_matches(row, view)]
    if not filtered_relations:
        raise SystemExit(f"No relations matched view={view!r} for system={system_key!r}")
    return system_row, nodes, filtered_relations


def relation_matches(row: sqlite3.Row, view: str) -> bool:
    if view == "all":
        return True
    tags = set(filter(None, (row["tags"] or "").split(",")))
    relation_type = row["relation_type"]
    if view == "hierarchy":
        return relation_type == "contains" or "hierarchy" in tags
    if view == "overview":
        return "overview" in tags
    if view == "cabling":
        return relation_type == "connects_to" or "cabling" in tags
    if view == "dependency":
        return relation_type in {"depends_on", "interfaces_with", "reads_out", "powers", "timed_by"} or "dependency" in tags
    return False


def load_graph_nodes(conn: sqlite3.Connection, system_key: str) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}

    for row in conn.execute("SELECT * FROM systems WHERE key = ?", (system_key,)).fetchall():
        nodes[row["key"]] = {
            "key": row["key"],
            "label": row["name"],
            "entity_type": "system",
            "system_key": row["key"],
            "subsystem_key": None,
            "category": "system",
            "is_batch": False,
        }

    for row in conn.execute("SELECT * FROM subsystems WHERE system_key = ?", (system_key,)).fetchall():
        nodes[row["key"]] = {
            "key": row["key"],
            "label": row["name"],
            "entity_type": "subsystem",
            "system_key": row["system_key"],
            "subsystem_key": None,
            "category": "subsystem",
            "is_batch": False,
        }

    for row in conn.execute("SELECT * FROM component_types WHERE system_key = ?", (system_key,)).fetchall():
        nodes[row["key"]] = {
            "key": row["key"],
            "label": row["name"],
            "entity_type": "component_type",
            "system_key": row["system_key"],
            "subsystem_key": row["subsystem_key"],
            "category": row["category"],
            "is_batch": bool(row["is_batch"]),
        }

    return nodes


def to_mermaid(system_row: sqlite3.Row, nodes: dict[str, dict[str, Any]], relations: list[sqlite3.Row]) -> str:
    included = set()
    for relation in relations:
        included.add(relation["source_node"])
        included.add(relation["target_node"])

    if system_row["key"] not in included:
        included.add(system_row["key"])

    subsystem_groups: dict[str, list[str]] = defaultdict(list)
    root_nodes: list[str] = []
    for key, node in nodes.items():
        if key not in included or node["entity_type"] != "component_type":
            continue
        subsystem_key = node.get("subsystem_key")
        if subsystem_key:
            subsystem_groups[subsystem_key].append(key)
        else:
            root_nodes.append(key)

    lines = ["flowchart LR"]
    lines.extend(style_lines())

    system_id = mermaid_id(system_row["key"])
    system_label = escape_label(system_row["name"])
    lines.append(f'  {system_id}["{system_label}"]')
    lines.append(f"  class {system_id} system;")

    for subsystem_key in sorted(subsystem_groups):
        if subsystem_key not in nodes:
            continue
        subsystem_label = escape_label(nodes[subsystem_key]["label"])
        lines.append(f'  subgraph {mermaid_id(subsystem_key)}["{subsystem_label}"]')
        for node_key in sorted(subsystem_groups[subsystem_key], key=lambda key: nodes[key]["label"]):
            lines.extend(render_node(nodes[node_key], indent="    "))
        lines.append("  end")

    for node_key in sorted(root_nodes, key=lambda key: nodes[key]["label"]):
        lines.extend(render_node(nodes[node_key], indent="  "))

    for relation in relations:
        lines.append(render_edge(relation))

    return "\n".join(lines) + "\n"


def to_graphviz(
    system_row: sqlite3.Row,
    nodes: dict[str, dict[str, Any]],
    relations: list[sqlite3.Row],
    view: str,
) -> str:
    included = {system_row["key"]}
    for relation in relations:
        included.add(relation["source_node"])
        included.add(relation["target_node"])

    subsystem_groups: dict[str, list[str]] = defaultdict(list)
    root_nodes: list[str] = []
    for key, node in nodes.items():
        if key not in included or node["entity_type"] != "component_type":
            continue
        subsystem_key = node.get("subsystem_key")
        if subsystem_key:
            subsystem_groups[subsystem_key].append(key)
        else:
            root_nodes.append(key)

    title = f"{system_row['name']} · {view_title(view)}"
    lines = [
        "digraph HardwareModel {",
        '  graph [',
        '    rankdir=LR,',
        '    splines=true,',
        '    overlap=false,',
        '    newrank=true,',
        '    compound=true,',
        '    bgcolor="#f6f1e8",',
        '    pad="0.35",',
        '    nodesep="0.45",',
        '    ranksep="0.8",',
        f'    label="{dot_escape(title)}",',
        '    labelloc="t",',
        '    labeljust="l",',
        '    fontname="Iowan Old Style",',
        '    fontsize=26,',
        '    fontcolor="#183247"',
        "  ];",
        '  node [fontname="Avenir Next", fontsize=12, margin="0.18,0.12", penwidth=1.3];',
        '  edge [fontname="Avenir Next", fontsize=10, arrowsize=0.8, penwidth=1.2, color="#33536a", fontcolor="#33536a"];',
        "",
    ]

    lines.append(f'  {dot_id(system_row["key"])} [{dot_node_attrs(nodes[system_row["key"]], system_root=True)}];')
    lines.append("")

    for subsystem_key in sorted(subsystem_groups, key=lambda key: nodes[key]["label"] if key in nodes else key):
        subsystem_node = nodes.get(subsystem_key)
        if subsystem_node is None:
            continue
        cluster_id = dot_id(subsystem_key)
        lines.extend(
            [
                f"  subgraph cluster_{cluster_id} {{",
                f'    label="{dot_escape(subsystem_node["label"])}";',
                '    style="rounded,filled";',
                '    color="#c7b8a1";',
                '    fillcolor="#fbf7f0";',
                '    penwidth=1.1;',
                '    fontname="Avenir Next";',
                '    fontsize=13;',
                '    fontcolor="#5a4634";',
            ]
        )
        for node_key in sorted(subsystem_groups[subsystem_key], key=lambda key: nodes[key]["label"]):
            lines.append(f'    {dot_id(node_key)} [{dot_node_attrs(nodes[node_key])}];')
        lines.append("  }")
        lines.append("")

    for node_key in sorted(root_nodes, key=lambda key: nodes[key]["label"]):
        lines.append(f'  {dot_id(node_key)} [{dot_node_attrs(nodes[node_key])}];')

    if root_nodes:
        lines.append("")

    for relation in relations:
        lines.append(f"  {dot_id(relation['source_node'])} -> {dot_id(relation['target_node'])} [{dot_edge_attrs(relation)}];")

    lines.append("}")
    return "\n".join(lines) + "\n"


def render_node(node: dict[str, Any], indent: str) -> list[str]:
    node_id = mermaid_id(node["key"])
    label = escape_label(node["label"])
    category = node["category"]
    is_batch = node["is_batch"]

    if category in {"cable", "fiber"}:
        declaration = f'{indent}{node_id}{{"{label}"}}'
    elif is_batch or category == "batch":
        declaration = f'{indent}{node_id}[/"{label}"/]'
    else:
        declaration = f'{indent}{node_id}["{label}"]'

    return [declaration, f"{indent}class {node_id} {class_name_for_node(node)};"]


def render_edge(relation: sqlite3.Row) -> str:
    source = mermaid_id(relation["source_node"])
    target = mermaid_id(relation["target_node"])
    relation_type = relation["relation_type"]

    if relation_type == "contains":
        return f"  {source} --> {target}"
    if relation_type == "connects_to":
        return f"  {source} -. connects .-> {target}"
    if relation_type == "interfaces_with":
        return f"  {source} -. interface .-> {target}"
    if relation_type == "reads_out":
        return f"  {source} -. readout .-> {target}"
    if relation_type == "powers":
        return f"  {source} -. power .-> {target}"
    if relation_type == "timed_by":
        return f"  {source} -. timing .-> {target}"
    if relation_type == "depends_on":
        return f"  {source} -. depends on .-> {target}"
    label = relation_type.replace("_", " ")
    return f"  {source} -. {label} .-> {target}"


def dot_node_attrs(node: dict[str, Any], system_root: bool = False) -> str:
    attrs: dict[str, str] = {
        "label": node["label"],
        "fontname": "Avenir Next",
    }

    if system_root or node["entity_type"] == "system":
        attrs.update(
            {
                "shape": "box",
                "style": "rounded,filled",
                "fillcolor": "#183247",
                "color": "#183247",
                "fontcolor": "#ffffff",
                "fontsize": "16",
                "margin": "0.24,0.16",
                "penwidth": "1.5",
            }
        )
        return format_dot_attrs(attrs)

    category = node["category"]
    if node["is_batch"] or category == "batch":
        attrs.update(
            {
                "shape": "note",
                "style": "filled",
                "fillcolor": "#f0e6fa",
                "color": "#7d5bb6",
                "fontcolor": "#382451",
            }
        )
    elif category == "electronics":
        attrs.update(
            {
                "shape": "box",
                "style": "rounded,filled",
                "fillcolor": "#e4efe0",
                "color": "#5d7f48",
                "fontcolor": "#18311a",
            }
        )
    elif category == "cable":
        attrs.update(
            {
                "shape": "hexagon",
                "style": "filled",
                "fillcolor": "#fff0cc",
                "color": "#a56b17",
                "fontcolor": "#4f370c",
            }
        )
    elif category == "fiber":
        attrs.update(
            {
                "shape": "octagon",
                "style": "filled",
                "fillcolor": "#fde7c8",
                "color": "#b7671e",
                "fontcolor": "#5a320a",
            }
        )
    elif category == "sensor":
        attrs.update(
            {
                "shape": "ellipse",
                "style": "filled",
                "fillcolor": "#f7dde0",
                "color": "#a14656",
                "fontcolor": "#4a1823",
            }
        )
    elif category == "interface":
        attrs.update(
            {
                "shape": "parallelogram",
                "style": "filled",
                "fillcolor": "#f3e5cc",
                "color": "#9d6a14",
                "fontcolor": "#52360a",
            }
        )
    else:
        attrs.update(
            {
                "shape": "box",
                "style": "rounded,filled",
                "fillcolor": "#dfe9f2",
                "color": "#53718a",
                "fontcolor": "#173140",
            }
        )
    return format_dot_attrs(attrs)


def dot_edge_attrs(relation: sqlite3.Row) -> str:
    relation_type = relation["relation_type"]
    attrs: dict[str, str] = {}
    cardinality = relation["cardinality"]

    if relation_type == "contains":
        attrs.update(
            {
                "color": "#274c67",
                "fontcolor": "#274c67",
                "penwidth": "1.4",
            }
        )
    elif relation_type == "connects_to":
        attrs.update(
            {
                "color": "#b36a00",
                "fontcolor": "#8f5600",
                "style": "dashed",
                "penwidth": "1.2",
                "label": "connects",
            }
        )
    elif relation_type == "interfaces_with":
        attrs.update(
            {
                "color": "#1f6f8b",
                "fontcolor": "#1f6f8b",
                "style": "dashed",
                "penwidth": "1.2",
                "label": "interface",
            }
        )
    elif relation_type == "reads_out":
        attrs.update(
            {
                "color": "#356c53",
                "fontcolor": "#356c53",
                "style": "dashed",
                "penwidth": "1.2",
                "label": "readout",
            }
        )
    elif relation_type == "powers":
        attrs.update(
            {
                "color": "#b54b32",
                "fontcolor": "#9e412b",
                "style": "dashed",
                "penwidth": "1.2",
                "label": "power",
            }
        )
    elif relation_type == "timed_by":
        attrs.update(
            {
                "color": "#4f7c7a",
                "fontcolor": "#4f7c7a",
                "style": "dashed",
                "penwidth": "1.2",
                "label": "timing",
            }
        )
    elif relation_type == "depends_on":
        attrs.update(
            {
                "color": "#7d5b2d",
                "fontcolor": "#7d5b2d",
                "style": "dashed",
                "penwidth": "1.2",
                "label": "depends on",
            }
        )
    else:
        attrs.update(
            {
                "style": "dashed",
                "label": relation_type.replace("_", " "),
            }
        )

    if cardinality:
        if "label" in attrs:
            attrs["xlabel"] = cardinality
        else:
            attrs["label"] = cardinality

    return format_dot_attrs(attrs)


def format_dot_attrs(attrs: dict[str, str]) -> str:
    ordered = []
    for key, value in attrs.items():
        ordered.append(f'{key}="{dot_escape(str(value))}"')
    return ", ".join(ordered)


def dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def dot_id(key: str) -> str:
    text = re.sub(r"[^0-9a-zA-Z_]", "_", key)
    if text and text[0].isdigit():
        text = f"n_{text}"
    return text


def render_dot(dot_text: str, output_type: str, text_output: bool = True) -> str | bytes:
    result = subprocess.run(
        ["dot", f"-T{output_type}"],
        input=dot_text if text_output else dot_text.encode("utf-8"),
        text=text_output,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"dot failed: {result.stderr.strip()}")
    return result.stdout if text_output else result.stdout


def publish_report(db_path: Path, output_dir: Path, views: list[str]) -> None:
    conn = open_db(db_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    diagrams_dir = output_dir / "diagrams"
    dot_dir = output_dir / "dot"
    pdf_dir = output_dir / "pdf"
    assets_dir = output_dir / "assets"
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    dot_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    systems = conn.execute("SELECT * FROM systems ORDER BY detector, name").fetchall()
    published_items: list[dict[str, str]] = []
    detector_plates: list[dict[str, str]] = []

    detectors = [row["detector"] for row in conn.execute("SELECT DISTINCT detector FROM systems WHERE detector IS NOT NULL AND detector != '' ORDER BY detector").fetchall()]
    for detector in detectors:
        safe_detector = detector.lower().replace("-", "_")
        svg_path = diagrams_dir / f"{safe_detector}_full_dense.svg"
        dot_path = dot_dir / f"{safe_detector}_full_dense.dot"
        pdf_path = pdf_dir / f"{safe_detector}_full_dense.pdf"
        try:
            render_detector_dense(conn, detector, svg_path, dot_output=dot_path, pdf_output=pdf_path)
        except SystemExit:
            continue
        detector_plates.append(
            {
                "detector": detector,
                "title": f"{detector} Full Dense Plate",
                "copy": "Detector-wide dense composition grouped by modeled systems and bridged with top-level context from the hierarchy deck.",
                "svg": f"diagrams/{svg_path.name}",
                "dot": f"dot/{dot_path.name}",
                "pdf": f"pdf/{pdf_path.name}",
            }
        )

    for system_row in systems:
        for view in views:
            svg_path = diagrams_dir / f"{system_row['key']}_{view}.svg"
            dot_path = dot_dir / f"{system_row['key']}_{view}.dot"
            pdf_path = pdf_dir / f"{system_row['key']}_{view}.pdf"
            try:
                render_svg(db_path, system_row["key"], view, svg_path, dot_output=dot_path, pdf_output=pdf_path, quiet=True)
            except SystemExit:
                continue
            published_items.append(
                {
                    "system_key": system_row["key"],
                    "system_name": system_row["name"],
                    "view": view,
                    "view_title": view_title(view),
                    "view_description": view_description(view),
                    "svg": f"diagrams/{svg_path.name}",
                    "dot": f"dot/{dot_path.name}",
                    "pdf": f"pdf/{pdf_path.name}",
                }
            )

    (assets_dir / "report.css").write_text(publication_css(), encoding="utf-8")
    (output_dir / "index.html").write_text(publication_html(conn, published_items, detector_plates), encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {output_dir / 'index.html'}")


def publication_html(
    conn: sqlite3.Connection,
    published_items: list[dict[str, str]],
    detector_plates: list[dict[str, str]],
) -> str:
    stats = {
        "systems": conn.execute("SELECT COUNT(*) FROM systems").fetchone()[0],
        "subsystems": conn.execute("SELECT COUNT(*) FROM subsystems").fetchone()[0],
        "component_types": conn.execute("SELECT COUNT(*) FROM component_types").fetchone()[0],
        "relations": conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0],
    }

    by_system: dict[str, list[dict[str, str]]] = defaultdict(list)
    system_names: dict[str, str] = {}
    for item in published_items:
        by_system[item["system_key"]].append(item)
        system_names[item["system_key"]] = item["system_name"]

    nav_items = []
    sections = []
    if detector_plates:
        nav_items.append('<a href="#detector-plates">Dense Detector Plate</a>')
    nav_items.append('<a href="#reading-guide">Reading Guide</a>')
    for system_key in sorted(by_system, key=lambda key: system_names[key]):
        system_name = system_names[system_key]
        nav_items.append(f'<a href="#{html.escape(system_key)}">{html.escape(system_name)}</a>')
        cards = []
        for item in sorted(by_system[system_key], key=lambda entry: view_order(entry["view"])):
            cards.append(
                f"""
                <article class="diagram-card" id="{html.escape(system_key)}-{html.escape(item["view"])}">
                  <div class="diagram-head">
                    <p class="eyebrow">{html.escape(item["view_title"])}</p>
                    <h3>{html.escape(system_name)}</h3>
                    <p class="diagram-copy">{html.escape(item["view_description"])}</p>
                    <p class="downloads">
                      <a href="{html.escape(item['svg'])}">SVG</a>
                      <a href="{html.escape(item['pdf'])}">PDF</a>
                      <a href="{html.escape(item['dot'])}">DOT</a>
                    </p>
                  </div>
                  <div class="diagram-frame">
                    <img src="{html.escape(item['svg'])}" alt="{html.escape(system_name)} {html.escape(item['view_title'])} diagram">
                  </div>
                </article>
                """
            )
        sections.append(
            f"""
            <section class="system-section" id="{html.escape(system_key)}">
              <div class="section-head">
                <p class="eyebrow">System Portfolio</p>
                <h2>{html.escape(system_name)}</h2>
              </div>
              <div class="diagram-grid">
                {''.join(cards)}
              </div>
            </section>
            """
        )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    detector_plate_section = ""
    if detector_plates:
        cards = []
        for plate in detector_plates:
            cards.append(
                f"""
                <article class="diagram-card detector-plate-card">
                  <div class="diagram-head">
                    <p class="eyebrow">Dense Detector Plate</p>
                    <h3>{html.escape(plate["title"])}</h3>
                    <p class="diagram-copy">{html.escape(plate["copy"])}</p>
                    <p class="downloads">
                      <a href="{html.escape(plate['svg'])}">SVG</a>
                      <a href="{html.escape(plate['pdf'])}">PDF</a>
                      <a href="{html.escape(plate['dot'])}">DOT</a>
                    </p>
                  </div>
                  <div class="diagram-frame large-frame">
                    <img src="{html.escape(plate['svg'])}" alt="{html.escape(plate['title'])}">
                  </div>
                </article>
                """
            )
        detector_plate_section = f"""
        <section class="system-section" id="detector-plates">
          <div class="section-head">
            <p class="eyebrow">Master Diagram</p>
            <h2>Dense Detector Plate</h2>
          </div>
          <div class="diagram-grid detector-plate-grid">
            {''.join(cards)}
          </div>
        </section>
        """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hardware Database Diagram Portfolio</title>
  <link rel="stylesheet" href="assets/report.css">
</head>
<body>
  <div class="page-shell">
    <header class="hero">
      <p class="eyebrow">Hardware Database Publication</p>
      <h1>FD2-VD Diagram Portfolio</h1>
      <p class="hero-copy">Styled SVG and PDF diagrams generated from the curated hardware model, grounded in the interface documents in this repository.</p>
      <div class="stat-strip">
        <div><span>{stats['systems']}</span><small>Systems</small></div>
        <div><span>{stats['subsystems']}</span><small>Subsystems</small></div>
        <div><span>{stats['component_types']}</span><small>Component Types</small></div>
        <div><span>{stats['relations']}</span><small>Relations</small></div>
      </div>
    </header>

    <aside class="side-nav">
      <div class="side-nav-inner">
        <p class="eyebrow">Navigate</p>
        {''.join(nav_items)}
        <p class="build-stamp">Generated {html.escape(generated)}</p>
      </div>
    </aside>

    <main class="content">
      <section class="legend">
        <div class="legend-card">
          <h2>Legend</h2>
          <div class="legend-grid">
            <span class="legend-chip hardware">Hardware</span>
            <span class="legend-chip electronics">Electronics</span>
            <span class="legend-chip cable">Cable</span>
            <span class="legend-chip fiber">Fiber</span>
            <span class="legend-chip sensor">Sensor</span>
            <span class="legend-chip interface">Connector / Interface</span>
            <span class="legend-chip batch">Batch</span>
          </div>
          <p class="legend-copy">Solid arrows indicate containment or assembly hierarchy. Dashed annotated arrows indicate interfaces, cable paths, timing, power, or other dependency links.</p>
        </div>
      </section>
      <section class="guide" id="reading-guide">
        <div class="guide-card">
          <div class="section-head">
            <p class="eyebrow">How To Read The Diagrams</p>
            <h2>Reading Guide</h2>
          </div>
          <div class="guide-grid">
            <div class="guide-item">
              <span class="legend-chip hardware">Hardware</span>
              <p>Mechanical assemblies, structures, boxes, frames, racks, drawers, and installed physical units.</p>
            </div>
            <div class="guide-item">
              <span class="legend-chip electronics">Electronics</span>
              <p>Active boards, readout cards, controllers, power modules, switches, and digitization hardware.</p>
            </div>
            <div class="guide-item">
              <span class="legend-chip interface">Connector / Interface</span>
              <p>Flanges, connector-bearing boundaries, warm/cold interfaces, and other typed connection surfaces. In these models, this category stands in for connectors and interface panels.</p>
            </div>
            <div class="guide-item">
              <span class="legend-chip cable">Cable</span>
              <p>Electrical cables and bundled cable objects that connect subsystems or carry power and signals.</p>
            </div>
            <div class="guide-item">
              <span class="legend-chip fiber">Fiber</span>
              <p>Optical fiber paths used for timing, data transport, or calibration distribution.</p>
            </div>
            <div class="guide-item">
              <span class="legend-chip sensor">Sensor</span>
              <p>Instrumentation endpoints such as RTDs, level meters, gas arrays, and other sensing elements.</p>
            </div>
            <div class="guide-item">
              <span class="legend-chip batch">Batch</span>
              <p>Batch-tracked or type-tracked parts where the model currently represents a production family rather than a serialized individual item.</p>
            </div>
            <div class="guide-item edge-guide">
              <p><strong>Solid arrows</strong> mean assembly or containment.</p>
              <p><strong>Dashed arrows</strong> mean functional links such as cabling, interfaces, timing, power, or readout dependence.</p>
              <p><strong>Subsystem boxes</strong> group related component types. The dense detector plate uses these groupings to approximate the presentation-style hierarchy sheets.</p>
            </div>
          </div>
        </div>
      </section>
      {detector_plate_section}
      {''.join(sections)}
    </main>
  </div>
</body>
</html>
"""


def publication_css() -> str:
    return """
:root {
  --paper: #f6f1e8;
  --paper-strong: #efe6d8;
  --ink: #183247;
  --ink-soft: #4a6274;
  --line: #cbbda8;
  --navy: #183247;
  --green: #5d7f48;
  --amber: #a56b17;
  --rose: #a14656;
  --violet: #7d5bb6;
  --shadow: 0 20px 50px rgba(24, 50, 71, 0.10);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  color: var(--ink);
  background:
    radial-gradient(circle at top left, rgba(255,255,255,0.75), transparent 38%),
    linear-gradient(180deg, #f8f4ec 0%, #f2ebdd 100%);
  font-family: "Avenir Next", "Helvetica Neue", sans-serif;
}

.page-shell {
  display: grid;
  grid-template-columns: 18rem minmax(0, 1fr);
  gap: 2rem;
  max-width: 1700px;
  margin: 0 auto;
  padding: 2rem;
}

.hero {
  grid-column: 1 / -1;
  padding: 2rem 2.2rem;
  border: 1px solid var(--line);
  border-radius: 28px;
  background:
    linear-gradient(135deg, rgba(24,50,71,0.98), rgba(36,72,93,0.94)),
    linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0));
  color: #f7f3ed;
  box-shadow: var(--shadow);
}

.hero h1,
.section-head h2,
.legend-card h2,
.diagram-head h3 {
  margin: 0;
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-weight: 600;
  letter-spacing: 0.01em;
}

.hero h1 {
  font-size: clamp(2.4rem, 4vw, 4.6rem);
  line-height: 0.96;
  max-width: 10ch;
}

.hero-copy {
  max-width: 62rem;
  margin: 1rem 0 0;
  color: rgba(247,243,237,0.82);
  font-size: 1.05rem;
  line-height: 1.55;
}

.eyebrow {
  margin: 0 0 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.16em;
  font-size: 0.76rem;
  font-weight: 700;
  opacity: 0.86;
}

.stat-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1rem;
  margin-top: 1.8rem;
}

.stat-strip div {
  padding: 1rem 1.1rem;
  border: 1px solid rgba(247,243,237,0.18);
  border-radius: 18px;
  background: rgba(255,255,255,0.06);
}

.stat-strip span {
  display: block;
  font-size: 1.6rem;
  font-weight: 700;
}

.stat-strip small {
  display: block;
  margin-top: 0.2rem;
  color: rgba(247,243,237,0.76);
  text-transform: uppercase;
  letter-spacing: 0.12em;
}

.side-nav {
  align-self: start;
  position: sticky;
  top: 1.5rem;
}

.side-nav-inner,
.legend-card,
.diagram-card {
  border: 1px solid var(--line);
  border-radius: 24px;
  background: rgba(255, 252, 247, 0.82);
  backdrop-filter: blur(6px);
  box-shadow: var(--shadow);
}

.side-nav-inner {
  padding: 1.2rem;
}

.side-nav a {
  display: block;
  padding: 0.62rem 0.2rem;
  color: var(--ink);
  text-decoration: none;
  border-top: 1px solid rgba(203, 189, 168, 0.6);
}

.side-nav a:first-of-type {
  border-top: 0;
}

.build-stamp {
  margin: 1rem 0 0;
  color: var(--ink-soft);
  font-size: 0.86rem;
}

.content {
  min-width: 0;
}

.legend-card {
  padding: 1.4rem 1.6rem;
}

.guide-card {
  padding: 1.4rem 1.6rem;
  border: 1px solid var(--line);
  border-radius: 24px;
  background: rgba(255, 252, 247, 0.82);
  backdrop-filter: blur(6px);
  box-shadow: var(--shadow);
}

.legend-copy {
  margin: 1rem 0 0;
  color: var(--ink-soft);
  line-height: 1.5;
}

.legend-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-top: 1rem;
}

.legend-chip {
  display: inline-flex;
  align-items: center;
  padding: 0.55rem 0.9rem;
  border-radius: 999px;
  font-size: 0.88rem;
  font-weight: 700;
  border: 1px solid transparent;
}

.legend-chip.hardware { background: #dfe9f2; border-color: #53718a; }
.legend-chip.electronics { background: #e4efe0; border-color: #5d7f48; }
.legend-chip.cable { background: #fff0cc; border-color: #a56b17; }
.legend-chip.fiber { background: #fde7c8; border-color: #b7671e; }
.legend-chip.sensor { background: #f7dde0; border-color: #a14656; }
.legend-chip.interface { background: #f3e5cc; border-color: #9d6a14; }
.legend-chip.batch { background: #f0e6fa; border-color: #7d5bb6; }

.guide {
  margin-top: 1.75rem;
}

.guide-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 1rem;
  margin-top: 1rem;
}

.guide-item {
  padding: 1rem;
  border: 1px solid rgba(203, 189, 168, 0.65);
  border-radius: 18px;
  background: rgba(255,255,255,0.45);
}

.guide-item p {
  margin: 0.8rem 0 0;
  color: var(--ink-soft);
  line-height: 1.5;
}

.edge-guide p:first-child {
  margin-top: 0;
}

.system-section {
  margin-top: 1.75rem;
}

.section-head {
  margin-bottom: 1rem;
}

.diagram-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 1.2rem;
}

.detector-plate-grid {
  grid-template-columns: 1fr;
}

.diagram-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.diagram-head {
  padding: 1.25rem 1.35rem 0.8rem;
  border-bottom: 1px solid rgba(203, 189, 168, 0.6);
}

.diagram-copy {
  color: var(--ink-soft);
  line-height: 1.45;
  min-height: 3.2rem;
}

.downloads {
  display: flex;
  gap: 0.8rem;
  flex-wrap: wrap;
  margin: 0.8rem 0 0;
}

.downloads a {
  color: var(--navy);
  text-decoration: none;
  font-weight: 700;
}

.diagram-frame {
  padding: 1rem;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.65), rgba(255,255,255,0.35)),
    repeating-linear-gradient(
      0deg,
      rgba(203, 189, 168, 0.08),
      rgba(203, 189, 168, 0.08) 1px,
      transparent 1px,
      transparent 22px
    );
}

.diagram-frame img {
  display: block;
  width: 100%;
  height: auto;
  border-radius: 18px;
  background: #ffffff;
  border: 1px solid rgba(203, 189, 168, 0.7);
}

.large-frame img {
  max-height: none;
}

@media (max-width: 980px) {
  .page-shell {
    grid-template-columns: 1fr;
  }

  .side-nav {
    position: static;
  }

  .stat-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
"""


def view_title(view: str) -> str:
    titles = {
        "overview": "Overview",
        "hierarchy": "Hierarchy",
        "dependency": "Dependencies",
        "cabling": "Cabling",
        "all": "All Relations",
    }
    return titles.get(view, view.replace("_", " ").title())


def view_description(view: str) -> str:
    descriptions = {
        "overview": "High-level system framing for publication and stakeholder review.",
        "hierarchy": "Assembly and parent-child structure across tracked component types.",
        "dependency": "Interface and functional dependencies across subsystems and services.",
        "cabling": "Cable, fiber, flange, timing, and support-path relationships.",
        "all": "Combined relation set without view filtering.",
    }
    return descriptions.get(view, "Rendered view of the current modeled relations.")


def view_order(view: str) -> int:
    order = {"overview": 0, "hierarchy": 1, "dependency": 2, "cabling": 3, "all": 4}
    return order.get(view, 99)


def render_detector_dense(
    conn: sqlite3.Connection,
    detector: str,
    output_path: Path,
    dot_output: Path | None = None,
    pdf_output: Path | None = None,
) -> None:
    dot_text = to_detector_graphviz(conn, detector)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_dot(dot_text, "svg"), encoding="utf-8")
    if dot_output is not None:
        dot_output.parent.mkdir(parents=True, exist_ok=True)
        dot_output.write_text(dot_text, encoding="utf-8")
    if pdf_output is not None:
        pdf_output.parent.mkdir(parents=True, exist_ok=True)
        pdf_output.write_bytes(render_dot(dot_text, "pdf", text_output=False))


def to_detector_graphviz(conn: sqlite3.Connection, detector: str) -> str:
    systems = conn.execute(
        "SELECT * FROM systems WHERE detector = ? ORDER BY CASE WHEN key LIKE '%overview' THEN 0 ELSE 1 END, name",
        (detector,),
    ).fetchall()
    if not systems:
        raise SystemExit(f"No systems found for detector {detector!r}")

    detail_systems = [row for row in systems if row["key"] != f"{detector.lower().replace('-', '_')}_overview" and row["key"] != "fd2_vd_overview"]
    if not detail_systems:
        raise SystemExit(f"No detailed systems found for detector {detector!r}")

    all_nodes: dict[str, dict[str, Any]] = {}
    relations: list[sqlite3.Row] = []
    shipping_subsystems: set[str] = set()
    for system_row in detail_systems:
        nodes = load_graph_nodes(conn, system_row["key"])
        all_nodes.update(nodes)
        system_relations = conn.execute(
            "SELECT * FROM relations WHERE system_key = ? ORDER BY id",
            (system_row["key"],),
        ).fetchall()
        shipping_subsystems.update(
            row["key"]
            for row in conn.execute(
                "SELECT key FROM subsystems WHERE system_key = ? AND lower(name) LIKE '%shipping%'",
                (system_row["key"],),
            ).fetchall()
        )
        relations.extend(system_relations)

    context_nodes = detector_context_nodes(detector)
    all_nodes.update(context_nodes)
    relations = [
        relation
        for relation in relations
        if not relation_touches_shipping(all_nodes, shipping_subsystems, relation)
    ]

    title = f"{detector} · Dense System Plate"
    lines = [
        "digraph DetectorPlate {",
        '  graph [',
        '    rankdir=LR,',
        '    splines=true,',
        '    overlap=false,',
        '    newrank=true,',
        '    compound=true,',
        '    bgcolor="#f4efe6",',
        '    pad="0.45",',
        '    nodesep="0.42",',
        '    ranksep="0.78",',
        f'    label="{dot_escape(title)}",',
        '    labelloc="t",',
        '    labeljust="l",',
        '    fontname="Iowan Old Style",',
        '    fontsize=30,',
        '    fontcolor="#183247"',
        "  ];",
        '  node [fontname="Avenir Next", fontsize=11, margin="0.16,0.11", penwidth=1.2];',
        '  edge [fontname="Avenir Next", fontsize=9, arrowsize=0.74, penwidth=1.1, color="#33536a", fontcolor="#33536a"];',
        "",
        "  subgraph cluster_context {",
        f'    label="{dot_escape(detector + " Context")}";',
        '    style="rounded,filled";',
        '    color="#b79d7c";',
        '    fillcolor="#fbf7f0";',
        '    penwidth=1.1;',
        '    fontname="Avenir Next";',
        '    fontsize=14;',
        '    fontcolor="#5a4634";',
    ]

    for key in sorted(context_nodes):
        lines.append(f'    {dot_id(key)} [{dot_node_attrs(context_nodes[key], system_root=context_nodes[key]["entity_type"] == "system")}];')
    lines.append("  }")
    lines.append("")

    for system_row in detail_systems:
        system_nodes = {key: value for key, value in all_nodes.items() if value.get("system_key") == system_row["key"]}
        subsystem_groups: dict[str, list[str]] = defaultdict(list)
        root_nodes: list[str] = []
        for key, node in system_nodes.items():
            if node["entity_type"] == "component_type":
                subsystem_key = node.get("subsystem_key")
                if subsystem_key:
                    subsystem_groups[subsystem_key].append(key)
                else:
                    root_nodes.append(key)
        lines.extend(
            [
                f"  subgraph cluster_{dot_id(system_row['key'])} {{",
                f'    label="{dot_escape(system_row["name"])}";',
                '    style="rounded,filled";',
                '    color="#b59f86";',
                '    fillcolor="#fffaf2";',
                '    penwidth=1.2;',
                '    fontname="Avenir Next";',
                '    fontsize=15;',
                '    fontcolor="#4f3f31";',
                f'    {dot_id(system_row["key"])} [{dot_node_attrs(all_nodes[system_row["key"]], system_root=True)}];',
            ]
        )
        for subsystem_key in sorted(subsystem_groups, key=lambda key: all_nodes[key]["label"]):
            if subsystem_key in shipping_subsystems:
                continue
            lines.extend(
                [
                    f"    subgraph cluster_{dot_id(subsystem_key)} {{",
                    f'      label="{dot_escape(all_nodes[subsystem_key]["label"])}";',
                    '      style="rounded,filled";',
                    '      color="#d4c6b4";',
                    '      fillcolor="#fffdf9";',
                    '      penwidth=1.0;',
                    '      fontname="Avenir Next";',
                    '      fontsize=12;',
                    '      fontcolor="#6a5640";',
                ]
            )
            for node_key in sorted(subsystem_groups[subsystem_key], key=lambda key: all_nodes[key]["label"]):
                lines.append(f'      {dot_id(node_key)} [{dot_node_attrs(all_nodes[node_key])}];')
            lines.append("    }")
        for node_key in sorted(root_nodes, key=lambda key: all_nodes[key]["label"]):
            lines.append(f'    {dot_id(node_key)} [{dot_node_attrs(all_nodes[node_key])}];')
        lines.append("  }")
        lines.append("")

    for relation in relations:
        lines.append(f"  {dot_id(relation['source_node'])} -> {dot_id(relation['target_node'])} [{dot_edge_attrs(relation)}];")

    for bridge in detector_bridge_relations(detector):
        lines.append(f"  {dot_id(bridge['source'])} -> {dot_id(bridge['target'])} [{format_dot_attrs(bridge['attrs'])}];")

    lines.append("}")
    return "\n".join(lines) + "\n"


def detector_context_nodes(detector: str) -> dict[str, dict[str, Any]]:
    if detector != "FD2-VD":
        return {
            f"context.{detector}.detector": {
                "key": f"context.{detector}.detector",
                "label": f"{detector} Complete Detector",
                "entity_type": "system",
                "category": "system",
                "is_batch": False,
            }
        }
    return {
        "context.fd2_vd.detector": {
            "key": "context.fd2_vd.detector",
            "label": "FD2-VD Complete Detector",
            "entity_type": "system",
            "category": "system",
            "is_batch": False,
        },
        "context.fd2_vd.superstructure": {
            "key": "context.fd2_vd.superstructure",
            "label": "Superstructure (SST)",
            "entity_type": "component_type",
            "category": "hardware",
            "is_batch": False,
        },
        "context.fd2_vd.field_cage": {
            "key": "context.fd2_vd.field_cage",
            "label": "Field Cage",
            "entity_type": "component_type",
            "category": "hardware",
            "is_batch": False,
        },
        "context.fd2_vd.bde": {
            "key": "context.fd2_vd.bde",
            "label": "BDE",
            "entity_type": "component_type",
            "category": "electronics",
            "is_batch": False,
        },
        "context.fd2_vd.daq": {
            "key": "context.fd2_vd.daq",
            "label": "DAQ",
            "entity_type": "component_type",
            "category": "electronics",
            "is_batch": False,
        },
        "context.fd2_vd.pds_cathode": {
            "key": "context.fd2_vd.pds_cathode",
            "label": "PDS Cathode Modules",
            "entity_type": "component_type",
            "category": "electronics",
            "is_batch": False,
        },
        "context.fd2_vd.pds_membrane": {
            "key": "context.fd2_vd.pds_membrane",
            "label": "PDS Membrane Modules",
            "entity_type": "component_type",
            "category": "electronics",
            "is_batch": False,
        },
    }


def detector_bridge_relations(detector: str) -> list[dict[str, Any]]:
    if detector != "FD2-VD":
        return []
    return [
        {"source": "context.fd2_vd.detector", "target": "context.fd2_vd.superstructure", "attrs": {"color": "#274c67", "penwidth": "1.4"}},
        {"source": "context.fd2_vd.detector", "target": "fd2_vd_hv_cathode", "attrs": {"color": "#274c67", "penwidth": "1.4"}},
        {"source": "context.fd2_vd.detector", "target": "context.fd2_vd.field_cage", "attrs": {"color": "#274c67", "penwidth": "1.4"}},
        {"source": "context.fd2_vd.detector", "target": "context.fd2_vd.daq", "attrs": {"color": "#274c67", "penwidth": "1.4"}},
        {"source": "context.fd2_vd.superstructure", "target": "fd2_vd_top_crp", "attrs": {"color": "#274c67", "penwidth": "1.4"}},
        {"source": "context.fd2_vd.superstructure", "target": "fd2_vd_ci", "attrs": {"color": "#274c67", "penwidth": "1.4"}},
        {"source": "fd2_vd_top_crp", "target": "fd2_vd_tde", "attrs": {"color": "#1f6f8b", "fontcolor": "#1f6f8b", "style": "dashed", "label": "interface"}},
        {"source": "fd2_vd_top_crp", "target": "context.fd2_vd.bde", "attrs": {"color": "#1f6f8b", "fontcolor": "#1f6f8b", "style": "dashed", "label": "interface"}},
        {"source": "fd2_vd_tde", "target": "context.fd2_vd.daq", "attrs": {"color": "#356c53", "fontcolor": "#356c53", "style": "dashed", "label": "readout"}},
        {"source": "fd2_vd_hv_cathode", "target": "context.fd2_vd.pds_cathode", "attrs": {"color": "#274c67", "penwidth": "1.4"}},
        {"source": "context.fd2_vd.field_cage", "target": "context.fd2_vd.pds_membrane", "attrs": {"color": "#274c67", "penwidth": "1.4"}},
        {"source": "context.fd2_vd.pds_cathode", "target": "context.fd2_vd.bde", "attrs": {"color": "#1f6f8b", "fontcolor": "#1f6f8b", "style": "dashed", "label": "interface"}},
        {"source": "context.fd2_vd.pds_membrane", "target": "context.fd2_vd.bde", "attrs": {"color": "#1f6f8b", "fontcolor": "#1f6f8b", "style": "dashed", "label": "interface"}},
        {"source": "fd2_vd_hv_cathode", "target": "context.fd2_vd.field_cage", "attrs": {"color": "#1f6f8b", "fontcolor": "#1f6f8b", "style": "dashed", "label": "interface"}},
    ]


def relation_touches_shipping(
    all_nodes: dict[str, dict[str, Any]],
    shipping_subsystems: set[str],
    relation: sqlite3.Row,
) -> bool:
    source = all_nodes.get(relation["source_node"])
    target = all_nodes.get(relation["target_node"])
    if source and source.get("subsystem_key") in shipping_subsystems:
        return True
    if target and target.get("subsystem_key") in shipping_subsystems:
        return True
    return False


def class_name_for_node(node: dict[str, Any]) -> str:
    if node["entity_type"] == "system":
        return "system"
    category = node["category"]
    if category == "electronics":
        return "electronics"
    if category == "cable":
        return "cable"
    if category == "fiber":
        return "fiber"
    if category == "sensor":
        return "sensor"
    if category == "interface":
        return "interface"
    if node["is_batch"] or category == "batch":
        return "batch"
    return "hardware"


def style_lines() -> list[str]:
    return [
        "  classDef system fill:#1f4b6e,stroke:#1f4b6e,color:#ffffff;",
        "  classDef hardware fill:#dceaf7,stroke:#4f6d7a,color:#111111;",
        "  classDef electronics fill:#e7f4e4,stroke:#4f772d,color:#111111;",
        "  classDef cable fill:#fff3cd,stroke:#8a6d3b,color:#111111;",
        "  classDef fiber fill:#fde2b7,stroke:#b35c00,color:#111111;",
        "  classDef sensor fill:#f8d7da,stroke:#8c2f39,color:#111111;",
        "  classDef batch fill:#efe3f7,stroke:#6f42c1,color:#111111;",
        "  classDef interface fill:#f4e1c1,stroke:#9a6700,color:#111111;",
    ]


def mermaid_id(key: str) -> str:
    text = re.sub(r"[^0-9a-zA-Z_]", "_", key)
    if text and text[0].isdigit():
        text = f"n_{text}"
    return text


def escape_label(text: str) -> str:
    return text.replace('"', '\\"')


def default_graph_path(output_dir: Path, system_key: str, view: str) -> Path:
    return output_dir / f"{system_key}_{view}.mmd"


def default_svg_path(output_dir: Path, system_key: str, view: str) -> Path:
    return output_dir / f"{system_key}_{view}.svg"


def open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    sys.exit(main())
