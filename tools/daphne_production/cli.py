"""Command-line interface for the DAPHNE production lifecycle."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import shlex
from pathlib import Path
from typing import Any

from .database import DEFAULT_DATABASE_URL, Database
from .errors import ProductionError
from .k26_eeprom import (
    DEFAULT_I2C_ADDRESS,
    build_discover_args,
    decode_k26_som_eeprom,
    read_eeprom_bytes,
    sysfs_eeprom_path,
)
from .service import ProductionService
from .values import canonical_json


DEFAULT_RECIPE = (
    Path(__file__).resolve().parents[2]
    / "specs/production/daphne-production-qa-v1.json"
)


def _json_object(value: str) -> dict[str, Any]:
    if value.startswith("@"):
        text = Path(value[1:]).read_text(encoding="utf-8")
    else:
        text = value
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return parsed


def _actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--operator", required=True)


def _evidence(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-uri")
    parser.add_argument("--evidence-sha256")


def _pass_fail(parser: argparse.ArgumentParser) -> None:
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--pass", dest="passed", action="store_true")
    choice.add_argument("--fail", dest="passed", action="store_false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DATABASE_URL,
        help="SQLite path/URL or PostgreSQL URL",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="Apply ordered production database migrations")

    eeprom = commands.add_parser(
        "eeprom-decode",
        help="Decode a Kria K26 SOM EEPROM dump for discover",
    )
    eeprom_source = eeprom.add_mutually_exclusive_group(required=True)
    eeprom_source.add_argument("--input", type=Path, help="Raw 8192-byte EEPROM dump")
    eeprom_source.add_argument(
        "--sysfs-eeprom",
        type=Path,
        help="Linux at24 sysfs EEPROM file, for example /sys/bus/i2c/devices/1-0050/eeprom",
    )
    eeprom_source.add_argument("--i2c-bus", type=int, help="Linux I2C bus exposing address 0x50")
    eeprom.add_argument("--i2c-address", type=lambda value: int(value, 0), default=DEFAULT_I2C_ADDRESS)
    eeprom.add_argument("--dump-output", type=Path, help="Copy the raw EEPROM bytes to evidence storage")
    eeprom.add_argument(
        "--allow-invalid-checksums",
        action="store_true",
        help="Decode for quarantine diagnostics instead of failing closed",
    )
    eeprom.add_argument("--asset-id", help="Include the scanned carrier asset in rendered discover args")
    eeprom.add_argument("--observed-at")
    eeprom.add_argument("--operation-id")
    eeprom.add_argument("--station-id")
    eeprom.add_argument("--operator")
    eeprom.add_argument("--evidence-uri")
    eeprom.add_argument("--evidence-sha256")
    eeprom.add_argument(
        "--discover-command",
        action="store_true",
        help="Also emit a shell-quoted daphne_production_cli.py discover command",
    )

    asset = commands.add_parser("asset-add", help="Create one authoritative carrier asset")
    asset.add_argument("--asset-id", required=True)
    asset.add_argument("--carrier-revision", required=True)
    asset.add_argument("--carrier-serial")
    asset.add_argument("--observed-at")
    _actor(asset)

    asset_import = commands.add_parser("asset-import", help="Import authoritative assets from CSV")
    asset_import.add_argument("csv_path", type=Path)
    asset_import.add_argument("--station-id", required=True)
    asset_import.add_argument("--operator", required=True)

    discover = commands.add_parser("discover", help="Bind a checksum-valid K26 SOM identity")
    discover.add_argument("--asset-id", required=True)
    discover.add_argument("--som-uuid", required=True)
    discover.add_argument("--som-serial", required=True)
    discover.add_argument("--som-product", required=True)
    discover.add_argument("--som-revision")
    discover.add_argument("--factory-mac", required=True)
    discover.add_argument("--fru-checksum-valid", action="store_true")
    discover.add_argument("--eeprom-sha256", required=True)
    discover.add_argument("--observed-at")
    _actor(discover)
    _evidence(discover)

    allocate = commands.add_parser("allocate", help="Create an immutable board assignment")
    allocate.add_argument("--asset-id", required=True)
    allocate.add_argument(
        "--mac-source",
        choices=("som_eeprom", "daphne_pool", "legacy_override"),
        required=True,
    )
    allocate.add_argument("--production-mac", required=True)
    allocate.add_argument("--ipv4-address", required=True)
    allocate.add_argument("--hostname", required=True)
    allocate.add_argument("--vlan", type=int)
    allocate.add_argument("--timing-endpoint", required=True)
    allocate.add_argument("--firmware-release", required=True)
    allocate.add_argument("--observed-at")
    _actor(allocate)

    reassign = commands.add_parser(
        "reassign", help="Create an explicit assignment revision after approved rework"
    )
    reassign.add_argument("--asset-id", required=True)
    reassign.add_argument("--reason", required=True)
    reassign.add_argument(
        "--mac-source",
        choices=("som_eeprom", "daphne_pool", "legacy_override"),
        required=True,
    )
    reassign.add_argument("--production-mac", required=True)
    reassign.add_argument("--ipv4-address", required=True)
    reassign.add_argument("--hostname", required=True)
    reassign.add_argument("--vlan", type=int)
    reassign.add_argument("--timing-endpoint", required=True)
    reassign.add_argument("--firmware-release", required=True)
    reassign.add_argument("--observed-at")
    _actor(reassign)

    render = commands.add_parser("render", help="Create a canonical per-board snapshot")
    render.add_argument("--asset-id", required=True)
    render.add_argument("--output-dir", type=Path, required=True)
    render.add_argument("--observed-at")
    _actor(render)

    provision = commands.add_parser("provision", help="Record common-image provisioning")
    provision.add_argument("--asset-id", required=True)
    provision.add_argument("--snapshot-sha256", required=True)
    provision.add_argument("--artifact-manifest-sha256", required=True)
    provision.add_argument("--observed-at")
    _pass_fail(provision)
    _actor(provision)
    _evidence(provision)

    qa_start = commands.add_parser("qa-start", help="Start or resume the QA recipe")
    qa_start.add_argument("--asset-id", required=True)
    _actor(qa_start)

    test = commands.add_parser("test", help="Append one QA test result")
    test.add_argument("--asset-id", required=True)
    test.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    test.add_argument("--test-id", required=True)
    test.add_argument("--measurements", type=_json_object, default={})
    test.add_argument("--observed-at")
    _pass_fail(test)
    _actor(test)
    _evidence(test)

    qualify = commands.add_parser("qualify", help="Require every mandatory recipe test")
    qualify.add_argument("--asset-id", required=True)
    qualify.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    _actor(qualify)

    release = commands.add_parser("release", help="Authorize a QA-passed board for production")
    release.add_argument("--asset-id", required=True)
    _actor(release)

    quarantine = commands.add_parser("quarantine", help="Block a board and preserve resume state")
    quarantine.add_argument("--asset-id", required=True)
    quarantine.add_argument("--reason", required=True)
    quarantine.add_argument("--details", type=_json_object, default={})
    _actor(quarantine)

    resume = commands.add_parser("resume", help="Restore the state saved by quarantine")
    resume.add_argument("--asset-id", required=True)
    _actor(resume)

    service = commands.add_parser("service", help="Remove a released board from production")
    service.add_argument("--asset-id", required=True)
    service.add_argument("--reason", required=True)
    _actor(service)

    replace = commands.add_parser("replace-som", help="Close the active SOM installation")
    replace.add_argument("--asset-id", required=True)
    replace.add_argument("--reason", required=True)
    _actor(replace)

    status = commands.add_parser("status", help="Show one asset as versioned JSON")
    status.add_argument("--asset-id", required=True)
    history = commands.add_parser("history", help="Export append-only evidence for one asset")
    history.add_argument("--asset-id", required=True)
    commands.add_parser("list", help="Show all assets as versioned JSON")
    commands.add_parser("validate", help="Check production lifecycle invariants")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "migrate":
            service = ProductionService(Database(args.database_url))
            print(json.dumps({"applied_migrations": service.migrate()}, indent=2))
            return 0
        if args.command == "eeprom-decode":
            result = _decode_eeprom_command(args)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        service = ProductionService(Database(args.database_url))
        service.database.require_ready()
        actor = {
            "operation_id": getattr(args, "operation_id", None),
            "station_id": getattr(args, "station_id", None),
            "operator": getattr(args, "operator", None),
        }
        if args.command == "asset-add":
            result = service.add_asset(
                asset_id=args.asset_id,
                carrier_revision=args.carrier_revision,
                carrier_serial=args.carrier_serial,
                observed_at=args.observed_at,
                **actor,
            )
        elif args.command == "asset-import":
            result = service.import_assets(
                args.csv_path, station_id=args.station_id, operator=args.operator
            )
        elif args.command == "discover":
            result = service.discover(
                asset_id=args.asset_id,
                som_uuid=args.som_uuid,
                som_serial=args.som_serial,
                som_product=args.som_product,
                som_revision=args.som_revision,
                factory_mac=args.factory_mac,
                fru_checksum_valid=args.fru_checksum_valid,
                eeprom_sha256=args.eeprom_sha256,
                observed_at=args.observed_at,
                evidence_uri=args.evidence_uri,
                evidence_sha256=args.evidence_sha256,
                **actor,
            )
        elif args.command == "allocate":
            result = service.allocate(
                asset_id=args.asset_id,
                mac_source=args.mac_source,
                production_mac=args.production_mac,
                ipv4_address=args.ipv4_address,
                hostname=args.hostname,
                vlan=args.vlan,
                timing_endpoint=args.timing_endpoint,
                firmware_release=args.firmware_release,
                observed_at=args.observed_at,
                **actor,
            )
        elif args.command == "reassign":
            result = service.reassign(
                asset_id=args.asset_id,
                reason=args.reason,
                mac_source=args.mac_source,
                production_mac=args.production_mac,
                ipv4_address=args.ipv4_address,
                hostname=args.hostname,
                vlan=args.vlan,
                timing_endpoint=args.timing_endpoint,
                firmware_release=args.firmware_release,
                observed_at=args.observed_at,
                **actor,
            )
        elif args.command == "render":
            config, digest = service.render_config(
                asset_id=args.asset_id, observed_at=args.observed_at, **actor
            )
            args.output_dir.mkdir(parents=True, exist_ok=True)
            config_path = args.output_dir / "board-config-v1.json"
            config_path.write_text(canonical_json(config), encoding="utf-8")
            (args.output_dir / "SHA256SUMS").write_text(
                f"{digest}  board-config-v1.json\n", encoding="utf-8"
            )
            result = {"configuration": str(config_path), "sha256": digest}
        elif args.command == "provision":
            result = service.provision(
                asset_id=args.asset_id,
                snapshot_sha256=args.snapshot_sha256,
                artifact_manifest_sha256=args.artifact_manifest_sha256,
                passed=args.passed,
                observed_at=args.observed_at,
                evidence_uri=args.evidence_uri,
                evidence_sha256=args.evidence_sha256,
                **actor,
            )
        elif args.command == "qa-start":
            result = service.start_qa(asset_id=args.asset_id, **actor)
        elif args.command == "test":
            result = service.record_test(
                asset_id=args.asset_id,
                recipe_path=args.recipe,
                test_id=args.test_id,
                passed=args.passed,
                measurements=args.measurements,
                observed_at=args.observed_at,
                evidence_uri=args.evidence_uri,
                evidence_sha256=args.evidence_sha256,
                **actor,
            )
        elif args.command == "qualify":
            result = service.qualify(asset_id=args.asset_id, recipe_path=args.recipe, **actor)
        elif args.command == "release":
            result = service.release(asset_id=args.asset_id, **actor)
        elif args.command == "quarantine":
            result = service.quarantine(
                asset_id=args.asset_id,
                reason=args.reason,
                data=args.details,
                **actor,
            )
        elif args.command == "resume":
            result = service.resume(asset_id=args.asset_id, **actor)
        elif args.command == "service":
            result = service.enter_service(
                asset_id=args.asset_id, reason=args.reason, **actor
            )
        elif args.command == "replace-som":
            result = service.replace_som(
                asset_id=args.asset_id, reason=args.reason, **actor
            )
        elif args.command == "status":
            result = service.status(args.asset_id)
        elif args.command == "history":
            result = service.history(args.asset_id)
        elif args.command == "list":
            result = service.list_status()
        elif args.command == "validate":
            errors, warnings = service.validate()
            print(json.dumps({"errors": errors, "warnings": warnings}, indent=2))
            return 1 if errors else 0
        else:
            raise AssertionError(args.command)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ProductionError, sqlite3.Error, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _decode_eeprom_command(args: argparse.Namespace) -> dict[str, Any]:
    source_path = args.input or args.sysfs_eeprom
    source: dict[str, Any]
    if args.i2c_bus is not None:
        source_path = sysfs_eeprom_path(args.i2c_bus, args.i2c_address)
        source = {
            "kind": "linux-sysfs-i2c",
            "i2c_bus": args.i2c_bus,
            "i2c_address": f"0x{args.i2c_address:02x}",
            "path": str(source_path),
        }
    elif args.sysfs_eeprom is not None:
        source = {"kind": "linux-sysfs-eeprom", "path": str(source_path)}
    else:
        source = {"kind": "file", "path": str(source_path)}

    assert source_path is not None
    data = read_eeprom_bytes(source_path)
    decoded = decode_k26_som_eeprom(
        data,
        allow_invalid_checksums=args.allow_invalid_checksums,
    )

    if args.dump_output:
        args.dump_output.parent.mkdir(parents=True, exist_ok=True)
        args.dump_output.write_bytes(data)
        source["dump_output"] = str(args.dump_output)
        decoded.setdefault("evidence", {})["dump_uri"] = str(args.dump_output)
        decoded["evidence"]["dump_sha256"] = decoded["eeprom"]["sha256"]

    rendered_evidence_sha = args.evidence_sha256
    if not rendered_evidence_sha and args.dump_output:
        rendered_evidence_sha = decoded["eeprom"]["sha256"]

    if decoded["eeprom"]["fru_checksum_valid"]:
        discover_args = build_discover_args(
            decoded,
            asset_id=args.asset_id,
            observed_at=args.observed_at,
            operation_id=args.operation_id,
            station_id=args.station_id,
            operator=args.operator,
            evidence_uri=args.evidence_uri or (str(args.dump_output) if args.dump_output else None),
            evidence_sha256=rendered_evidence_sha,
        )
        decoded["discover_args"] = discover_args
        if args.discover_command:
            decoded["discover_command"] = " ".join(
                ["python3", "tools/daphne_production_cli.py", "discover"]
                + [shlex.quote(part) for part in discover_args]
            )

    decoded["source"] = source
    return decoded
