from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from daphne_production.errors import ProductionError  # noqa: E402
from daphne_production.k26_eeprom import (  # noqa: E402
    decode_k26_som_eeprom,
    sysfs_eeprom_path,
)


class K26EepromTests(unittest.TestCase):
    def test_decode_valid_k26_som_eeprom(self) -> None:
        image = k26_eeprom_image()
        decoded = decode_k26_som_eeprom(image)

        self.assertEqual(decoded["contract"], "daphne.k26-som-eeprom")
        self.assertEqual(decoded["som"]["uuid"], "7602d763-78d4-42f3-8a94-87448d7e1d18")
        self.assertEqual(decoded["som"]["serial"], "XFL1F5B1N4KP")
        self.assertEqual(decoded["som"]["product"], "SM-K26-XCL2GC-ED")
        self.assertEqual(decoded["som"]["revision"], "5057-02E")
        self.assertEqual(decoded["som"]["factory_mac_id_0"], "00:0a:35:0d:03:66")
        self.assertTrue(decoded["eeprom"]["fru_checksum_valid"])
        self.assertEqual(decoded["discover"]["factory_mac"], "00:0a:35:0d:03:66")

    def test_invalid_checksum_fails_closed_by_default(self) -> None:
        image = bytearray(k26_eeprom_image())
        image[0x20] ^= 0x01

        with self.assertRaisesRegex(ProductionError, "invalid K26 SOM EEPROM checksum"):
            decode_k26_som_eeprom(bytes(image))

        decoded = decode_k26_som_eeprom(bytes(image), allow_invalid_checksums=True)
        self.assertFalse(decoded["eeprom"]["fru_checksum_valid"])
        self.assertFalse(decoded["eeprom"]["checksum_summary"]["board_area"])

    def test_decode_accepts_padding_after_mac_record(self) -> None:
        image = bytearray(k26_eeprom_image())
        image[0x89:0xE5] = b"\xFF" * (0xE5 - 0x89)

        decoded = decode_k26_som_eeprom(bytes(image))

        self.assertEqual(decoded["som"]["factory_mac_id_0"], "00:0a:35:0d:03:66")
        self.assertTrue(decoded["eeprom"]["fru_checksum_valid"])

    def test_missing_uuid_is_rejected(self) -> None:
        image = bytearray(k26_eeprom_image())
        image[0x55] = 0x00
        image[0x56] = 0xC1
        refresh_board_checksum(image)

        with self.assertRaisesRegex(ProductionError, "missing K26 SOM UUID"):
            decode_k26_som_eeprom(bytes(image))

    def test_sysfs_path_uses_k26_som_address(self) -> None:
        self.assertEqual(str(sysfs_eeprom_path(1)), "/sys/bus/i2c/devices/1-0050/eeprom")

    def test_cli_renders_discover_command_from_dump(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            dump = Path(root) / "som-eeprom.bin"
            dump.write_bytes(k26_eeprom_image())
            command = [
                sys.executable,
                str(REPO / "tools/daphne_production_cli.py"),
                "eeprom-decode",
                "--input",
                str(dump),
                "--asset-id",
                "DAPHNE-001",
                "--operation-id",
                "run:discover",
                "--station-id",
                "station-01",
                "--operator",
                "operator-a",
                "--discover-command",
            ]
            completed = subprocess.run(command, check=True, text=True, capture_output=True)
            decoded = json.loads(completed.stdout)

        self.assertIn("--asset-id", decoded["discover_args"])
        self.assertIn("DAPHNE-001", decoded["discover_args"])
        self.assertIn("--fru-checksum-valid", decoded["discover_args"])
        self.assertIn("daphne_production_cli.py discover", decoded["discover_command"])


def k26_eeprom_image() -> bytes:
    image = bytearray([0xFF] * 8192)
    image[:8] = bytes([0x01, 0x00, 0x00, 0x01, 0x00, 0x0D, 0x00, 0x00])
    image[7] = checksum(image[:7])

    board = bytearray([0x00] * 96)
    board[0:6] = bytes([0x01, 0x0C, 0x19, 0x00, 0x00, 0x00])
    cursor = 6
    cursor = append_ascii_field(board, cursor, "AMD   ", 6)
    cursor = append_ascii_field(board, cursor, "SM-K26-XCL2GC-ED", 16)
    cursor = append_ascii_field(board, cursor, "XFL1F5B1N4KP", 16)
    cursor = append_ascii_field(board, cursor, "5057-02ED", 9)
    cursor = append_binary_field(board, cursor, b"\x00")
    cursor = append_ascii_field(board, cursor, "5057-02E", 8)
    cursor = append_binary_field(board, cursor, bytes.fromhex("10ee000000000000"))
    cursor = append_binary_field(
        board,
        cursor,
        uuid.UUID("7602d763-78d4-42f3-8a94-87448d7e1d18").bytes,
    )
    board[cursor] = 0xC1
    board[-1] = checksum(board[:-1])
    image[0x08:0x68] = board

    put_record(image, 0x68, 0x02, 0x02, bytes.fromhex("0102f4010000a00f64002602c20100")[:13])
    put_record(image, 0x7A, 0xD2, 0x02, bytes.fromhex("da100031000a350d0366"))
    memory_payload = (
        b"\xda\x10\x00"
        + b"Memory: "
        + b"QSPI:512Mb  "
        + b"\x00"
        + b"Memory: "
        + b"eMMC:16GB   "
        + b"\x00"
        + b"Memory: "
        + b"PSDDR4:4GB  "
        + b"\x00"
        + b"Memory: "
        + b"PLDDR4:None "
        + b"\x00"
    )
    self_length_check = 0x57
    assert len(memory_payload) == self_length_check
    put_record(image, 0x89, 0xD3, 0x82, memory_payload)
    return bytes(image)


def append_ascii_field(target: bytearray, cursor: int, value: str, length: int) -> int:
    data = value.encode("ascii")[:length].ljust(length, b"\x00")
    target[cursor] = 0xC0 | length
    target[cursor + 1 : cursor + 1 + length] = data
    return cursor + 1 + length


def append_binary_field(target: bytearray, cursor: int, data: bytes) -> int:
    target[cursor] = len(data)
    target[cursor + 1 : cursor + 1 + len(data)] = data
    return cursor + 1 + len(data)


def put_record(image: bytearray, offset: int, record_type: int, format_byte: int, payload: bytes) -> None:
    record_checksum = checksum(payload)
    header_checksum = checksum(bytes([record_type, format_byte, len(payload), record_checksum]))
    image[offset : offset + 5 + len(payload)] = bytes(
        [record_type, format_byte, len(payload), record_checksum, header_checksum]
    ) + payload


def checksum(data: bytes | bytearray) -> int:
    return (-sum(data)) & 0xFF


def refresh_board_checksum(image: bytearray) -> None:
    image[0x67] = 0
    image[0x67] = checksum(image[0x08:0x67])


if __name__ == "__main__":
    unittest.main()
