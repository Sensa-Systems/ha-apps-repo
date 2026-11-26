"""Canonical deployment identifiers, shared by the licence claim + the add-on.

There are THREE values, used by both concepts:

    byo        -> the customer's own Home Assistant (self-install)
    enthusiast -> a Sensa-supplied Home Assistant OS (HAOS) appliance
    appliance  -> a Sensa-supplied Container appliance

* The LICENCE carries a ``deployment_type`` claim = which product was SOLD.
* The add-on option ``deployment_mode`` = how this box was INSTALLED.

They should match; a mismatch is the anti-arbitrage case (e.g. a cheap ``byo``
licence running on a Sensa appliance). The device, the Hub and the licensing
Function all key off these EXACT strings, so keep them byte-for-byte identical
across the three repos.
"""

from __future__ import annotations

DEPLOYMENT_BYO = "byo"
DEPLOYMENT_ENTHUSIAST = "enthusiast"
DEPLOYMENT_APPLIANCE = "appliance"

DEPLOYMENT_TYPES = (DEPLOYMENT_BYO, DEPLOYMENT_ENTHUSIAST, DEPLOYMENT_APPLIANCE)

# The Sensa-appliance deployments (everything that is not a self-install).
APPLIANCE_DEPLOYMENTS = (DEPLOYMENT_ENTHUSIAST, DEPLOYMENT_APPLIANCE)


def normalise_deployment(value: str | None) -> str:
    """Lowercase/strip; return the value if it's a known deployment, else ``""``."""
    key = (value or "").strip().lower()
    return key if key in DEPLOYMENT_TYPES else ""


def is_appliance_deployment(value: str | None) -> bool:
    """True for a Sensa appliance (``enthusiast``/``appliance``); False for
    ``byo`` or an unknown/empty value."""
    return normalise_deployment(value) in APPLIANCE_DEPLOYMENTS
