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
from urllib.parse import quote
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

XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
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
        if path.suffix.lower() not in {".docx", ".pptx", ".xlsx"}:
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
    elif path.suffix.lower() == ".xlsx":
        metadata, text = parse_xlsx(path)
        file_type = "xlsx"
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


def parse_xlsx(path: Path) -> tuple[dict[str, Any], str]:
    with ZipFile(path) as archive:
        metadata = parse_core_properties(archive, XLSX_NS)
        shared_strings = parse_xlsx_shared_strings(archive)
        sheets = parse_xlsx_sheets(archive)
        sections: list[str] = []
        for sheet_name, sheet_path in sheets:
            if sheet_path not in archive.namelist():
                continue
            rows = parse_xlsx_worksheet(archive.read(sheet_path), shared_strings)
            lines = ["\t".join(row).rstrip() for row in rows]
            lines = [line for line in lines if line.strip()]
            if lines:
                sections.append(f"[{sheet_name}]\n" + "\n".join(lines))
        return metadata, "\n\n".join(sections)


def parse_xlsx_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root.findall("main:si", XLSX_NS):
        parts = [node.text or "" for node in item.findall(".//main:t", XLSX_NS)]
        values.append("".join(parts))
    return values


def parse_xlsx_sheets(archive: ZipFile) -> list[tuple[str, str]]:
    workbook_path = "xl/workbook.xml"
    rels_path = "xl/_rels/workbook.xml.rels"
    if workbook_path not in archive.namelist() or rels_path not in archive.namelist():
        return []

    rels_root = ET.fromstring(archive.read(rels_path))
    relationships = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_root.findall("pkgrel:Relationship", XLSX_NS)
        if "Id" in rel.attrib and "Target" in rel.attrib
    }

    workbook_root = ET.fromstring(archive.read(workbook_path))
    sheets: list[tuple[str, str]] = []
    for sheet in workbook_root.findall(".//main:sheet", XLSX_NS):
        rel_id = sheet.attrib.get(f"{{{XLSX_NS['rel']}}}id")
        if rel_id is None or rel_id not in relationships:
            continue
        target = relationships[rel_id]
        sheet_path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        sheets.append((sheet.attrib.get("name", rel_id), sheet_path))
    return sheets


def parse_xlsx_worksheet(xml_bytes: bytes, shared_strings: list[str]) -> list[list[str]]:
    root = ET.fromstring(xml_bytes)
    rows: list[list[str]] = []
    for row_node in root.findall(".//main:sheetData/main:row", XLSX_NS):
        cells: dict[int, str] = {}
        max_col = 0
        for cell in row_node.findall("main:c", XLSX_NS):
            column = xlsx_column_index(cell.attrib.get("r", ""))
            max_col = max(max_col, column)
            value = xlsx_cell_text(cell, shared_strings)
            if value:
                cells[column] = value
        if max_col:
            rows.append([cells.get(index, "") for index in range(1, max_col + 1)])
    return rows


def xlsx_column_index(cell_reference: str) -> int:
    letters = re.match(r"[A-Z]+", cell_reference.upper())
    if letters is None:
        return 1
    value = 0
    for letter in letters.group(0):
        value = value * 26 + (ord(letter) - ord("A") + 1)
    return value


def xlsx_cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", XLSX_NS)).strip()

    value = cell.findtext("main:v", default="", namespaces=XLSX_NS)
    if value is None:
        return ""
    value = value.strip()
    if cell_type == "s" and value:
        try:
            return shared_strings[int(value)].strip()
        except (IndexError, ValueError):
            return value
    return value


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
    output_path.write_text(responsive_svg(render_dot(dot_text, "svg")), encoding="utf-8")

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
                *dot_cluster_click_lines(subsystem_key, subsystem_node["label"]),
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
    attrs.update(dot_click_attrs(node.get("key"), node.get("label")))

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
    attrs: dict[str, str] = dot_click_attrs(f"relation:{relation['id']}", f"{relation_type.replace('_', ' ')} relation")
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


def dot_cluster_click_lines(object_key: str, label: str | None) -> list[str]:
    attrs = dot_click_attrs(object_key, label)
    return [f'    {key}="{dot_escape(value)}";' for key, value in attrs.items()]


def responsive_svg(svg_text: str) -> str:
    svg_text = re.sub(r'<svg width="[^"]+" height="[^"]+"', '<svg width="100%" height="100%"', svg_text, count=1)
    if 'preserveAspectRatio="' not in svg_text:
        svg_text = svg_text.replace("<svg ", '<svg preserveAspectRatio="xMidYMid meet" ', 1)
    return svg_text


def dot_click_attrs(object_key: str | None, label: str | None = None) -> dict[str, str]:
    if not object_key or object_key.startswith("context."):
        return {}
    return {
        "URL": explorer_object_href(object_key),
        "target": "_top",
        "tooltip": f"Open {label or object_key} in the interactive explorer",
    }


def explorer_object_href(object_key: str) -> str:
    return f"../explorer.html#object={quote(object_key, safe='')}"


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
    data_dir = output_dir / "data"
    system_pages_dir = output_dir / "systems"
    view_pages_dir = output_dir / "views"
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    dot_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    system_pages_dir.mkdir(parents=True, exist_ok=True)
    view_pages_dir.mkdir(parents=True, exist_ok=True)

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
                "page": f"views/{safe_detector}_full_dense.html",
                "inline_svg": inline_publication_svg(svg_path.read_text(encoding="utf-8")),
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
                    "page": f"views/{system_row['key']}_{view}.html",
                    "system_page": f"systems/{system_row['key']}.html",
                    "inline_svg": inline_publication_svg(svg_path.read_text(encoding="utf-8")),
                }
    )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    (assets_dir / "report.css").write_text(publication_css(), encoding="utf-8")
    (assets_dir / "explorer.css").write_text(explorer_css(), encoding="utf-8")
    (assets_dir / "explorer.js").write_text(explorer_js(), encoding="utf-8")
    (assets_dir / "viewer.js").write_text(viewer_js(), encoding="utf-8")
    (assets_dir / "diagram-dialog.js").write_text(diagram_dialog_js(), encoding="utf-8")
    write_explorer_data(conn, data_dir)
    for plate in detector_plates:
        svg_markup = inline_viewer_svg((output_dir / plate["svg"]).read_text(encoding="utf-8"))
        (output_dir / plate["page"]).write_text(clean_html(diagram_page_html(plate, generated, is_plate=True, svg_markup=svg_markup)), encoding="utf-8")
    by_system: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in published_items:
        by_system[item["system_key"]].append(item)
        svg_markup = inline_viewer_svg((output_dir / item["svg"]).read_text(encoding="utf-8"))
        (output_dir / item["page"]).write_text(clean_html(diagram_page_html(item, generated, svg_markup=svg_markup)), encoding="utf-8")
    for system_key, items in by_system.items():
        system_name = items[0]["system_name"]
        (system_pages_dir / f"{system_key}.html").write_text(clean_html(system_page_html(system_key, system_name, items, generated)), encoding="utf-8")
    (output_dir / "index.html").write_text(clean_html(publication_html(conn, published_items, detector_plates, generated)), encoding="utf-8")
    (output_dir / "explorer.html").write_text(clean_html(explorer_html(generated)), encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {output_dir / 'index.html'}")
    print(f"wrote {output_dir / 'explorer.html'}")


def explorer_data(conn: sqlite3.Connection) -> dict[str, Any]:
    systems = conn.execute("SELECT * FROM systems ORDER BY detector, name").fetchall()
    subsystems = conn.execute("SELECT * FROM subsystems ORDER BY system_key, subsystem_id, name").fetchall()
    components = conn.execute("SELECT * FROM component_types ORDER BY system_key, name").fetchall()
    relations = conn.execute("SELECT * FROM relations ORDER BY system_key, id").fetchall()

    fields_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM fields ORDER BY component_key, id").fetchall():
        fields_by_component[row["component_key"]].append(
            {
                "name": row["name"],
                "data_type": row["data_type"],
                "is_qc": bool(row["is_qc"]),
                "description": row["description"],
            }
        )

    artifacts_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM artifacts ORDER BY component_key, id").fetchall():
        artifacts_by_component[row["component_key"]].append(
            {
                "name": row["name"],
                "artifact_type": row["artifact_type"],
                "description": row["description"],
            }
        )

    source_rows = conn.execute("SELECT key, filename, file_type FROM sources ORDER BY filename").fetchall()
    sources = {row["key"]: {"filename": row["filename"], "file_type": row["file_type"]} for row in source_rows}

    node_labels: dict[str, str] = {}
    system_labels: dict[str, str] = {}
    subsystem_labels: dict[str, str] = {}
    for row in systems:
        node_labels[row["key"]] = row["name"]
        system_labels[row["key"]] = row["name"]
    for row in subsystems:
        node_labels[row["key"]] = row["name"]
        subsystem_labels[row["key"]] = row["name"]
    for row in components:
        node_labels[row["key"]] = row["name"]

    records: list[dict[str, Any]] = []
    system_payloads: list[dict[str, Any]] = []
    for row in systems:
        system_key = row["key"]
        component_count = sum(1 for component in components if component["system_key"] == system_key)
        subsystem_count = sum(1 for subsystem in subsystems if subsystem["system_key"] == system_key)
        relation_count = sum(1 for relation in relations if relation["system_key"] == system_key)
        system_payload = {
            "key": system_key,
            "name": row["name"],
            "short_name": row["short_name"],
            "detector": row["detector"],
            "pid_prefix": row["pid_prefix"],
            "description": row["description"],
            "source_key": row["source_key"],
            "notes": row["notes"],
            "stats": {
                "subsystems": subsystem_count,
                "components": component_count,
                "relations": relation_count,
            },
        }
        system_payloads.append(system_payload)
        records.append(
            {
                "key": system_key,
                "kind": "system",
                "kind_label": "System",
                "system_key": system_key,
                "label": row["name"],
                "description": row["description"],
                "notes": row["notes"],
                "source_key": row["source_key"],
                "detector": row["detector"],
                "pid_prefix": row["pid_prefix"],
                "category": "system",
                "fields": [],
                "artifacts": [],
            }
        )

    for row in subsystems:
        records.append(
            {
                "key": row["key"],
                "kind": "subsystem",
                "kind_label": "Subsystem",
                "system_key": row["system_key"],
                "system_label": system_labels.get(row["system_key"], row["system_key"]),
                "label": row["name"],
                "description": row["description"],
                "notes": row["notes"],
                "source_key": row["source_key"],
                "pid_id": row["subsystem_id"],
                "category": "subsystem",
                "fields": [],
                "artifacts": [],
            }
        )

    for row in components:
        records.append(
            {
                "key": row["key"],
                "kind": "component",
                "kind_label": "Component Type",
                "system_key": row["system_key"],
                "system_label": system_labels.get(row["system_key"], row["system_key"]),
                "subsystem_key": row["subsystem_key"],
                "subsystem_label": subsystem_labels.get(row["subsystem_key"], row["subsystem_key"]),
                "label": row["name"],
                "description": row["description"],
                "notes": row["notes"],
                "source_key": row["source_key"],
                "category": row["category"],
                "is_batch": bool(row["is_batch"]),
                "has_exec_summary": bool(row["has_exec_summary"]),
                "fields": fields_by_component.get(row["key"], []),
                "artifacts": artifacts_by_component.get(row["key"], []),
            }
        )

    for row in relations:
        source_label = node_labels.get(row["source_node"], row["source_node"])
        target_label = node_labels.get(row["target_node"], row["target_node"])
        label = f"{source_label} {row['relation_type'].replace('_', ' ')} {target_label}"
        records.append(
            {
                "key": f"relation:{row['id']}",
                "kind": "relation",
                "kind_label": "Relation",
                "system_key": row["system_key"],
                "system_label": system_labels.get(row["system_key"], row["system_key"]),
                "label": label,
                "description": row["description"],
                "source_key": row["source_key"],
                "category": row["relation_type"],
                "relation_type": row["relation_type"],
                "source_node": row["source_node"],
                "target_node": row["target_node"],
                "source_label": source_label,
                "target_label": target_label,
                "cardinality": row["cardinality"],
                "phase": row["phase"],
                "tags": [tag for tag in (row["tags"] or "").split(",") if tag],
                "fields": [],
                "artifacts": [],
            }
        )

    return {
        "systems": system_payloads,
        "records": records,
        "relations": [record for record in records if record["kind"] == "relation"],
        "sources": sources,
    }


def write_explorer_data(conn: sqlite3.Connection, data_dir: Path) -> None:
    data = explorer_data(conn)
    systems_dir = data_dir / "systems"
    systems_dir.mkdir(parents=True, exist_ok=True)

    object_index: dict[str, dict[str, str]] = {}
    for system in data["systems"]:
        system_key = system["key"]
        records = [record for record in data["records"] if record["system_key"] == system_key]
        relations = [record for record in records if record["kind"] == "relation"]
        for record in records:
            object_index[record["key"]] = {
                "system_key": system_key,
                "kind": record["kind"],
                "label": record["label"],
            }
        payload = {
            "system": system,
            "records": records,
            "relations": relations,
        }
        write_json(systems_dir / f"{system_key}.json", payload)
        write_data_script(
            systems_dir / f"{system_key}.js",
            f"window.HWDB_DATA.systems[{json.dumps(system_key)}]",
            payload,
        )

    index_payload = {
        "schema_version": 1,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "systems": data["systems"],
        "sources": data["sources"],
        "object_index": object_index,
    }
    write_json(data_dir / "hwdb-index.json", index_payload)
    write_data_script(data_dir / "hwdb-index.js", "window.HWDB_DATA.index", index_payload)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def clean_html(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()) + "\n"


def write_data_script(path: Path, assignment: str, data: Any) -> None:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=True).replace("</", "<\\/")
    path.write_text(
        "\n".join(
            [
                "window.HWDB_DATA = window.HWDB_DATA || {};",
                "window.HWDB_DATA.systems = window.HWDB_DATA.systems || {};",
                f"{assignment} = {payload};",
                "",
            ]
        ),
        encoding="utf-8",
    )


def legacy_explorer_html(conn: sqlite3.Connection) -> str:
    data_json = json.dumps(explorer_data(conn), separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    generated = html.escape(datetime.now().strftime("%Y-%m-%d %H:%M"))
    page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hardware Database Explorer</title>
  <style>
    :root {
      --bg: #f5f7f8;
      --panel: #ffffff;
      --ink: #172634;
      --muted: #607080;
      --line: #d7dee4;
      --blue: #244b6a;
      --teal: #24736f;
      --green: #4f7b42;
      --amber: #9d6514;
      --rose: #9a4254;
      --violet: #7451a5;
      --shadow: 0 16px 40px rgba(22, 38, 52, 0.10);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      color: var(--ink);
      background: var(--bg);
      font-family: "Avenir Next", "Helvetica Neue", Arial, sans-serif;
    }

    a { color: inherit; }

    .app-shell {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 18rem minmax(0, 1fr);
      grid-template-rows: auto minmax(0, 1fr);
    }

    header {
      grid-column: 1 / -1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 1rem 1.25rem;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 10;
    }

    h1 {
      margin: 0;
      font-size: 1.15rem;
      line-height: 1.2;
      font-weight: 700;
    }

    .top-actions {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      color: var(--muted);
      font-size: 0.86rem;
    }

    .top-actions a {
      color: var(--blue);
      text-decoration: none;
      font-weight: 700;
    }

    aside {
      border-right: 1px solid var(--line);
      background: #eef3f5;
      padding: 1rem;
      overflow: auto;
    }

    .systems-title,
    .panel-title {
      margin: 0 0 0.65rem;
      color: var(--muted);
      font-size: 0.75rem;
      font-weight: 800;
      text-transform: uppercase;
    }

    .system-list {
      display: grid;
      gap: 0.55rem;
    }

    .system-button {
      display: block;
      width: 100%;
      padding: 0.75rem;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel);
      text-align: left;
      text-decoration: none;
      box-shadow: 0 1px 0 rgba(22, 38, 52, 0.04);
    }

    .system-button.active {
      border-color: var(--teal);
      outline: 2px solid rgba(36, 115, 111, 0.14);
    }

    .system-name {
      display: block;
      font-weight: 800;
      line-height: 1.25;
    }

    .system-meta {
      display: block;
      margin-top: 0.35rem;
      color: var(--muted);
      font-size: 0.78rem;
    }

    main {
      min-width: 0;
      padding: 1rem;
      overflow: auto;
    }

    .query-panel {
      display: grid;
      gap: 0.75rem;
      padding: 1rem;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }

    .query-row {
      display: grid;
      grid-template-columns: minmax(14rem, 1fr) auto;
      gap: 0.75rem;
      align-items: center;
    }

    input[type="search"] {
      width: 100%;
      min-height: 2.55rem;
      padding: 0.55rem 0.7rem;
      border: 1px solid var(--line);
      border-radius: 7px;
      color: var(--ink);
      font: inherit;
      background: #fbfcfd;
    }

    .kind-filter {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
    }

    .kind-filter button,
    .dialog-actions button {
      min-height: 2.25rem;
      padding: 0 0.7rem;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfd;
      color: var(--ink);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }

    .kind-filter button.active {
      border-color: var(--blue);
      background: var(--blue);
      color: #ffffff;
    }

    .summary-line {
      margin: 0;
      color: var(--muted);
      font-size: 0.88rem;
    }

    .result-panel {
      margin-top: 1rem;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
      box-shadow: var(--shadow);
    }

    table {
      width: 100%;
      border-collapse: collapse;
    }

    th,
    td {
      padding: 0.72rem 0.8rem;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 0.9rem;
    }

    th {
      background: #f0f4f7;
      color: var(--muted);
      font-size: 0.75rem;
      text-transform: uppercase;
    }

    tbody tr:hover {
      background: #f8fbfc;
    }

    .object-link {
      color: var(--blue);
      font-weight: 800;
      text-decoration: none;
    }

    .object-key {
      display: block;
      margin-top: 0.25rem;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.76rem;
      overflow-wrap: anywhere;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 1.45rem;
      padding: 0 0.45rem;
      border-radius: 999px;
      color: #ffffff;
      font-size: 0.75rem;
      font-weight: 800;
      white-space: nowrap;
    }

    .badge.system { background: var(--blue); }
    .badge.subsystem { background: var(--teal); }
    .badge.component { background: var(--green); }
    .badge.relation { background: var(--amber); }
    .badge.batch { background: var(--violet); }
    .badge.sensor { background: var(--rose); }

    dialog {
      width: min(860px, calc(100vw - 2rem));
      max-height: min(780px, calc(100vh - 2rem));
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 0;
      color: var(--ink);
      box-shadow: 0 24px 80px rgba(22, 38, 52, 0.30);
    }

    dialog::backdrop {
      background: rgba(11, 20, 28, 0.45);
    }

    .dialog-shell {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      max-height: inherit;
    }

    .dialog-head,
    .dialog-actions {
      padding: 1rem;
      border-bottom: 1px solid var(--line);
      background: #f8fafb;
    }

    .dialog-actions {
      border-top: 1px solid var(--line);
      border-bottom: 0;
      display: flex;
      justify-content: flex-end;
      gap: 0.5rem;
    }

    .dialog-title {
      margin: 0.45rem 0 0;
      font-size: 1.35rem;
      line-height: 1.2;
    }

    .dialog-body {
      overflow: auto;
      padding: 1rem;
      display: grid;
      gap: 1rem;
    }

    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.65rem;
    }

    .detail-item {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 0.65rem;
      background: #fbfcfd;
    }

    .detail-item span {
      display: block;
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 800;
      text-transform: uppercase;
    }

    .detail-item code,
    code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.83rem;
      overflow-wrap: anywhere;
    }

    .dialog-section h3 {
      margin: 0 0 0.45rem;
      font-size: 0.92rem;
    }

    .dialog-section p {
      margin: 0;
      color: var(--muted);
      line-height: 1.45;
    }

    .mini-table th,
    .mini-table td {
      font-size: 0.83rem;
      padding: 0.55rem;
    }

    .empty-state {
      padding: 1.4rem;
      color: var(--muted);
    }

    @media (max-width: 860px) {
      .app-shell {
        display: block;
      }

      header {
        position: static;
        align-items: flex-start;
        flex-direction: column;
      }

      aside {
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }

      .system-list {
        grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
      }

      .query-row,
      .detail-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <header>
      <h1>Hardware Database Explorer</h1>
      <div class="top-actions">
        <a href="index.html">Portfolio</a>
        <span>Generated __GENERATED__</span>
      </div>
    </header>
    <aside>
      <p class="systems-title">Systems</p>
      <nav class="system-list" id="system-list"></nav>
    </aside>
    <main>
      <section class="query-panel" aria-label="Database query">
        <div>
          <p class="panel-title" id="active-system-title">Database Query</p>
          <p class="summary-line" id="summary-line"></p>
        </div>
        <div class="query-row">
          <input id="query-input" type="search" autocomplete="off" placeholder="Search key, PID, name, source, relation">
          <div class="kind-filter" id="kind-filter">
            <button type="button" data-kind="all" class="active">All</button>
            <button type="button" data-kind="subsystem">Subsystems</button>
            <button type="button" data-kind="component">Components</button>
            <button type="button" data-kind="relation">Relations</button>
          </div>
        </div>
      </section>
      <section class="result-panel" aria-label="Query results">
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Object</th>
              <th>Context</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody id="result-body"></tbody>
        </table>
        <div class="empty-state" id="empty-state" hidden>No matching rows.</div>
      </section>
    </main>
  </div>
  <dialog id="object-dialog"></dialog>
  <script id="hwdb-data" type="application/json">__DATA_JSON__</script>
  <script>
(() => {
  const data = JSON.parse(document.getElementById("hwdb-data").textContent);
  const records = data.records;
  const systems = data.systems;
  const relations = data.relations;
  const recordByKey = new Map(records.map((record) => [record.key, record]));
  const systemByKey = new Map(systems.map((system) => [system.key, system]));
  const state = {
    systemKey: systems[0] ? systems[0].key : "",
    kind: "all",
    query: "",
  };

  const systemList = document.getElementById("system-list");
  const queryInput = document.getElementById("query-input");
  const resultBody = document.getElementById("result-body");
  const emptyState = document.getElementById("empty-state");
  const activeSystemTitle = document.getElementById("active-system-title");
  const summaryLine = document.getElementById("summary-line");
  const dialog = document.getElementById("object-dialog");

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function compact(value) {
    return value === undefined || value === null || value === "" ? "-" : value;
  }

  function recordText(record) {
    return [
      record.key,
      record.kind_label,
      record.label,
      record.description,
      record.notes,
      record.source_key,
      record.category,
      record.pid_id,
      record.subsystem_label,
      record.source_label,
      record.target_label,
      record.relation_type,
      (record.fields || []).map((field) => `${field.name} ${field.description}`).join(" "),
      (record.artifacts || []).map((artifact) => `${artifact.name} ${artifact.description}`).join(" "),
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function badgeClass(record) {
    if (record.kind === "system" || record.kind === "subsystem" || record.kind === "relation") {
      return record.kind;
    }
    if (record.is_batch || record.category === "batch") {
      return "batch";
    }
    if (record.category === "sensor") {
      return "sensor";
    }
    return "component";
  }

  function renderSystems() {
    systemList.innerHTML = systems.map((system) => `
      <a class="system-button ${system.key === state.systemKey ? "active" : ""}" href="#system=${encodeURIComponent(system.key)}" data-system="${esc(system.key)}">
        <span class="system-name">${esc(system.name)}</span>
        <span class="system-meta">${esc(compact(system.pid_prefix))} PID prefix · ${system.stats.subsystems} subsystems · ${system.stats.components} components</span>
      </a>
    `).join("");
  }

  function matchingRecords() {
    const query = state.query.trim().toLowerCase();
    return records
      .filter((record) => record.system_key === state.systemKey)
      .filter((record) => state.kind === "all" || record.kind === state.kind)
      .filter((record) => !query || recordText(record).includes(query))
      .sort((left, right) => {
        const order = {system: 0, subsystem: 1, component: 2, relation: 3};
        return (order[left.kind] - order[right.kind]) || left.label.localeCompare(right.label);
      });
  }

  function renderResults() {
    const system = systemByKey.get(state.systemKey);
    const rows = matchingRecords();
    activeSystemTitle.textContent = system ? system.name : "Database Query";
    summaryLine.textContent = `${rows.length} rows from ${system ? system.name : "the database"}`;
    resultBody.innerHTML = rows.map((record) => {
      const context = record.kind === "relation"
        ? `${record.source_label} -> ${record.target_label}`
        : (record.subsystem_label || record.system_label || system?.name || "-");
      return `
        <tr data-key="${esc(record.key)}">
          <td><span class="badge ${badgeClass(record)}">${esc(record.kind_label)}</span></td>
          <td>
            <a class="object-link" href="#object=${encodeURIComponent(record.key)}">${esc(record.label)}</a>
            <span class="object-key">${esc(record.key)}</span>
          </td>
          <td>${esc(compact(context))}</td>
          <td>${esc(compact(record.source_key))}</td>
        </tr>
      `;
    }).join("");
    emptyState.hidden = rows.length !== 0;
  }

  function renderKindFilter() {
    document.querySelectorAll("[data-kind]").forEach((button) => {
      button.classList.toggle("active", button.dataset.kind === state.kind);
    });
  }

  function selectSystem(systemKey, updateHash = true) {
    if (!systemByKey.has(systemKey)) {
      return;
    }
    state.systemKey = systemKey;
    renderSystems();
    renderResults();
    if (updateHash) {
      history.replaceState(null, "", `#system=${encodeURIComponent(systemKey)}`);
    }
  }

  function detailItems(record) {
    const items = [
      ["Database Key", `<code>${esc(record.key)}</code>`],
      ["System", esc(systemByKey.get(record.system_key)?.name || record.system_key)],
      ["Type", esc(record.kind_label)],
      ["Category", esc(compact(record.category))],
      ["PID / Subsystem ID", esc(compact(record.pid_id || record.pid_prefix))],
      ["Source", esc(compact(record.source_key))],
    ];
    if (record.subsystem_label) {
      items.splice(2, 0, ["Subsystem", esc(record.subsystem_label)]);
    }
    return items.map(([label, value]) => `
      <div class="detail-item"><span>${label}</span>${value}</div>
    `).join("");
  }

  function tableRows(rows, columns) {
    if (!rows.length) {
      return "";
    }
    return `
      <table class="mini-table">
        <thead><tr>${columns.map((column) => `<th>${esc(column.label)}</th>`).join("")}</tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>${columns.map((column) => `<td>${esc(compact(row[column.key]))}</td>`).join("")}</tr>
          `).join("")}
        </tbody>
      </table>
    `;
  }

  function relatedRows(record) {
    if (record.kind === "relation") {
      return [];
    }
    return relations
      .filter((relation) => relation.source_node === record.key || relation.target_node === record.key)
      .map((relation) => ({
        key: relation.key,
        relation_type: relation.relation_type,
        source: relation.source_label,
        target: relation.target_label,
      }));
  }

  function renderRelationDetail(record) {
    if (record.kind !== "relation") {
      return "";
    }
    return `
      <section class="dialog-section">
        <h3>Relation</h3>
        <div class="detail-grid">
          <div class="detail-item"><span>Source</span><a href="#object=${encodeURIComponent(record.source_node)}">${esc(record.source_label)}</a></div>
          <div class="detail-item"><span>Target</span><a href="#object=${encodeURIComponent(record.target_node)}">${esc(record.target_label)}</a></div>
          <div class="detail-item"><span>Relation Type</span>${esc(record.relation_type)}</div>
          <div class="detail-item"><span>Cardinality</span>${esc(compact(record.cardinality))}</div>
        </div>
      </section>
    `;
  }

  function renderDialog(record) {
    const fields = tableRows(record.fields || [], [
      {key: "name", label: "Field"},
      {key: "data_type", label: "Type"},
      {key: "description", label: "Description"},
    ]);
    const artifacts = tableRows(record.artifacts || [], [
      {key: "name", label: "Artifact"},
      {key: "artifact_type", label: "Type"},
      {key: "description", label: "Description"},
    ]);
    const related = relatedRows(record);
    const relatedTable = tableRows(related, [
      {key: "relation_type", label: "Type"},
      {key: "source", label: "Source"},
      {key: "target", label: "Target"},
    ]);
    const link = `${location.origin}${location.pathname}#object=${encodeURIComponent(record.key)}`;
    dialog.innerHTML = `
      <div class="dialog-shell">
        <div class="dialog-head">
          <span class="badge ${badgeClass(record)}">${esc(record.kind_label)}</span>
          <h2 class="dialog-title">${esc(record.label)}</h2>
        </div>
        <div class="dialog-body">
          <section class="dialog-section">
            <div class="detail-grid">${detailItems(record)}</div>
          </section>
          ${record.description ? `<section class="dialog-section"><h3>Description</h3><p>${esc(record.description)}</p></section>` : ""}
          ${record.notes ? `<section class="dialog-section"><h3>Notes</h3><p>${esc(record.notes)}</p></section>` : ""}
          ${renderRelationDetail(record)}
          ${fields ? `<section class="dialog-section"><h3>Fields</h3>${fields}</section>` : ""}
          ${artifacts ? `<section class="dialog-section"><h3>Artifacts</h3>${artifacts}</section>` : ""}
          ${relatedTable ? `<section class="dialog-section"><h3>Related Relations</h3>${relatedTable}</section>` : ""}
          <section class="dialog-section"><h3>Stable Link</h3><p><a class="object-link" href="#object=${encodeURIComponent(record.key)}">${esc(link)}</a></p></section>
        </div>
        <div class="dialog-actions">
          <button type="button" id="close-dialog">Close</button>
        </div>
      </div>
    `;
    dialog.querySelector("#close-dialog").addEventListener("click", () => dialog.close());
    dialog.querySelectorAll("a[href^='#object=']").forEach((linkNode) => {
      linkNode.addEventListener("click", (event) => {
        const params = new URLSearchParams(linkNode.hash.slice(1));
        const key = params.get("object");
        if (key && recordByKey.has(key)) {
          event.preventDefault();
          openRecord(key, true);
        }
      });
    });
  }

  function openRecord(key, updateHash = true) {
    const record = recordByKey.get(key);
    if (!record) {
      return;
    }
    if (record.system_key !== state.systemKey) {
      selectSystem(record.system_key, false);
    }
    renderDialog(record);
    if (!dialog.open) {
      dialog.showModal();
    }
    if (updateHash) {
      history.replaceState(null, "", `#object=${encodeURIComponent(key)}`);
    }
  }

  function syncFromHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    const objectKey = params.get("object");
    const systemKey = params.get("system");
    if (objectKey && recordByKey.has(objectKey)) {
      openRecord(objectKey, false);
      return;
    }
    if (systemKey && systemByKey.has(systemKey)) {
      selectSystem(systemKey, false);
    }
  }

  systemList.addEventListener("click", (event) => {
    const link = event.target.closest("[data-system]");
    if (!link) {
      return;
    }
    event.preventDefault();
    selectSystem(link.dataset.system);
  });

  resultBody.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-key]");
    if (!row) {
      return;
    }
    event.preventDefault();
    openRecord(row.dataset.key);
  });

  document.getElementById("kind-filter").addEventListener("click", (event) => {
    const button = event.target.closest("[data-kind]");
    if (!button) {
      return;
    }
    state.kind = button.dataset.kind;
    renderKindFilter();
    renderResults();
  });

  queryInput.addEventListener("input", () => {
    state.query = queryInput.value;
    renderResults();
  });

  window.addEventListener("hashchange", syncFromHash);
  renderSystems();
  renderKindFilter();
  renderResults();
  syncFromHash();
})();
  </script>
</body>
</html>
"""
    return page.replace("__DATA_JSON__", data_json).replace("__GENERATED__", generated)


def explorer_html(generated: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hardware Database Explorer</title>
  <link rel="stylesheet" href="assets/explorer.css">
  <script defer src="assets/explorer.js"></script>
</head>
<body>
  <div class="app-shell">
    <header class="app-header">
      <h1>Hardware Database Explorer</h1>
      <div class="top-actions">
        <a href="index.html">Portfolio</a>
        <span>Generated {html.escape(generated)}</span>
      </div>
    </header>
    <aside class="system-sidebar" aria-label="Systems">
      <p class="systems-title">Systems</p>
      <nav class="system-list" id="system-list"></nav>
    </aside>
    <main class="explorer-main">
      <section class="query-panel" aria-label="Database query">
        <div>
          <p class="panel-title" id="active-system-title">Database Query</p>
          <p class="summary-line" id="summary-line">Loading database index...</p>
        </div>
        <div class="query-row">
          <input id="query-input" type="search" autocomplete="off" placeholder="Search key, PID, name, source, relation">
          <div class="kind-filter" id="kind-filter">
            <button type="button" data-kind="all" class="active">All</button>
            <button type="button" data-kind="subsystem">Subsystems</button>
            <button type="button" data-kind="component">Components</button>
            <button type="button" data-kind="relation">Relations</button>
          </div>
        </div>
      </section>
      <section class="result-panel" aria-label="Query results">
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Object</th>
              <th>Context</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody id="result-body"></tbody>
        </table>
        <div class="empty-state" id="empty-state" hidden>No matching rows.</div>
      </section>
    </main>
  </div>
  <dialog id="object-dialog"></dialog>
</body>
</html>
"""


def explorer_css() -> str:
    return """
:root {
  --paper: #f6f1e8;
  --paper-strong: #efe6d8;
  --panel: rgba(255,255,255,0.62);
  --panel-solid: #fffdf8;
  --ink: #183247;
  --ink-soft: #4a6274;
  --line: #cbbda8;
  --navy: #183247;
  --blue: #274c67;
  --teal: #4f7c7a;
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

a { color: inherit; }

.app-shell {
  min-height: 100vh;
  display: grid;
  grid-template-columns: 18rem minmax(0, 1fr);
  gap: 1.4rem;
  align-items: start;
  max-width: 1700px;
  margin: 0 auto;
  padding: 2rem;
}

.app-header {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1.55rem 1.75rem;
  border: 1px solid var(--line);
  border-radius: 28px;
  background:
    linear-gradient(135deg, rgba(24,50,71,0.98), rgba(36,72,93,0.94)),
    linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0));
  color: #f7f3ed;
  box-shadow: var(--shadow);
}

h1 {
  margin: 0;
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-size: clamp(1.8rem, 3vw, 3rem);
  line-height: 1;
  font-weight: 600;
  letter-spacing: 0.01em;
}

.top-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  color: rgba(247,243,237,0.8);
  font-size: 0.86rem;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.top-actions a {
  color: #f7f3ed;
  text-decoration: none;
  font-weight: 500;
}

.system-sidebar {
  border: 1px solid var(--line);
  border-radius: 18px;
  background: var(--panel);
  padding: 1.1rem;
  box-shadow: var(--shadow);
  overflow: auto;
  position: sticky;
  top: 1rem;
  max-height: calc(100vh - 2rem);
}

.systems-title,
.panel-title {
  margin: 0 0 0.65rem;
  color: var(--ink-soft);
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.16em;
}

.system-list {
  display: grid;
  gap: 0.55rem;
}

.system-button {
  display: block;
  width: 100%;
  padding: 0.82rem 0.85rem;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: rgba(255,255,255,0.54);
  text-align: left;
  text-decoration: none;
  box-shadow: 0 1px 0 rgba(24, 50, 71, 0.04);
}

.system-button.active {
  border-color: var(--navy);
  background: rgba(24,50,71,0.94);
  color: #f7f3ed;
}

.system-name {
  display: block;
  font-weight: 500;
  line-height: 1.25;
}

.system-meta {
  display: block;
  margin-top: 0.35rem;
  color: var(--ink-soft);
  font-size: 0.78rem;
}

.system-button.active .system-meta {
  color: rgba(247,243,237,0.74);
}

.explorer-main {
  min-width: 0;
}

.query-panel {
  display: grid;
  gap: 0.75rem;
  padding: 1.15rem;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: var(--panel);
  box-shadow: var(--shadow);
}

.query-row {
  display: grid;
  grid-template-columns: minmax(14rem, 1fr) auto;
  gap: 0.75rem;
  align-items: center;
}

input[type="search"] {
  width: 100%;
  min-height: 2.55rem;
  padding: 0.55rem 0.75rem;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: var(--ink);
  font: inherit;
  background: rgba(255,255,255,0.68);
}

.kind-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.kind-filter button,
.dialog-actions button {
  min-height: 2.25rem;
  padding: 0 0.7rem;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(255,255,255,0.54);
  color: var(--ink);
  font: inherit;
  font-weight: 500;
  cursor: pointer;
}

.kind-filter button.active {
  border-color: var(--navy);
  background: var(--navy);
  color: #f7f3ed;
}

.summary-line {
  margin: 0;
  color: var(--ink-soft);
  font-size: 0.88rem;
}

.result-panel {
  margin-top: 1rem;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: var(--panel-solid);
  overflow: auto;
  box-shadow: var(--shadow);
}

table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  padding: 0.72rem 0.85rem;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
  font-size: 0.9rem;
}

th {
  background: rgba(239,230,216,0.66);
  color: var(--ink-soft);
  font-size: 0.75rem;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

tbody tr:hover {
  background: rgba(239,230,216,0.28);
}

.object-link {
  color: var(--navy);
  font-weight: 500;
  text-decoration: none;
}

.detail-item .object-link,
.mini-table .object-link {
  color: var(--navy);
}

.relation-arrow {
  color: var(--ink-soft);
  font-weight: 500;
}

.object-key {
  display: block;
  margin-top: 0.25rem;
  color: var(--ink-soft);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.76rem;
  overflow-wrap: anywhere;
}

.badge {
  display: inline-flex;
  align-items: center;
  min-height: 1.45rem;
  padding: 0 0.45rem;
  border-radius: 999px;
  color: #ffffff;
  font-size: 0.75rem;
  font-weight: 500;
  white-space: nowrap;
}

.badge.system { background: var(--blue); }
.badge.subsystem { background: var(--teal); }
.badge.component { background: var(--green); }
.badge.relation { background: var(--amber); }
.badge.batch { background: var(--violet); }
.badge.sensor { background: var(--rose); }

dialog {
  width: min(860px, calc(100vw - 2rem));
  max-height: min(780px, calc(100vh - 2rem));
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 0;
  color: var(--ink);
  background: var(--panel-solid);
  box-shadow: 0 24px 80px rgba(24, 50, 71, 0.30);
}

dialog:focus {
  outline: none;
}

dialog::backdrop {
  background: rgba(24, 50, 71, 0.44);
}

.dialog-shell {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  max-height: inherit;
}

.dialog-head,
.dialog-actions {
  padding: 1rem;
  border-bottom: 1px solid var(--line);
  background: rgba(239,230,216,0.54);
}

.dialog-actions {
  border-top: 1px solid var(--line);
  border-bottom: 0;
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}

.dialog-title {
  margin: 0.45rem 0 0;
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 1.35rem;
  line-height: 1.2;
  font-weight: 600;
}

.dialog-body {
  overflow: auto;
  padding: 1rem;
  display: grid;
  gap: 1rem;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.65rem;
}

.detail-item {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 0.65rem;
  background: rgba(255,255,255,0.58);
}

.detail-item span {
  display: block;
  color: var(--ink-soft);
  font-size: 0.72rem;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.detail-item code,
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.83rem;
  overflow-wrap: anywhere;
}

.dialog-section h3 {
  margin: 0 0 0.45rem;
  font-size: 0.92rem;
}

.dialog-section p {
  margin: 0;
  color: var(--ink-soft);
  line-height: 1.45;
}

.mini-table th,
.mini-table td {
  font-size: 0.83rem;
  padding: 0.55rem;
}

.empty-state {
  padding: 1.4rem;
  color: var(--ink-soft);
}

/* Match the publication issue framing while keeping the explorer dense. */
body {
  background:
    linear-gradient(90deg, rgba(24,50,71,0.045) 1px, transparent 1px),
    linear-gradient(180deg, #fbf7ef 0%, #efe7d9 56%, #f7f3ec 100%);
  background-size: 32px 32px, auto;
}

.app-shell {
  grid-template-columns: 15rem minmax(0, 1fr);
  gap: clamp(1rem, 2vw, 1.8rem);
  max-width: 1620px;
  padding: clamp(1rem, 2.8vw, 3rem);
}

.app-header {
  border: 0;
  border-top: 4px solid var(--ink);
  border-bottom: 1px solid rgba(24,50,71,0.72);
  border-radius: 0;
  background: transparent;
  color: var(--ink);
  box-shadow: none;
  padding: 1rem 0 1.35rem;
}

h1 {
  color: var(--ink);
  font-size: clamp(2.2rem, 5vw, 5.6rem);
  letter-spacing: 0;
}

.top-actions {
  color: var(--ink-soft);
  font-weight: 600;
}

.top-actions a {
  color: var(--ink);
  text-decoration: underline;
  text-underline-offset: 0.16em;
}

.system-sidebar,
.query-panel,
.result-panel,
.system-button,
input[type="search"],
.kind-filter button,
.dialog-actions button,
dialog,
.detail-item {
  border-radius: 0;
  box-shadow: none;
}

.system-sidebar {
  border: 0;
  border-left: 2px solid var(--ink);
  background: transparent;
  padding: 0.2rem 0 0.2rem 1rem;
}

.system-button {
  background: rgba(255,255,255,0.24);
}

.system-button.active {
  border-color: var(--ink);
  background: var(--ink);
}

.query-panel {
  border-color: rgba(24,50,71,0.38);
  background: rgba(255,255,255,0.34);
}

.result-panel {
  border-color: rgba(24,50,71,0.38);
  background: rgba(255,255,255,0.48);
}

.kind-filter button {
  background: rgba(255,255,255,0.22);
}

.kind-filter button.active {
  border-color: var(--ink);
  background: var(--ink);
}

@media (max-width: 860px) {
  .app-shell {
    display: block;
    padding: 1rem;
  }

  .app-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .system-sidebar {
    position: static;
    max-height: none;
    margin-top: 1rem;
    margin-bottom: 1rem;
  }

  .system-list {
    grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  }

  .query-row,
  .detail-grid {
    grid-template-columns: 1fr;
  }
}
"""


def diagram_dialog_js() -> str:
    return """
const hwdbDialogScript = document.currentScript
  || Array.from(document.scripts).find((script) => script.src && script.src.endsWith("assets/diagram-dialog.js"));
const hwdbSiteRoot = hwdbDialogScript ? new URL("../", hwdbDialogScript.src) : new URL("./", location.href);
const hwdbSystemCache = new Map();
const hwdbDataScriptCache = new Map();

let hwdbIndexData = null;
let hwdbSystemByKey = new Map();
let hwdbObjectIndex = new Map();

function hwdbDataUrl(path) {
  return new URL(path, hwdbSiteRoot).href;
}

function hwdbDataScriptPath(path) {
  return path.replace(/\\.json$/, ".js");
}

function hwdbDataKeyFromPath(path) {
  const fileName = new URL(path, location.href).pathname.split("/").pop() || "";
  const key = fileName.replace(/\\.json$/, "");
  try {
    return decodeURIComponent(key);
  } catch {
    return key;
  }
}

function hwdbScriptPayload(path) {
  const root = window.HWDB_DATA || {};
  if (path.endsWith("hwdb-index.json")) {
    return root.index;
  }
  return root.systems ? root.systems[hwdbDataKeyFromPath(path)] : null;
}

async function hwdbLoadDataScript(path) {
  const scriptPath = hwdbDataScriptPath(path);
  if (!hwdbDataScriptCache.has(scriptPath)) {
    hwdbDataScriptCache.set(scriptPath, new Promise((resolve, reject) => {
      const script = document.createElement("script");
      const timeout = window.setTimeout(() => {
        script.remove();
        reject(new Error(`Timed out while loading ${scriptPath}`));
      }, 15000);
      script.onload = () => {
        window.clearTimeout(timeout);
        resolve();
      };
      script.onerror = () => {
        window.clearTimeout(timeout);
        reject(new Error(`Unable to load ${scriptPath}`));
      };
      script.async = true;
      script.src = scriptPath;
      document.head.appendChild(script);
    }));
  }
  await hwdbDataScriptCache.get(scriptPath);
  const payload = hwdbScriptPayload(path);
  if (!payload) {
    throw new Error(`Data script did not register ${path}`);
  }
  return payload;
}

async function hwdbLoadData(path) {
  const url = hwdbDataUrl(path);
  if (location.protocol === "file:") {
    return hwdbLoadDataScript(url);
  }
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText} while loading ${url}`);
    }
    return response.json();
  } catch (error) {
    try {
      return await hwdbLoadDataScript(url);
    } catch {
      throw error;
    }
  }
}

async function hwdbEnsureIndex() {
  if (!hwdbIndexData) {
    hwdbIndexData = await hwdbLoadData("data/hwdb-index.json");
    hwdbSystemByKey = new Map(hwdbIndexData.systems.map((system) => [system.key, system]));
    hwdbObjectIndex = new Map(Object.entries(hwdbIndexData.object_index));
  }
  return hwdbIndexData;
}

async function hwdbLoadSystem(systemKey) {
  if (!hwdbSystemCache.has(systemKey)) {
    hwdbSystemCache.set(systemKey, hwdbLoadData(`data/systems/${encodeURIComponent(systemKey)}.json`));
  }
  return hwdbSystemCache.get(systemKey);
}

function hwdbEsc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function hwdbCompact(value) {
  return value === undefined || value === null || value === "" ? "-" : value;
}

function hwdbBadgeClass(record) {
  if (record.kind === "system" || record.kind === "subsystem" || record.kind === "relation") {
    return record.kind;
  }
  if (record.is_batch || record.category === "batch") {
    return "batch";
  }
  if (record.category === "sensor") {
    return "sensor";
  }
  return "component";
}

function hwdbObjectLink(key, label) {
  if (!key || !hwdbObjectIndex.has(key)) {
    return hwdbEsc(hwdbCompact(label || key));
  }
  return `<a class="hwdb-object-link" href="#object=${encodeURIComponent(key)}" data-object-key="${hwdbEsc(key)}">${hwdbEsc(label || key)}</a>`;
}

function hwdbEnsureDialog() {
  let dialog = document.getElementById("hwdb-object-dialog");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.id = "hwdb-object-dialog";
    dialog.className = "hwdb-object-dialog";
    dialog.addEventListener("click", (event) => {
      if (event.target.closest("[data-dialog-close]")) {
        dialog.close();
      }
    });
    document.body.appendChild(dialog);
  }
  return dialog;
}

function hwdbDetailItems(record) {
  const systemLabel = hwdbSystemByKey.get(record.system_key)?.name || record.system_label || record.system_key;
  const subsystemKey = record.subsystem_key || (record.kind === "subsystem" ? record.key : "");
  const items = [
    ["Database Key", `<code>${hwdbEsc(record.key)}</code>`],
    ["System", hwdbObjectLink(record.system_key, systemLabel)],
    ["Type", hwdbEsc(record.kind_label)],
    ["Category", hwdbEsc(hwdbCompact(record.category))],
    ["PID / Subsystem ID", hwdbEsc(hwdbCompact(record.pid_id || record.pid_prefix))],
    ["Source", hwdbEsc(hwdbCompact(record.source_key))],
  ];
  if (record.subsystem_label) {
    items.splice(2, 0, ["Subsystem", hwdbObjectLink(subsystemKey, record.subsystem_label)]);
  }
  return items.map(([label, value]) => `
    <div class="hwdb-detail-item"><span>${label}</span>${value}</div>
  `).join("");
}

function hwdbTableRows(rows, columns) {
  if (!rows.length) {
    return "";
  }
  return `
    <table class="hwdb-mini-table">
      <thead><tr>${columns.map((column) => `<th>${hwdbEsc(column.label)}</th>`).join("")}</tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr>${columns.map((column) => `<td>${column.render ? column.render(row) : hwdbEsc(hwdbCompact(row[column.key]))}</td>`).join("")}</tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function hwdbRelatedRows(record, relations) {
  if (record.kind === "relation") {
    return [];
  }
  return relations
    .filter((relation) => relation.source_node === record.key || relation.target_node === record.key)
    .map((relation) => ({
      key: relation.key,
      relation_type: relation.relation_type,
      source_key: relation.source_node,
      source: relation.source_label,
      target_key: relation.target_node,
      target: relation.target_label,
    }));
}

function hwdbRelationDetail(record) {
  if (record.kind !== "relation") {
    return "";
  }
  return `
    <section class="hwdb-dialog-section">
      <h3>Relation</h3>
      <div class="hwdb-detail-grid">
        <div class="hwdb-detail-item"><span>Source</span>${hwdbObjectLink(record.source_node, record.source_label)}</div>
        <div class="hwdb-detail-item"><span>Target</span>${hwdbObjectLink(record.target_node, record.target_label)}</div>
        <div class="hwdb-detail-item"><span>Relation Type</span>${hwdbEsc(record.relation_type)}</div>
        <div class="hwdb-detail-item"><span>Cardinality</span>${hwdbEsc(hwdbCompact(record.cardinality))}</div>
      </div>
    </section>
  `;
}

function hwdbRecordMap(payload) {
  return new Map(payload.records.map((record) => [record.key, record]));
}

function hwdbRelationLabel(edge) {
  return (edge.relation_type || edge.type || "related_to").replace(/_/g, " ");
}

function hwdbGraphEdges(payload) {
  const recordsByKey = hwdbRecordMap(payload);
  const edges = (payload.relations || []).map((relation) => ({
    key: relation.key,
    source: relation.source_node,
    target: relation.target_node,
    type: relation.relation_type,
    relation_type: relation.relation_type,
    label: hwdbRelationLabel(relation),
    cardinality: relation.cardinality,
    synthetic: false,
  }));

  payload.records.forEach((record) => {
    if (record.kind === "subsystem" && record.system_key && record.key !== record.system_key) {
      edges.push({
        key: `synthetic:system:${record.system_key}:${record.key}`,
        source: record.system_key,
        target: record.key,
        type: "subsystem",
        relation_type: "subsystem",
        label: "has subsystem",
        synthetic: true,
      });
    }
    if (record.kind === "component" && record.subsystem_key) {
      edges.push({
        key: `synthetic:subsystem:${record.subsystem_key}:${record.key}`,
        source: record.subsystem_key,
        target: record.key,
        type: "member",
        relation_type: "member",
        label: "in subsystem",
        synthetic: true,
      });
    }
  });

  return edges.filter((edge) => recordsByKey.has(edge.source) && recordsByKey.has(edge.target));
}

function hwdbEdgePriority(edge) {
  const order = {
    contains: 0,
    subsystem: 1,
    member: 2,
    connects_to: 3,
    interfaces_with: 4,
    reads_out: 5,
    powers: 6,
    timed_by: 7,
    depends_on: 8,
  };
  return order[edge.type] ?? 20;
}

function hwdbNeighborhood(payload, record, depth = 1, maxNodes = 34) {
  const recordsByKey = hwdbRecordMap(payload);
  const allEdges = hwdbGraphEdges(payload);

  if (record.kind === "relation") {
    const nodes = [record.source_node, record.target_node].filter((key) => recordsByKey.has(key));
    return {
      focusKey: record.key,
      nodeKeys: new Set(nodes),
      levels: new Map(nodes.map((key) => [key, key === record.source_node ? -1 : 1])),
      edges: [{
        key: record.key,
        source: record.source_node,
        target: record.target_node,
        type: record.relation_type,
        relation_type: record.relation_type,
        label: hwdbRelationLabel(record),
        cardinality: record.cardinality,
        synthetic: false,
        focus: true,
      }].filter((edge) => recordsByKey.has(edge.source) && recordsByKey.has(edge.target)),
      recordsByKey,
      truncated: false,
    };
  }

  const adjacency = new Map();
  allEdges.forEach((edge) => {
    if (!adjacency.has(edge.source)) {
      adjacency.set(edge.source, []);
    }
    if (!adjacency.has(edge.target)) {
      adjacency.set(edge.target, []);
    }
    adjacency.get(edge.source).push(edge);
    adjacency.get(edge.target).push(edge);
  });

  adjacency.forEach((edges) => {
    edges.sort((left, right) => {
      const priority = hwdbEdgePriority(left) - hwdbEdgePriority(right);
      if (priority !== 0) {
        return priority;
      }
      return left.label.localeCompare(right.label);
    });
  });

  const nodeKeys = new Set([record.key]);
  const levels = new Map([[record.key, 0]]);
  const queue = [record.key];
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index];
    const currentLevel = levels.get(current) || 0;
    if (Math.abs(currentLevel) >= depth) {
      continue;
    }
    for (const edge of adjacency.get(current) || []) {
      const outbound = edge.source === current;
      const neighbor = outbound ? edge.target : edge.source;
      const nextLevel = currentLevel + (outbound ? 1 : -1);
      if (Math.abs(nextLevel) > depth) {
        continue;
      }
      const existing = levels.get(neighbor);
      if (existing === undefined || Math.abs(nextLevel) < Math.abs(existing)) {
        levels.set(neighbor, nextLevel);
        nodeKeys.add(neighbor);
        queue.push(neighbor);
      }
    }
  }

  let truncated = false;
  if (nodeKeys.size > maxNodes) {
    truncated = true;
    const sorted = Array.from(nodeKeys).sort((left, right) => {
      if (left === record.key) {
        return -1;
      }
      if (right === record.key) {
        return 1;
      }
      const leftLevel = levels.get(left) || 0;
      const rightLevel = levels.get(right) || 0;
      const distance = Math.abs(leftLevel) - Math.abs(rightLevel);
      if (distance !== 0) {
        return distance;
      }
      if (leftLevel !== rightLevel) {
        return leftLevel - rightLevel;
      }
      return (recordsByKey.get(left)?.label || left).localeCompare(recordsByKey.get(right)?.label || right);
    });
    nodeKeys.clear();
    sorted.slice(0, maxNodes).forEach((key) => nodeKeys.add(key));
  }

  const edges = allEdges.filter((edge) => nodeKeys.has(edge.source) && nodeKeys.has(edge.target));
  return {focusKey: record.key, nodeKeys, levels, edges, recordsByKey, truncated};
}

function hwdbWrapLabel(label, maxChars = 24, maxLines = 3) {
  const words = String(label || "").split(/\\s+/).filter(Boolean);
  const lines = [];
  let current = "";
  words.forEach((word) => {
    const next = current ? `${current} ${word}` : word;
    if (next.length > maxChars && current) {
      lines.push(current);
      current = word;
    } else {
      current = next;
    }
  });
  if (current) {
    lines.push(current);
  }
  if (lines.length > maxLines) {
    const clipped = lines.slice(0, maxLines);
    clipped[maxLines - 1] = `${clipped[maxLines - 1].replace(/\\.+$/, "")}...`;
    return clipped;
  }
  return lines.length ? lines : ["-"];
}

function hwdbNodeText(label, x, y, width) {
  const lines = hwdbWrapLabel(label);
  const startY = y - ((lines.length - 1) * 7);
  return lines.map((line, index) => (
    `<text x="${x + width / 2}" y="${startY + index * 15}" text-anchor="middle" class="hwdb-graph-node-label">${hwdbEsc(line)}</text>`
  )).join("");
}

function hwdbGraphNodeClass(record, focusKey) {
  const classes = ["hwdb-graph-node", hwdbBadgeClass(record)];
  if (record.key === focusKey) {
    classes.push("focus");
  }
  return classes.join(" ");
}

function hwdbEdgeClass(edge) {
  if (edge.type === "contains" || edge.type === "subsystem" || edge.type === "member") {
    return "hwdb-graph-edge hierarchy";
  }
  return "hwdb-graph-edge dependency";
}

function hwdbRenderNeighborhoodSvg(payload, record, depth) {
  const graph = hwdbNeighborhood(payload, record, depth);
  const nodeWidth = 210;
  const nodeHeight = 62;
  const columnGap = 110;
  const rowGap = 42;
  const pad = 40;
  const levels = Array.from(new Set(Array.from(graph.nodeKeys).map((key) => graph.levels.get(key) || 0))).sort((a, b) => a - b);
  const levelIndex = new Map(levels.map((level, index) => [level, index]));
  const nodesByLevel = new Map(levels.map((level) => [level, []]));

  Array.from(graph.nodeKeys).forEach((key) => {
    const level = graph.levels.get(key) || 0;
    nodesByLevel.get(level).push(key);
  });
  nodesByLevel.forEach((keys) => {
    keys.sort((left, right) => {
      if (left === record.key) {
        return -1;
      }
      if (right === record.key) {
        return 1;
      }
      return (graph.recordsByKey.get(left)?.label || left).localeCompare(graph.recordsByKey.get(right)?.label || right);
    });
  });

  const maxRows = Math.max(1, ...Array.from(nodesByLevel.values()).map((keys) => keys.length));
  const width = Math.max(640, pad * 2 + levels.length * nodeWidth + Math.max(0, levels.length - 1) * columnGap);
  const height = Math.max(300, pad * 2 + maxRows * nodeHeight + Math.max(0, maxRows - 1) * rowGap);
  const positions = new Map();

  levels.forEach((level) => {
    const keys = nodesByLevel.get(level);
    const x = pad + (levelIndex.get(level) || 0) * (nodeWidth + columnGap);
    const groupHeight = keys.length * nodeHeight + Math.max(0, keys.length - 1) * rowGap;
    const startY = pad + Math.max(0, (height - pad * 2 - groupHeight) / 2);
    keys.forEach((key, row) => {
      positions.set(key, {x, y: startY + row * (nodeHeight + rowGap)});
    });
  });

  const edgeMarkup = graph.edges.map((edge) => {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) {
      return "";
    }
    let sx;
    let sy;
    let tx;
    let ty;
    let path;
    if (Math.abs(source.x - target.x) < 2) {
      sx = source.x + nodeWidth / 2;
      sy = source.y < target.y ? source.y + nodeHeight : source.y;
      tx = target.x + nodeWidth / 2;
      ty = source.y < target.y ? target.y : target.y + nodeHeight;
      const bend = source.y < target.y ? 36 : -36;
      path = `M ${sx} ${sy} C ${sx + bend} ${sy + 24}, ${tx + bend} ${ty - 24}, ${tx} ${ty}`;
    } else {
      const forward = source.x < target.x;
      sx = forward ? source.x + nodeWidth : source.x;
      sy = source.y + nodeHeight / 2;
      tx = forward ? target.x : target.x + nodeWidth;
      ty = target.y + nodeHeight / 2;
      const curve = Math.max(42, Math.abs(tx - sx) * 0.45);
      path = `M ${sx} ${sy} C ${sx + (forward ? curve : -curve)} ${sy}, ${tx + (forward ? -curve : curve)} ${ty}, ${tx} ${ty}`;
    }
    const label = `${edge.label}${edge.cardinality ? ` (${edge.cardinality})` : ""}`;
    const labelX = Math.round((sx + tx) / 2);
    const labelY = Math.round((sy + ty) / 2) - 8;
    const linkAttrs = edge.synthetic ? "" : ` href="#object=${encodeURIComponent(edge.key)}" data-object-key="${hwdbEsc(edge.key)}"`;
    const labelText = `<text x="${labelX}" y="${labelY}" text-anchor="middle" class="hwdb-graph-edge-label">${hwdbEsc(label)}</text>`;
    const edgeBody = `<path d="${path}" class="${hwdbEdgeClass(edge)}" marker-end="url(#hwdb-arrow)"></path>${labelText}`;
    return edge.synthetic ? edgeBody : `<a${linkAttrs}>${edgeBody}</a>`;
  }).join("");

  const nodeMarkup = Array.from(graph.nodeKeys).map((key) => {
    const node = graph.recordsByKey.get(key);
    const position = positions.get(key);
    if (!node || !position) {
      return "";
    }
    const labelY = position.y + nodeHeight / 2 + 5;
    return `
      <a href="#object=${encodeURIComponent(key)}" data-object-key="${hwdbEsc(key)}">
        <g class="${hwdbGraphNodeClass(node, graph.focusKey)}" transform="translate(${position.x}, ${position.y})">
          <rect width="${nodeWidth}" height="${nodeHeight}" rx="0" ry="0"></rect>
          <text x="12" y="17" class="hwdb-graph-node-kind">${hwdbEsc(node.kind_label || node.kind)}</text>
          ${hwdbNodeText(node.label, 0, labelY, nodeWidth)}
        </g>
      </a>
    `;
  }).join("");

  const empty = graph.nodeKeys.size === 0
    ? '<p class="hwdb-neighborhood-empty">No local graph is available for this object.</p>'
    : "";
  const truncated = graph.truncated
    ? '<p class="hwdb-neighborhood-note">Showing the nearest objects first. Increase precision by selecting a closer node.</p>'
    : "";
  return `
    <p class="hwdb-neighborhood-summary">${graph.nodeKeys.size} objects · ${graph.edges.length} links · depth ${depth}</p>
    ${empty || `<svg class="hwdb-neighborhood-svg" role="img" aria-label="Local object graph" viewBox="0 0 ${width} ${height}" style="width:${width}px;height:${height}px">
      <defs>
        <marker id="hwdb-arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
          <path d="M 0 0 L 10 4 L 0 8 z" class="hwdb-graph-arrow"></path>
        </marker>
      </defs>
      <g class="hwdb-graph-edges">${edgeMarkup}</g>
      <g class="hwdb-graph-nodes">${nodeMarkup}</g>
    </svg>`}
    ${truncated}
  `;
}

function hwdbNeighborhoodSection(payload, record, depth = 1) {
  return `
    <section class="hwdb-dialog-section hwdb-neighborhood-section">
      <div class="hwdb-neighborhood-head">
        <h3>Local Diagram</h3>
        <div class="hwdb-graph-toolbar" aria-label="Local graph depth">
          <button type="button" data-graph-depth="1" class="active">1 level</button>
          <button type="button" data-graph-depth="2">2 levels</button>
          <button type="button" data-graph-depth="3">3 levels</button>
        </div>
      </div>
      <div class="hwdb-neighborhood-frame" data-neighborhood-frame>
        ${hwdbRenderNeighborhoodSvg(payload, record, depth)}
      </div>
    </section>
  `;
}

function hwdbWireNeighborhoodControls(dialog, payload, record) {
  const frame = dialog.querySelector("[data-neighborhood-frame]");
  if (!frame) {
    return;
  }
  dialog.querySelectorAll("[data-graph-depth]").forEach((button) => {
    button.addEventListener("click", () => {
      dialog.querySelectorAll("[data-graph-depth]").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
      frame.innerHTML = hwdbRenderNeighborhoodSvg(payload, record, Number(button.dataset.graphDepth || "1"));
    });
  });
}

async function hwdbRecordContext(key) {
  await hwdbEnsureIndex();
  const indexed = hwdbObjectIndex.get(key);
  if (!indexed) {
    throw new Error(`No database object found for ${key}`);
  }
  const payload = await hwdbLoadSystem(indexed.system_key);
  const record = payload.records.find((item) => item.key === key);
  if (!record) {
    throw new Error(`No record found for ${key}`);
  }
  return {record, payload};
}

async function hwdbOpenObject(key) {
  const dialog = hwdbEnsureDialog();
  dialog.innerHTML = `
    <div class="hwdb-dialog-shell">
      <div class="hwdb-dialog-head">
        <p class="hwdb-dialog-kicker">Database Object</p>
        <h2>Loading...</h2>
      </div>
    </div>
  `;
  if (!dialog.open) {
    dialog.showModal();
  }
  try {
    const {record, payload} = await hwdbRecordContext(key);
    const fields = hwdbTableRows(record.fields || [], [
      {key: "name", label: "Field"},
      {key: "data_type", label: "Type"},
      {key: "description", label: "Description"},
    ]);
    const artifacts = hwdbTableRows(record.artifacts || [], [
      {key: "name", label: "Artifact"},
      {key: "artifact_type", label: "Type"},
      {key: "description", label: "Description"},
    ]);
    const related = hwdbRelatedRows(record, payload.relations || []);
    const relatedTable = hwdbTableRows(related, [
      {key: "relation_type", label: "Type", render: (row) => hwdbObjectLink(row.key, row.relation_type)},
      {key: "source", label: "Source", render: (row) => hwdbObjectLink(row.source_key, row.source)},
      {key: "target", label: "Target", render: (row) => hwdbObjectLink(row.target_key, row.target)},
    ]);
    dialog.innerHTML = `
      <div class="hwdb-dialog-shell">
        <div class="hwdb-dialog-head">
          <span class="hwdb-dialog-badge ${hwdbBadgeClass(record)}">${hwdbEsc(record.kind_label)}</span>
          <h2>${hwdbEsc(record.label)}</h2>
        </div>
        <div class="hwdb-dialog-body">
          ${hwdbNeighborhoodSection(payload, record)}
          <section class="hwdb-dialog-section">
            <div class="hwdb-detail-grid">${hwdbDetailItems(record)}</div>
          </section>
          ${record.description ? `<section class="hwdb-dialog-section"><h3>Description</h3><p>${hwdbEsc(record.description)}</p></section>` : ""}
          ${record.notes ? `<section class="hwdb-dialog-section"><h3>Notes</h3><p>${hwdbEsc(record.notes)}</p></section>` : ""}
          ${hwdbRelationDetail(record)}
          ${fields ? `<section class="hwdb-dialog-section"><h3>Fields</h3>${fields}</section>` : ""}
          ${artifacts ? `<section class="hwdb-dialog-section"><h3>Artifacts</h3>${artifacts}</section>` : ""}
          ${relatedTable ? `<section class="hwdb-dialog-section"><h3>Related Relations</h3>${relatedTable}</section>` : ""}
        </div>
        <div class="hwdb-dialog-actions">
          <button type="button" data-dialog-close>Close</button>
        </div>
      </div>
    `;
    hwdbWireNeighborhoodControls(dialog, payload, record);
  } catch (error) {
    dialog.innerHTML = `
      <div class="hwdb-dialog-shell">
        <div class="hwdb-dialog-head">
          <p class="hwdb-dialog-kicker">Database Object</p>
          <h2>Unable to load object</h2>
        </div>
        <div class="hwdb-dialog-body">
          <p>${hwdbEsc(error.message)}</p>
        </div>
        <div class="hwdb-dialog-actions">
          <button type="button" data-dialog-close>Close</button>
        </div>
      </div>
    `;
  }
}

function hwdbFindAnchor(node) {
  let current = node;
  while (current && current.nodeType === 1) {
    if (current.localName && current.localName.toLowerCase() === "a") {
      return current;
    }
    current = current.parentNode;
  }
  return null;
}

function hwdbHrefValue(link) {
  return link.getAttribute("href")
    || link.getAttributeNS("http://www.w3.org/1999/xlink", "href")
    || (link.href && (typeof link.href === "string" ? link.href : link.href.baseVal))
    || "";
}

function hwdbObjectKeyFromHref(href, base) {
  if (!href) {
    return "";
  }
  try {
    const url = new URL(href, base || location.href);
    return new URLSearchParams(url.hash.slice(1)).get("object") || "";
  } catch {
    const marker = "#object=";
    const index = href.indexOf(marker);
    return index >= 0 ? decodeURIComponent(href.slice(index + marker.length)) : "";
  }
}

function hwdbHandleObjectClick(event) {
  const explicit = event.target.closest ? event.target.closest("[data-object-key]") : null;
  const link = explicit || hwdbFindAnchor(event.target);
  if (!link) {
    return;
  }
  const base = link.ownerDocument?.location?.href || location.href;
  const key = link.dataset?.objectKey || hwdbObjectKeyFromHref(hwdbHrefValue(link), base);
  if (!key) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  hwdbOpenObject(key);
}

function hwdbAttachFrame(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc || doc.__hwdbDialogAttached) {
      return;
    }
    doc.__hwdbDialogAttached = true;
    doc.addEventListener("click", hwdbHandleObjectClick, true);
  } catch {
    // Cross-origin or not-yet-loaded frames are ignored; generated diagrams are same-origin.
  }
}

function hwdbScanDiagramFrames() {
  document.querySelectorAll("iframe.diagram-object").forEach((frame) => {
    if (!frame.dataset.hwdbDialogFrame) {
      frame.dataset.hwdbDialogFrame = "1";
      frame.addEventListener("load", () => hwdbAttachFrame(frame));
    }
    hwdbAttachFrame(frame);
  });
}

document.addEventListener("click", hwdbHandleObjectClick, true);

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", hwdbScanDiagramFrames);
} else {
  hwdbScanDiagramFrames();
}

window.addEventListener("load", hwdbScanDiagramFrames);

new MutationObserver(hwdbScanDiagramFrames).observe(document.documentElement, {
  childList: true,
  subtree: true,
});

window.HWDB_DIALOG = {open: hwdbOpenObject};
"""


def viewer_js() -> str:
    return """
const viewer = document.querySelector(".viewer-svg-wrap");
const svg = viewer ? viewer.querySelector("svg") : null;
const controls = Array.from(document.querySelectorAll("[data-view-mode]"));
let currentMode = "100";

function setActive(mode) {
  controls.forEach((button) => {
    button.classList.toggle("active", button.dataset.viewMode === mode);
  });
}

function viewBoxParts(svg) {
  const parts = (svg.getAttribute("viewBox") || "").trim().split(/\\s+/).map(Number);
  if (parts.length === 4 && parts[2] > 0 && parts[3] > 0) {
    return parts;
  }
  return [0, 0, 1200, 900];
}

function viewBoxRatio(svg) {
  const parts = viewBoxParts(svg);
  return parts[3] / parts[2];
}

function viewBoxWidth(svg) {
  return viewBoxParts(svg)[2];
}

function applyMode(mode) {
  if (!viewer || !svg) {
    return;
  }
  svg.style.maxWidth = "none";
  svg.style.display = "block";
  currentMode = mode;
  if (mode === "fit") {
    viewer.style.overflow = "hidden";
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "100%");
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  } else {
    viewer.style.overflow = "auto";
    const scale = mode === "width" ? 1 : Number(mode) / 100;
    const baseWidth = mode === "width" ? viewer.clientWidth : viewBoxWidth(svg);
    const width = Math.max(320, Math.round(baseWidth * scale));
    const height = Math.max(240, Math.round(width * viewBoxRatio(svg)));
    svg.setAttribute("width", `${width}px`);
    svg.setAttribute("height", `${height}px`);
    svg.setAttribute("preserveAspectRatio", "xMidYMin meet");
  }
  setActive(mode);
}

applyMode("100");

window.addEventListener("resize", () => applyMode(currentMode));

controls.forEach((button) => {
  button.addEventListener("click", () => applyMode(button.dataset.viewMode));
});
"""


def explorer_js() -> str:
    return """
const state = {
  systemKey: "",
  kind: "all",
  query: "",
};

let indexData = null;
let systems = [];
let relations = [];
let currentRecords = [];
let recordByKey = new Map();
let systemByKey = new Map();
let objectIndex = new Map();
const systemCache = new Map();
const dataScriptCache = new Map();

const systemList = document.getElementById("system-list");
const queryInput = document.getElementById("query-input");
const resultBody = document.getElementById("result-body");
const emptyState = document.getElementById("empty-state");
const activeSystemTitle = document.getElementById("active-system-title");
const summaryLine = document.getElementById("summary-line");
const dialog = document.getElementById("object-dialog");

function dataScriptPath(path) {
  return path.replace(/\\.json$/, ".js");
}

function dataKeyFromPath(path) {
  const fileName = path.split("/").pop() || "";
  const key = fileName.replace(/\\.json$/, "");
  try {
    return decodeURIComponent(key);
  } catch {
    return key;
  }
}

function scriptPayload(path) {
  const root = window.HWDB_DATA || {};
  if (path.endsWith("hwdb-index.json")) {
    return root.index;
  }
  return root.systems ? root.systems[dataKeyFromPath(path)] : null;
}

async function loadDataScript(path) {
  const scriptPath = dataScriptPath(path);
  if (!dataScriptCache.has(scriptPath)) {
    dataScriptCache.set(scriptPath, new Promise((resolve, reject) => {
      const script = document.createElement("script");
      const timeout = window.setTimeout(() => {
        script.remove();
        reject(new Error(`Timed out while loading ${scriptPath}`));
      }, 15000);
      script.onload = () => {
        window.clearTimeout(timeout);
        resolve();
      };
      script.onerror = () => {
        window.clearTimeout(timeout);
        reject(new Error(`Unable to load ${scriptPath}`));
      };
      script.async = true;
      script.src = scriptPath;
      document.head.appendChild(script);
    }));
  }
  await dataScriptCache.get(scriptPath);
  const payload = scriptPayload(path);
  if (!payload) {
    throw new Error(`Data script did not register ${path}`);
  }
  return payload;
}

async function loadData(path) {
  if (location.protocol === "file:") {
    return loadDataScript(path);
  }
  try {
    const response = await fetch(path);
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText} while loading ${path}`);
    }
    return response.json();
  } catch (error) {
    try {
      return await loadDataScript(path);
    } catch {
      throw error;
    }
  }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function compact(value) {
  return value === undefined || value === null || value === "" ? "-" : value;
}

function objectHref(key) {
  return `#object=${encodeURIComponent(key)}`;
}

function objectLink(key, label, className = "object-link") {
  if (!key || !objectIndex.has(key)) {
    return esc(compact(label || key));
  }
  return `<a class="${className}" href="${objectHref(key)}">${esc(label || key)}</a>`;
}

function recordText(record) {
  return [
    record.key,
    record.kind_label,
    record.label,
    record.description,
    record.notes,
    record.source_key,
    record.category,
    record.pid_id,
    record.subsystem_label,
    record.source_label,
    record.target_label,
    record.relation_type,
    (record.fields || []).map((field) => `${field.name} ${field.description}`).join(" "),
    (record.artifacts || []).map((artifact) => `${artifact.name} ${artifact.description}`).join(" "),
  ].filter(Boolean).join(" ").toLowerCase();
}

function badgeClass(record) {
  if (record.kind === "system" || record.kind === "subsystem" || record.kind === "relation") {
    return record.kind;
  }
  if (record.is_batch || record.category === "batch") {
    return "batch";
  }
  if (record.category === "sensor") {
    return "sensor";
  }
  return "component";
}

function renderSystems() {
  systemList.innerHTML = systems.map((system) => `
    <a class="system-button ${system.key === state.systemKey ? "active" : ""}" href="#system=${encodeURIComponent(system.key)}" data-system="${esc(system.key)}">
      <span class="system-name">${esc(system.name)}</span>
      <span class="system-meta">${esc(compact(system.pid_prefix))} PID prefix · ${system.stats.subsystems} subsystems · ${system.stats.components} components</span>
    </a>
  `).join("");
}

async function loadSystem(systemKey) {
  if (!systemCache.has(systemKey)) {
    systemCache.set(systemKey, loadData(`data/systems/${encodeURIComponent(systemKey)}.json`));
  }
  return systemCache.get(systemKey);
}

function setActiveDataset(payload) {
  currentRecords = payload.records;
  relations = payload.relations;
  recordByKey = new Map(currentRecords.map((record) => [record.key, record]));
}

function matchingRecords() {
  const query = state.query.trim().toLowerCase();
  return currentRecords
    .filter((record) => state.kind === "all" || record.kind === state.kind)
    .filter((record) => !query || recordText(record).includes(query))
    .sort((left, right) => {
      const order = {system: 0, subsystem: 1, component: 2, relation: 3};
      return (order[left.kind] - order[right.kind]) || left.label.localeCompare(right.label);
    });
}

function renderResults() {
  const system = systemByKey.get(state.systemKey);
  const rows = matchingRecords();
  activeSystemTitle.textContent = system ? system.name : "Database Query";
  summaryLine.textContent = `${rows.length} rows from ${system ? system.name : "the database"}`;
  resultBody.innerHTML = rows.map((record) => {
    const context = record.kind === "relation"
      ? `${objectLink(record.source_node, record.source_label)} <span class="relation-arrow">-&gt;</span> ${objectLink(record.target_node, record.target_label)}`
      : (record.subsystem_label
        ? objectLink(record.subsystem_key, record.subsystem_label)
        : objectLink(record.system_key, record.system_label || system?.name || record.system_key));
    return `
      <tr data-key="${esc(record.key)}">
        <td><span class="badge ${badgeClass(record)}">${esc(record.kind_label)}</span></td>
        <td>
          ${objectLink(record.key, record.label)}
          <span class="object-key">${esc(record.key)}</span>
        </td>
        <td>${context}</td>
        <td>${esc(compact(record.source_key))}</td>
      </tr>
    `;
  }).join("");
  emptyState.hidden = rows.length !== 0;
}

function renderKindFilter() {
  document.querySelectorAll("[data-kind]").forEach((button) => {
    button.classList.toggle("active", button.dataset.kind === state.kind);
  });
}

async function selectSystem(systemKey, updateHash = true) {
  if (!systemByKey.has(systemKey)) {
    return;
  }
  state.systemKey = systemKey;
  renderSystems();
  summaryLine.textContent = "Loading system records...";
  const payload = await loadSystem(systemKey);
  setActiveDataset(payload);
  renderResults();
  if (updateHash) {
    history.replaceState(null, "", `#system=${encodeURIComponent(systemKey)}`);
  }
}

function detailItems(record) {
  const systemLabel = systemByKey.get(record.system_key)?.name || record.system_label || record.system_key;
  const subsystemKey = record.subsystem_key || (record.kind === "subsystem" ? record.key : "");
  const items = [
    ["Database Key", `<code>${esc(record.key)}</code>`],
    ["System", objectLink(record.system_key, systemLabel)],
    ["Type", esc(record.kind_label)],
    ["Category", esc(compact(record.category))],
    ["PID / Subsystem ID", esc(compact(record.pid_id || record.pid_prefix))],
    ["Source", esc(compact(record.source_key))],
  ];
  if (record.subsystem_label) {
    items.splice(2, 0, ["Subsystem", objectLink(subsystemKey, record.subsystem_label)]);
  }
  return items.map(([label, value]) => `
    <div class="detail-item"><span>${label}</span>${value}</div>
  `).join("");
}

function tableRows(rows, columns) {
  if (!rows.length) {
    return "";
  }
  return `
    <table class="mini-table">
      <thead><tr>${columns.map((column) => `<th>${esc(column.label)}</th>`).join("")}</tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr>${columns.map((column) => `<td>${column.render ? column.render(row) : esc(compact(row[column.key]))}</td>`).join("")}</tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function relatedRows(record) {
  if (record.kind === "relation") {
    return [];
  }
  return relations
    .filter((relation) => relation.source_node === record.key || relation.target_node === record.key)
    .map((relation) => ({
      key: relation.key,
      relation_type: relation.relation_type,
      source_key: relation.source_node,
      source: relation.source_label,
      target_key: relation.target_node,
      target: relation.target_label,
    }));
}

function renderRelationDetail(record) {
  if (record.kind !== "relation") {
    return "";
  }
  return `
    <section class="dialog-section">
      <h3>Relation</h3>
      <div class="detail-grid">
        <div class="detail-item"><span>Source</span>${objectLink(record.source_node, record.source_label)}</div>
        <div class="detail-item"><span>Target</span>${objectLink(record.target_node, record.target_label)}</div>
        <div class="detail-item"><span>Relation Type</span>${esc(record.relation_type)}</div>
        <div class="detail-item"><span>Cardinality</span>${esc(compact(record.cardinality))}</div>
      </div>
    </section>
  `;
}

function renderDialog(record) {
  const fields = tableRows(record.fields || [], [
    {key: "name", label: "Field"},
    {key: "data_type", label: "Type"},
    {key: "description", label: "Description"},
  ]);
  const artifacts = tableRows(record.artifacts || [], [
    {key: "name", label: "Artifact"},
    {key: "artifact_type", label: "Type"},
    {key: "description", label: "Description"},
  ]);
  const related = relatedRows(record);
  const relatedTable = tableRows(related, [
    {key: "relation_type", label: "Type", render: (row) => objectLink(row.key, row.relation_type)},
    {key: "source", label: "Source", render: (row) => objectLink(row.source_key, row.source)},
    {key: "target", label: "Target", render: (row) => objectLink(row.target_key, row.target)},
  ]);
  const link = `${location.origin}${location.pathname}#object=${encodeURIComponent(record.key)}`;
  dialog.innerHTML = `
    <div class="dialog-shell">
      <div class="dialog-head">
        <span class="badge ${badgeClass(record)}">${esc(record.kind_label)}</span>
        <h2 class="dialog-title">${esc(record.label)}</h2>
      </div>
      <div class="dialog-body">
        <section class="dialog-section">
          <div class="detail-grid">${detailItems(record)}</div>
        </section>
        ${record.description ? `<section class="dialog-section"><h3>Description</h3><p>${esc(record.description)}</p></section>` : ""}
        ${record.notes ? `<section class="dialog-section"><h3>Notes</h3><p>${esc(record.notes)}</p></section>` : ""}
        ${renderRelationDetail(record)}
        ${fields ? `<section class="dialog-section"><h3>Fields</h3>${fields}</section>` : ""}
        ${artifacts ? `<section class="dialog-section"><h3>Artifacts</h3>${artifacts}</section>` : ""}
        ${relatedTable ? `<section class="dialog-section"><h3>Related Relations</h3>${relatedTable}</section>` : ""}
        <section class="dialog-section"><h3>Stable Link</h3><p><a class="object-link" href="#object=${encodeURIComponent(record.key)}">${esc(link)}</a></p></section>
      </div>
      <div class="dialog-actions">
        <button type="button" id="close-dialog">Close</button>
      </div>
    </div>
  `;
  dialog.querySelector("#close-dialog").addEventListener("click", () => dialog.close());
  dialog.querySelectorAll("a[href^='#object=']").forEach((linkNode) => {
    linkNode.addEventListener("click", async (event) => {
      const params = new URLSearchParams(linkNode.hash.slice(1));
      const key = params.get("object");
      if (key && objectIndex.has(key)) {
        event.preventDefault();
        await openRecord(key, true);
      }
    });
  });
}

async function openRecord(key, updateHash = true) {
  const indexed = objectIndex.get(key);
  if (!indexed) {
    return;
  }
  if (indexed.system_key !== state.systemKey) {
    await selectSystem(indexed.system_key, false);
  }
  const record = recordByKey.get(key);
  if (!record) {
    return;
  }
  renderDialog(record);
  if (!dialog.open) {
    dialog.showModal();
  }
  if (updateHash) {
    history.replaceState(null, "", `#object=${encodeURIComponent(key)}`);
  }
}

async function syncFromHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const objectKey = params.get("object");
  const systemKey = params.get("system");
  if (objectKey && objectIndex.has(objectKey)) {
    await openRecord(objectKey, false);
    return;
  }
  if (systemKey && systemByKey.has(systemKey)) {
    await selectSystem(systemKey, false);
    return;
  }
  if (!state.systemKey && systems.length) {
    await selectSystem(systems[0].key, false);
  }
}

function showLoadError(error) {
  activeSystemTitle.textContent = "Database Query";
  summaryLine.textContent = "Unable to load explorer data.";
  emptyState.hidden = false;
  emptyState.textContent = `${error.message}. Open this page through GitHub Pages or run: python3 -m http.server 8765 --directory docs, then browse to http://127.0.0.1:8765/explorer.html.`;
}

async function init() {
  try {
    indexData = await loadData("data/hwdb-index.json");
    systems = indexData.systems;
    systemByKey = new Map(systems.map((system) => [system.key, system]));
    objectIndex = new Map(Object.entries(indexData.object_index));
    renderSystems();
    renderKindFilter();
    await syncFromHash();
  } catch (error) {
    showLoadError(error);
  }
}

systemList.addEventListener("click", async (event) => {
  const link = event.target.closest("[data-system]");
  if (!link) {
    return;
  }
  event.preventDefault();
  await selectSystem(link.dataset.system);
});

resultBody.addEventListener("click", async (event) => {
  const objectNode = event.target.closest("a[href^='#object=']");
  if (objectNode) {
    const params = new URLSearchParams(objectNode.hash.slice(1));
    const key = params.get("object");
    if (key && objectIndex.has(key)) {
      event.preventDefault();
      await openRecord(key);
      return;
    }
  }
  const row = event.target.closest("tr[data-key]");
  if (!row) {
    return;
  }
  event.preventDefault();
  await openRecord(row.dataset.key);
});

document.getElementById("kind-filter").addEventListener("click", (event) => {
  const button = event.target.closest("[data-kind]");
  if (!button) {
    return;
  }
  state.kind = button.dataset.kind;
  renderKindFilter();
  renderResults();
});

queryInput.addEventListener("input", () => {
  state.query = queryInput.value;
  renderResults();
});

window.addEventListener("hashchange", () => {
  syncFromHash();
});

init();
"""


def publication_html(
    conn: sqlite3.Connection,
    published_items: list[dict[str, str]],
    detector_plates: list[dict[str, str]],
    generated: str,
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
    nav_items.append('<a href="#contents">Contents</a>')
    if detector_plates:
        nav_items.append('<a href="#detector-plates">Dense Plate</a>')
    nav_items.append('<a href="#reading-guide">Reading Guide</a>')
    nav_items.append('<a href="#systems">Systems</a>')
    nav_items.append('<a href="explorer.html">Interactive Explorer</a>')

    system_cards = []
    toc_entries = []
    if detector_plates:
        toc_entries.append(
            """
            <a class="toc-entry" href="#detector-plates">
              <span class="toc-number">00</span>
              <span class="toc-kicker">Master Plate</span>
              <span class="toc-title">Dense Detector Plate</span>
              <span class="toc-meta">Detector-wide composition and system grouping</span>
            </a>
            """
        )
    toc_entries.append(
        f"""
        <a class="toc-entry" href="#reading-guide">
          <span class="toc-number">{len(toc_entries):02d}</span>
          <span class="toc-kicker">Guide</span>
          <span class="toc-title">Reading Guide</span>
          <span class="toc-meta">Legend, object classes, and edge semantics</span>
        </a>
        """
    )

    for issue_number, system_key in enumerate(sorted(by_system, key=lambda key: system_names[key]), start=len(toc_entries)):
        system_name = system_names[system_key]
        ordered = sorted(by_system[system_key], key=lambda entry: view_order(entry["view"]))
        preview = next((item for item in ordered if item["view"] == "overview"), ordered[0])
        system_number = f"{issue_number:02d}"
        toc_entries.append(
            f"""
            <a class="toc-entry" href="#{html.escape(system_key)}">
              <span class="toc-number">{system_number}</span>
              <span class="toc-kicker">System</span>
              <span class="toc-title">{html.escape(system_name)}</span>
              <span class="toc-meta">{len(ordered)} publication views</span>
            </a>
            """
        )
        stage_links = []
        for item in ordered:
            stage_links.append(
                f"""
                <a class="stage-link" href="{html.escape(item['page'])}">
                  <span>{html.escape(view_stage(item['view']))}</span>
                  {html.escape(item['view_title'])}
                </a>
                """
            )
        system_cards.append(
            f"""
            <article class="system-card" id="{html.escape(system_key)}">
              <div class="system-card-number">{system_number}</div>
              <div class="system-card-copy">
                <p class="eyebrow">System Sheet</p>
                <h3>{html.escape(system_name)}</h3>
                <p>{html.escape(preview['view_description'])}</p>
                <div class="card-actions">
                  <a href="{html.escape(preview['system_page'])}">Open System Page</a>
                  <a href="explorer.html#system={quote(system_key, safe='')}">Query Objects</a>
                </div>
                <div class="view-sequence">
                  {''.join(stage_links)}
                </div>
              </div>
              <div class="diagram-frame preview-frame">
                {diagram_object_html(preview['svg'], f"{system_name} {preview['view_title']} preview", "preview-object", preview.get("inline_svg", ""))}
              </div>
            </article>
            """
        )

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
                      <a href="{html.escape(plate['page'])}">Full Page</a>
                      <a href="{html.escape(plate['svg'])}">SVG</a>
                      <a href="{html.escape(plate['pdf'])}">PDF</a>
                      <a href="{html.escape(plate['dot'])}">DOT</a>
                    </p>
                  </div>
                  <div class="diagram-frame large-frame">
                    {diagram_object_html(plate['svg'], plate['title'], svg_markup=plate.get("inline_svg", ""))}
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
  <script defer src="assets/diagram-dialog.js"></script>
</head>
<body>
  <div class="page-shell">
    <header class="hero">
      <div class="masthead-line">
        <span>Hardware Database</span>
        <span>FD2-VD Issue</span>
        <span>Generated {html.escape(generated)}</span>
      </div>
      <div class="hero-grid">
        <div>
          <p class="eyebrow">Curated Hardware Model</p>
          <h1>FD2-VD Diagram Portfolio</h1>
          <p class="hero-copy">A publication-style atlas of detector systems, generated from the TOML hardware model and organized as dense plates, reading guides, and progressive system views.</p>
        </div>
        <div class="stat-strip" aria-label="Database summary">
          <div><span>{stats['systems']}</span><small>Systems</small></div>
          <div><span>{stats['subsystems']}</span><small>Subsystems</small></div>
          <div><span>{stats['component_types']}</span><small>Component Types</small></div>
          <div><span>{stats['relations']}</span><small>Relations</small></div>
        </div>
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
      <section class="toc-section" id="contents">
        <div class="section-head">
          <p class="eyebrow">Table of Contents</p>
          <h2>Issue Map</h2>
          <p class="section-copy">Start with the master plate, then use the numbered system sheets to move from overview diagrams into hierarchy, dependency, and cabling views.</p>
        </div>
        <div class="toc-grid">
          {''.join(toc_entries)}
        </div>
      </section>
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
      <section class="system-section" id="systems">
        <div class="section-head">
          <p class="eyebrow">System Pages</p>
          <h2>Progressive Diagrams</h2>
          <p class="section-copy">Each system page starts with the simpler overview and steps into hierarchy, dependency, and cabling views. Use the full-page links when the trees need more room.</p>
        </div>
        <div class="system-card-grid">
          {''.join(system_cards)}
        </div>
      </section>
    </main>
  </div>
</body>
</html>
"""


def system_page_html(system_key: str, system_name: str, items: list[dict[str, str]], generated: str) -> str:
    ordered = sorted(items, key=lambda entry: view_order(entry["view"]))
    nav_links = []
    cards = []
    for item in ordered:
        nav_links.append(f'<a href="#{html.escape(item["view"])}">{html.escape(item["view_title"])}</a>')
        cards.append(
            f"""
            <article class="diagram-card full-diagram-card" id="{html.escape(item["view"])}">
              <div class="diagram-head">
                <p class="eyebrow">{html.escape(view_stage(item["view"]))}</p>
                <h3>{html.escape(item["view_title"])}</h3>
                <p class="diagram-copy">{html.escape(item["view_description"])}</p>
                <p class="downloads">
                  <a href="../{html.escape(item['page'])}">Full Page</a>
                  <a href="../{html.escape(item['svg'])}">SVG</a>
                  <a href="../{html.escape(item['pdf'])}">PDF</a>
                  <a href="../{html.escape(item['dot'])}">DOT</a>
                </p>
              </div>
              <div class="diagram-frame system-frame">
                {diagram_object_html("../" + item["svg"], f"{system_name} {item['view_title']} diagram", "system-object", item.get("inline_svg", ""))}
              </div>
            </article>
            """
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(system_name)} Diagrams</title>
  <link rel="stylesheet" href="../assets/report.css">
  <script defer src="../assets/diagram-dialog.js"></script>
</head>
<body>
  <div class="page-shell system-page">
    <header class="hero compact-hero">
      <p class="eyebrow">Hardware Database System</p>
      <h1>{html.escape(system_name)}</h1>
      <p class="hero-copy">Progressive views from overview through detailed dependency and cabling trees.</p>
    </header>
    <aside class="side-nav">
      <div class="side-nav-inner">
        <p class="eyebrow">Navigate</p>
        <a href="../index.html#systems">Portfolio</a>
        <a href="../explorer.html#system={quote(system_key, safe='')}">Database Query</a>
        {''.join(nav_links)}
        <p class="build-stamp">Generated {html.escape(generated)}</p>
      </div>
    </aside>
    <main class="content">
      {''.join(cards)}
    </main>
  </div>
</body>
</html>
"""


def diagram_page_html(item: dict[str, str], generated: str, is_plate: bool = False, svg_markup: str = "") -> str:
    title = item["title"] if is_plate else f"{item['system_name']} · {item['view_title']}"
    copy = item["copy"] if is_plate else item["view_description"]
    back_href = "../index.html#detector-plates" if is_plate else f"../systems/{item['system_key']}.html#{item['view']}"
    explorer_href = "../explorer.html" if is_plate else f"../explorer.html#system={quote(item['system_key'], safe='')}"
    stage = "Master Diagram" if is_plate else view_stage(item["view"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="../assets/report.css">
  <script defer src="../assets/viewer.js"></script>
  <script defer src="../assets/diagram-dialog.js"></script>
</head>
<body class="viewer-body">
  <main class="viewer-shell">
    <header class="viewer-header">
      <div>
        <p class="eyebrow">{html.escape(stage)}</p>
        <h1>{html.escape(title)}</h1>
        <p>{html.escape(copy)}</p>
      </div>
      <nav class="viewer-actions">
        <button type="button" data-view-mode="fit">Fit</button>
        <button type="button" data-view-mode="width">Width</button>
        <button type="button" data-view-mode="100" class="active">100%</button>
        <button type="button" data-view-mode="150">150%</button>
        <button type="button" data-view-mode="200">200%</button>
        <a href="{html.escape(back_href)}">Back</a>
        <a href="{html.escape(explorer_href)}">Database Query</a>
        <a href="../{html.escape(item['svg'])}">SVG</a>
        <a href="../{html.escape(item['pdf'])}">PDF</a>
        <a href="../{html.escape(item['dot'])}">DOT</a>
      </nav>
    </header>
    <section class="diagram-frame viewer-frame">
      <div class="viewer-object viewer-svg-wrap">
        {svg_markup}
      </div>
    </section>
    <p class="build-stamp viewer-stamp">Generated {html.escape(generated)}</p>
  </main>
</body>
</html>
"""


def view_stage(view: str) -> str:
    stages = {
        "overview": "1. Overview",
        "hierarchy": "2. Assembly Tree",
        "dependency": "3. Dependency Tree",
        "cabling": "4. Cabling Tree",
        "all": "5. Full Model",
    }
    return stages.get(view, view_title(view))


def inline_viewer_svg(svg_text: str) -> str:
    svg_text = re.sub(r"<\?xml[^>]*>\s*", "", svg_text, count=1)
    svg_text = re.sub(r"<!DOCTYPE[\s\S]*?>\s*", "", svg_text, count=1)
    return normalize_inline_object_links(svg_text).replace("<svg ", '<svg class="viewer-svg" ', 1)


def inline_publication_svg(svg_text: str) -> str:
    svg_text = re.sub(r"<\?xml[^>]*>\s*", "", svg_text, count=1)
    svg_text = re.sub(r"<!DOCTYPE[\s\S]*?>\s*", "", svg_text, count=1)
    return normalize_inline_object_links(svg_text).replace("<svg ", '<svg class="inline-diagram-svg" ', 1)


def normalize_inline_object_links(svg_text: str) -> str:
    return svg_text.replace('xlink:href="../explorer.html#object=', 'xlink:href="#object=')


def diagram_object_html(svg_path: str, label: str, extra_class: str = "", svg_markup: str = "") -> str:
    class_name = "diagram-object" if not extra_class else f"diagram-object {extra_class}"
    if svg_markup:
        return (
            f'<div class="{html.escape(class_name)} inline-diagram" role="img" '
            f'aria-label="{html.escape(label)}">\n{svg_markup}\n</div>'
        )
    return (
        f'<iframe class="{html.escape(class_name)}" src="{html.escape(svg_path)}" '
        f'title="{html.escape(label)}" loading="lazy"></iframe>'
    )


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
.diagram-card,
.system-card {
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
  font-weight: 650;
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

.section-copy {
  margin: 0.55rem 0 0;
  color: var(--ink-soft);
  line-height: 1.5;
  max-width: 66rem;
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
  font-weight: 600;
}

.system-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 1.2rem;
}

.system-card {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  overflow: hidden;
}

.system-card-copy {
  padding: 1.25rem 1.35rem 1rem;
  border-bottom: 1px solid rgba(203, 189, 168, 0.6);
}

.system-card h3 {
  margin: 0;
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 1.45rem;
  font-weight: 600;
}

.system-card-copy p:not(.eyebrow) {
  color: var(--ink-soft);
  line-height: 1.45;
}

.card-actions,
.view-sequence,
.viewer-actions {
  display: flex;
  gap: 0.65rem;
  flex-wrap: wrap;
}

.card-actions {
  margin: 0.85rem 0;
}

.card-actions a,
.viewer-actions a,
.viewer-actions button,
.stage-link {
  color: var(--navy);
  text-decoration: none;
  font-weight: 600;
}

.card-actions a,
.viewer-actions a,
.viewer-actions button {
  padding: 0.45rem 0.7rem;
  border: 1px solid rgba(203, 189, 168, 0.75);
  border-radius: 999px;
  background: rgba(255,255,255,0.55);
}

.viewer-actions button {
  font: inherit;
  cursor: pointer;
}

.viewer-actions button.active {
  color: #f7f3ed;
  border-color: var(--navy);
  background: var(--navy);
}

.view-sequence {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
}

.stage-link {
  padding: 0.55rem 0.65rem;
  border: 1px solid rgba(203, 189, 168, 0.65);
  border-radius: 12px;
  background: rgba(255,255,255,0.45);
}

.stage-link span {
  display: block;
  color: var(--ink-soft);
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.diagram-frame {
  padding: 0.65rem;
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

.diagram-object {
  width: 100%;
  height: clamp(24rem, 46vw, 52rem);
  border-radius: 18px;
  background: #ffffff;
  border: 1px solid rgba(203, 189, 168, 0.7);
  overflow: hidden;
  display: block;
}

.inline-diagram {
  overflow: auto;
}

.inline-diagram-svg {
  display: block;
  width: 100%;
  height: 100%;
}

.inline-diagram a,
.viewer-svg a {
  cursor: pointer;
}

.large-frame .diagram-object {
  height: clamp(32rem, 62vw, 70rem);
}

.preview-frame .diagram-object {
  height: clamp(20rem, 34vw, 34rem);
}

.system-frame .diagram-object {
  height: clamp(34rem, 72vh, 58rem);
}

.compact-hero h1 {
  max-width: none;
  font-size: clamp(2rem, 3.5vw, 3.6rem);
}

.system-page .diagram-card + .diagram-card {
  margin-top: 1.3rem;
}

.viewer-body {
  min-height: 100vh;
}

.viewer-shell {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto minmax(28rem, 1fr) auto;
  gap: 1rem;
  padding: 1rem;
}

.viewer-header {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 1rem;
  align-items: end;
  padding: 1rem 1.2rem;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: rgba(255, 252, 247, 0.84);
  box-shadow: var(--shadow);
}

.viewer-header h1 {
  margin: 0;
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-size: clamp(1.7rem, 2.8vw, 3rem);
  font-weight: 600;
}

.viewer-header p:not(.eyebrow) {
  margin: 0.45rem 0 0;
  color: var(--ink-soft);
}

.viewer-frame {
  border: 1px solid var(--line);
  border-radius: 20px;
  overflow: hidden;
}

.viewer-object {
  height: calc(100vh - 12.5rem);
  min-height: 34rem;
  overflow: auto;
  border-radius: 18px;
  background: #ffffff;
}

.viewer-svg {
  display: block;
  max-width: none;
}

.viewer-stamp {
  margin: 0 0.2rem;
}

.hwdb-object-link {
  color: var(--navy);
  font-weight: 600;
  text-decoration: none;
}

.hwdb-object-dialog {
  width: min(1180px, calc(100vw - 2rem));
  max-height: min(900px, calc(100vh - 2rem));
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 0;
  color: var(--ink);
  background: #fffdf8;
  box-shadow: 0 24px 80px rgba(24, 50, 71, 0.30);
}

.hwdb-object-dialog:focus {
  outline: none;
}

.hwdb-object-dialog::backdrop {
  background: rgba(24, 50, 71, 0.44);
}

.hwdb-dialog-shell {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  max-height: inherit;
}

.hwdb-dialog-head,
.hwdb-dialog-actions {
  padding: 1rem 1.1rem;
  border-bottom: 1px solid var(--line);
  background: rgba(239,230,216,0.54);
}

.hwdb-dialog-head h2 {
  margin: 0.45rem 0 0;
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-size: clamp(1.35rem, 2vw, 2rem);
  font-weight: 600;
  line-height: 1.15;
}

.hwdb-dialog-kicker {
  margin: 0;
  color: var(--ink-soft);
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
}

.hwdb-dialog-body {
  overflow: auto;
  padding: 1rem;
  display: grid;
  gap: 1rem;
}

.hwdb-dialog-actions {
  border-top: 1px solid var(--line);
  border-bottom: 0;
  display: flex;
  justify-content: flex-end;
}

.hwdb-dialog-actions button {
  min-height: 2.3rem;
  padding: 0 0.8rem;
  border: 1px solid rgba(203, 189, 168, 0.8);
  border-radius: 999px;
  background: rgba(255,255,255,0.72);
  color: var(--navy);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}

.hwdb-dialog-badge {
  display: inline-flex;
  align-items: center;
  min-height: 1.45rem;
  padding: 0 0.55rem;
  border-radius: 999px;
  color: #ffffff;
  font-size: 0.75rem;
  font-weight: 600;
  white-space: nowrap;
}

.hwdb-dialog-badge.system { background: var(--navy); }
.hwdb-dialog-badge.subsystem { background: #4f7c7a; }
.hwdb-dialog-badge.component { background: var(--green); }
.hwdb-dialog-badge.relation { background: var(--amber); }
.hwdb-dialog-badge.batch { background: var(--violet); }
.hwdb-dialog-badge.sensor { background: var(--rose); }

.hwdb-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.7rem;
}

.hwdb-detail-item {
  border: 1px solid rgba(203, 189, 168, 0.75);
  border-radius: 12px;
  padding: 0.7rem;
  background: rgba(255,255,255,0.58);
}

.hwdb-detail-item span {
  display: block;
  margin-bottom: 0.24rem;
  color: var(--ink-soft);
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.hwdb-neighborhood-section {
  border: 1px solid rgba(24,50,71,0.30);
  background: rgba(255,255,255,0.34);
}

.hwdb-neighborhood-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.75rem 0.8rem;
  border-bottom: 1px solid rgba(24,50,71,0.22);
}

.hwdb-neighborhood-head h3 {
  margin: 0;
}

.hwdb-graph-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
}

.hwdb-graph-toolbar button {
  min-height: 2rem;
  padding: 0 0.65rem;
  border: 1px solid rgba(24,50,71,0.34);
  border-radius: 0;
  background: rgba(255,255,255,0.42);
  color: var(--ink);
  font: inherit;
  font-size: 0.82rem;
  font-weight: 600;
  cursor: pointer;
}

.hwdb-graph-toolbar button.active {
  color: #fffdf8;
  border-color: var(--ink);
  background: var(--ink);
}

.hwdb-neighborhood-frame {
  min-height: 22rem;
  max-height: 48rem;
  overflow: auto;
  padding: 0.75rem;
  background:
    linear-gradient(90deg, rgba(24,50,71,0.06) 1px, transparent 1px),
    linear-gradient(0deg, rgba(24,50,71,0.06) 1px, transparent 1px),
    rgba(255,253,248,0.76);
  background-size: 24px 24px, 24px 24px, auto;
}

.hwdb-neighborhood-svg {
  display: block;
  max-width: none;
}

.hwdb-neighborhood-summary,
.hwdb-neighborhood-note,
.hwdb-neighborhood-empty {
  margin: 0 0 0.65rem;
  color: var(--ink-soft);
  font-size: 0.84rem;
}

.hwdb-graph-edge {
  fill: none;
  stroke: #33536a;
  stroke-width: 1.8;
}

.hwdb-graph-edge.hierarchy {
  stroke: #274c67;
}

.hwdb-graph-edge.dependency {
  stroke: #a56b17;
  stroke-dasharray: 7 5;
}

.hwdb-graph-arrow {
  fill: #33536a;
}

.hwdb-graph-edge-label {
  fill: var(--ink-soft);
  stroke: #fffdf8;
  stroke-width: 5px;
  paint-order: stroke;
  font-family: "Avenir Next", "Helvetica Neue", sans-serif;
  font-size: 11px;
  font-weight: 600;
  pointer-events: none;
}

.hwdb-graph-node rect {
  fill: #fffdf8;
  stroke: rgba(24,50,71,0.34);
  stroke-width: 1.2;
}

.hwdb-graph-node.system rect { fill: #dfe9f2; }
.hwdb-graph-node.subsystem rect { fill: #e6f1ef; }
.hwdb-graph-node.component rect { fill: #e4efe0; }
.hwdb-graph-node.batch rect { fill: #f0e6fa; }
.hwdb-graph-node.sensor rect { fill: #f7dde0; }
.hwdb-graph-node.relation rect { fill: #fff0cc; }

.hwdb-graph-node.focus rect {
  fill: #fff7f3;
  stroke: var(--rose);
  stroke-width: 2.6;
}

.hwdb-graph-node-label,
.hwdb-graph-node-kind {
  fill: var(--ink);
  font-family: "Avenir Next", "Helvetica Neue", sans-serif;
  pointer-events: none;
}

.hwdb-graph-node-label {
  font-size: 12px;
  font-weight: 700;
}

.hwdb-graph-node-kind {
  fill: var(--ink-soft);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.hwdb-detail-item code,
.hwdb-dialog-body code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.84rem;
  overflow-wrap: anywhere;
}

.hwdb-dialog-section h3 {
  margin: 0 0 0.45rem;
  font-size: 0.96rem;
  font-weight: 600;
}

.hwdb-dialog-section p {
  margin: 0;
  color: var(--ink-soft);
  line-height: 1.5;
}

.hwdb-mini-table {
  width: 100%;
  border-collapse: collapse;
}

.hwdb-mini-table th,
.hwdb-mini-table td {
  padding: 0.55rem;
  border-bottom: 1px solid rgba(203, 189, 168, 0.7);
  text-align: left;
  vertical-align: top;
  font-size: 0.86rem;
}

.hwdb-mini-table th {
  background: rgba(239,230,216,0.66);
  color: var(--ink-soft);
  font-size: 0.74rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

/* Editorial issue treatment inspired by Parametric Press. */
body {
  background:
    linear-gradient(90deg, rgba(24,50,71,0.045) 1px, transparent 1px),
    linear-gradient(180deg, #fbf7ef 0%, #efe7d9 56%, #f7f3ec 100%);
  background-size: 32px 32px, auto;
}

a {
  text-underline-offset: 0.16em;
}

.page-shell {
  grid-template-columns: 15rem minmax(0, 1fr);
  gap: clamp(1.2rem, 2.4vw, 2.6rem);
  max-width: 1620px;
  padding: clamp(1rem, 2.8vw, 3rem);
}

.hero {
  border: 0;
  border-top: 4px solid var(--ink);
  border-bottom: 1px solid rgba(24,50,71,0.72);
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  color: var(--ink);
  padding: 1rem 0 1.45rem;
}

.masthead-line {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1rem;
  padding-bottom: 0.8rem;
  border-bottom: 1px solid rgba(24,50,71,0.44);
  color: var(--ink-soft);
  font-size: 0.74rem;
  font-weight: 700;
  letter-spacing: 0.13em;
  text-transform: uppercase;
}

.masthead-line span:nth-child(2) {
  text-align: center;
}

.masthead-line span:nth-child(3) {
  text-align: right;
}

.hero-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(18rem, 0.36fr);
  gap: clamp(1.5rem, 4vw, 4.5rem);
  align-items: end;
  padding-top: 1.45rem;
}

.hero h1 {
  max-width: 12ch;
  font-size: clamp(3rem, 8vw, 8.4rem);
  line-height: 0.87;
  letter-spacing: 0;
}

.hero-copy {
  max-width: 54rem;
  color: var(--ink-soft);
  font-size: clamp(1rem, 1.25vw, 1.24rem);
}

.stat-strip {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0;
  margin-top: 0;
  border-top: 1px solid rgba(24,50,71,0.46);
  border-left: 1px solid rgba(24,50,71,0.46);
}

.stat-strip div {
  min-height: 6rem;
  padding: 0.85rem;
  border: 0;
  border-right: 1px solid rgba(24,50,71,0.46);
  border-bottom: 1px solid rgba(24,50,71,0.46);
  border-radius: 0;
  background: rgba(255,255,255,0.26);
}

.stat-strip span {
  color: var(--rose);
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-size: clamp(2rem, 4vw, 3.6rem);
  line-height: 0.9;
}

.stat-strip small {
  color: var(--ink-soft);
  font-size: 0.68rem;
  font-weight: 700;
}

.side-nav-inner,
.legend-card,
.guide-card,
.diagram-card,
.system-card,
.viewer-header,
.viewer-frame {
  border-radius: 0;
  box-shadow: none;
  backdrop-filter: none;
}

.side-nav-inner {
  border: 0;
  border-left: 2px solid var(--ink);
  background: transparent;
  padding: 0.2rem 0 0.2rem 1rem;
}

.side-nav a {
  padding: 0.65rem 0;
  border-top: 1px solid rgba(24,50,71,0.22);
  font-size: 0.9rem;
}

.toc-section {
  margin-bottom: 1.8rem;
}

.toc-grid {
  border-top: 1px solid var(--ink);
  border-bottom: 1px solid var(--ink);
}

.toc-entry {
  display: grid;
  grid-template-columns: 4rem minmax(7rem, 0.25fr) minmax(13rem, 0.45fr) minmax(12rem, 1fr);
  gap: 1rem;
  align-items: baseline;
  padding: 0.9rem 0;
  border-top: 1px solid rgba(24,50,71,0.22);
  color: var(--ink);
  text-decoration: none;
}

.toc-entry:first-child {
  border-top: 0;
}

.toc-number,
.system-card-number {
  color: var(--rose);
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 2rem;
  line-height: 1;
}

.toc-entry:nth-child(3n+2) .toc-number,
.system-card:nth-child(3n+2) .system-card-number {
  color: var(--green);
}

.toc-entry:nth-child(3n+3) .toc-number,
.system-card:nth-child(3n+3) .system-card-number {
  color: var(--amber);
}

.toc-kicker {
  color: var(--ink-soft);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.toc-title {
  font-family: "Iowan Old Style", "Palatino Linotype", serif;
  font-size: clamp(1.18rem, 1.6vw, 1.65rem);
  line-height: 1.08;
}

.toc-meta {
  color: var(--ink-soft);
  line-height: 1.4;
}

.legend-card,
.guide-card {
  border: 0;
  border-top: 1px solid rgba(24,50,71,0.7);
  border-bottom: 1px solid rgba(24,50,71,0.28);
  background: rgba(255,255,255,0.22);
  padding: 1.25rem 0;
}

.guide-item {
  border-radius: 0;
  background: rgba(255,255,255,0.22);
}

.section-head {
  margin-bottom: 1.1rem;
  padding-top: 0.2rem;
}

.section-head h2 {
  font-size: clamp(2rem, 4.5vw, 4.8rem);
  line-height: 0.92;
  letter-spacing: 0;
}

.diagram-card {
  border-color: rgba(24,50,71,0.38);
  background: rgba(255,255,255,0.42);
}

.diagram-head,
.system-card-copy {
  border-bottom-color: rgba(24,50,71,0.22);
}

.downloads a,
.card-actions a,
.viewer-actions a,
.viewer-actions button,
.stage-link {
  border-radius: 0;
}

.card-actions a,
.viewer-actions a,
.viewer-actions button {
  background: transparent;
  border-color: rgba(24,50,71,0.36);
}

.system-card-grid {
  grid-template-columns: 1fr;
}

.system-card {
  display: grid;
  grid-template-columns: 4rem minmax(18rem, 0.42fr) minmax(0, 1fr);
  grid-template-rows: none;
  border-color: rgba(24,50,71,0.38);
  background: rgba(255,255,255,0.34);
}

.system-card-number {
  padding: 1.25rem 0 0 1rem;
  font-size: 2.25rem;
}

.system-card-copy {
  border-right: 1px solid rgba(24,50,71,0.22);
  border-bottom: 0;
}

.system-card h3 {
  font-size: clamp(1.45rem, 2.2vw, 2.2rem);
  line-height: 1.05;
}

.view-sequence {
  grid-template-columns: 1fr;
}

.stage-link {
  background: rgba(255,255,255,0.18);
}

.diagram-frame {
  background:
    linear-gradient(180deg, rgba(255,255,255,0.56), rgba(255,255,255,0.26)),
    linear-gradient(90deg, rgba(24,50,71,0.08) 1px, transparent 1px),
    linear-gradient(0deg, rgba(24,50,71,0.08) 1px, transparent 1px);
  background-size: auto, 24px 24px, 24px 24px;
}

.diagram-object,
.viewer-object {
  border-radius: 0;
}

.viewer-header {
  border: 0;
  border-top: 3px solid var(--ink);
  border-bottom: 1px solid rgba(24,50,71,0.48);
  background: transparent;
}

@media (max-width: 980px) {
  .page-shell {
    grid-template-columns: 1fr;
  }

  .masthead-line,
  .hero-grid,
  .toc-entry,
  .system-card {
    grid-template-columns: 1fr;
  }

  .masthead-line span:nth-child(2),
  .masthead-line span:nth-child(3) {
    text-align: left;
  }

  .side-nav {
    position: static;
  }

  .stat-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .system-card-grid,
  .viewer-header {
    grid-template-columns: 1fr;
  }

  .system-card-number {
    padding: 1rem 1.35rem 0;
  }

  .system-card-copy {
    border-right: 0;
    border-bottom: 1px solid rgba(24,50,71,0.22);
  }

  .viewer-actions {
    justify-content: flex-start;
  }

  .viewer-object {
    height: 72vh;
    min-height: 28rem;
  }

  .hwdb-neighborhood-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .hwdb-neighborhood-frame {
    max-height: 70vh;
  }

  .hwdb-detail-grid {
    grid-template-columns: 1fr;
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
    output_path.write_text(responsive_svg(render_dot(dot_text, "svg")), encoding="utf-8")
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
                *dot_cluster_click_lines(system_row["key"], system_row["name"]),
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
                    *dot_cluster_click_lines(subsystem_key, all_nodes[subsystem_key]["label"]),
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
