from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from daphne_production import Database, ProductionError, ProductionService  # noqa: E402


RECIPE = REPO / "specs/production/daphne-production-qa-v1.json"


class DaphneProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "production.db"
        self.service = ProductionService(Database(str(self.db_path)))
        self.assertEqual(self.service.migrate(), ["001"])

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def add_and_discover(self, asset_id: str, suffix: int) -> None:
        self.service.add_asset(
            asset_id=asset_id,
            carrier_revision="DAPHNE V2",
            carrier_serial=f"CARRIER-{suffix:03d}",
            operation_id=f"asset-{suffix}",
            station_id="station-a",
            operator="operator-a",
        )
        self.service.discover(
            asset_id=asset_id,
            som_uuid=f"00000000-0000-4000-8000-{suffix:012d}",
            som_serial=f"SOM-{suffix:03d}",
            som_product="SM-K26-XCL2GC-ED",
            som_revision="5057-02ED",
            factory_mac=f"02:00:00:00:{suffix // 256:02x}:{suffix % 256:02x}",
            fru_checksum_valid=True,
            eeprom_sha256=f"{suffix:064x}",
            operation_id=f"discover-{suffix}",
            station_id="station-a",
            operator="operator-a",
        )

    def allocate(self, asset_id: str, suffix: int, operation_id: str | None = None) -> None:
        self.service.allocate(
            asset_id=asset_id,
            mac_source="som_eeprom",
            production_mac=f"02:00:00:00:{suffix // 256:02x}:{suffix % 256:02x}",
            ipv4_address=f"192.0.2.{suffix}",
            hostname=f"daphne-{suffix:03d}.example",
            vlan=100,
            timing_endpoint=f"0x{suffix:03x}",
            firmware_release="daphne-test-release",
            operation_id=operation_id or f"allocate-{suffix}",
            station_id="station-a",
            operator="operator-a",
        )

    def test_complete_lifecycle_and_idempotent_release(self) -> None:
        self.add_and_discover("DAPHNE-001", 1)
        self.allocate("DAPHNE-001", 1)
        config, snapshot = self.service.render_config(
            asset_id="DAPHNE-001",
            operation_id="render-1",
            station_id="station-a",
            operator="operator-a",
        )
        self.assertEqual(config["contract"], "daphne.board-config")
        self.assertFalse(config["network"]["authorized"])
        self.service.provision(
            asset_id="DAPHNE-001",
            snapshot_sha256=snapshot,
            artifact_manifest_sha256="a" * 64,
            passed=True,
            operation_id="provision-1",
            station_id="station-a",
            operator="operator-a",
        )
        self.service.start_qa(
            asset_id="DAPHNE-001",
            operation_id="qa-start-1",
            station_id="station-a",
            operator="operator-a",
        )
        test_ids = (
            "identity_chain",
            "artifact_integrity",
            "cold_boot",
            "cold_boot",
            "cold_boot",
            "fpga_register_access",
            "timing_lock",
            "management_network",
            "frontend_connectivity",
            "stability_soak",
        )
        for index, test_id in enumerate(test_ids):
            self.service.record_test(
                asset_id="DAPHNE-001",
                recipe_path=RECIPE,
                test_id=test_id,
                passed=True,
                measurements={"sequence": index},
                operation_id=f"test-{index}",
                station_id="station-a",
                operator="operator-a",
            )
        self.service.record_test(
            asset_id="DAPHNE-001",
            recipe_path=RECIPE,
            test_id="identity_chain",
            passed=False,
            measurements={"reason": "deliberate regression check"},
            operation_id="test-regression",
            station_id="station-a",
            operator="operator-a",
        )
        with self.assertRaisesRegex(ProductionError, "identity_chain"):
            self.service.qualify(
                asset_id="DAPHNE-001",
                recipe_path=RECIPE,
                operation_id="qualify-before-recovery",
                station_id="review-station",
                operator="reviewer",
            )
        self.service.record_test(
            asset_id="DAPHNE-001",
            recipe_path=RECIPE,
            test_id="identity_chain",
            passed=True,
            measurements={"retest": True},
            operation_id="test-recovery",
            station_id="station-a",
            operator="operator-a",
        )
        self.service.qualify(
            asset_id="DAPHNE-001",
            recipe_path=RECIPE,
            operation_id="qualify-1",
            station_id="review-station",
            operator="reviewer",
        )
        released = self.service.release(
            asset_id="DAPHNE-001",
            operation_id="release-1",
            station_id="release-station",
            operator="approver",
        )
        retried = self.service.release(
            asset_id="DAPHNE-001",
            operation_id="release-1",
            station_id="release-station",
            operator="approver",
        )
        self.assertEqual(released, retried)
        self.assertEqual(released["lifecycle_state"], "released")
        self.assertTrue(released["assignment"]["network_authorized"])
        quarantined = self.service.quarantine(
            asset_id="DAPHNE-001",
            reason="release-gate regression test",
            operation_id="quarantine-after-release",
            station_id="station-a",
            operator="operator-a",
        )
        self.assertEqual(quarantined["resume_state"], "qa_passed")
        self.assertFalse(quarantined["assignment"]["network_authorized"])
        resumed = self.service.resume(
            asset_id="DAPHNE-001",
            operation_id="resume-after-release",
            station_id="station-a",
            operator="operator-a",
        )
        self.assertEqual(resumed["lifecycle_state"], "qa_passed")
        self.assertFalse(resumed["assignment"]["network_authorized"])
        self.service.release(
            asset_id="DAPHNE-001",
            operation_id="release-2",
            station_id="release-station",
            operator="approver",
        )
        history = self.service.history("DAPHNE-001")
        self.assertEqual(history[0]["contract"], "daphne.evidence")
        self.assertEqual(history[-1]["event_type"], "released")
        self.assertEqual(self.service.validate(), ([], []))

    def test_qualification_requires_all_recipe_results(self) -> None:
        self.add_and_discover("DAPHNE-002", 2)
        self.allocate("DAPHNE-002", 2)
        _, snapshot = self.service.render_config(
            asset_id="DAPHNE-002",
            operation_id="render-2",
            station_id="station-a",
            operator="operator-a",
        )
        self.service.provision(
            asset_id="DAPHNE-002",
            snapshot_sha256=snapshot,
            artifact_manifest_sha256="b" * 64,
            passed=True,
            operation_id="provision-2",
            station_id="station-a",
            operator="operator-a",
        )
        self.service.start_qa(
            asset_id="DAPHNE-002",
            operation_id="qa-start-2",
            station_id="station-a",
            operator="operator-a",
        )
        with self.assertRaisesRegex(ProductionError, "required QA tests"):
            self.service.qualify(
                asset_id="DAPHNE-002",
                recipe_path=RECIPE,
                operation_id="qualify-too-early",
                station_id="review-station",
                operator="reviewer",
            )

    def test_quarantine_resume_and_som_replacement_preserve_history(self) -> None:
        self.add_and_discover("DAPHNE-003", 3)
        self.allocate("DAPHNE-003", 3)
        quarantined = self.service.quarantine(
            asset_id="DAPHNE-003",
            reason="fixture lost power",
            operation_id="quarantine-3",
            station_id="station-a",
            operator="operator-a",
        )
        self.assertEqual(quarantined["resume_state"], "allocated")
        resumed = self.service.resume(
            asset_id="DAPHNE-003",
            operation_id="resume-3",
            station_id="station-a",
            operator="operator-a",
        )
        self.assertEqual(resumed["lifecycle_state"], "allocated")
        self.service.quarantine(
            asset_id="DAPHNE-003",
            reason="replace damaged SOM",
            operation_id="quarantine-replace-3",
            station_id="station-a",
            operator="operator-a",
        )
        replaced = self.service.replace_som(
            asset_id="DAPHNE-003",
            reason="approved rework",
            operation_id="replace-3",
            station_id="station-a",
            operator="operator-a",
        )
        self.assertEqual(replaced["lifecycle_state"], "received")
        self.assertIsNone(replaced["active_som_uuid"])
        self.service.discover(
            asset_id="DAPHNE-003",
            som_uuid="00000000-0000-4000-8000-000000000030",
            som_serial="SOM-030",
            som_product="SM-K26-XCL2GC-ED",
            som_revision="5057-02ED",
            factory_mac="02:00:00:00:00:1e",
            fru_checksum_valid=True,
            eeprom_sha256=f"{30:064x}",
            operation_id="discover-replacement-3",
            station_id="station-a",
            operator="operator-a",
        )
        reassigned = self.service.reassign(
            asset_id="DAPHNE-003",
            reason="replacement SOM has a different factory MAC",
            mac_source="som_eeprom",
            production_mac="02:00:00:00:00:1e",
            ipv4_address="192.0.2.3",
            hostname="daphne-003.example",
            vlan=100,
            timing_endpoint="0x003",
            firmware_release="daphne-test-release",
            operation_id="reassign-3",
            station_id="station-a",
            operator="operator-a",
        )
        self.assertEqual(reassigned["lifecycle_state"], "allocated")
        self.assertEqual(reassigned["assignment"]["assignment_revision"], 2)
        connection = self.service.database.connect()
        try:
            installation = connection.execute(
                "SELECT removed_at FROM installations "
                "WHERE asset_id = 'DAPHNE-003' AND removed_at IS NOT NULL"
            ).fetchone()
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM assignment_revisions WHERE asset_id = 'DAPHNE-003'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIsNotNone(installation["removed_at"])
        self.assertEqual(revision_count, 2)

    def test_two_stations_cannot_allocate_same_network_identity(self) -> None:
        self.add_and_discover("DAPHNE-010", 10)
        self.add_and_discover("DAPHNE-011", 11)

        def allocate_conflicting(asset_id: str, suffix: int) -> str:
            service = ProductionService(Database(str(self.db_path)))
            try:
                service.allocate(
                    asset_id=asset_id,
                    mac_source="daphne_pool",
                    production_mac=f"02:00:00:10:00:{suffix:02x}",
                    ipv4_address="192.0.2.200",
                    hostname=f"conflict-{suffix}.example",
                    vlan=100,
                    timing_endpoint=f"0x2{suffix}",
                    firmware_release="daphne-test-release",
                    operation_id=f"concurrent-{suffix}",
                    station_id=f"station-{suffix}",
                    operator="operator",
                )
                return "allocated"
            except (sqlite3.IntegrityError, ProductionError):
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(allocate_conflicting, ("DAPHNE-010", "DAPHNE-011"), (10, 11))
            )
        self.assertCountEqual(results, ["allocated", "rejected"])

    def test_operation_id_cannot_be_reused_for_different_data(self) -> None:
        self.service.add_asset(
            asset_id="DAPHNE-020",
            carrier_revision="DAPHNE V2",
            carrier_serial=None,
            operation_id="same-operation",
            station_id="station-a",
            operator="operator-a",
        )
        with self.assertRaisesRegex(ProductionError, "conflicting data"):
            self.service.add_asset(
                asset_id="DAPHNE-021",
                carrier_revision="DAPHNE V3",
                carrier_serial=None,
                operation_id="same-operation",
                station_id="station-a",
                operator="operator-a",
            )

    def test_200_board_campaign_allocations_are_unique_and_valid(self) -> None:
        for suffix in range(1, 201):
            asset_id = f"DAPHNE-{suffix:03d}"
            self.add_and_discover(asset_id, suffix)
            self.allocate(asset_id, suffix)
        statuses = self.service.list_status()
        self.assertEqual(len(statuses), 200)
        self.assertEqual(len({row["assignment"]["ipv4_address"] for row in statuses}), 200)
        self.assertEqual(len({row["assignment"]["production_mac"] for row in statuses}), 200)
        self.assertEqual(self.service.validate(), ([], []))


if __name__ == "__main__":
    unittest.main()
