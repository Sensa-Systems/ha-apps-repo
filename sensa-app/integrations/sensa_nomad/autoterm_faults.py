"""Autoterm heater fault codes: the one code-to-text table.

The controller firmware publishes the heater's fault code raw ("Fault Code",
0 = no fault); this table turns it into the words the customer reads in the
log and the diagnostics popup. Kept here, not in the firmware, so a wording
fix needs no reflash and the firmware and the log can never disagree.

The table itself is `autoterm_faults.json` beside this file: per code a name,
Autoterm's own wording, a description and Autoterm's troubleshooting steps.
Source: the manufacturer's diagnostic tool tables recorded in
docs/devices/autoterm/autoterm-uart-protocol.md "Fault code tables". Three
tables matter to what we ship: the Air 2D / 4D group (our Air D controller),
the Air 9D group (our Air 9D controller) and the Flow group (the Flow 5D and
the Baltica, hydronic heaters). A family that lists the heater's model wins;
otherwise the service class picks, since that tells an air heater from a
hydronic one on a device row.
"""

from __future__ import annotations

import json
import os
from typing import Any

FAULTS_FILE = "autoterm_faults.json"


def _load_families() -> dict[str, dict[str, Any]]:
    """The families from the JSON, or none if the file is unreadable.

    Never raises: this is imported at integration setup, and a malformed JSON
    must not take Nomad down. With no table every code still reads as
    "Unrecognised fault (code N)", which support can quote.
    """
    path = os.path.join(os.path.dirname(__file__), FAULTS_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            families = json.load(handle)["families"]
        return families if isinstance(families, dict) else {}
    except Exception:  # noqa: BLE001 - a bad file must not break setup
        return {}


FAMILIES = _load_families()


def _faults_for(service_class: str | None, model: str | None = None) -> dict[str, dict[str, Any]]:
    if model:
        for family in FAMILIES.values():
            if model in (family.get("models") or []):
                return family.get("faults") or {}
    family = "flow" if service_class == "hydronic_heater" else "air"
    return (FAMILIES.get(family) or {}).get("faults") or {}


def fault_info(service_class: str | None, code: int, model: str | None = None) -> dict[str, Any]:
    """Name, description and troubleshooting steps for one fault code. An
    unlisted code still names its number, so the customer (and support) has
    something to quote, never a blank."""
    entry = _faults_for(service_class, model).get(str(code)) or {}
    return {
        "name": entry.get("name") or f"Unrecognised fault (code {code})",
        "description": entry.get("description") or "",
        "troubleshooting": list(entry.get("troubleshooting") or []),
    }


def describe_fault(service_class: str | None, code: int, model: str | None = None) -> str:
    """Words for one fault code."""
    return fault_info(service_class, code, model)["name"]
