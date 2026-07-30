"""Transactional, resumable operations for DAPHNE production enrollment."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from .contracts import load_qa_recipe, validate_board_config
from .database import Database
from .errors import ProductionError
from .lifecycle import check_transition, require_state
from .values import (
    canonical_json,
    normalize_hostname,
    normalize_ipv4,
    normalize_mac,
    normalize_sha256,
    normalize_timestamp,
    normalize_uuid,
    sha256_json,
    utc_now,
)


def _dict(row: Any) -> dict[str, Any]:
    return dict(row)


class ProductionService:
    def __init__(self, database: Database):
        self.database = database

    def migrate(self) -> list[str]:
        return self.database.migrate()

    def _asset(self, connection: Any, asset_id: str) -> dict[str, Any]:
        suffix = " FOR UPDATE" if self.database.backend == "postgresql" else ""
        row = self.database.execute(
            connection, f"SELECT * FROM assets WHERE asset_id = ?{suffix}", (asset_id,)
        ).fetchone()
        if row is None:
            raise ProductionError(f"unknown asset: {asset_id}")
        return _dict(row)

    def _active_som(self, connection: Any, asset_id: str) -> dict[str, Any] | None:
        row = self.database.execute(
            connection,
            """
            SELECT s.*, i.installation_id, i.installed_at
            FROM installations i JOIN soms s ON s.som_uuid = i.som_uuid
            WHERE i.asset_id = ? AND i.removed_at IS NULL
            """,
            (asset_id,),
        ).fetchone()
        return _dict(row) if row else None

    @staticmethod
    def _normalize_assignment(
        *,
        mac_source: str,
        production_mac: str,
        ipv4_address: str,
        hostname: str,
        vlan: int | None,
        timing_endpoint: str,
        firmware_release: str,
    ) -> dict[str, Any]:
        if mac_source not in {"som_eeprom", "daphne_pool", "legacy_override"}:
            raise ProductionError(f"invalid MAC source: {mac_source}")
        if vlan is not None and not 1 <= vlan <= 4094:
            raise ProductionError(f"VLAN must be between 1 and 4094: {vlan}")
        assignment = {
            "mac_source": mac_source,
            "production_mac": normalize_mac(production_mac),
            "ipv4_address": normalize_ipv4(ipv4_address),
            "hostname": normalize_hostname(hostname),
            "vlan": vlan,
            "timing_endpoint": timing_endpoint.strip(),
            "firmware_release": firmware_release.strip(),
        }
        if not assignment["timing_endpoint"] or not assignment["firmware_release"]:
            raise ProductionError("timing_endpoint and firmware_release are required")
        return assignment

    @staticmethod
    def _event_hash(asset_id: str, event_type: str, data: dict[str, Any]) -> str:
        return sha256_json({"asset_id": asset_id, "event_type": event_type, "data": data})

    def _operation_exists(
        self,
        connection: Any,
        operation_id: str,
        asset_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> bool:
        row = self.database.execute(
            connection,
            "SELECT asset_id, event_type, payload_sha256 FROM evidence_events WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return False
        expected_hash = self._event_hash(asset_id, event_type, data)
        if (
            row["asset_id"] != asset_id
            or row["event_type"] != event_type
            or row["payload_sha256"] != expected_hash
        ):
            raise ProductionError(
                f"operation_id {operation_id!r} was already used for different data"
            )
        return True

    def _record_event(
        self,
        connection: Any,
        *,
        operation_id: str,
        asset_id: str,
        som_uuid: str | None,
        event_type: str,
        outcome: str,
        from_state: str | None,
        to_state: str | None,
        station_id: str,
        operator: str,
        observed_at: str,
        data: dict[str, Any],
        evidence_uri: str | None = None,
        evidence_sha256: str | None = None,
    ) -> None:
        if not operation_id.strip():
            raise ProductionError("operation_id must not be empty")
        if outcome not in {"recorded", "passed", "failed"}:
            raise ProductionError(f"invalid evidence outcome: {outcome}")
        self.database.execute(
            connection,
            """
            INSERT INTO evidence_events(
                operation_id, asset_id, som_uuid, event_type, outcome,
                from_state, to_state, station_id, operator, observed_at,
                evidence_uri, evidence_sha256, payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                asset_id,
                som_uuid,
                event_type,
                outcome,
                from_state,
                to_state,
                station_id.strip(),
                operator.strip(),
                observed_at,
                evidence_uri.strip() if evidence_uri else None,
                normalize_sha256(evidence_sha256, "evidence SHA-256")
                if evidence_sha256
                else None,
                canonical_json(data),
                self._event_hash(asset_id, event_type, data),
            ),
        )

    def _set_state(
        self,
        connection: Any,
        asset: dict[str, Any],
        target: str,
        observed_at: str,
        *,
        resume_state: str | None = None,
        special: bool = False,
    ) -> None:
        current = asset["lifecycle_state"]
        if not special:
            check_transition(current, target)
        if current == target and asset.get("resume_state") == resume_state:
            return
        self.database.execute(
            connection,
            """
            UPDATE assets
            SET lifecycle_state = ?, resume_state = ?,
                record_revision = record_revision + 1, updated_at = ?
            WHERE asset_id = ?
            """,
            (target, resume_state, observed_at, asset["asset_id"]),
        )

    def add_asset(
        self,
        *,
        asset_id: str,
        carrier_revision: str,
        carrier_serial: str | None,
        operation_id: str,
        station_id: str,
        operator: str,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(observed_at or utc_now())
        data = {
            "carrier_revision": carrier_revision.strip(),
            "carrier_serial": carrier_serial.strip() if carrier_serial else None,
        }
        if not asset_id.strip() or not data["carrier_revision"]:
            raise ProductionError("asset_id and carrier_revision are required")
        with self.database.transaction() as connection:
            existing_operation = self.database.execute(
                connection,
                "SELECT asset_id, event_type, payload_sha256 FROM evidence_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing_operation:
                if (
                    existing_operation["asset_id"] != asset_id
                    or existing_operation["event_type"] != "asset_received"
                    or existing_operation["payload_sha256"]
                    != self._event_hash(asset_id, "asset_received", data)
                ):
                    raise ProductionError(f"operation_id {operation_id!r} has conflicting data")
                return self.status(asset_id, connection=connection)
            row = self.database.execute(
                connection, "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if row:
                existing = _dict(row)
                if (
                    existing["carrier_revision"] != data["carrier_revision"]
                    or existing["carrier_serial"] != data["carrier_serial"]
                ):
                    raise ProductionError(f"asset {asset_id} already exists with different identity")
            else:
                self.database.execute(
                    connection,
                    """
                    INSERT INTO assets(
                        asset_id, carrier_serial, carrier_revision, lifecycle_state,
                        record_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, 'received', 1, ?, ?)
                    """,
                    (asset_id, data["carrier_serial"], data["carrier_revision"], timestamp, timestamp),
                )
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=None,
                event_type="asset_received",
                outcome="recorded",
                from_state=None,
                to_state="received",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return self.status(asset_id, connection=connection)

    def import_assets(
        self, csv_path: Path, *, station_id: str, operator: str
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with csv_path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            required = {"asset_id", "carrier_revision"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ProductionError(f"asset CSV is missing: {', '.join(sorted(missing))}")
            for row in reader:
                identity = {
                    "asset_id": row["asset_id"].strip(),
                    "carrier_revision": row["carrier_revision"].strip(),
                    "carrier_serial": row.get("carrier_serial", "").strip() or None,
                }
                operation_id = "asset-import:" + sha256_json(identity)
                results.append(
                    self.add_asset(
                        **identity,
                        operation_id=operation_id,
                        station_id=station_id,
                        operator=operator,
                    )
                )
        return results

    def discover(
        self,
        *,
        asset_id: str,
        som_uuid: str,
        som_serial: str,
        som_product: str,
        som_revision: str | None,
        factory_mac: str,
        fru_checksum_valid: bool,
        eeprom_sha256: str,
        operation_id: str,
        station_id: str,
        operator: str,
        observed_at: str | None = None,
        evidence_uri: str | None = None,
        evidence_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not fru_checksum_valid:
            raise ProductionError("invalid FRU must be recorded with quarantine, not discovered")
        timestamp = normalize_timestamp(observed_at or utc_now())
        identity = {
            "som_uuid": normalize_uuid(som_uuid),
            "som_serial": som_serial.strip(),
            "som_product": som_product.strip(),
            "som_revision": som_revision.strip() if som_revision else None,
            "factory_mac_id_0": normalize_mac(factory_mac),
            "fru_checksum_valid": True,
            "eeprom_sha256": normalize_sha256(eeprom_sha256, "EEPROM SHA-256"),
        }
        with self.database.transaction() as connection:
            if self._operation_exists(
                connection, operation_id, asset_id, "som_discovered", identity
            ):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(asset["lifecycle_state"], {"received", "discovered"}, "discover SOM")
            row = self.database.execute(
                connection, "SELECT * FROM soms WHERE som_uuid = ?", (identity["som_uuid"],)
            ).fetchone()
            if row:
                existing = _dict(row)
                for key in (
                    "som_serial",
                    "som_product",
                    "som_revision",
                    "factory_mac_id_0",
                    "eeprom_sha256",
                ):
                    if existing[key] != identity[key]:
                        raise ProductionError(
                            f"SOM {identity['som_uuid']} already exists with different {key}"
                        )
            else:
                self.database.execute(
                    connection,
                    """
                    INSERT INTO soms(
                        som_uuid, som_serial, som_product, som_revision,
                        factory_mac_id_0, fru_checksum_valid, eeprom_sha256, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        identity["som_uuid"],
                        identity["som_serial"],
                        identity["som_product"],
                        identity["som_revision"],
                        identity["factory_mac_id_0"],
                        identity["eeprom_sha256"],
                        timestamp,
                    ),
                )
            active = self._active_som(connection, asset_id)
            if active and active["som_uuid"] != identity["som_uuid"]:
                raise ProductionError(
                    f"asset {asset_id} already contains SOM {active['som_uuid']}; use replace-som"
                )
            if active is None:
                self.database.execute(
                    connection,
                    """
                    INSERT INTO installations(
                        installation_id, asset_id, som_uuid, installed_at, station_id, operator
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"installation:{operation_id}",
                        asset_id,
                        identity["som_uuid"],
                        timestamp,
                        station_id,
                        operator,
                    ),
                )
            self._set_state(connection, asset, "discovered", timestamp)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=identity["som_uuid"],
                event_type="som_discovered",
                outcome="passed",
                from_state=asset["lifecycle_state"],
                to_state="discovered",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=identity,
                evidence_uri=evidence_uri,
                evidence_sha256=evidence_sha256,
            )
            return self.status(asset_id, connection=connection)

    def allocate(
        self,
        *,
        asset_id: str,
        mac_source: str,
        production_mac: str,
        ipv4_address: str,
        hostname: str,
        vlan: int | None,
        timing_endpoint: str,
        firmware_release: str,
        operation_id: str,
        station_id: str,
        operator: str,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(observed_at or utc_now())
        assignment = self._normalize_assignment(
            mac_source=mac_source,
            production_mac=production_mac,
            ipv4_address=ipv4_address,
            hostname=hostname,
            vlan=vlan,
            timing_endpoint=timing_endpoint,
            firmware_release=firmware_release,
        )
        with self.database.transaction() as connection:
            if self._operation_exists(
                connection, operation_id, asset_id, "assignment_allocated", assignment
            ):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(asset["lifecycle_state"], {"discovered", "allocated"}, "allocate")
            active = self._active_som(connection, asset_id)
            if active is None:
                raise ProductionError(f"asset {asset_id} has no active SOM")
            if (
                assignment["mac_source"] == "som_eeprom"
                and assignment["production_mac"] != active["factory_mac_id_0"]
            ):
                raise ProductionError("som_eeprom production MAC does not match the active SOM")
            row = self.database.execute(
                connection, "SELECT * FROM assignments WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if row:
                existing = _dict(row)
                for key, expected in assignment.items():
                    if existing[key] != expected:
                        raise ProductionError(
                            f"asset {asset_id} already has a different immutable {key} assignment"
                        )
                assignment_revision = existing["assignment_revision"]
            else:
                assignment_revision = 1
                self.database.execute(
                    connection,
                    """
                    INSERT INTO assignments(
                        asset_id, assignment_revision, mac_source, production_mac,
                        ipv4_address, hostname, vlan, timing_endpoint, firmware_release,
                        network_authorized, created_at, updated_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        asset_id,
                        assignment["mac_source"],
                        assignment["production_mac"],
                        assignment["ipv4_address"],
                        assignment["hostname"],
                        assignment["vlan"],
                        assignment["timing_endpoint"],
                        assignment["firmware_release"],
                        timestamp,
                        timestamp,
                    ),
                )
                self.database.execute(
                    connection,
                    """
                    INSERT INTO assignment_revisions(
                        asset_id, assignment_revision, mac_source, production_mac,
                        ipv4_address, hostname, vlan, timing_endpoint, firmware_release,
                        reason, operation_id, created_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 'initial allocation', ?, ?)
                    """,
                    (
                        asset_id,
                        assignment["mac_source"],
                        assignment["production_mac"],
                        assignment["ipv4_address"],
                        assignment["hostname"],
                        assignment["vlan"],
                        assignment["timing_endpoint"],
                        assignment["firmware_release"],
                        operation_id,
                        timestamp,
                    ),
                )
            self._set_state(connection, asset, "allocated", timestamp)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=active["som_uuid"],
                event_type="assignment_allocated",
                outcome="recorded",
                from_state=asset["lifecycle_state"],
                to_state="allocated",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=assignment,
            )
            return self.status(asset_id, connection=connection)

    def reassign(
        self,
        *,
        asset_id: str,
        reason: str,
        mac_source: str,
        production_mac: str,
        ipv4_address: str,
        hostname: str,
        vlan: int | None,
        timing_endpoint: str,
        firmware_release: str,
        operation_id: str,
        station_id: str,
        operator: str,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(observed_at or utc_now())
        assignment = self._normalize_assignment(
            mac_source=mac_source,
            production_mac=production_mac,
            ipv4_address=ipv4_address,
            hostname=hostname,
            vlan=vlan,
            timing_endpoint=timing_endpoint,
            firmware_release=firmware_release,
        )
        data = {"reason": reason.strip(), **assignment}
        if not data["reason"]:
            raise ProductionError("reassignment reason is required")
        with self.database.transaction() as connection:
            if self._operation_exists(
                connection, operation_id, asset_id, "assignment_revised", data
            ):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(asset["lifecycle_state"], {"discovered"}, "revise assignment")
            active = self._active_som(connection, asset_id)
            if active is None:
                raise ProductionError(f"asset {asset_id} has no active SOM")
            if (
                assignment["mac_source"] == "som_eeprom"
                and assignment["production_mac"] != active["factory_mac_id_0"]
            ):
                raise ProductionError("som_eeprom production MAC does not match the active SOM")
            current_row = self.database.execute(
                connection, "SELECT * FROM assignments WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if current_row is None:
                raise ProductionError(f"asset {asset_id} has no assignment to revise; use allocate")
            current = _dict(current_row)
            revision = current["assignment_revision"] + 1
            self.database.execute(
                connection,
                """
                UPDATE assignments SET
                    assignment_revision = ?, mac_source = ?, production_mac = ?,
                    ipv4_address = ?, hostname = ?, vlan = ?, timing_endpoint = ?,
                    firmware_release = ?, network_authorized = 0, updated_at = ?
                WHERE asset_id = ?
                """,
                (
                    revision,
                    assignment["mac_source"],
                    assignment["production_mac"],
                    assignment["ipv4_address"],
                    assignment["hostname"],
                    assignment["vlan"],
                    assignment["timing_endpoint"],
                    assignment["firmware_release"],
                    timestamp,
                    asset_id,
                ),
            )
            self.database.execute(
                connection,
                """
                INSERT INTO assignment_revisions(
                    asset_id, assignment_revision, mac_source, production_mac,
                    ipv4_address, hostname, vlan, timing_endpoint, firmware_release,
                    reason, operation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    revision,
                    assignment["mac_source"],
                    assignment["production_mac"],
                    assignment["ipv4_address"],
                    assignment["hostname"],
                    assignment["vlan"],
                    assignment["timing_endpoint"],
                    assignment["firmware_release"],
                    data["reason"],
                    operation_id,
                    timestamp,
                ),
            )
            self._set_state(connection, asset, "allocated", timestamp)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=active["som_uuid"],
                event_type="assignment_revised",
                outcome="recorded",
                from_state="discovered",
                to_state="allocated",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return self.status(asset_id, connection=connection)

    def render_config(
        self,
        *,
        asset_id: str,
        operation_id: str,
        station_id: str,
        operator: str,
        observed_at: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        timestamp = normalize_timestamp(observed_at or utc_now())
        with self.database.transaction() as connection:
            asset = self._asset(connection, asset_id)
            require_state(
                asset["lifecycle_state"],
                {"allocated", "provisioned", "qa_running", "qa_passed", "released"},
                "render configuration",
            )
            som = self._active_som(connection, asset_id)
            assignment_row = self.database.execute(
                connection, "SELECT * FROM assignments WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if som is None or assignment_row is None:
                raise ProductionError(f"asset {asset_id} is missing SOM or assignment")
            assignment = _dict(assignment_row)
            config = {
                "contract": "daphne.board-config",
                "version": 1,
                "asset": {
                    "asset_id": asset_id,
                    "carrier_revision": asset["carrier_revision"],
                },
                "som": {
                    "uuid": som["som_uuid"],
                    "serial": som["som_serial"],
                    "product": som["som_product"],
                    "factory_mac_id_0": som["factory_mac_id_0"],
                },
                "network": {
                    "mac_source": assignment["mac_source"],
                    "production_mac": assignment["production_mac"],
                    "ipv4_address": assignment["ipv4_address"],
                    "hostname": assignment["hostname"],
                    "vlan": assignment["vlan"],
                    "authorized": bool(assignment["network_authorized"]),
                },
                "runtime": {
                    "timing_endpoint": assignment["timing_endpoint"],
                    "firmware_release": assignment["firmware_release"],
                },
                "source": {
                    "assignment_revision": assignment["assignment_revision"],
                    "asset_record_revision": asset["record_revision"],
                },
            }
            validate_board_config(config)
            digest = sha256_json(config)
            data = {"snapshot_sha256": digest, "configuration": config}
            if self._operation_exists(
                connection, operation_id, asset_id, "configuration_rendered", data
            ):
                return config, digest
            self.database.execute(
                connection,
                """
                INSERT INTO configuration_snapshots(
                    snapshot_sha256, asset_id, assignment_revision,
                    contract_version, payload_json, created_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(snapshot_sha256) DO NOTHING
                """,
                (
                    digest,
                    asset_id,
                    assignment["assignment_revision"],
                    canonical_json(config),
                    timestamp,
                ),
            )
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"],
                event_type="configuration_rendered",
                outcome="recorded",
                from_state=asset["lifecycle_state"],
                to_state=asset["lifecycle_state"],
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return config, digest

    def provision(
        self,
        *,
        asset_id: str,
        snapshot_sha256: str,
        artifact_manifest_sha256: str,
        passed: bool,
        operation_id: str,
        station_id: str,
        operator: str,
        observed_at: str | None = None,
        evidence_uri: str | None = None,
        evidence_sha256: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(observed_at or utc_now())
        data = {
            "snapshot_sha256": normalize_sha256(snapshot_sha256, "snapshot SHA-256"),
            "artifact_manifest_sha256": normalize_sha256(
                artifact_manifest_sha256, "artifact manifest SHA-256"
            ),
            "passed": bool(passed),
        }
        with self.database.transaction() as connection:
            if self._operation_exists(connection, operation_id, asset_id, "provision", data):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(asset["lifecycle_state"], {"allocated", "provisioned"}, "provision")
            snapshot = self.database.execute(
                connection,
                "SELECT asset_id FROM configuration_snapshots WHERE snapshot_sha256 = ?",
                (data["snapshot_sha256"],),
            ).fetchone()
            if snapshot is None or snapshot["asset_id"] != asset_id:
                raise ProductionError("configuration snapshot does not belong to this asset")
            target = "provisioned" if passed else "quarantined"
            resume_state = None if passed else asset["lifecycle_state"]
            self._set_state(connection, asset, target, timestamp, resume_state=resume_state)
            som = self._active_som(connection, asset_id)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"] if som else None,
                event_type="provision",
                outcome="passed" if passed else "failed",
                from_state=asset["lifecycle_state"],
                to_state=target,
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
                evidence_uri=evidence_uri,
                evidence_sha256=evidence_sha256,
            )
            return self.status(asset_id, connection=connection)

    def start_qa(
        self, *, asset_id: str, operation_id: str, station_id: str, operator: str
    ) -> dict[str, Any]:
        timestamp = utc_now()
        data: dict[str, Any] = {}
        with self.database.transaction() as connection:
            if self._operation_exists(connection, operation_id, asset_id, "qa_started", data):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(asset["lifecycle_state"], {"provisioned", "qa_running"}, "start QA")
            self._set_state(connection, asset, "qa_running", timestamp)
            som = self._active_som(connection, asset_id)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"] if som else None,
                event_type="qa_started",
                outcome="recorded",
                from_state=asset["lifecycle_state"],
                to_state="qa_running",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return self.status(asset_id, connection=connection)

    def record_test(
        self,
        *,
        asset_id: str,
        recipe_path: Path,
        test_id: str,
        passed: bool,
        measurements: dict[str, Any],
        operation_id: str,
        station_id: str,
        operator: str,
        observed_at: str | None = None,
        evidence_uri: str | None = None,
        evidence_sha256: str | None = None,
    ) -> dict[str, Any]:
        recipe = load_qa_recipe(recipe_path)
        valid_ids = {test["test_id"] for test in recipe["tests"]}
        if test_id not in valid_ids:
            raise ProductionError(f"test {test_id!r} is not in recipe {recipe['recipe_id']}")
        timestamp = normalize_timestamp(observed_at or utc_now())
        data = {
            "recipe_id": recipe["recipe_id"],
            "recipe_version": recipe["recipe_version"],
            "test_id": test_id,
            "passed": bool(passed),
            "measurements": measurements,
        }
        with self.database.transaction() as connection:
            if self._operation_exists(connection, operation_id, asset_id, "qa_test", data):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(asset["lifecycle_state"], {"qa_running"}, "record QA test")
            assignment = self.database.execute(
                connection,
                "SELECT firmware_release FROM assignments WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if assignment is None or not re.fullmatch(
                recipe["release_pattern"], assignment["firmware_release"]
            ):
                raise ProductionError(
                    f"release {assignment['firmware_release'] if assignment else None!r} "
                    f"does not match QA recipe"
                )
            som = self._active_som(connection, asset_id)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"] if som else None,
                event_type="qa_test",
                outcome="passed" if passed else "failed",
                from_state="qa_running",
                to_state="qa_running",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
                evidence_uri=evidence_uri,
                evidence_sha256=evidence_sha256,
            )
            self.database.execute(
                connection,
                """
                INSERT INTO qa_results(
                    operation_id, asset_id, recipe_id, recipe_version,
                    test_id, passed, observed_at, measurements_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    asset_id,
                    recipe["recipe_id"],
                    recipe["recipe_version"],
                    test_id,
                    int(passed),
                    timestamp,
                    canonical_json(measurements),
                ),
            )
            return self.status(asset_id, connection=connection)

    def qualify(
        self,
        *,
        asset_id: str,
        recipe_path: Path,
        operation_id: str,
        station_id: str,
        operator: str,
    ) -> dict[str, Any]:
        recipe = load_qa_recipe(recipe_path)
        data = {"recipe_id": recipe["recipe_id"], "recipe_version": recipe["recipe_version"]}
        timestamp = utc_now()
        with self.database.transaction() as connection:
            if self._operation_exists(connection, operation_id, asset_id, "qa_qualified", data):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(asset["lifecycle_state"], {"qa_running", "qa_passed"}, "qualify")
            missing: list[str] = []
            for test in recipe["tests"]:
                if not test["required"]:
                    continue
                row = self.database.execute(
                    connection,
                    """
                    SELECT COUNT(*) AS passes FROM qa_results
                    WHERE asset_id = ? AND recipe_id = ? AND recipe_version = ?
                      AND test_id = ? AND passed = 1
                    """,
                    (
                        asset_id,
                        recipe["recipe_id"],
                        recipe["recipe_version"],
                        test["test_id"],
                    ),
                ).fetchone()
                latest = self.database.execute(
                    connection,
                    """
                    SELECT q.passed FROM qa_results q
                    JOIN evidence_events e ON e.operation_id = q.operation_id
                    WHERE q.asset_id = ? AND q.recipe_id = ? AND q.recipe_version = ?
                      AND q.test_id = ?
                    ORDER BY e.event_sequence DESC LIMIT 1
                    """,
                    (
                        asset_id,
                        recipe["recipe_id"],
                        recipe["recipe_version"],
                        test["test_id"],
                    ),
                ).fetchone()
                if (
                    row["passes"] < test.get("minimum_passes", 1)
                    or latest is None
                    or not latest["passed"]
                ):
                    missing.append(test["test_id"])
            if missing:
                raise ProductionError("required QA tests have not passed: " + ", ".join(missing))
            self._set_state(connection, asset, "qa_passed", timestamp)
            som = self._active_som(connection, asset_id)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"] if som else None,
                event_type="qa_qualified",
                outcome="passed",
                from_state=asset["lifecycle_state"],
                to_state="qa_passed",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return self.status(asset_id, connection=connection)

    def release(
        self, *, asset_id: str, operation_id: str, station_id: str, operator: str
    ) -> dict[str, Any]:
        timestamp = utc_now()
        data = {"network_authorized": True}
        with self.database.transaction() as connection:
            if self._operation_exists(connection, operation_id, asset_id, "released", data):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(asset["lifecycle_state"], {"qa_passed", "released"}, "release")
            assignment = self.database.execute(
                connection, "SELECT asset_id FROM assignments WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if assignment is None:
                raise ProductionError(f"asset {asset_id} has no assignment")
            self.database.execute(
                connection,
                "UPDATE assignments SET network_authorized = 1, updated_at = ? WHERE asset_id = ?",
                (timestamp, asset_id),
            )
            self._set_state(connection, asset, "released", timestamp)
            som = self._active_som(connection, asset_id)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"] if som else None,
                event_type="released",
                outcome="passed",
                from_state=asset["lifecycle_state"],
                to_state="released",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return self.status(asset_id, connection=connection)

    def quarantine(
        self,
        *,
        asset_id: str,
        reason: str,
        operation_id: str,
        station_id: str,
        operator: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = utc_now()
        event_data = {"reason": reason.strip(), "details": data or {}}
        if not event_data["reason"]:
            raise ProductionError("quarantine reason is required")
        with self.database.transaction() as connection:
            if self._operation_exists(
                connection, operation_id, asset_id, "quarantined", event_data
            ):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(
                asset["lifecycle_state"],
                {"received", "discovered", "allocated", "provisioned", "qa_running", "qa_passed", "released", "service", "quarantined"},
                "quarantine",
            )
            resume_state = asset.get("resume_state") or asset["lifecycle_state"]
            if resume_state == "released":
                resume_state = "qa_passed"
            self.database.execute(
                connection,
                "UPDATE assignments SET network_authorized = 0, updated_at = ? WHERE asset_id = ?",
                (timestamp, asset_id),
            )
            self._set_state(
                connection, asset, "quarantined", timestamp, resume_state=resume_state
            )
            som = self._active_som(connection, asset_id)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"] if som else None,
                event_type="quarantined",
                outcome="failed",
                from_state=asset["lifecycle_state"],
                to_state="quarantined",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=event_data,
            )
            return self.status(asset_id, connection=connection)

    def resume(
        self, *, asset_id: str, operation_id: str, station_id: str, operator: str
    ) -> dict[str, Any]:
        timestamp = utc_now()
        with self.database.transaction() as connection:
            existing = self.database.execute(
                connection,
                "SELECT asset_id, event_type FROM evidence_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing:
                if existing["asset_id"] != asset_id or existing["event_type"] != "resumed":
                    raise ProductionError(f"operation_id {operation_id!r} has conflicting data")
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            target = asset.get("resume_state")
            if asset["lifecycle_state"] != "quarantined" or not target:
                raise ProductionError(f"asset {asset_id} has no quarantined state to resume")
            data = {"restored_state": target}
            if self._operation_exists(connection, operation_id, asset_id, "resumed", data):
                return self.status(asset_id, connection=connection)
            self._set_state(connection, asset, target, timestamp, special=True)
            som = self._active_som(connection, asset_id)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"] if som else None,
                event_type="resumed",
                outcome="recorded",
                from_state="quarantined",
                to_state=target,
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return self.status(asset_id, connection=connection)

    def enter_service(
        self, *, asset_id: str, reason: str, operation_id: str, station_id: str, operator: str
    ) -> dict[str, Any]:
        timestamp = utc_now()
        data = {"reason": reason.strip()}
        if not data["reason"]:
            raise ProductionError("service reason is required")
        with self.database.transaction() as connection:
            if self._operation_exists(
                connection, operation_id, asset_id, "service_entered", data
            ):
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            require_state(
                asset["lifecycle_state"],
                {"provisioned", "qa_running", "qa_passed", "released", "service"},
                "enter service",
            )
            self.database.execute(
                connection,
                "UPDATE assignments SET network_authorized = 0, updated_at = ? WHERE asset_id = ?",
                (timestamp, asset_id),
            )
            self._set_state(connection, asset, "service", timestamp)
            som = self._active_som(connection, asset_id)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=som["som_uuid"] if som else None,
                event_type="service_entered",
                outcome="recorded",
                from_state=asset["lifecycle_state"],
                to_state="service",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return self.status(asset_id, connection=connection)

    def replace_som(
        self, *, asset_id: str, reason: str, operation_id: str, station_id: str, operator: str
    ) -> dict[str, Any]:
        timestamp = utc_now()
        with self.database.transaction() as connection:
            existing = self.database.execute(
                connection,
                "SELECT asset_id, event_type FROM evidence_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing:
                if existing["asset_id"] != asset_id or existing["event_type"] != "som_removed":
                    raise ProductionError(f"operation_id {operation_id!r} has conflicting data")
                return self.status(asset_id, connection=connection)
            asset = self._asset(connection, asset_id)
            active = self._active_som(connection, asset_id)
            data = {"reason": reason.strip(), "removed_som_uuid": active["som_uuid"] if active else None}
            if self._operation_exists(connection, operation_id, asset_id, "som_removed", data):
                return self.status(asset_id, connection=connection)
            require_state(asset["lifecycle_state"], {"service", "quarantined"}, "replace SOM")
            if active is None:
                raise ProductionError(f"asset {asset_id} has no active SOM")
            self.database.execute(
                connection,
                "UPDATE installations SET removed_at = ? WHERE installation_id = ?",
                (timestamp, active["installation_id"]),
            )
            self.database.execute(
                connection,
                "UPDATE assignments SET network_authorized = 0, updated_at = ? WHERE asset_id = ?",
                (timestamp, asset_id),
            )
            self._set_state(connection, asset, "received", timestamp, special=True)
            self._record_event(
                connection,
                operation_id=operation_id,
                asset_id=asset_id,
                som_uuid=active["som_uuid"],
                event_type="som_removed",
                outcome="recorded",
                from_state=asset["lifecycle_state"],
                to_state="received",
                station_id=station_id,
                operator=operator,
                observed_at=timestamp,
                data=data,
            )
            return self.status(asset_id, connection=connection)

    def status(self, asset_id: str, *, connection: Any | None = None) -> dict[str, Any]:
        own_connection = connection is None
        connection = connection or self.database.connect()
        try:
            asset = self._asset(connection, asset_id)
            som = self._active_som(connection, asset_id)
            assignment_row = self.database.execute(
                connection, "SELECT * FROM assignments WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            assignment = _dict(assignment_row) if assignment_row else None
            return {
                "contract": "daphne.inventory-record",
                "version": 1,
                "asset_id": asset_id,
                "carrier_serial": asset["carrier_serial"],
                "carrier_revision": asset["carrier_revision"],
                "lifecycle_state": asset["lifecycle_state"],
                "resume_state": asset["resume_state"],
                "record_revision": asset["record_revision"],
                "active_som_uuid": som["som_uuid"] if som else None,
                "assignment": {
                    "contract": "daphne.assignment",
                    "version": 1,
                    "asset_id": asset_id,
                    "assignment_revision": assignment["assignment_revision"],
                    "mac_source": assignment["mac_source"],
                    "production_mac": assignment["production_mac"],
                    "ipv4_address": assignment["ipv4_address"],
                    "hostname": assignment["hostname"],
                    "vlan": assignment["vlan"],
                    "timing_endpoint": assignment["timing_endpoint"],
                    "firmware_release": assignment["firmware_release"],
                    "network_authorized": bool(assignment["network_authorized"]),
                }
                if assignment
                else None,
                "updated_at": asset["updated_at"],
            }
        finally:
            if own_connection:
                connection.close()

    def list_status(self) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            asset_ids = [
                row["asset_id"]
                for row in self.database.execute(
                    connection, "SELECT asset_id FROM assets ORDER BY asset_id"
                ).fetchall()
            ]
            return [self.status(asset_id, connection=connection) for asset_id in asset_ids]
        finally:
            connection.close()

    def history(self, asset_id: str) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            self._asset(connection, asset_id)
            rows = self.database.execute(
                connection,
                """
                SELECT * FROM evidence_events
                WHERE asset_id = ? ORDER BY event_sequence
                """,
                (asset_id,),
            ).fetchall()
            return [
                {
                    "contract": "daphne.evidence",
                    "version": 1,
                    "operation_id": row["operation_id"],
                    "asset_id": row["asset_id"],
                    "som_uuid": row["som_uuid"],
                    "event_type": row["event_type"],
                    "outcome": row["outcome"],
                    "station_id": row["station_id"],
                    "operator": row["operator"],
                    "observed_at": row["observed_at"],
                    "evidence_uri": row["evidence_uri"],
                    "evidence_sha256": row["evidence_sha256"],
                    "data": json.loads(row["payload_json"]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def validate(self) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        connection = self.database.connect()
        try:
            for asset in self.database.execute(
                connection, "SELECT * FROM assets ORDER BY asset_id"
            ).fetchall():
                state = asset["lifecycle_state"]
                som = self._active_som(connection, asset["asset_id"])
                assignment = self.database.execute(
                    connection,
                    "SELECT * FROM assignments WHERE asset_id = ?",
                    (asset["asset_id"],),
                ).fetchone()
                if state in {"discovered", "allocated", "provisioned", "qa_running", "qa_passed", "released"} and som is None:
                    errors.append(f"{asset['asset_id']}: {state} without an active SOM")
                if state in {"allocated", "provisioned", "qa_running", "qa_passed", "released"} and assignment is None:
                    errors.append(f"{asset['asset_id']}: {state} without an assignment")
                if state == "released" and assignment and not assignment["network_authorized"]:
                    errors.append(f"{asset['asset_id']}: released without network authorization")
                if state != "released" and assignment and assignment["network_authorized"]:
                    errors.append(f"{asset['asset_id']}: network authorized while state is {state}")
                if state == "quarantined" and not asset["resume_state"]:
                    warnings.append(f"{asset['asset_id']}: quarantined without a resumable state")
            for event in self.database.execute(
                connection, "SELECT operation_id, payload_json FROM evidence_events"
            ).fetchall():
                try:
                    json.loads(event["payload_json"])
                except json.JSONDecodeError:
                    errors.append(f"event {event['operation_id']}: invalid payload JSON")
        finally:
            connection.close()
        return errors, warnings
