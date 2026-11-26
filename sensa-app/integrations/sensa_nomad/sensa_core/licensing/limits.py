"""Per-edition device caps.

The number of confirmed devices a licence may register is a function of its
Sensa Control edition (``editions["nomad"]``):

    base   -> 50
    pro    -> 100
    vision -> unlimited
    ai -> unlimited

(Raised from 30 / 50 on 2026-10-25.) An explicit ``max_devices`` claim on the
licence can only RAISE the cap above the edition default -- an operator
goodwill bump for a specific customer. It never lowers it, because every
licence issued before the raise carries the OLD default as an explicit claim,
signed into a key that is never re-issued; honouring that number as an
override would leave every van sold before the change on the old cap.
``None`` means "use the edition default". An unknown
or absent edition falls back to Base, matching the dashboard's tier default
(see ``layout_catalog.py``).

**Legacy identifiers.** The edition identifiers were originally ``lite`` /
``plus``, renamed to ``base`` / ``pro`` to match the customer-facing names. A
signed JWT can never be re-issued, so every licence in the field still carries
the old identifier and always will until it is replaced. Both spellings are
therefore accepted forever: ``normalise_tier`` folds the legacy pair onto the
canonical one, and every tier comparison in the codebase goes through it. Do
not drop the aliases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sensa_core.licensing.models import LicenceClaims

TIER_BASE = "base"
TIER_PRO = "pro"
TIER_VISION = "vision"
# Vision plus on-device conversation (business-plan.md): a superset of Vision,
# never instead of it, so every Vision gate is an AI gate too.
TIER_AI = "ai"

# Retired identifiers, still carried by every licence issued before the rename.
TIER_LITE = "lite"
TIER_PLUS = "plus"

_TIER_ALIASES: dict[str, str] = {
    TIER_LITE: TIER_BASE,
    TIER_PLUS: TIER_PRO,
}

# None = unlimited.
_EDITION_DEVICE_CAPS: dict[str, int | None] = {
    TIER_BASE: 50,
    TIER_PRO: 100,
    TIER_VISION: None,
    TIER_AI: None,
}


def normalise_tier(tier: str | None) -> str:
    """Canonical identifier for an edition tier, folding the legacy names.

    ``lite`` -> ``base``, ``plus`` -> ``pro``; anything else is returned
    lowercased and stripped, unchanged. An unknown tier is NOT coerced to a
    known one — callers decide what an unrecognised tier means for them (the
    device-cap and tier-gating paths both treat it as Base).
    """
    key = (tier or "").strip().lower()
    return _TIER_ALIASES.get(key, key)


def edition_device_cap(tier: str | None) -> int | None:
    """Default device cap for an edition. ``None`` = unlimited.

    An unknown / absent / empty edition falls back to the Base cap so an
    unresolved licence is never accidentally treated as unlimited.
    """
    key = normalise_tier(tier)
    if key in _EDITION_DEVICE_CAPS:
        return _EDITION_DEVICE_CAPS[key]
    return _EDITION_DEVICE_CAPS[TIER_BASE]


def resolve_device_cap(claims: LicenceClaims) -> int | None:
    """Resolve the device cap for a licence. ``None`` = unlimited.

    The higher of the explicit ``max_devices`` claim and the edition default
    (see the module docstring for why a claim cannot lower it). An unlimited
    edition stays unlimited whatever the claim says.
    """
    default = edition_device_cap(claims.editions.get("nomad"))
    if default is None or claims.max_devices is None:
        return default
    return max(claims.max_devices, default)
