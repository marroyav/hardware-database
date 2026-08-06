"""Kria K26 SOM EEPROM capture and FRU identity decoding."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ProductionError
from .values import normalize_mac


EEPROM_SIZE_BYTES = 8192
DEFAULT_I2C_ADDRESS = 0x50
K26_PRODUCT_PREFIXES = ("SM-K26-", "SMK-K26")

IPMI_TYPE_BINARY = 0
IPMI_TYPE_ASCII = 3
IPMI_FIELD_END = 0xC1
XILINX_MAC_RECORD_TYPE = 0xD2


@dataclass(frozen=True)
class FruField:
    offset: int
    type_code: int
    length: int
    raw: bytes

    @property
    def text(self) -> str:
        return self.raw.decode("latin-1", errors="replace").rstrip("\x00 ")

    @property
    def hex(self) -> str:
        return self.raw.hex()


@dataclass(frozen=True)
class FruRecord:
    offset: int
    record_type: int
    format_version: int
    end_of_list: bool
    length: int
    record_checksum_valid: bool
    header_checksum_valid: bool
    payload: bytes


def sysfs_eeprom_path(i2c_bus: int, address: int = DEFAULT_I2C_ADDRESS) -> Path:
    return Path(f"/sys/bus/i2c/devices/{i2c_bus}-{address:04x}/eeprom")


def read_eeprom_bytes(path: Path, *, expected_size: int = EEPROM_SIZE_BYTES) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ProductionError(f"cannot read EEPROM data from {path}: {exc}") from exc
    if len(data) < 0x89:
        raise ProductionError(f"EEPROM dump is too small: {len(data)} bytes")
    if expected_size and len(data) != expected_size:
        raise ProductionError(
            f"expected {expected_size} EEPROM bytes from {path}, got {len(data)}"
        )
    return data


def decode_k26_som_eeprom(
    data: bytes,
    *,
    allow_invalid_checksums: bool = False,
) -> dict[str, Any]:
    if len(data) < 0x89:
        raise ProductionError(f"EEPROM dump is too small: {len(data)} bytes")

    header = _parse_common_header(data)
    board = _parse_board_area(data, header["board_area_offset"])
    records = _parse_multirecord_area(data, header["multirecord_area_offset"])

    mac_records = [_decode_mac_record(record) for record in records if record.record_type == XILINX_MAC_RECORD_TYPE]
    mac_records = [record for record in mac_records if record is not None]
    if not mac_records:
        raise ProductionError("no Xilinx MAC address multirecord found in EEPROM")

    manufacturer = _required_text(board["fields"], 0, "board manufacturer")
    product = _required_text(board["fields"], 1, "SOM product")
    serial = _required_text(board["fields"], 2, "SOM serial")
    part_number = _optional_text(board["fields"], 3)
    revision = _find_revision(board["fields"])
    som_uuid = _find_uuid(board["fields"])

    if not product.startswith(K26_PRODUCT_PREFIXES):
        raise ProductionError(f"EEPROM product is not a supported K26 SOM: {product!r}")

    checksum_summary = {
        "common_header": header["checksum_valid"],
        "board_area": board["checksum_valid"],
        "multirecord_headers": all(record.header_checksum_valid for record in records),
        "multirecord_payloads": all(record.record_checksum_valid for record in records),
    }
    fru_checksum_valid = all(checksum_summary.values())
    if not fru_checksum_valid and not allow_invalid_checksums:
        bad = ", ".join(name for name, ok in checksum_summary.items() if not ok)
        raise ProductionError(f"invalid K26 SOM EEPROM checksum(s): {bad}")

    factory_mac = mac_records[0]["mac_ids"][0]
    decoded = {
        "contract": "daphne.k26-som-eeprom",
        "version": 1,
        "eeprom": {
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "fru_checksum_valid": fru_checksum_valid,
            "checksum_summary": checksum_summary,
            "common_header": {
                "format_version": header["format_version"],
                "board_area_offset": f"0x{header['board_area_offset']:02x}",
                "multirecord_area_offset": f"0x{header['multirecord_area_offset']:02x}",
            },
        },
        "som": {
            "uuid": str(som_uuid),
            "serial": serial,
            "product": product,
            "revision": revision,
            "part_number": part_number,
            "manufacturer": manufacturer,
            "factory_mac_id_0": factory_mac,
        },
        "mac_records": mac_records,
        "discover": {
            "som_uuid": str(som_uuid),
            "som_serial": serial,
            "som_product": product,
            "som_revision": revision,
            "factory_mac": factory_mac,
            "fru_checksum_valid": fru_checksum_valid,
            "eeprom_sha256": hashlib.sha256(data).hexdigest(),
        },
    }
    return decoded


def build_discover_args(
    decoded: dict[str, Any],
    *,
    asset_id: str | None = None,
    observed_at: str | None = None,
    operation_id: str | None = None,
    station_id: str | None = None,
    operator: str | None = None,
    evidence_uri: str | None = None,
    evidence_sha256: str | None = None,
) -> list[str]:
    discover = decoded["discover"]
    if not discover["fru_checksum_valid"]:
        raise ProductionError("will not render discover arguments for invalid EEPROM checksums")

    args: list[str] = []
    if asset_id:
        args += ["--asset-id", asset_id]
    args += [
        "--som-uuid",
        discover["som_uuid"],
        "--som-serial",
        discover["som_serial"],
        "--som-product",
        discover["som_product"],
    ]
    if discover.get("som_revision"):
        args += ["--som-revision", discover["som_revision"]]
    args += [
        "--factory-mac",
        discover["factory_mac"],
        "--fru-checksum-valid",
        "--eeprom-sha256",
        discover["eeprom_sha256"],
    ]
    if observed_at:
        args += ["--observed-at", observed_at]
    if operation_id:
        args += ["--operation-id", operation_id]
    if station_id:
        args += ["--station-id", station_id]
    if operator:
        args += ["--operator", operator]
    if evidence_uri:
        args += ["--evidence-uri", evidence_uri]
    if evidence_sha256:
        args += ["--evidence-sha256", evidence_sha256]
    return args


def _parse_common_header(data: bytes) -> dict[str, Any]:
    if len(data) < 8:
        raise ProductionError("EEPROM dump is missing the IPMI common header")
    header = data[:8]
    format_version = header[0] & 0x0F
    if format_version != 1:
        raise ProductionError(f"unsupported IPMI FRU common header version: {format_version}")
    board_offset = header[3] * 8
    multirecord_offset = header[5] * 8
    if board_offset <= 0 or board_offset >= len(data):
        raise ProductionError(f"invalid board area offset: 0x{board_offset:x}")
    if multirecord_offset <= 0 or multirecord_offset >= len(data):
        raise ProductionError(f"invalid multirecord area offset: 0x{multirecord_offset:x}")
    return {
        "format_version": format_version,
        "board_area_offset": board_offset,
        "multirecord_area_offset": multirecord_offset,
        "checksum_valid": _checksum_ok(header),
    }


def _parse_board_area(data: bytes, offset: int) -> dict[str, Any]:
    if offset + 6 > len(data):
        raise ProductionError("EEPROM dump is missing the board information area")
    area_length = data[offset + 1] * 8
    if area_length < 8 or offset + area_length > len(data):
        raise ProductionError(f"invalid board information area length: {area_length}")
    area = data[offset : offset + area_length]
    fields = _parse_board_fields(area, offset)
    return {
        "offset": offset,
        "length": area_length,
        "checksum_valid": _checksum_ok(area),
        "fields": fields,
    }


def _parse_board_fields(area: bytes, base_offset: int) -> list[FruField]:
    fields: list[FruField] = []
    index = 6
    checksum_index = len(area) - 1
    while index < checksum_index:
        type_length = area[index]
        if type_length == IPMI_FIELD_END:
            return fields
        type_code = type_length >> 6
        length = type_length & 0x3F
        start = index + 1
        end = start + length
        if end > checksum_index:
            raise ProductionError(
                f"FRU board field at 0x{base_offset + index:02x} exceeds board area"
            )
        fields.append(
            FruField(
                offset=base_offset + start,
                type_code=type_code,
                length=length,
                raw=area[start:end],
            )
        )
        index = end
    raise ProductionError("FRU board information area is missing the end marker")


def _parse_multirecord_area(data: bytes, offset: int) -> list[FruRecord]:
    records: list[FruRecord] = []
    cursor = offset
    for _ in range(64):
        if cursor + 5 > len(data):
            raise ProductionError("EEPROM dump ends inside a multirecord header")
        if data[cursor] in (0x00, 0xFF):
            k26_v1_memory_offset = 0x9B
            if cursor < k26_v1_memory_offset and data[k26_v1_memory_offset] not in (0x00, 0xFF):
                cursor = k26_v1_memory_offset
            else:
                return records
        record_type = data[cursor]
        format_byte = data[cursor + 1]
        length = data[cursor + 2]
        record_checksum = data[cursor + 3]
        header_checksum = data[cursor + 4]
        payload_start = cursor + 5
        payload_end = payload_start + length
        if payload_end > len(data):
            raise ProductionError(f"multirecord at 0x{cursor:02x} exceeds EEPROM dump")
        payload = data[payload_start:payload_end]
        records.append(
            FruRecord(
                offset=cursor,
                record_type=record_type,
                format_version=format_byte & 0x0F,
                end_of_list=bool(format_byte & 0x80),
                length=length,
                record_checksum_valid=((sum(payload) + record_checksum) & 0xFF) == 0,
                header_checksum_valid=((record_type + format_byte + length + record_checksum + header_checksum) & 0xFF)
                == 0,
                payload=payload,
            )
        )
        cursor = payload_end
        if records[-1].end_of_list:
            return records
    raise ProductionError("multirecord area did not terminate after 64 records")


def _decode_mac_record(record: FruRecord) -> dict[str, Any] | None:
    if record.length < 10:
        return None
    payload = record.payload
    mac_payload = payload[4:]
    mac_count = len(mac_payload) // 6
    if mac_count < 1:
        return None
    macs = [normalize_mac(mac_payload[index * 6 : index * 6 + 6].hex()) for index in range(mac_count)]
    return {
        "offset": f"0x{record.offset:02x}",
        "iana_id": payload[:3].hex(),
        "version": f"0x{payload[3]:02x}",
        "mac_ids": macs,
        "record_checksum_valid": record.record_checksum_valid,
        "header_checksum_valid": record.header_checksum_valid,
    }


def _required_text(fields: list[FruField], index: int, label: str) -> str:
    value = _optional_text(fields, index)
    if not value:
        raise ProductionError(f"missing {label} in EEPROM board information area")
    return value


def _optional_text(fields: list[FruField], index: int) -> str | None:
    if index >= len(fields):
        return None
    field = fields[index]
    if field.type_code != IPMI_TYPE_ASCII:
        return None
    return field.text or None


def _find_revision(fields: list[FruField]) -> str | None:
    for field in fields:
        if field.offset == 0x44 and field.type_code == IPMI_TYPE_ASCII:
            return field.text or None
    for field in fields[4:]:
        if field.type_code == IPMI_TYPE_ASCII and 0 < field.length <= 8:
            return field.text or None
    return None


def _find_uuid(fields: list[FruField]) -> uuid.UUID:
    for field in fields:
        if field.offset == 0x56 and field.type_code == IPMI_TYPE_BINARY and field.length == 16:
            try:
                return uuid.UUID(bytes=field.raw)
            except ValueError as exc:
                raise ProductionError("invalid binary UUID in EEPROM") from exc
    for field in fields[4:]:
        if field.type_code == IPMI_TYPE_BINARY and field.length == 16:
            try:
                return uuid.UUID(bytes=field.raw)
            except ValueError as exc:
                raise ProductionError("invalid binary UUID in EEPROM") from exc
    raise ProductionError("missing K26 SOM UUID in EEPROM board information area")


def _checksum_ok(data: bytes) -> bool:
    return (sum(data) & 0xFF) == 0
