from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import daphne_staging as staging  # noqa: E402


class DaphneStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = self.root / "staging.db"
        staging.initialize_database(
            self.db,
            REPO / "specs/staging/daphne_hwdb.toml",
            REPO / "specs/staging/daphne_observed_seed.toml",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_observed_seed_preserves_eeprom_and_active_mac(self) -> None:
        conn = staging.open_db(self.db)
        try:
            row = staging.inventory_rows(conn)[0]
        finally:
            conn.close()
        self.assertEqual(row["factory_mac_id_0"], "00:0a:35:0e:9b:63")
        self.assertEqual(row["production_mac"], "ba:be:ba:d1:cc:ff")
        self.assertEqual(row["state"], "observed")
        self.assertFalse(row["boot_chain_passed"])

    def test_export_never_invents_hwdb_ids(self) -> None:
        output = self.root / "export"
        staging.export_hwdb(self.db, output)
        manifest = json.loads((output / "manifest.json").read_text())
        item_payloads = json.loads((output / "item-payloads.json").read_text())
        self.assertFalse(manifest["hwdb_writes_performed"])
        self.assertIn("daphne_board.hwdb_part_type_id", manifest["unresolved_identifiers"])
        self.assertTrue(all(item["hwdb_part_id"] is None for item in item_payloads))
        self.assertTrue(all(not item["ready_for_submission"] for item in item_payloads))
        self.assertTrue(
            all(item["payload"]["component_type"]["part_type_id"] is None for item in item_payloads)
        )

    def test_duplicate_factory_mac_is_rejected_atomically(self) -> None:
        csv_path = self.root / "assets.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(
                target,
                fieldnames=("asset_id", "carrier_hardware_revision", "country_code"),
            )
            writer.writeheader()
            writer.writerow(
                {
                    "asset_id": "DAPHNE-NEW",
                    "carrier_hardware_revision": "DAPHNE V2",
                    "country_code": "US",
                }
            )
        staging.import_assets(self.db, csv_path)
        args = enrollment_args(
            asset_id="DAPHNE-NEW",
            som_uuid="11111111-2222-4333-8444-555555555555",
            factory_mac="00:0a:35:0e:9b:63",
            production_mac="02:00:00:00:10:01",
            ipv4_address="192.0.2.10",
            hostname="daphne-new.example",
            timing_endpoint="0x100",
        )
        with self.assertRaises(staging.StagingError):
            staging.enroll(self.db, args)
        conn = staging.open_db(self.db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM soms WHERE som_uuid = ?", (args.som_uuid,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_matching_boot_chain_can_be_qualified(self) -> None:
        csv_path = self.root / "assets.csv"
        csv_path.write_text(
            "asset_id,carrier_hardware_revision,country_code\n"
            "DAPHNE-NEW,DAPHNE V2,US\n",
            encoding="utf-8",
        )
        staging.import_assets(self.db, csv_path)
        args = enrollment_args(
            asset_id="DAPHNE-NEW",
            som_uuid="11111111-2222-4333-8444-555555555555",
            factory_mac="00:0a:35:00:10:01",
            production_mac="02:00:00:00:10:01",
            ipv4_address="192.0.2.10",
            hostname="daphne-new.example",
            timing_endpoint="0x100",
        )
        staging.enroll(self.db, args)
        errors, _ = staging.validate_database(self.db, quiet=True)
        self.assertEqual(errors, [])
        conn = staging.open_db(self.db)
        try:
            state = conn.execute(
                "SELECT state FROM assets WHERE asset_id = 'DAPHNE-NEW'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(state, "qualified")


def enrollment_args(**values: str) -> argparse.Namespace:
    production_mac = values["production_mac"]
    defaults = {
        "asset_id": values["asset_id"],
        "som_uuid": values["som_uuid"],
        "som_serial": f"SERIAL-{values['asset_id']}",
        "som_product": "SM-K26-XCL2GC-ED",
        "som_revision": "5057-02ED",
        "factory_mac": values["factory_mac"],
        "fru_checksum_valid": True,
        "eeprom_sha256": "a" * 64,
        "mac_source": "daphne_pool",
        "production_mac": production_mac,
        "ipv4_address": values["ipv4_address"],
        "hostname": values["hostname"],
        "vlan": 100,
        "timing_endpoint": values["timing_endpoint"],
        "firmware_release": "test-release",
        "uboot_ethaddr": production_mac,
        "fdt_mac": production_mac,
        "linux_active_mac": production_mac,
        "network_admission_approved": True,
        "boot_chain_passed": True,
        "station_id": "test-station",
        "operator": "tester",
        "observed_at": "2026-07-14T18:30:00Z",
        "evidence_uri": "sha256:test",
        "comments": "unit test",
    }
    return argparse.Namespace(**defaults)


if __name__ == "__main__":
    unittest.main()
