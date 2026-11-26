"""Physical device catalog for Sensa Nomad.

Loads the catalog from `device_catalog.json` in this directory so product
metadata (image filenames, vendor model strings, etc.) can be edited without
touching Python. The JSON is the source of truth; this module exposes it as
a plain list for the rest of the integration.

Each entry carries a `service_class` — the canonical, vendor-neutral device
kind (see `vendors/service_classes.py`). `service_class` is the source of
truth for *what kind of thing* a device is; the per-entry `category` is the
coarse view-routing bucket it rolls up into and is **derivable** from the
service class via `service_classes.category_of()`. `category` is kept on the
entries for now because the Discovery picker still reads it directly; new
code should prefer `service_class` + `category_of()`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# View-routing buckets. `electrical` / `switching` / `network` / `environment`
# back the existing dashboard views; `water` is defined in the service-class
# registry for a future Water view but has no catalog device yet. `vehicle`
# backs the levelling sensor (Van Level Pro 6) — discovered + categorised here,
# its dashboard surface is a later slice.
CATEGORY_ORDER: list[str] = [
    "electrical",
    "switching",
    "network",
    "environment",
    "vehicle",
]

# Display title per category. Covers every category a SERVICE CLASS can carry,
# not just the CATEGORY_ORDER discovery buckets above, because the Routines
# picker groups its entity list under these words and a customer must never see
# the slug. A new category in `vendors/service_classes.py` needs a title here.
CATEGORY_TITLES: dict[str, str] = {
    "electrical": "Electrical",
    "switching": "Switching",
    "network": "Network",
    "environment": "Environment",
    "vehicle": "Vehicle",
    "lighting": "Lighting",
    "climate": "Climate",
    "water": "Water",
    "security": "Security",
    "media": "Media",
    "display": "Display",
    "settings": "Settings",
    "presence": "Presence",
}


def _load_catalog() -> list[dict[str, Any]]:
    path = Path(__file__).with_name("device_catalog.json")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


PHYSICAL_DEVICE_CATALOG: list[dict[str, Any]] = _load_catalog()


def model_slug_for(entry: dict[str, Any], detected_model: str) -> str:
    """Resolve a row's detected model to one of this entry's model slugs.

    `detected_model` arrives in two shapes depending on where the row came
    from: the classifier's own DISPLAY string ("Roamer 12V 200Ah Xtreme
    (#3655)", "DJI Power 1000 V2") on a live discovery row, or the slug
    itself on a backfilled one. Both resolve here so no caller has to know
    which it is holding.

    Display matching is longest-substring, so a more specific label wins when
    several are substrings of the same name. Returns "" when nothing matches.
    """
    detection = entry.get("model_detection") or {}
    display_names: dict[str, str] = detection.get("display_names") or {}
    model = str(detected_model or "").strip()
    if not model or not display_names:
        return ""
    if model in display_names:
        return model
    haystack = model.lower()
    best_slug = ""
    best_len = 0
    for slug, label in display_names.items():
        if label and label.lower() in haystack and len(label) > best_len:
            best_slug = slug
            best_len = len(label)
    return best_slug


def catalog_verified(entry: dict[str, Any], detected_model: str = "") -> bool:
    """True when this device has been proven against the real hardware.

    Everything starts unverified and stays that way until someone has run it
    against the physical device: being in the catalog means the code exists,
    not that it has ever been seen to work. The picker badges an unverified
    row so neither a customer nor a dealer reads the one for the other.

    Verification is per MODEL, not per catalog entry, because one entry can
    span a whole product range: the single DJI Power entry covers five
    models and only the 1000 V2 has been on the bench. So an entry with
    `model_detection` carries `model_detection.verified`, a flag per model
    slug (the same keys as `display_names` / `model_images`); an entry with
    no model variants carries a plain top-level `verified`.

    A model we cannot resolve falls back to the ENTRY: verified if ANY of its
    models is. Before a device is added there is often nothing to resolve (a
    Starlink only reports its generation once it has been polled), and badging
    a tested product Unverified because the row cannot yet say which variant
    it is reads as a bug, not as caution. Once the model IS known the answer
    is exact, so an untested variant still badges after the add.

    A missing flag and a model we recognise but have not tested both read as
    unverified.

    Independent of `experimental`, which decides whether an entry is VISIBLE
    at all (hidden until the Experimental Devices switch reveals it). A
    verified device can still be experimental, and vice versa.
    """
    by_model = (entry.get("model_detection") or {}).get("verified")
    if isinstance(by_model, dict):
        slug = model_slug_for(entry, detected_model)
        if not slug:
            return any(v is True for v in by_model.values())
        return by_model.get(slug) is True
    return entry.get("verified") is True


# How many relay channels a relay controller has when its catalog entry does
# not say. The Sensa controllers are 8-channel boards and predate the field.
RELAY_CHANNELS_DEFAULT = 8


def firmware_code_for(device: dict[str, Any]) -> str:
    """The firmware build code for a device row, or "" when Sensa builds none.

    The code is the DEVICE name in the firmware pipeline
    (`Sensa.Control.Firmware/pipelines/templates/build-channel.yml`), which is also
    its folder in the firmware blob container (`<code>/manifest.json`). Declared in
    the catalog per entry, so the one place a device's facts live is the one place
    that says which firmware it runs.

    This doubles as the "is this OURS" test, and it has to, because `esphome` alone
    is not one: the catalog also carries third-party ESPHome kit (Home Assistant
    Voice PE). A device only reports Sensa firmware if Sensa builds firmware for it.

    Matched by `catalog_id` where the row carries one, then by the model, which is
    the `vendor_model` under the name the registry writes (`model`). A registered
    row does NOT carry `catalog_id` - checked against a live van, 2026-09-27, where
    the relay controllers carry `model: rly-8di-8ro-e` and no catalog id at all - so
    the model match is the one that usually answers.
    """
    catalog_id = str(device.get("catalog_id") or "").strip()
    if catalog_id:
        for entry in PHYSICAL_DEVICE_CATALOG:
            if entry.get("id") == catalog_id:
                return str(entry.get("firmware_code") or "")
    model = str(
        device.get("vendor_model") or device.get("model") or ""
    ).strip().lower()
    if model:
        for entry in PHYSICAL_DEVICE_CATALOG:
            if str(entry.get("vendor_model") or "").lower() == model:
                return str(entry.get("firmware_code") or "")
    return ""


def catalog_id_for(device: dict[str, Any]) -> str:
    """The catalog entry id a registered device row IS, or "".

    For the diagnostics inventory, which Hub counts per catalog entry. A
    registered row almost never carries `catalog_id` (empty on every van in
    prod telemetry, 2026-10-23), so every Supported devices count in Hub Admin
    read zero. Most specific signal first, and never a guess: a key that names
    more than one entry answers nothing rather than crediting the wrong
    product.

    1. `catalog_id` on the row.
    2. The model against `vendor_model`, narrowed by `device_subtype` where two
       entries share one (Teltonika router / switch are both `teltonika`).
    3. `bms_family` (Fogstar `jbd`, Roamer Gen 2 `jk`, ...).
    4. The Tuya `manufacturerName` (Z2M reports it as the vendor) against
       `discovery.zigbee.manufacturer_names`: a TS0601 whose batch code Z2M
       does not know (the Gledopto `_TZE204_8fffc3kb`) has no other identity.
    5. The integration with the saved type (`role` / `device_type` /
       `device_subtype` against `vendor_device_type`), then the integration
       alone, each only when exactly one entry fits.
    """
    catalog_id = str(device.get("catalog_id") or "").strip()
    if catalog_id:
        return catalog_id
    config = device.get("config") or {}

    def one(matches: list[dict[str, Any]]) -> str:
        return str(matches[0].get("id") or "") if len(matches) == 1 else ""

    def low(value: Any) -> str:
        return str(value or "").strip().lower()

    subtype = low(device.get("device_subtype") or config.get("device_subtype"))
    model = low(device.get("vendor_model") or device.get("model"))
    if model:
        on_model = [
            e for e in PHYSICAL_DEVICE_CATALOG if low(e.get("vendor_model")) == model
        ]
        if len(on_model) > 1 and subtype:
            on_model = [
                e for e in on_model if low(e.get("vendor_device_type")) == subtype
            ]
        if found := one(on_model):
            return found

    family = low(device.get("bms_family") or config.get("bms_family"))
    if family and (
        found := one(
            [e for e in PHYSICAL_DEVICE_CATALOG if low(e.get("bms_family")) == family]
        )
    ):
        return found

    vendor = low(device.get("vendor"))
    if vendor and (
        found := one(
            [
                e
                for e in PHYSICAL_DEVICE_CATALOG
                if vendor
                in {
                    low(n)
                    for n in ((e.get("discovery") or {}).get("zigbee") or {}).get(
                        "manufacturer_names"
                    )
                    or []
                }
            ]
        )
    ):
        return found

    integration = low(device.get("integration_type") or device.get("integration"))
    if not integration:
        return ""
    on_integration = [
        e for e in PHYSICAL_DEVICE_CATALOG if low(e.get("integration")) == integration
    ]
    kinds = {
        low(device.get("role")),
        low(device.get("device_type")),
        subtype,
    } - {"", "unknown"}
    if kinds and (
        found := one(
            [e for e in on_integration if low(e.get("vendor_device_type")) in kinds]
        )
    ):
        return found
    return one(on_integration)


def relay_catalog_entry(device: dict[str, Any]) -> dict[str, Any]:
    """The `service_class: relay` catalog entry backing a device row, or {}.

    Matched by `catalog_id` where the row carries one, then by the
    `vendor_model` its `device_id` is built from (`shelly-relay-<mac>`), then
    by integration. A registered row does NOT always carry `catalog_id`: the
    discovery sensor's `_render_rows` projection drops it, so the device_id
    prefix is the one that usually answers for an adopted device.
    """
    catalog_id = str(device.get("catalog_id") or "").strip()
    device_id = str(device.get("device_id") or device.get("id") or "").strip().lower()
    integration = str(
        device.get("integration_type") or device.get("integration") or ""
    ).strip().lower()

    relays = [
        e for e in PHYSICAL_DEVICE_CATALOG if e.get("service_class") == "relay"
    ]
    if catalog_id:
        for entry in relays:
            if entry.get("id") == catalog_id:
                return entry
    if device_id:
        for entry in relays:
            vendor_model = str(entry.get("vendor_model") or "").lower()
            if vendor_model and device_id.startswith(f"{vendor_model}-"):
                return entry
    if integration:
        for entry in relays:
            if str(entry.get("integration") or "").lower() == integration:
                return entry
    return {}


def relay_channel_count(device: dict[str, Any]) -> int:
    """How many channels this relay device actually has.

    The ONE answer: the entity builders (name / image / enabled / assignment)
    and the Settings card all ask here, so a device cannot grow entities for
    channels its card will never show. Everything used to hard-code 8, which
    gave an adopted Shelly Pro 4PM eight rows for its four relays (customer
    box, 2026-09-23).

    Clamped to 1..`RELAY_CHANNELS_DEFAULT`: a catalog typo must not create a
    hundred entities, and it must never create zero.
    """
    raw = relay_catalog_entry(device).get("relay_channels")
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return RELAY_CHANNELS_DEFAULT
    return max(1, min(count, RELAY_CHANNELS_DEFAULT))
