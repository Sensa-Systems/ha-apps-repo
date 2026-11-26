"""Pure logic for the discovery picker's "Ignored" list with Cerbo ownership.

A device lands in the picker's Ignored group for one of two reasons:

  * **MANUAL** — the customer ticked it and ignored it themselves.
  * **CERBO**  — Sensa auto-ignored it because a Victron Cerbo GX reports the
                 same physical device over MQTT, so the standalone Bluetooth
                 advert is a duplicate. The ignore is **tagged with the owning
                 Cerbo's device_id**; removing that Cerbo restores the device.

Keeping the two reasons separate is what fixes the original bug (Cerbo-created
ignores never cleaned up) WITHOUT clobbering the customer's own manual ignores,
and lets two Cerbos that both cover a device keep it ignored until both are gone.

State shape (held in runtime and persisted via HA `Store`):

    {
        "manual": set[str],             # MAC/id (UPPER) the customer ignored
        "cerbo":  dict[str, set[str]],  # MAC/id (UPPER) -> {owning cerbo device_id}
    }

The EFFECTIVE ignored set the picker reads is
``manual ∪ {mac : cerbo[mac] is non-empty}``.

This module is deliberately free of Home Assistant imports so the behaviour is
unit-testable in isolation. MAC/id keys are upper-cased on the way in to match
the discovery sensor's ``row_id.upper() in ignored_device_ids`` check.
"""
from __future__ import annotations

from typing import Any


def new_state() -> dict[str, Any]:
    """A fresh, empty ignore state."""
    return {"manual": set(), "cerbo": {}}


def migrate(loaded: Any) -> dict[str, Any]:
    """Load persisted data into state, accepting the legacy formats.

    Legacy stores were ``{"ids": [...]}`` (or older ``{"macs": [...]}``) — every
    entry there was a manual ignore, so it migrates into ``manual``.
    """
    state = new_state()
    if not isinstance(loaded, dict):
        return state
    legacy = loaded.get("ids")
    if legacy is None:
        legacy = loaded.get("macs")
    if isinstance(legacy, (list, set, tuple)):
        state["manual"] = {str(x).upper() for x in legacy}
    manual = loaded.get("manual")
    if isinstance(manual, (list, set, tuple)):
        state["manual"] = {str(x).upper() for x in manual}
    cerbo = loaded.get("cerbo")
    if isinstance(cerbo, dict):
        for mac, owners in cerbo.items():
            if isinstance(owners, (list, set, tuple)):
                owner_set = {str(o) for o in owners if o}
                if owner_set:
                    state["cerbo"][str(mac).upper()] = owner_set
    return state


def serialize(state: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe snapshot for the HA `Store` (sets → sorted lists)."""
    return {
        "manual": sorted(state["manual"]),
        "cerbo": {
            mac: sorted(owners)
            for mac, owners in state["cerbo"].items()
            if owners
        },
    }


def effective(state: dict[str, Any]) -> set[str]:
    """The set of MAC/ids the picker should show as ignored."""
    out = set(state["manual"])
    for mac, owners in state["cerbo"].items():
        if owners:
            out.add(mac)
    return out


def add_manual(state: dict[str, Any], mac: str) -> None:
    """Customer manually ignored a device."""
    state["manual"].add(str(mac).upper())


def remove(state: dict[str, Any], mac: str) -> None:
    """Customer manually restored a device — full restore.

    Drops it from `manual` AND from all Cerbo ownership: an explicit restore
    wins over an auto-ignore (otherwise the device would snap straight back to
    Ignored while the Cerbo is still present).
    """
    mac = str(mac).upper()
    state["manual"].discard(mac)
    state["cerbo"].pop(mac, None)


def add_cerbo(state: dict[str, Any], mac: str, cerbo_id: str) -> bool:
    """Auto-ignore `mac` on behalf of Cerbo `cerbo_id`.

    Returns True if this newly added the Cerbo as an owner (i.e. the caller
    should persist + refresh), False if it was already owned by that Cerbo.
    """
    mac = str(mac).upper()
    owners = state["cerbo"].setdefault(mac, set())
    if cerbo_id in owners:
        return False
    owners.add(cerbo_id)
    return True


def remove_cerbo_owner(state: dict[str, Any], cerbo_id: str) -> list[str]:
    """A Cerbo was removed — drop its ownership everywhere.

    Returns the MAC/ids that are now fully restored (no remaining Cerbo owner
    AND not also manually ignored), so the caller can log / refresh.
    """
    restored: list[str] = []
    for mac, owners in list(state["cerbo"].items()):
        if cerbo_id in owners:
            owners.discard(cerbo_id)
            if not owners:
                del state["cerbo"][mac]
                if mac not in state["manual"]:
                    restored.append(mac)
    return restored


def match_ble_ignores(
    product_counts: dict[int, int],
    ble_devices: list[tuple[str, int | None]],
) -> list[str]:
    """Decide which BLE MACs a Cerbo covers — count-safe, never over-hides.

    `product_counts`: ``{product_id: count}`` of the devices wired to the Cerbo
        (from `VictronCoordinator.connected_product_ids`). A Victron `ProductId`
        is the same 16-bit number as the BLE advert's `model_id`.
    `ble_devices`: ``[(mac, model_id), …]`` for the discovered Victron BLE rows.

    Rule per product number:
      * Cerbo doesn't report it → ignore none (it can't be on this Cerbo).
      * BLE count ≤ Cerbo count → ignore them all (they're accounted for).
      * BLE count  > Cerbo count → ignore NONE of that model: there are more
        standalone adverts than the Cerbo has of that model and — pre-add, the
        advert is encrypted so there's no serial/live-data to tell them apart —
        we must not risk hiding the standalone one.
    """
    by_model: dict[int, list[str]] = {}
    for mac, model_id in ble_devices:
        if model_id is None or not mac:
            continue
        by_model.setdefault(model_id, []).append(str(mac).upper())

    out: list[str] = []
    for model_id, macs in by_model.items():
        cerbo_count = product_counts.get(model_id, 0)
        if cerbo_count and len(macs) <= cerbo_count:
            out.extend(macs)
    return out
