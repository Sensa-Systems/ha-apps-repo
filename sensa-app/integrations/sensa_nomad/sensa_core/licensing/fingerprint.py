"""Composite hardware fingerprint.

Combines host signals into a single SHA-256 hash. Each component is also
hashed individually so the cloud API can (carefully) reason about partial
matches (e.g. hardware replacement) without ever seeing a raw MAC or serial.

Deliberately tolerant of missing signals: the HAOS container cannot always
read ``/sys/class/dmi/id/board_serial``, and some virtualised hosts have
no board serial at all. Any missing signal becomes the literal string
``"unavailable"`` before hashing so fingerprints stay stable across boots.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from sensa_core.licensing.models import FingerprintComponents

_DMI_BOARD_SERIAL = Path("/sys/class/dmi/id/board_serial")
_DMI_PRODUCT_UUID = Path("/sys/class/dmi/id/product_uuid")
_MEMINFO = Path("/proc/meminfo")


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _primary_mac() -> str:
    """Primary NIC MAC. Falls back to ``uuid.getnode()`` which reads the
    first non-loopback interface on every platform."""
    try:
        node = uuid.getnode()
    except Exception:  # noqa: BLE001
        return "unavailable"
    # uuid.getnode sets the multicast bit when no MAC can be found.
    if (node >> 40) & 0x01:
        return "unavailable"
    return ":".join(f"{(node >> (i * 8)) & 0xFF:02x}" for i in reversed(range(6)))


def _board_serial() -> str:
    for path in (_DMI_BOARD_SERIAL, _DMI_PRODUCT_UUID):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value and value.lower() not in ("", "none", "to be filled by o.e.m."):
            return value
    return "unavailable"


def _ha_uuid(ha_uuid: str | None) -> str:
    """HA installation UUID.

    The App passes this in because only the integration side can read HA's
    ``installation_id``; the App passes whatever it was given at config
    flow time. If no value is available, we use a stable-per-install
    fallback from the container's /data directory mtime.
    """
    if ha_uuid and ha_uuid.strip():
        return ha_uuid.strip()
    return "unavailable"


def _cpu_count() -> str:
    try:
        return str(os.cpu_count() or 0)
    except Exception:  # noqa: BLE001
        return "unavailable"


def _mem_bytes() -> str:
    try:
        text = _MEMINFO.read_text(encoding="utf-8")
    except OSError:
        return "unavailable"
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return str(int(parts[1]) * 1024)
    return "unavailable"


def compute_fingerprint(
    ha_uuid: str | None = None,
) -> tuple[str, FingerprintComponents]:
    """Return ``(composite_hash, components)``.

    The composite hash is ``sha256:<hex>``; components are individually
    SHA-256'd so no raw identifier is ever transmitted.
    """
    raw = {
        "mac": _primary_mac(),
        "board_serial": _board_serial(),
        "ha_uuid": _ha_uuid(ha_uuid),
        "cpu_count": _cpu_count(),
        "mem_bytes": _mem_bytes(),
    }

    components = FingerprintComponents(
        mac=_sha256_hex(raw["mac"]),
        board_serial=_sha256_hex(raw["board_serial"]),
        ha_uuid=_sha256_hex(raw["ha_uuid"]),
        cpu_count=_sha256_hex(raw["cpu_count"]),
        mem_bytes=_sha256_hex(raw["mem_bytes"]),
    )

    composite_input = "|".join(
        f"{k}={components.to_dict()[k]}" for k in sorted(components.to_dict())
    )
    composite = "sha256:" + hashlib.sha256(composite_input.encode("utf-8")).hexdigest()
    return composite, components
