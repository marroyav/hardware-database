"""Normalization and canonical serialization for production records."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .errors import ProductionError


HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.?$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ProductionError(f"timestamp must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ProductionError(f"invalid UUID: {value!r}") from exc


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(compact) != 12 or not re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        raise ProductionError(f"invalid MAC address: {value!r}")
    octets = bytes.fromhex(compact)
    if octets in {b"\x00" * 6, b"\xff" * 6}:
        raise ProductionError(f"invalid all-zero/all-ff MAC address: {value!r}")
    if octets[0] & 1:
        raise ProductionError(f"multicast MAC address is not valid: {value!r}")
    return ":".join(f"{part:02x}" for part in octets)


def normalize_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ProductionError(f"invalid IP address: {value!r}") from exc
    if address.version != 4:
        raise ProductionError(f"expected an IPv4 address: {value!r}")
    return str(address)


def normalize_hostname(value: str) -> str:
    hostname = value.strip().rstrip(".").lower()
    if not HOSTNAME_RE.fullmatch(hostname):
        raise ProductionError(f"invalid hostname: {value!r}")
    return hostname


def normalize_sha256(value: str, label: str = "SHA-256") -> str:
    digest = value.strip().lower()
    if not SHA256_RE.fullmatch(digest):
        raise ProductionError(f"invalid {label}: {value!r}")
    return digest


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
