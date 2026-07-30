#!/usr/bin/env python3
"""Build and maintain the legacy HWDB-compatible DAPHNE handoff database.

The database deliberately uses local identifiers until the DUNE HWDB assigns
component type IDs and Part IDs.  It never writes to the live HWDB.

New production enrollment must use daphne_production_cli.py.  This module
remains available for reviewable HWDB-shaped exports and migration of the
original one-step staging data.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import re
import sqlite3
import sys
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1"
DEFAULT_DB = Path("build/daphne-staging.db")
DEFAULT_SPEC = Path("specs/staging/daphne_hwdb.toml")
DEFAULT_SEED = Path("specs/staging/daphne_observed_seed.toml")

PART_TYPE_RE = re.compile(r"^[A-Z][0-9]{11}$")
PART_ID_RE = re.compile(r"^[A-Z][0-9]{11}-[0-9]{5}(?:-[A-Z]{2}[0-9]{3})?$")
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.?$"
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_type_proposals (
    type_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    comments TEXT,
    project_id TEXT NOT NULL,
    system_id TEXT NOT NULL,
    system_name TEXT NOT NULL,
    subsystem_id TEXT NOT NULL,
    subsystem_name TEXT NOT NULL,
    hwdb_part_type_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS component_type_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type_key TEXT NOT NULL REFERENCES component_type_proposals(type_key) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    hwdb_name TEXT NOT NULL,
    data_type TEXT NOT NULL,
    required INTEGER NOT NULL CHECK(required IN (0, 1)),
    description TEXT,
    UNIQUE(type_key, field_key),
    UNIQUE(type_key, hwdb_name)
);

CREATE TABLE IF NOT EXISTS component_type_connectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type_key TEXT NOT NULL REFERENCES component_type_proposals(type_key) ON DELETE CASCADE,
    functional_position TEXT NOT NULL,
    target_type_key TEXT NOT NULL REFERENCES component_type_proposals(type_key),
    description TEXT,
    UNIQUE(parent_type_key, functional_position)
);

CREATE TABLE IF NOT EXISTS test_type_proposals (
    test_key TEXT PRIMARY KEY,
    component_type_key TEXT NOT NULL REFERENCES component_type_proposals(type_key) ON DELETE CASCADE,
    name TEXT NOT NULL,
    comments TEXT,
    hwdb_test_type_id INTEGER UNIQUE
);

CREATE TABLE IF NOT EXISTS test_type_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_key TEXT NOT NULL REFERENCES test_type_proposals(test_key) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    hwdb_name TEXT NOT NULL,
    data_type TEXT NOT NULL,
    required INTEGER NOT NULL CHECK(required IN (0, 1)),
    description TEXT,
    UNIQUE(test_key, field_key),
    UNIQUE(test_key, hwdb_name)
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    hwdb_part_id TEXT UNIQUE,
    carrier_serial_number TEXT,
    carrier_hardware_revision TEXT NOT NULL,
    schematic_reference TEXT,
    country_code TEXT NOT NULL,
    institution_id INTEGER,
    location TEXT,
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'observed', 'enrolled', 'qualified', 'quarantined', 'retired'
    )),
    comments TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS soms (
    som_uuid TEXT PRIMARY KEY,
    hwdb_part_id TEXT UNIQUE,
    som_serial_number TEXT NOT NULL UNIQUE,
    som_product_name TEXT NOT NULL,
    som_hardware_revision TEXT,
    factory_mac_id_0 TEXT NOT NULL UNIQUE,
    fru_checksum_valid INTEGER NOT NULL CHECK(fru_checksum_valid IN (0, 1)),
    eeprom_sha256 TEXT,
    comments TEXT,
    discovered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS installations (
    installation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    som_uuid TEXT NOT NULL REFERENCES soms(som_uuid),
    installed_at TEXT NOT NULL,
    removed_at TEXT,
    station_id TEXT NOT NULL,
    comments TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_som_per_asset
ON installations(asset_id) WHERE removed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS one_active_asset_per_som
ON installations(som_uuid) WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS deployments (
    asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
    mac_source TEXT NOT NULL CHECK(mac_source IN (
        'som_eeprom', 'daphne_pool', 'legacy_override'
    )),
    production_mac TEXT NOT NULL UNIQUE,
    ipv4_address TEXT NOT NULL UNIQUE,
    hostname TEXT NOT NULL UNIQUE,
    vlan INTEGER,
    timing_endpoint TEXT NOT NULL UNIQUE,
    firmware_release TEXT NOT NULL,
    uboot_ethaddr TEXT NOT NULL,
    fdt_mac TEXT NOT NULL,
    linux_active_mac TEXT NOT NULL,
    network_admission_approved INTEGER NOT NULL CHECK(network_admission_approved IN (0, 1)),
    boot_chain_passed INTEGER NOT NULL CHECK(boot_chain_passed IN (0, 1)),
    station_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    comments TEXT
);

CREATE TABLE IF NOT EXISTS qa_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT REFERENCES assets(asset_id),
    som_uuid TEXT REFERENCES soms(som_uuid),
    event_type TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
    station_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_uri TEXT,
    data_json TEXT NOT NULL,
    comments TEXT
);
"""


class StagingError(RuntimeError):
    """A user-facing staging database error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(compact) != 12 or not re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        raise StagingError(f"invalid MAC address: {value!r}")
    octets = bytes.fromhex(compact)
    if octets == b"\x00" * 6 or octets == b"\xff" * 6:
        raise StagingError(f"invalid all-zero/all-ff MAC address: {value!r}")
    if octets[0] & 0x01:
        raise StagingError(f"multicast MAC address is not valid for an interface: {value!r}")
    return ":".join(f"{part:02x}" for part in octets)


def normalize_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise StagingError(f"invalid UUID: {value!r}") from exc


def normalize_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise StagingError(f"invalid IP address: {value!r}") from exc
    if address.version != 4:
        raise StagingError(f"expected an IPv4 address, got: {value!r}")
    return str(address)


def normalize_hostname(value: str) -> str:
    hostname = value.strip().rstrip(".")
    if not HOSTNAME_RE.fullmatch(hostname):
        raise StagingError(f"invalid hostname: {value!r}")
    return hostname


def normalize_timestamp(value: str) -> str:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StagingError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise StagingError(f"timestamp must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StagingError(f"file does not exist: {path}")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def initialize_database(db_path: Path, spec_path: Path, seed_path: Path | None) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        load_proposals(conn, read_toml(spec_path))
        if seed_path is not None:
            load_seed(conn, read_toml(seed_path))
        conn.commit()
    finally:
        conn.close()
    print(f"database={db_path}")


def load_proposals(conn: sqlite3.Connection, spec: dict[str, Any]) -> None:
    hwdb = spec["hwdb"]
    conn.execute("DELETE FROM component_type_connectors")
    conn.execute("DELETE FROM test_type_fields")
    conn.execute("DELETE FROM test_type_proposals")
    conn.execute("DELETE FROM component_type_fields")
    conn.execute("DELETE FROM component_type_proposals")

    component_types = spec.get("component_types", [])
    for component in component_types:
        conn.execute(
            """
            INSERT INTO component_type_proposals(
                type_key, name, category, comments, project_id, system_id,
                system_name, subsystem_id, subsystem_name, hwdb_part_type_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                component["key"],
                component["name"],
                component["category"],
                optional_text(component.get("comments")),
                hwdb["project_id"],
                hwdb["system_id"],
                hwdb["system_name"],
                hwdb["subsystem_id"],
                hwdb["subsystem_name"],
                optional_text(component.get("hwdb_part_type_id")),
            ),
        )
        for field in component.get("fields", []):
            conn.execute(
                """
                INSERT INTO component_type_fields(
                    type_key, field_key, hwdb_name, data_type, required, description
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    component["key"],
                    field["key"],
                    field["hwdb_name"],
                    field["data_type"],
                    int(field.get("required", False)),
                    optional_text(field.get("description")),
                ),
            )

    for component in component_types:
        for connector in component.get("connectors", []):
            conn.execute(
                """
                INSERT INTO component_type_connectors(
                    parent_type_key, functional_position, target_type_key, description
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    component["key"],
                    connector["functional_position"],
                    connector["target_type_key"],
                    optional_text(connector.get("description")),
                ),
            )

    for test in spec.get("test_types", []):
        conn.execute(
            """
            INSERT INTO test_type_proposals(
                test_key, component_type_key, name, comments, hwdb_test_type_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                test["key"],
                test["component_type_key"],
                test["name"],
                optional_text(test.get("comments")),
                test.get("hwdb_test_type_id"),
            ),
        )
        for field in test.get("fields", []):
            conn.execute(
                """
                INSERT INTO test_type_fields(
                    test_key, field_key, hwdb_name, data_type, required, description
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    test["key"],
                    field["key"],
                    field["hwdb_name"],
                    field["data_type"],
                    int(field.get("required", False)),
                    optional_text(field.get("description")),
                ),
            )


def load_seed(conn: sqlite3.Connection, seed: dict[str, Any]) -> None:
    now = utc_now()
    for asset in seed.get("assets", []):
        conn.execute(
            """
            INSERT OR IGNORE INTO assets(
                asset_id, carrier_serial_number, carrier_hardware_revision,
                schematic_reference, country_code, institution_id, location,
                state, comments, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset["asset_id"],
                optional_text(asset.get("carrier_serial_number")),
                asset["carrier_hardware_revision"],
                optional_text(asset.get("schematic_reference")),
                asset.get("country_code", "US"),
                asset.get("institution_id") or None,
                optional_text(asset.get("location")),
                asset.get("state", "pending"),
                optional_text(asset.get("comments")),
                now,
                now,
            ),
        )

    for som in seed.get("soms", []):
        conn.execute(
            """
            INSERT OR IGNORE INTO soms(
                som_uuid, som_serial_number, som_product_name, som_hardware_revision,
                factory_mac_id_0, fru_checksum_valid, eeprom_sha256, comments,
                discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalize_uuid(som["som_uuid"]),
                som["som_serial_number"].strip(),
                som["som_product_name"].strip(),
                optional_text(som.get("som_hardware_revision")),
                normalize_mac(som["factory_mac_id_0"]),
                int(som.get("fru_checksum_valid", False)),
                optional_text(som.get("eeprom_sha256")),
                optional_text(som.get("comments")),
                now,
            ),
        )

    for installation in seed.get("installations", []):
        exists = conn.execute(
            """
            SELECT 1 FROM installations
            WHERE asset_id = ? AND som_uuid = ? AND removed_at IS NULL
            """,
            (installation["asset_id"], normalize_uuid(installation["som_uuid"])),
        ).fetchone()
        if not exists:
            conn.execute(
                """
                INSERT INTO installations(
                    asset_id, som_uuid, installed_at, station_id, comments
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    installation["asset_id"],
                    normalize_uuid(installation["som_uuid"]),
                    normalize_timestamp(installation["installed_at"]),
                    installation["station_id"],
                    optional_text(installation.get("comments")),
                ),
            )

    for deployment in seed.get("deployments", []):
        insert_deployment(conn, deployment, replace=False)

    for event in seed.get("qa_events", []):
        duplicate = conn.execute(
            """
            SELECT 1 FROM qa_events
            WHERE asset_id IS ? AND som_uuid IS ? AND event_type = ? AND observed_at = ?
            """,
            (
                optional_text(event.get("asset_id")),
                normalize_uuid(event["som_uuid"]) if event.get("som_uuid") else None,
                event["event_type"],
                normalize_timestamp(event["observed_at"]),
            ),
        ).fetchone()
        if not duplicate:
            conn.execute(
                """
                INSERT INTO qa_events(
                    asset_id, som_uuid, event_type, passed, station_id, operator,
                    observed_at, evidence_uri, data_json, comments
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    optional_text(event.get("asset_id")),
                    normalize_uuid(event["som_uuid"]) if event.get("som_uuid") else None,
                    event["event_type"],
                    int(event["passed"]),
                    event["station_id"],
                    event["operator"],
                    normalize_timestamp(event["observed_at"]),
                    optional_text(event.get("evidence_uri")),
                    canonical_json(json.loads(event.get("data_json", "{}"))),
                    optional_text(event.get("comments")),
                ),
            )


def insert_deployment(conn: sqlite3.Connection, deployment: dict[str, Any], replace: bool) -> None:
    values = (
        deployment["asset_id"],
        deployment["mac_source"],
        normalize_mac(deployment["production_mac"]),
        normalize_ipv4(deployment["ipv4_address"]),
        normalize_hostname(deployment["hostname"]),
        deployment.get("vlan"),
        deployment["timing_endpoint"].strip(),
        deployment["firmware_release"].strip(),
        normalize_mac(deployment["uboot_ethaddr"]),
        normalize_mac(deployment["fdt_mac"]),
        normalize_mac(deployment["linux_active_mac"]),
        int(deployment.get("network_admission_approved", False)),
        int(deployment.get("boot_chain_passed", False)),
        deployment["station_id"].strip(),
        normalize_timestamp(deployment["observed_at"]),
        optional_text(deployment.get("comments")),
    )
    if replace:
        conn.execute(
            """
            INSERT INTO deployments(
                asset_id, mac_source, production_mac, ipv4_address, hostname, vlan,
                timing_endpoint, firmware_release, uboot_ethaddr, fdt_mac,
                linux_active_mac, network_admission_approved, boot_chain_passed,
                station_id, observed_at, comments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                mac_source=excluded.mac_source,
                production_mac=excluded.production_mac,
                ipv4_address=excluded.ipv4_address,
                hostname=excluded.hostname,
                vlan=excluded.vlan,
                timing_endpoint=excluded.timing_endpoint,
                firmware_release=excluded.firmware_release,
                uboot_ethaddr=excluded.uboot_ethaddr,
                fdt_mac=excluded.fdt_mac,
                linux_active_mac=excluded.linux_active_mac,
                network_admission_approved=excluded.network_admission_approved,
                boot_chain_passed=excluded.boot_chain_passed,
                station_id=excluded.station_id,
                observed_at=excluded.observed_at,
                comments=excluded.comments
            """,
            values,
        )
    else:
        conn.execute(
            """
            INSERT OR IGNORE INTO deployments(
                asset_id, mac_source, production_mac, ipv4_address, hostname, vlan,
                timing_endpoint, firmware_release, uboot_ethaddr, fdt_mac,
                linux_active_mac, network_admission_approved, boot_chain_passed,
                station_id, observed_at, comments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def import_assets(db_path: Path, csv_path: Path) -> None:
    required = {"asset_id", "carrier_hardware_revision"}
    conn = open_db(db_path)
    inserted = 0
    try:
        with csv_path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise StagingError(f"asset CSV is missing columns: {', '.join(sorted(missing))}")
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            for row in reader:
                if not optional_text(row.get("asset_id")):
                    continue
                conn.execute(
                    """
                    INSERT INTO assets(
                        asset_id, carrier_serial_number, carrier_hardware_revision,
                        schematic_reference, country_code, institution_id, location,
                        state, comments, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["asset_id"].strip(),
                        optional_text(row.get("carrier_serial_number")),
                        row["carrier_hardware_revision"].strip(),
                        optional_text(row.get("schematic_reference")),
                        optional_text(row.get("country_code")) or "US",
                        int(row["institution_id"]) if optional_text(row.get("institution_id")) else None,
                        optional_text(row.get("location")),
                        optional_text(row.get("state")) or "pending",
                        optional_text(row.get("comments")),
                        now,
                        now,
                    ),
                )
                inserted += 1
            conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise StagingError(f"asset import rejected: {exc}") from exc
    finally:
        conn.close()
    print(f"imported_assets={inserted}")


def enroll(db_path: Path, args: argparse.Namespace) -> None:
    som_uuid = normalize_uuid(args.som_uuid)
    factory_mac = normalize_mac(args.factory_mac)
    observed_at = normalize_timestamp(args.observed_at or utc_now())
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        asset = conn.execute("SELECT * FROM assets WHERE asset_id = ?", (args.asset_id,)).fetchone()
        if asset is None:
            raise StagingError(f"unknown asset: {args.asset_id}; import/scan the carrier asset first")

        existing_som = conn.execute("SELECT * FROM soms WHERE som_uuid = ?", (som_uuid,)).fetchone()
        if existing_som:
            expected = (existing_som["som_serial_number"], existing_som["factory_mac_id_0"])
            observed = (args.som_serial, factory_mac)
            if expected != observed:
                raise StagingError(
                    f"UUID {som_uuid} already exists with serial/MAC {expected}, not {observed}"
                )
        else:
            conn.execute(
                """
                INSERT INTO soms(
                    som_uuid, som_serial_number, som_product_name, som_hardware_revision,
                    factory_mac_id_0, fru_checksum_valid, eeprom_sha256, comments,
                    discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    som_uuid,
                    args.som_serial,
                    args.som_product,
                    optional_text(args.som_revision),
                    factory_mac,
                    int(args.fru_checksum_valid),
                    optional_text(args.eeprom_sha256),
                    "Discovered by automated DAPHNE enrollment.",
                    observed_at,
                ),
            )

        active = conn.execute(
            "SELECT * FROM installations WHERE asset_id = ? AND removed_at IS NULL",
            (args.asset_id,),
        ).fetchone()
        if active and active["som_uuid"] != som_uuid:
            raise StagingError(
                f"asset {args.asset_id} already has active SOM {active['som_uuid']}; remove it explicitly first"
            )
        if not active:
            conn.execute(
                """
                INSERT INTO installations(asset_id, som_uuid, installed_at, station_id, comments)
                VALUES (?, ?, ?, ?, ?)
                """,
                (args.asset_id, som_uuid, observed_at, args.station_id, "Automated enrollment"),
            )

        deployment = {
            "asset_id": args.asset_id,
            "mac_source": args.mac_source,
            "production_mac": args.production_mac,
            "ipv4_address": args.ipv4_address,
            "hostname": args.hostname,
            "vlan": args.vlan,
            "timing_endpoint": args.timing_endpoint,
            "firmware_release": args.firmware_release,
            "uboot_ethaddr": args.uboot_ethaddr,
            "fdt_mac": args.fdt_mac,
            "linux_active_mac": args.linux_active_mac,
            "network_admission_approved": args.network_admission_approved,
            "boot_chain_passed": args.boot_chain_passed,
            "station_id": args.station_id,
            "observed_at": observed_at,
            "comments": optional_text(args.comments),
        }
        insert_deployment(conn, deployment, replace=True)

        identity_data = {
            "fru_checksum_valid": bool(args.fru_checksum_valid),
            "identity_matches_item": True,
            "eeprom_sha256": optional_text(args.eeprom_sha256),
        }
        conn.execute(
            """
            INSERT INTO qa_events(
                asset_id, som_uuid, event_type, passed, station_id, operator,
                observed_at, evidence_uri, data_json, comments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.asset_id,
                som_uuid,
                "eeprom_identity_inspection",
                int(args.fru_checksum_valid),
                args.station_id,
                args.operator,
                observed_at,
                optional_text(args.evidence_uri),
                canonical_json(identity_data),
                "Automated EEPROM discovery and validation",
            ),
        )
        network_passed = bool(args.network_admission_approved and args.boot_chain_passed)
        conn.execute(
            """
            INSERT INTO qa_events(
                asset_id, som_uuid, event_type, passed, station_id, operator,
                observed_at, evidence_uri, data_json, comments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.asset_id,
                som_uuid,
                "production_network_enrollment",
                int(network_passed),
                args.station_id,
                args.operator,
                observed_at,
                optional_text(args.evidence_uri),
                canonical_json(deployment),
                optional_text(args.comments),
            ),
        )
        state = "qualified" if network_passed else "enrolled"
        conn.execute(
            "UPDATE assets SET state = ?, updated_at = ? WHERE asset_id = ?",
            (state, observed_at, args.asset_id),
        )
        conn.commit()
    except (sqlite3.IntegrityError, StagingError) as exc:
        conn.rollback()
        if isinstance(exc, StagingError):
            raise
        raise StagingError(f"enrollment rejected: {exc}") from exc
    finally:
        conn.close()
    print(f"asset_id={args.asset_id} som_uuid={som_uuid} state={state}")


def list_inventory(db_path: Path) -> None:
    conn = open_db(db_path)
    try:
        rows = inventory_rows(conn)
    finally:
        conn.close()
    columns = ["asset_id", "state", "som_uuid", "factory_mac_id_0", "production_mac", "ipv4_address"]
    print("\t".join(columns))
    for row in rows:
        print("\t".join(str(row.get(column) or "") for column in columns))


def inventory_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                a.asset_id, a.hwdb_part_id AS asset_hwdb_part_id,
                a.carrier_serial_number, a.carrier_hardware_revision,
                a.schematic_reference, a.country_code, a.institution_id,
                a.location, a.state, a.comments AS asset_comments,
                i.som_uuid, s.hwdb_part_id AS som_hwdb_part_id,
                s.som_serial_number, s.som_product_name, s.som_hardware_revision,
                s.factory_mac_id_0, s.fru_checksum_valid, s.eeprom_sha256,
                d.mac_source, d.production_mac, d.ipv4_address, d.hostname,
                d.vlan, d.timing_endpoint, d.firmware_release,
                d.uboot_ethaddr, d.fdt_mac, d.linux_active_mac,
                d.network_admission_approved, d.boot_chain_passed,
                d.station_id, d.observed_at
            FROM assets a
            LEFT JOIN installations i ON i.asset_id = a.asset_id AND i.removed_at IS NULL
            LEFT JOIN soms s ON s.som_uuid = i.som_uuid
            LEFT JOIN deployments d ON d.asset_id = a.asset_id
            ORDER BY a.asset_id
            """
        ).fetchall()
    ]


def validate_database(db_path: Path, quiet: bool = False) -> tuple[list[str], list[str]]:
    conn = open_db(db_path)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"SQLite integrity check: {integrity}")

        for row in conn.execute("SELECT * FROM component_type_proposals ORDER BY type_key"):
            if row["system_id"].startswith("TBD") or row["subsystem_id"].startswith("TBD"):
                warnings.append(f"{row['type_key']}: official HWDB system/subsystem IDs unresolved")
            if not row["hwdb_part_type_id"]:
                warnings.append(f"{row['type_key']}: official HWDB component type ID unresolved")
            elif not PART_TYPE_RE.fullmatch(row["hwdb_part_type_id"]):
                errors.append(f"{row['type_key']}: malformed HWDB component type ID")

        for row in inventory_rows(conn):
            asset_id = row["asset_id"]
            try:
                if row.get("som_uuid"):
                    normalize_uuid(row["som_uuid"])
                    normalize_mac(row["factory_mac_id_0"])
                if row.get("production_mac"):
                    for key in ("production_mac", "uboot_ethaddr", "fdt_mac", "linux_active_mac"):
                        normalize_mac(row[key])
                    normalize_ipv4(row["ipv4_address"])
                    normalize_hostname(row["hostname"])
            except StagingError as exc:
                errors.append(f"{asset_id}: {exc}")
                continue

            if row.get("production_mac") and not row.get("som_uuid"):
                errors.append(f"{asset_id}: deployment exists without an active installed SOM")
            if row.get("mac_source") == "som_eeprom" and row["production_mac"] != row["factory_mac_id_0"]:
                errors.append(f"{asset_id}: som_eeprom policy does not use the EEPROM MAC")
            if row.get("mac_source") == "legacy_override":
                warnings.append(f"{asset_id}: legacy_override must be replaced or explicitly approved")
            if row.get("boot_chain_passed"):
                expected = row["production_mac"]
                actual = (row["uboot_ethaddr"], row["fdt_mac"], row["linux_active_mac"])
                if actual != (expected, expected, expected):
                    errors.append(f"{asset_id}: boot_chain_passed but MAC layers are {actual}, expected {expected}")
            if row["state"] == "qualified":
                if not row.get("network_admission_approved") or not row.get("boot_chain_passed"):
                    errors.append(f"{asset_id}: qualified without network approval and boot-chain pass")
                if not row.get("asset_hwdb_part_id"):
                    warnings.append(f"{asset_id}: qualified locally but has no assigned HWDB Part ID")

        bad_json = conn.execute("SELECT event_id, data_json FROM qa_events ORDER BY event_id").fetchall()
        for event in bad_json:
            try:
                json.loads(event["data_json"])
            except json.JSONDecodeError:
                errors.append(f"qa_event {event['event_id']}: invalid data_json")
    finally:
        conn.close()

    if not quiet:
        for warning in warnings:
            print(f"WARNING: {warning}")
        for error in errors:
            print(f"ERROR: {error}")
        print(f"validation_errors={len(errors)} validation_warnings={len(warnings)}")
    return errors, warnings


def export_hwdb(db_path: Path, output_dir: Path) -> None:
    errors, _ = validate_database(db_path, quiet=True)
    if errors:
        raise StagingError("database has validation errors; run validate before export")
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        component_types = component_type_exports(conn)
        test_types = test_type_exports(conn)
        inventory = inventory_rows(conn)
        items = item_exports(conn, inventory)
        links = subcomponent_exports(conn, inventory)
        tests = test_result_exports(conn, inventory)
        unresolved = unresolved_identifiers(conn, inventory)
        manifest = {
            "format": "daphne-hwdb-staging-export-v1",
            "generated_at": utc_now(),
            "source_database": str(db_path),
            "hwdb_writes_performed": False,
            "counts": {
                "component_type_proposals": len(component_types),
                "test_type_proposals": len(test_types),
                "items": len(items),
                "subcomponent_links": len(links),
                "test_results": len(tests),
            },
            "unresolved_identifiers": unresolved,
        }

        write_json(output_dir / "manifest.json", manifest)
        write_json(output_dir / "component-type-proposals.json", component_types)
        write_json(output_dir / "test-type-proposals.json", test_types)
        write_json(output_dir / "item-payloads.json", items)
        write_json(output_dir / "subcomponent-links.json", links)
        write_json(output_dir / "test-result-payloads.json", tests)
        write_inventory_csv(output_dir / "inventory.csv", inventory)
    finally:
        conn.close()
    print(f"export_dir={output_dir}")


def component_type_exports(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    exports: list[dict[str, Any]] = []
    for component in conn.execute("SELECT * FROM component_type_proposals ORDER BY type_key"):
        fields = conn.execute(
            "SELECT * FROM component_type_fields WHERE type_key = ? ORDER BY id",
            (component["type_key"],),
        ).fetchall()
        connectors = conn.execute(
            """
            SELECT c.*, t.hwdb_part_type_id AS target_part_type_id
            FROM component_type_connectors c
            JOIN component_type_proposals t ON t.type_key = c.target_type_key
            WHERE c.parent_type_key = ? ORDER BY c.id
            """,
            (component["type_key"],),
        ).fetchall()
        part_type_id = component["hwdb_part_type_id"]
        exports.append(
            {
                "local_type_key": component["type_key"],
                "requested_hierarchy": {
                    "project_id": component["project_id"],
                    "system_id": component["system_id"],
                    "system_name": component["system_name"],
                    "subsystem_id": component["subsystem_id"],
                    "subsystem_name": component["subsystem_name"],
                    "component_type_name": component["name"],
                },
                "endpoint_template": f"/component-types/{part_type_id or '<HWDB_PART_TYPE_ID>'}",
                "method": "PATCH",
                "ready_for_submission": False,
                "review_required": "HWDB architect must confirm hierarchy, roles, manufacturers, and field schema",
                "payload": {
                    "part_type_id": part_type_id,
                    "comments": component["comments"],
                    "manufacturers": [],
                    "roles": [],
                    "properties": {
                        "specifications": {field["hwdb_name"]: {} for field in fields}
                    },
                    "connectors": {
                        connector["functional_position"]: connector["target_part_type_id"]
                        for connector in connectors
                    },
                },
                "field_metadata": [dict(field) for field in fields],
            }
        )
    return exports


def test_type_exports(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    exports: list[dict[str, Any]] = []
    for test in conn.execute(
        """
        SELECT t.*, c.hwdb_part_type_id
        FROM test_type_proposals t
        JOIN component_type_proposals c ON c.type_key = t.component_type_key
        ORDER BY t.test_key
        """
    ):
        fields = conn.execute(
            "SELECT * FROM test_type_fields WHERE test_key = ? ORDER BY id",
            (test["test_key"],),
        ).fetchall()
        part_type_id = test["hwdb_part_type_id"]
        exports.append(
            {
                "local_test_key": test["test_key"],
                "endpoint_template": (
                    f"/component-types/{part_type_id or '<HWDB_PART_TYPE_ID>'}/test-types"
                ),
                "method": "POST",
                "ready_for_submission": bool(part_type_id),
                "payload": {
                    "component_type": {"part_type_id": part_type_id},
                    "name": test["name"],
                    "comments": test["comments"],
                    "specifications": {field["hwdb_name"]: {} for field in fields},
                },
                "field_metadata": [dict(field) for field in fields],
            }
        )
    return exports


def item_exports(conn: sqlite3.Connection, inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    type_ids = {
        row["type_key"]: row["hwdb_part_type_id"]
        for row in conn.execute("SELECT type_key, hwdb_part_type_id FROM component_type_proposals")
    }
    exports: list[dict[str, Any]] = []
    seen_soms: set[str] = set()
    for row in inventory:
        exports.append(
            {
                "local_kind": "daphne_board",
                "local_id": row["asset_id"],
                "hwdb_part_id": row["asset_hwdb_part_id"],
                "endpoint_template": (
                    f"/component-types/{type_ids['daphne_board'] or '<HWDB_PART_TYPE_ID:daphne_board>'}/components"
                ),
                "method": "POST",
                "ready_for_submission": bool(type_ids["daphne_board"] and row["institution_id"]),
                "payload": {
                    "component_type": {"part_type_id": type_ids["daphne_board"]},
                    "country_code": row["country_code"],
                    "institution": {"id": row["institution_id"]},
                    "serial_number": row["carrier_serial_number"] or row["asset_id"],
                    "comments": row["asset_comments"],
                    "specifications": {
                        "DAPHNE Asset ID": row["asset_id"],
                        "Carrier Serial Number": row["carrier_serial_number"],
                        "Carrier Hardware Revision": row["carrier_hardware_revision"],
                        "Schematic Reference": row["schematic_reference"],
                    },
                },
            }
        )
        if not row.get("som_uuid") or row["som_uuid"] in seen_soms:
            continue
        seen_soms.add(row["som_uuid"])
        exports.append(
            {
                "local_kind": "kria_k26_som",
                "local_id": row["som_uuid"],
                "hwdb_part_id": row["som_hwdb_part_id"],
                "endpoint_template": (
                    f"/component-types/{type_ids['kria_k26_som'] or '<HWDB_PART_TYPE_ID:kria_k26_som>'}/components"
                ),
                "method": "POST",
                "ready_for_submission": bool(type_ids["kria_k26_som"] and row["institution_id"]),
                "payload": {
                    "component_type": {"part_type_id": type_ids["kria_k26_som"]},
                    "country_code": row["country_code"],
                    "institution": {"id": row["institution_id"]},
                    "serial_number": row["som_serial_number"],
                    "comments": "AMD/Xilinx K26 SOM discovered during DAPHNE enrollment",
                    "specifications": {
                        "SOM UUID": row["som_uuid"],
                        "SOM Serial Number": row["som_serial_number"],
                        "SOM Product Name": row["som_product_name"],
                        "SOM Hardware Revision": row["som_hardware_revision"],
                        "Factory MAC ID 0": row["factory_mac_id_0"],
                    },
                },
            }
        )
    return exports


def subcomponent_exports(
    conn: sqlite3.Connection, inventory: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    del conn
    exports: list[dict[str, Any]] = []
    for row in inventory:
        if not row.get("som_uuid"):
            continue
        parent_pid = row["asset_hwdb_part_id"]
        child_pid = row["som_hwdb_part_id"]
        exports.append(
            {
                "parent_local_id": row["asset_id"],
                "child_local_id": row["som_uuid"],
                "functional_position": "Kria SOM",
                "endpoint_template": (
                    f"/components/{parent_pid or '<DAPHNE_HWDB_PART_ID>'}/subcomponents"
                ),
                "method": "PATCH",
                "ready_for_submission": bool(parent_pid and child_pid),
                "payload": {
                    "component": {"part_id": parent_pid},
                    "subcomponents": {"Kria SOM": child_pid},
                },
            }
        )
    return exports


def test_result_exports(
    conn: sqlite3.Connection, inventory: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows_by_asset = {row["asset_id"]: row for row in inventory}
    test_names = {
        row["test_key"]: row["name"]
        for row in conn.execute("SELECT test_key, name FROM test_type_proposals")
    }
    exports: list[dict[str, Any]] = []
    for event in conn.execute("SELECT * FROM qa_events ORDER BY event_id"):
        row = rows_by_asset.get(event["asset_id"])
        if event["event_type"] == "eeprom_identity_inspection":
            target_pid = row["som_hwdb_part_id"] if row else None
            test_key = "eeprom_identity_inspection"
            data = json.loads(event["data_json"])
            test_data = {
                "FRU Checksum Valid": bool(data.get("fru_checksum_valid", event["passed"])),
                "Identity Matches Item": bool(data.get("identity_matches_item", event["passed"])),
                "EEPROM Dump SHA256": data.get("eeprom_sha256") or (row["eeprom_sha256"] if row else None),
                "Enrollment Station ID": event["station_id"],
                "Observed At UTC": event["observed_at"],
            }
        elif event["event_type"] == "production_network_enrollment":
            target_pid = row["asset_hwdb_part_id"] if row else None
            test_key = "production_network_enrollment"
            if not row or not row.get("production_mac"):
                continue
            test_data = {
                "MAC Source": row["mac_source"],
                "Production MAC": row["production_mac"],
                "IPv4 Address": row["ipv4_address"],
                "Hostname": row["hostname"],
                "VLAN": row["vlan"],
                "Timing Endpoint": row["timing_endpoint"],
                "Firmware Release": row["firmware_release"],
                "U-Boot ethaddr": row["uboot_ethaddr"],
                "Working FDT MAC": row["fdt_mac"],
                "Linux Active MAC": row["linux_active_mac"],
                "Network Admission Approved": bool(row["network_admission_approved"]),
                "Boot Chain Passed": bool(row["boot_chain_passed"]),
                "Enrollment Station ID": row["station_id"],
                "Observed At UTC": row["observed_at"],
            }
        else:
            continue
        exports.append(
            {
                "local_event_id": event["event_id"],
                "target_hwdb_part_id": target_pid,
                "endpoint_template": f"/components/{target_pid or '<HWDB_PART_ID>'}/tests",
                "method": "POST",
                "ready_for_submission": bool(target_pid),
                "payload": {
                    "test_type": test_names[test_key],
                    "comments": event["comments"],
                    "test_data": test_data,
                },
                "evidence_uri": event["evidence_uri"],
            }
        )
    return exports


def unresolved_identifiers(
    conn: sqlite3.Connection, inventory: list[dict[str, Any]]
) -> list[str]:
    unresolved: list[str] = []
    for row in conn.execute("SELECT * FROM component_type_proposals ORDER BY type_key"):
        if row["system_id"].startswith("TBD"):
            unresolved.append(f"{row['type_key']}.system_id")
        if row["subsystem_id"].startswith("TBD"):
            unresolved.append(f"{row['type_key']}.subsystem_id")
        if not row["hwdb_part_type_id"]:
            unresolved.append(f"{row['type_key']}.hwdb_part_type_id")
        unresolved.append(f"{row['type_key']}.manufacturer_ids")
        unresolved.append(f"{row['type_key']}.role_ids")
    if any(not row.get("institution_id") for row in inventory):
        unresolved.append("institution_id")
    for row in inventory:
        if not row.get("asset_hwdb_part_id"):
            unresolved.append(f"asset:{row['asset_id']}.hwdb_part_id")
        if row.get("som_uuid") and not row.get("som_hwdb_part_id"):
            unresolved.append(f"som:{row['som_uuid']}.hwdb_part_id")
    return sorted(set(unresolved))


def set_hwdb_id(db_path: Path, kind: str, local_id: str, hwdb_id: str) -> None:
    conn = open_db(db_path)
    try:
        if kind == "type":
            if not PART_TYPE_RE.fullmatch(hwdb_id):
                raise StagingError(f"invalid HWDB component type ID: {hwdb_id}")
            result = conn.execute(
                "UPDATE component_type_proposals SET hwdb_part_type_id = ? WHERE type_key = ?",
                (hwdb_id, local_id),
            )
        elif kind == "asset":
            if not PART_ID_RE.fullmatch(hwdb_id):
                raise StagingError(f"invalid HWDB Part ID: {hwdb_id}")
            result = conn.execute(
                "UPDATE assets SET hwdb_part_id = ?, updated_at = ? WHERE asset_id = ?",
                (hwdb_id, utc_now(), local_id),
            )
        else:
            if not PART_ID_RE.fullmatch(hwdb_id):
                raise StagingError(f"invalid HWDB Part ID: {hwdb_id}")
            result = conn.execute(
                "UPDATE soms SET hwdb_part_id = ? WHERE som_uuid = ?",
                (hwdb_id, normalize_uuid(local_id)),
            )
        if result.rowcount != 1:
            raise StagingError(f"unknown {kind} local ID: {local_id}")
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise StagingError(f"HWDB ID already assigned: {exc}") from exc
    finally:
        conn.close()
    print(f"{kind}={local_id} hwdb_id={hwdb_id}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_inventory_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0]) if rows else ["asset_id"]
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def require_database(path: Path) -> None:
    if not path.exists():
        raise StagingError(f"database does not exist: {path}; run init first")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite staging database")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="Create/update schema and load proposals")
    init_parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    init_parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    init_parser.add_argument("--no-seed", action="store_true")

    import_parser = commands.add_parser("import-assets", help="Import authoritative carrier rows")
    import_parser.add_argument("csv_path", type=Path)

    enroll_parser = commands.add_parser(
        "enroll", help="Legacy one-step import; use daphne_production_cli.py for production"
    )
    enroll_parser.add_argument("--asset-id", required=True)
    enroll_parser.add_argument("--som-uuid", required=True)
    enroll_parser.add_argument("--som-serial", required=True)
    enroll_parser.add_argument("--som-product", required=True)
    enroll_parser.add_argument("--som-revision", default="")
    enroll_parser.add_argument("--factory-mac", required=True)
    enroll_parser.add_argument("--fru-checksum-valid", action="store_true")
    enroll_parser.add_argument("--eeprom-sha256", default="")
    enroll_parser.add_argument(
        "--mac-source", choices=("som_eeprom", "daphne_pool", "legacy_override"), required=True
    )
    enroll_parser.add_argument("--production-mac", required=True)
    enroll_parser.add_argument("--ipv4-address", required=True)
    enroll_parser.add_argument("--hostname", required=True)
    enroll_parser.add_argument("--vlan", type=int)
    enroll_parser.add_argument("--timing-endpoint", required=True)
    enroll_parser.add_argument("--firmware-release", required=True)
    enroll_parser.add_argument("--uboot-ethaddr", required=True)
    enroll_parser.add_argument("--fdt-mac", required=True)
    enroll_parser.add_argument("--linux-active-mac", required=True)
    enroll_parser.add_argument("--network-admission-approved", action="store_true")
    enroll_parser.add_argument("--boot-chain-passed", action="store_true")
    enroll_parser.add_argument("--station-id", required=True)
    enroll_parser.add_argument("--operator", required=True)
    enroll_parser.add_argument("--observed-at")
    enroll_parser.add_argument("--evidence-uri", default="")
    enroll_parser.add_argument("--comments", default="")

    commands.add_parser("list", help="List the current carrier/SOM/network inventory")
    commands.add_parser("validate", help="Validate uniqueness and production invariants")

    export_parser = commands.add_parser("export-hwdb", help="Write reviewable HWDB-shaped payloads")
    export_parser.add_argument("--output-dir", type=Path, default=Path("build/daphne-hwdb-export"))

    id_parser = commands.add_parser("set-hwdb-id", help="Record IDs assigned later by HWDB")
    id_parser.add_argument("kind", choices=("type", "asset", "som"))
    id_parser.add_argument("local_id")
    id_parser.add_argument("hwdb_id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            initialize_database(args.db, args.spec, None if args.no_seed else args.seed)
            return 0
        require_database(args.db)
        if args.command == "import-assets":
            import_assets(args.db, args.csv_path)
        elif args.command == "enroll":
            print(
                "warning: legacy one-step enrollment; use daphne_production_cli.py in production",
                file=sys.stderr,
            )
            enroll(args.db, args)
        elif args.command == "list":
            list_inventory(args.db)
        elif args.command == "validate":
            errors, _ = validate_database(args.db)
            return 1 if errors else 0
        elif args.command == "export-hwdb":
            export_hwdb(args.db, args.output_dir)
        elif args.command == "set-hwdb-id":
            set_hwdb_id(args.db, args.kind, args.local_id, args.hwdb_id)
        return 0
    except (StagingError, sqlite3.Error, OSError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
