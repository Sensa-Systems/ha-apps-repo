"""Sensa BMS-BLE vendor module.

Generic BLE Battery Management System coordinator + entity factory. Uses
`aiobmsble` parser classes (JK, JBD, Seplos, Daly, Ant, etc.) for standard
families; dispatches by `bms_family` set on each registered device. Additional
families (e.g. Roamer Gen1 VIP) can be added by subclassing `aiobmsble.BMS`
and registering in `BMS_FAMILIES`.

One coordinator per battery — each battery is its own MAC, holds its own GATT
connection via the HA bluetooth integration (which transparently routes through
a Sensa Bluetooth Proxy when the Pi's radio can't reach the battery).
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import timedelta
from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=30)

# Entity domains a scene can actually actuate. `get_mode_defaults` filters its
# contributions to these so read-only flags (today the charge / discharge
# MOSFET binary_sensors) never land in a scene the customer can't edit.
_SETTABLE_SCENE_DOMAINS = frozenset({
    "switch", "light", "fan", "climate", "cover", "lock", "number",
    "select", "input_boolean", "input_number", "input_select", "humidifier",
    "siren", "valve", "water_heater", "media_player",
})


def get_mode_defaults(
    mode_slug: str, role_map: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Per-mode entity-state contributions for Roamer Gen 1 + Gen 2 packs.

    Called by `modes._build_scenes_dict` at scene-YAML regen time with
    the mode slug and the `{role: entity_id}` map resolved from HA's
    entity registry. Returns `{entity_id: {state, attributes}}`.

    BMS packs don't have a lot of directly "set-able" entities: the
    BMS chipset's own firmware owns charge / discharge / balance
    decisions. The two flags we can express intent on are the charge
    and discharge MOSFET allow/deny lines (role-map keys
    `charging_allowed` / `discharging_allowed`).

    On today's packs these resolve to READ-ONLY `binary_sensor.*`
    entities. We deliberately do NOT emit those into scenes: HA can't
    actuate a read-only entity, so it only clutters the Scene editor
    with rows the customer can't remove (the scene is re-merged from
    this hook on every regen, so a delete never sticks). The final
    `_SETTABLE_SCENE_DOMAINS` filter drops them. The mode intent below
    is still computed, so the moment a future firmware / hardware tier
    exposes a settable `switch.*` for these lines the contribution
    lights up automatically with no change here.
    """
    # role_map keys are slugified `original_name` (how HA builds entity_ids
    # for `has_entity_name=True` entities). For BMS:
    #   "Charging Allowed"    → charging_allowed
    #   "Discharging Allowed" → discharging_allowed
    # Use those as the lookup keys, not the aiobmsble sample-field
    # names (chrg_mosfet, dischrg_mosfet) that the entity factory
    # consumes internally.
    contrib: dict[str, dict[str, Any]] = {}
    chrg = role_map.get("charging_allowed")
    dischrg = role_map.get("discharging_allowed")

    if mode_slug == "sleep":
        # Overnight — freeze charge activity so the pack can balance
        # and cool. Discharge stays allowed so essentials keep running.
        if chrg:
            contrib[chrg] = {"state": "off"}
    elif mode_slug == "service":
        # Maintenance — both rails on so a technician can exercise
        # every path.
        if chrg:
            contrib[chrg] = {"state": "on"}
        if dischrg:
            contrib[dischrg] = {"state": "on"}
    elif mode_slug == "standby":
        # Low-power hold — disallow discharge to preserve SoC, allow
        # charge so anything upstream (solar, shore) tops the pack up.
        if dischrg:
            contrib[dischrg] = {"state": "off"}
        if chrg:
            contrib[chrg] = {"state": "on"}
    # Driving / Parking / Work / Movie / Sentry: default behaviour,
    # no explicit BMS contribution, pack self-manages.

    # Drop contributions for entities a scene can't actuate (today the MOSFET
    # flags are read-only binary_sensors): emitting them only adds un-removable
    # clutter to the editor. A future settable switch.* passes straight through.
    return {
        eid: state
        for eid, state in contrib.items()
        if eid.split(".", 1)[0] in _SETTABLE_SCENE_DOMAINS
    }


def classify_bms_model(device_name: str | None) -> tuple[str | None, str | None, str | None]:
    """Classify a BMS pack from its BLE advertised device name.

    Scoped to catalog entries with `model_detection.source == "device_name"`
    — currently the JK-family packs whose advert name encodes pack metadata
    (Roamer Gen 2's `ROAM{V}{C}{serial}`, plus any future rebadged-JK pack
    like Fogstar if/when we confirm the same scheme). Other BMS chipsets
    (JBD, Seplos, Daly, Ant…) surface identity through different signals
    and should grow their own classifier with a distinct `source` value.
    Keeping the source tag explicit means this function never accidentally
    misclassifies a pack whose advert name looks similar but whose true
    identity comes from somewhere else.

    Longest-prefix match wins — guards against future overlapping prefixes
    like `ROAM12` vs `ROAM12105`. Returns `(slug, display_name, image_filename)`;
    all None on miss so the caller can fall back to the capacity-based name
    generator.
    """
    if not device_name:
        return (None, None, None)

    from ..device_catalog import PHYSICAL_DEVICE_CATALOG

    name = str(device_name).strip()
    best_slug: str | None = None
    best_display: str | None = None
    best_image: str | None = None
    best_prefix_len = -1

    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("integration") != "sensa_bms_ble":
            continue
        detection = entry.get("model_detection") or {}
        if detection.get("source") != "device_name":
            continue
        prefixes = detection.get("device_name_prefixes") or {}
        display_names = detection.get("display_names") or {}
        model_images = detection.get("model_images") or {}
        for prefix, slug in prefixes.items():
            if name.startswith(prefix) and len(prefix) > best_prefix_len:
                best_slug = slug
                best_display = display_names.get(slug)
                best_image = model_images.get(slug)
                best_prefix_len = len(prefix)

    return (best_slug, best_display, best_image)


def classify_bms_model_by_capacity(
    family: str | None, design_capacity: Any
) -> tuple[str | None, str | None, str | None]:
    """Classify a BMS pack from the rated capacity its BMS reports.

    Scoped to catalog entries with `model_detection.source ==
    "design_capacity"` and the same `bms_family`: packs whose advert carries
    no model (Foxygen's Daly packs advertise no name at all) but whose BMS
    stores its rated Ah. `capacity_models` maps whole Ah, as a string, to a
    model slug. Only the reported rating is matched, never the
    remaining-over-SoC estimate, which drifts by an Ah either way.
    Returns `(slug, display_name, image_filename)`, all None on a miss.
    """
    try:
        capacity = round(float(design_capacity))
    except (TypeError, ValueError):
        return (None, None, None)
    if not family or capacity <= 0:
        return (None, None, None)

    from ..device_catalog import PHYSICAL_DEVICE_CATALOG

    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("integration") != "sensa_bms_ble" or entry.get("bms_family") != family:
            continue
        detection = entry.get("model_detection") or {}
        if detection.get("source") != "design_capacity":
            continue
        slug = (detection.get("capacity_models") or {}).get(str(capacity))
        if slug:
            return (
                slug,
                (detection.get("display_names") or {}).get(slug),
                (detection.get("model_images") or {}).get(slug),
            )
    return (None, None, None)


def _canonical_mac(value: str) -> str:
    """Return MAC in HA's canonical AA:BB:CC:DD:EE:FF form.

    Accepts no-separator, colon, hyphen, or mixed-case input. HA's bluetooth
    integration keys its scanner registry by the colon form — passing the raw
    backend string (often no-separator) to `async_ble_device_from_address`
    silently returns None and the coordinator appears offline even when the
    device is in range.
    """
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    if len(hex_only) != 12:
        return str(value or "").upper()
    return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))


def _load_aiobmsble_families_sync() -> dict[str, type]:
    """Sync loader — call only from an executor thread.

    Importing aiobmsble triggers metadata / entry-point reads that hit the
    filesystem synchronously; HA flags those as blocking if they run in the
    event loop, so we always bounce through `hass.async_add_executor_job`.

    Also registers Sensa-owned families (currently just VIP for Roamer
    Gen 1) alongside the aiobmsble families, so the `family` slug →
    parser class dispatch works uniformly regardless of origin.
    """
    families: dict[str, type] = {}

    try:
        from aiobmsble.bms import jikong_bms  # type: ignore[import-not-found]
        families["jk"] = jikong_bms.BMS
    except ImportError:
        _LOGGER.warning(
            "aiobmsble not available; BMS-BLE coordinator will refuse to start "
            "for JK-family packs (Roamer Gen 2, etc.)"
        )

    # Optional aiobmsble families — import guarded because earlier aiobmsble
    # versions may not carry every parser.
    for slug, module_name in (
        ("jbd", "jbd_bms"),
        ("seplos", "seplos_bms"),
        ("daly", "daly_bms"),
        ("ant", "ant_bms"),
        # Renogy packs with the BMS built into the pack (Pro / Core series,
        # advert names RNGRBP* / RNGC*). The separate "renogy" family in
        # aiobmsble is the older BT-2 dongle (BT-TH-* advert, no
        # manufacturer_id) and isn't catalogued, so it isn't loaded here.
        ("renogy_pro", "renogy_pro_bms"),
    ):
        try:
            module = __import__(
                f"aiobmsble.bms.{module_name}", fromlist=["BMS"]
            )
            families[slug] = module.BMS
        except ImportError:
            _LOGGER.debug(
                "aiobmsble family '%s' not present in this version", slug
            )

    # Sensa-owned families — shipped alongside the integration because
    # aiobmsble doesn't cover them. VIP is the Roamer Gen 1 chipset
    # (proprietary protocol — see bms_ble_vip.py module docstring).
    try:
        from . import bms_ble_vip
        families["vip"] = bms_ble_vip.BMS
    except ImportError:
        _LOGGER.warning(
            "bms_ble_vip parser missing — Roamer Gen 1 packs won't poll"
        )

    # Daly gets aiobmsble's parser plus the rated-capacity read Foxygen
    # model detection needs. Without it the plain parser above still polls.
    try:
        from . import bms_ble_daly
        families["daly"] = bms_ble_daly.BMS
    except ImportError:
        _LOGGER.debug("bms_ble_daly unavailable; Daly packs use the plain parser")

    return families


BMS_FAMILIES: dict[str, type] | None = None


async def async_ensure_bms_families(hass: HomeAssistant) -> dict[str, type]:
    """Populate BMS_FAMILIES off the event loop, once per HA start."""
    global BMS_FAMILIES
    if BMS_FAMILIES is None:
        BMS_FAMILIES = await hass.async_add_executor_job(
            _load_aiobmsble_families_sync
        )
    return BMS_FAMILIES


_MAC_SUFFIX_RE = re.compile(
    r"\s*\((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\)\s*$"
)
_GEN_SUFFIX_RE = re.compile(r"\s*\(Gen\s*\d+\)\s*", flags=re.IGNORECASE)
_MAC_TAG_SUFFIX_RE = re.compile(r"\s+\(?#[0-9A-Fa-f]{4}\)?\s*$")


def _mac_tag(mac: str) -> str:
    """Return the last 4 hex chars of a MAC address as a stable disambiguator."""
    cleaned = mac.replace(":", "").upper()
    return cleaned[-4:] if len(cleaned) >= 4 else cleaned


def _upgrade_display_name(current: str, capacity_ah: int, mac: str) -> str:
    """Build the customer-facing display name for an enriched BMS device.

    "Roamer Leisure Battery"                              →  "Roamer 105Ah Leisure Battery #3655"
    "Roamer Leisure Battery (Gen 2)"                      →  "Roamer 105Ah Leisure Battery #3655"
    "Roamer Leisure Battery (Gen 2) (C8:47:80:2F:36:55)"  →  "Roamer 105Ah Leisure Battery #3655"
    "Fogstar Leisure Battery"                             →  "Fogstar 100Ah Leisure Battery #A4B7"

    Legacy suffixes stripped first:
    - `(XX:XX:XX:XX:XX:XX)` — MAC inserted into the display name by old
      registration code before the add-flow dropped it.
    - `(Gen N)` — generation tag no longer part of customer-facing names.
    - ` #XXXX` — previous MAC-tag suffix (so repeat enrichments don't stack).

    The capacity token is inserted before "Leisure Battery"; the MAC-tag
    suffix is always appended so a fleet of identical packs stays
    distinguishable in HA's device list.
    """
    working = current
    working = _MAC_SUFFIX_RE.sub("", working)
    working = _GEN_SUFFIX_RE.sub(" ", working)
    working = _MAC_TAG_SUFFIX_RE.sub("", working)
    working = " ".join(working.split()).strip()

    token = f"{capacity_ah}Ah"
    anchor = "Leisure Battery"
    if token not in working:
        if anchor in working:
            working = working.replace(anchor, f"{token} {anchor}", 1)
        else:
            working = f"{token} {working}"

    tag = _mac_tag(mac)
    if tag:
        working = f"{working} (#{tag})"

    return working


def _secret_ctor_kwargs(parser_cls: type, secret: str) -> dict[str, Any]:
    """Ctor kwargs that hand `secret` to whichever aiobmsble is installed.

    aiobmsble MOVED the application-level secret out of a top-level `secret=`
    argument and into a `BMSConfig` dataclass. In 0.26.0 the signature is
    `(ble_device, config: BMSConfig | None = None, logger_name: str = "")`,
    so the old call raises `TypeError: BMS.__init__() got an unexpected
    keyword argument 'secret'`. That fails EVERY poll, which is what left a
    Fogstar Drift (real and simulated alike) with all its entities
    registered but no data and a red reachability LED.

    Decided from the INSTALLED signature rather than a version number, so it
    keeps working either side of the move and needs no pin bump to match.
    Only reached when a secret is actually set; a PIN-less battery never
    passes through here.
    """
    try:
        params = inspect.signature(parser_cls.__init__).parameters
    except (TypeError, ValueError):  # C-implemented / unintrospectable ctor
        params = {}

    if "config" in params:
        try:
            from aiobmsble import BMSConfig
        except ImportError:
            _LOGGER.warning(
                "aiobmsble exposes a `config` ctor arg but no BMSConfig to "
                "build — connecting without the secret",
            )
            return {}
        return {"config": BMSConfig(secret=secret)}

    if "secret" in params:
        return {"secret": secret}

    # Neither shape. Connect WITHOUT the secret rather than raising: a pack
    # that doesn't actually require the PIN still reads fine, and a degraded
    # read beats a coordinator that fails every cycle.
    _LOGGER.warning(
        "aiobmsble %s accepts neither `config` nor `secret` — connecting "
        "without the stored secret; authentication may fail",
        parser_cls.__module__,
    )
    return {}


def _as_float(value: Any) -> Any:
    """Return a plain float for a numeric-like sample value.

    aiobmsble 0.28 returns temperatures as `TempSensor` dataclasses rather
    than bare floats. HA stringifies whatever it is given, so an un-coerced
    sensor shows `TempSensor(21.5, ...)` as its state. `TempSensor` defines
    `__float__`, so this covers both the old and the new shape.
    """
    if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _coerce_numeric_fields(data: dict[str, Any]) -> None:
    """Flatten wrapped numeric values in a sample, in place."""
    for key in ("temp_values", "cell_voltages", "cell_resistances"):
        seq = data.get(key)
        if isinstance(seq, (list, tuple)):
            data[key] = [_as_float(v) for v in seq]
    if "temperature" in data:
        data["temperature"] = _as_float(data["temperature"])


class BmsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls a single battery's BMS data over BLE.

    Uses aiobmsble's connection handling (which itself uses HA's bleak adapter
    so Sensa Bluetooth Proxies are picked up transparently). Data shape is the
    `aiobmsble.BMSSample` dict — see entity factory for the field names.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        mac_address: str,
        family: str,
        display_name: str,
        client: Any = None,
        secret: str = "",
        name_locked: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"sensa_bms_{device_id}",
            update_interval=POLL_INTERVAL,
        )
        self._device_id = device_id
        self._mac_address = _canonical_mac(mac_address)
        self._family = family
        self._display_name = display_name
        self._client = client
        self._secret = secret
        # True once the customer has renamed this pack. Read from the
        # backend record at startup: `_enriched` lives only as long as
        # this object, so a restart otherwise rewrote the catalog name
        # over the customer's on the first good poll.
        self._name_locked = name_locked
        self._bms: Any | None = None
        self._enriched = False
        self._enriched_without_rating = False
        self._logged_first_sample = False
        self._cached_bms_info: dict[str, Any] | None = None
        self._info_failed = False
        # HA's coordinator logs a fetch error only on a success-to-failure
        # flip, and `_bounded_first_refresh` marks a timed-out first refresh
        # failed without one, so a pack that never answers was never logged.
        # We log the first failure ourselves and the recovery, on the edge.
        self._poll_failing = False

    def _log_poll_failure(self, err: BaseException) -> None:
        if self._poll_failing:
            return
        self._poll_failing = True
        cause = err.__cause__ if isinstance(err, UpdateFailed) and err.__cause__ else err
        _LOGGER.warning(
            "BMS-BLE %s (%s, %s): poll failed: %s: %s; logged once until it recovers",
            self._device_id, self._family, self._mac_address,
            type(cause).__name__, cause,
        )

    async def _ensure_bms(self) -> Any:
        if self._bms is not None:
            return self._bms

        families = await async_ensure_bms_families(self.hass)
        parser_cls = families.get(self._family)
        if parser_cls is None:
            raise UpdateFailed(
                f"BMS family '{self._family}' not available in installed aiobmsble"
            )

        try:
            from homeassistant.components.bluetooth import (
                async_ble_device_from_address,
            )
        except ImportError as err:
            raise UpdateFailed("bluetooth integration unavailable") from err

        # Try connectable=True first (HA's recommended filter for GATT work).
        # Fall back to the unfiltered lookup when the active proxy hasn't
        # recently advertised connectable capability — aiobmsble will then
        # attempt the GATT connection itself and fail cleanly if unreachable.
        ble_device = async_ble_device_from_address(
            self.hass, self._mac_address, connectable=True
        )
        if ble_device is None:
            ble_device = async_ble_device_from_address(
                self.hass, self._mac_address
            )
        if ble_device is None:
            raise UpdateFailed(
                f"No BLE device found for {self._mac_address} — out of range "
                "of any scanner"
            )

        # aiobmsble BMS ctor signature varies slightly between versions; both
        # modern and older call shapes accept a BLEDevice as the first arg.
        # Pass secret only when set — empty string means no PIN required.
        kwargs: dict[str, Any] = {}
        if self._secret:
            kwargs.update(_secret_ctor_kwargs(parser_cls, self._secret))
        self._bms = parser_cls(ble_device, **kwargs)
        return self._bms

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            bms = await self._ensure_bms()
            sample = await bms.async_update()
        except UpdateFailed as err:
            self._log_poll_failure(err)
            raise
        except Exception as err:
            self._log_poll_failure(err)
            raise UpdateFailed(f"BMS read failed: {err!r}") from err
        if self._poll_failing:
            self._poll_failing = False
            _LOGGER.warning(
                "BMS-BLE %s (%s, %s): polling recovered",
                self._device_id, self._family, self._mac_address,
            )

        # aiobmsble returns a TypedDict / dict. Normalise to a plain dict for
        # entity consumption and stash display metadata alongside.
        data = dict(sample) if sample is not None else {}
        data["_mac"] = self._mac_address
        data["_display_name"] = self._display_name
        data["_family"] = self._family
        _coerce_numeric_fields(data)

        # Log the full BMSSample on first successful read so we can see exactly
        # what aiobmsble surfaces for this specific battery.
        if not self._logged_first_sample:
            _LOGGER.info(
                "BMS-BLE %s: first aiobmsble sample keys=%s full=%s",
                self._device_id,
                sorted(data.keys()),
                data,
            )
            self._logged_first_sample = True

        # Diagnostic: warn when list-based fields are missing from a sample.
        # Helps surface intermittent partial-frame issues in real time.
        cell_voltages = data.get("cell_voltages")
        temp_values = data.get("temp_values")
        if not cell_voltages or not temp_values:
            _LOGGER.warning(
                "BMS-BLE %s: partial sample — cell_voltages=%s temp_values=%s "
                "scalar keys present=%s",
                self._device_id,
                "present" if cell_voltages else "MISSING",
                "present" if temp_values else "MISSING",
                [k for k in ("voltage", "current", "battery_level", "cycle_charge") if k in data],
            )

        # Fetch JK device-info strings (model / firmware / serial / hardware)
        # once, after the first regular poll succeeds. aiobmsble's `device_info`
        # opens its own connection; running after the regular update avoids
        # double-connecting during the initial handshake. Failures are logged
        # once and skipped thereafter so a BMS that doesn't answer type-0x03
        # doesn't cost time on every poll.
        if self._cached_bms_info is None and not self._info_failed:
            info = None
            try:
                info = await bms.device_info()
            except Exception:
                _LOGGER.debug(
                    "BMS-BLE %s: device_info fetch failed",
                    self._device_id,
                    exc_info=True,
                )
                self._info_failed = True
            finally:
                # device_info changes aiobmsble's internal `_valid_reply`
                # from 0x02 (cell info) to 0x03 (device info). Force a
                # disconnect so the next regular poll reconnects with a
                # clean state and the cell/temp list fields populate again.
                try:
                    await bms.disconnect()
                except Exception:
                    _LOGGER.debug(
                        "BMS-BLE %s: post-device_info disconnect failed",
                        self._device_id,
                        exc_info=True,
                    )
                # Drop the cached BMS instance so _ensure_bms creates a
                # fresh one on the next poll. Required for JBD family:
                # aiobmsble keeps _connected=True after disconnect(), so
                # it skips re-authentication on reconnect — the device
                # then rejects reads and everything shows unavailable.
                # Clearing here forces a clean authenticated connection.
                self._bms = None
            if isinstance(info, dict) and info:
                self._cached_bms_info = dict(info)
                _LOGGER.info(
                    "BMS-BLE %s: device_info model=%s sw=%s hw=%s serial=%s name=%s",
                    self._device_id,
                    info.get("model"),
                    info.get("sw_version"),
                    info.get("hw_version"),
                    info.get("serial_number"),
                    info.get("name"),
                )

        # Merge cached device-info strings into the sample under prefixed keys
        # so entity descriptors can read them directly without colliding with
        # BMSSample's own field names.
        if self._cached_bms_info:
            info = self._cached_bms_info
            if info.get("model"):
                data["_info_model"] = info["model"]
            if info.get("sw_version"):
                data["_info_fw_version"] = info["sw_version"]
            if info.get("hw_version"):
                data["_info_hw_version"] = info["hw_version"]
            if info.get("serial_number"):
                data["_info_serial"] = info["serial_number"]
            if info.get("name"):
                data["_info_device_name"] = info["name"]

        # Stage 1b — one-shot enrichment. Push whatever metadata we can observe
        # into the backend device config so dashboards / docs / model-specific
        # catalog matching have real values instead of the generic placeholder.
        # A second pass when the rated capacity first turns up after an
        # enrichment that ran without it: a model keyed on the rating (a
        # Foxygen's size) can only be classified from it.
        late_rating = self._enriched_without_rating and bool(data.get("design_capacity"))
        if (not self._enriched or late_rating) and self._client is not None:
            await self._maybe_enrich(data)

        return data

    async def _maybe_enrich(self, data: dict[str, Any]) -> None:
        config_update: dict[str, Any] = {"bms_family": self._family}
        # Preserve credential so a backend full-replace of the config dict
        # doesn't wipe it — enrichment only adds metadata, never removes auth.
        if self._secret:
            config_update["pin"] = self._secret

        # Capacity can arrive as design_capacity (Ah, preferred). If absent
        # and the family is trustworthy, compute an approximation from
        # cycle_charge + SoC. VIP is EXCLUDED from that fallback: its
        # `remaining_mAh` field is uint16 and overflows for packs >65 Ah,
        # so the inferred capacity is wildly wrong ("8 Ah" for a 200 Ah
        # pack). VIP gets design_capacity from the advert name instead —
        # see `bms_ble_vip.BMS.async_update`'s `_advert_capacity_ah`.
        capacity = data.get("design_capacity")
        if not capacity and self._family != "vip":
            cycle_charge = data.get("cycle_charge")
            soc = data.get("battery_level")
            if cycle_charge and soc:
                try:
                    approx = float(cycle_charge) / (float(soc) / 100.0)
                    capacity = round(approx)
                except (TypeError, ValueError, ZeroDivisionError):
                    capacity = None

        if capacity:
            try:
                config_update["capacity_ah"] = int(capacity)
            except (TypeError, ValueError):
                pass

        cell_count = data.get("cell_count")
        if not cell_count:
            cell_voltages = data.get("cell_voltages")
            if isinstance(cell_voltages, (list, tuple)):
                cell_count = len(cell_voltages)
        if cell_count:
            try:
                config_update["cell_count"] = int(cell_count)
            except (TypeError, ValueError):
                pass

        # JK device-info strings (from type-0x03 frame) if present.
        # `_info_model` is the JK chipset model (e.g. `JK_B2A8S30P`) — kept
        # under the `bms_model` config key so it doesn't collide with the
        # product-level `model` slug written by the classifier below.
        for src_key, cfg_key in (
            ("_info_model", "bms_model"),
            ("_info_fw_version", "fw_version"),
            ("_info_hw_version", "hw_version"),
            ("_info_serial", "serial_number"),
            ("_info_device_name", "device_name"),
        ):
            value = data.get(src_key)
            if value:
                config_update[cfg_key] = str(value)

        # Catalog-driven model classification off the BLE advert name
        # (e.g. `ROAM12105` → `roamer-12105`). Wins over the capacity-only
        # name upgrade below because the catalog string encodes both
        # voltage and capacity, which matters for mixed-bank installs.
        classified_slug, classified_display, _ = classify_bms_model(
            data.get("_info_device_name")
        )
        if not classified_slug:
            classified_slug, classified_display, _ = classify_bms_model_by_capacity(
                self._family, data.get("design_capacity")
            )
        if classified_slug:
            config_update["model"] = classified_slug

        # Nothing worth writing — log once and bail without marking enriched,
        # so a later poll that returns more data gets another shot.
        meaningful_keys = [k for k in config_update if k != "bms_family"]
        if not meaningful_keys:
            _LOGGER.info(
                "BMS-BLE %s: no capacity/cell data in sample yet; "
                "enrichment deferred",
                self._device_id,
            )
            return

        # Display name resolution — catalog classification wins when present
        # because its string encodes both voltage and capacity (`Roamer 12V
        # 200Ah Xtreme`, `Roamer 12V 105Smart5 Max`). For unclassified JK-
        # family packs we fall back to the capacity-only builder so an
        # unknown ROAM12280 reads `Roamer 280Ah Leisure Battery #XXXX`
        # rather than a cryptic advert string.
        #
        # VIP family (Gen 1 Roamer) is EXCLUDED from the capacity
        # fallback — its `remaining_mAh` field is uint16 and overflows
        # for packs >65 Ah, producing spurious "8Ah" / "16Ah" prefixes
        # on the display name. Capacity comes from the advert name via
        # classification; if classification fails we leave the discovered
        # name alone rather than synthesising from the bogus mAh field.
        # A customer rename owns the name from then on — only the config
        # half (capacity, cells, model slug) keeps enriching.
        upgraded_name: str | None = None
        if self._name_locked:
            upgraded_name = None
        elif classified_display:
            tag = _mac_tag(self._mac_address)
            upgraded_name = (
                f"{classified_display} (#{tag})" if tag else classified_display
            )
        elif self._family != "vip":
            capacity_value = config_update.get("capacity_ah")
            if capacity_value and self._display_name:
                upgraded_name = _upgrade_display_name(
                    self._display_name, int(capacity_value), self._mac_address
                )

        try:
            await self._client.async_update_device(
                device_id=self._device_id,
                config=config_update,
                display_name=upgraded_name,
            )
            self._enriched = True
            self._enriched_without_rating = not data.get("design_capacity")
            _LOGGER.info(
                "BMS-BLE %s: enriched backend config with capacity=%s cells=%s model=%s",
                self._device_id,
                config_update.get("capacity_ah"),
                config_update.get("cell_count"),
                config_update.get("model") or "(unclassified)",
            )
            if upgraded_name and upgraded_name != self._display_name:
                _LOGGER.info(
                    "BMS-BLE %s: upgraded display name to %r",
                    self._device_id,
                    upgraded_name,
                )
                self._display_name = upgraded_name
                # Propagate to HA's device registry so Settings → Devices &
                # Services shows the upgraded name too (not just our own
                # Diagnostics table). HA caches the device name from the
                # initial device_info — it doesn't re-read automatically.
                self._update_ha_device_name(upgraded_name)
                # Tell the integration to rebuild the dashboards so the
                # BMS card picks up the new display_name and the role-map
                # stays in sync with the (now-renamed) device entry. The
                # listener in `__init__.py` debounces bursts.
                try:
                    from homeassistant.helpers.dispatcher import (
                        async_dispatcher_send,
                    )

                    from ..const import (
                        SIGNAL_DASHBOARD_REGEN_REQUESTED,
                        SIGNAL_MODES_REGEN_REQUESTED,
                    )
                    async_dispatcher_send(
                        self.hass, SIGNAL_DASHBOARD_REGEN_REQUESTED,
                    )
                    # Mode scene YAML references entity_ids that were
                    # created under the pre-enrichment name; role-map
                    # resolution stays correct but the embedded
                    # display_name on each scene tile should refresh
                    # too, so the same event kicks the modes pipeline.
                    async_dispatcher_send(
                        self.hass, SIGNAL_MODES_REGEN_REQUESTED,
                    )
                except Exception:
                    _LOGGER.debug(
                        "BMS-BLE %s: regen dispatch failed",
                        self._device_id, exc_info=True,
                    )
        except Exception:
            _LOGGER.warning(
                "BMS-BLE %s: backend enrichment failed",
                self._device_id,
                exc_info=True,
            )

    def _update_ha_device_name(self, new_name: str) -> None:
        try:
            from homeassistant.helpers import device_registry as dr
            dev_reg = dr.async_get(self.hass)
            entry = dev_reg.async_get_device(
                identifiers={("sensa_nomad", f"bms_{self._device_id}")}
            )
            if entry:
                dev_reg.async_update_device(entry.id, name=new_name)
        except Exception:
            _LOGGER.debug(
                "BMS-BLE %s: HA device registry update failed",
                self._device_id,
                exc_info=True,
            )


# Entity factory — Stage 2 expands the set to include per-cell voltages, named
# temperature sensors, balance/fault indicators. We still lean on aiobmsble for
# the raw fields; a later JK subclass can inject model/firmware strings once we
# extend the library (Stage 2b).

# Base scalar sensors — keys map directly into the aiobmsble BMSSample dict.
SENSOR_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "battery_level",
        "name": "State of Charge",
        "unit": "%",
        "device_class": "battery",
        "state_class": "measurement",
        "icon": "mdi:battery",
        "precision": 0,
    },
    {
        "key": "voltage",
        "name": "Pack Voltage",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "icon": "mdi:flash",
        "precision": 2,
    },
    {
        "key": "current",
        "name": "Pack Current",
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
        "icon": "mdi:current-dc",
        "precision": 2,
    },
    {
        "key": "power",
        "name": "Pack Power",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "icon": "mdi:lightning-bolt",
        "precision": 1,
    },
    {
        "key": "cycle_charge",
        "name": "Remaining Capacity",
        "unit": "Ah",
        "state_class": "measurement",
        "icon": "mdi:battery-charging-60",
        "precision": 3,
    },
    {
        "key": "cycles",
        "name": "Cycle Count",
        "state_class": "total_increasing",
        "icon": "mdi:counter",
        "precision": 0,
    },
    {
        "key": "delta_voltage",
        "name": "Cell Voltage Delta",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "icon": "mdi:sine-wave",
        "precision": 3,
    },
    {
        "key": "battery_health",
        "name": "State of Health",
        "unit": "%",
        "state_class": "measurement",
        "icon": "mdi:heart-pulse",
        "precision": 0,
    },
    {
        "key": "balance_current",
        "name": "Balance Current",
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
        "icon": "mdi:scale-balance",
        "precision": 2,
    },
)

# Device-info string sensors — populated from aiobmsble's `device_info()`
# (JK type-0x03 frame). Diagnostic category so they don't clutter the main
# "Sensors" list on the HA device page.
INFO_STRING_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "_info_model", "name": "Model", "icon": "mdi:chip"},
    {"key": "_info_fw_version", "name": "Firmware Version", "icon": "mdi:update"},
    {"key": "_info_hw_version", "name": "Hardware Version", "icon": "mdi:chip"},
    {"key": "_info_serial", "name": "Serial Number", "icon": "mdi:identifier"},
    {"key": "_info_device_name", "name": "Device Name", "icon": "mdi:tag"},
)

# Named temperature sensors per family. JK exposes MOSFET + up to three probes;
# `aiobmsble` returns them as a flat `temp_values` list in this order.
TEMP_LABELS: dict[str, tuple[str, ...]] = {
    # JK is currently only the Roamer Gen2, which reports TWO probes (not
    # per-cell): a general pack temp + a BMS/board (MOSFET) temp. Probe order
    # is Richard's best guess (2026-06-03) — VERIFY against the Roamer BLE app
    # and swap if the readings line up the other way. The "Temperature N"
    # fallback covers any extra probe a larger pack exposes; if a non-Roamer
    # JK pack with different wiring is added, split this out by model.
    "jk": (
        "Battery Temp",
        "BMS Temp",
    ),
    # JBD is currently only the Fogstar Drift Gen2, whose three probes are
    # wired: probe 1 = overall pack temp, probe 2 = cell 1, probe 3 = cell 2
    # (cells 3-4 have no probe). Confirmed against the Fogstar BLE app
    # (Richard, 2026-06-03). If a non-Fogstar JBD pack with different wiring
    # is ever added, split this out of the family table by model.
    "jbd": ("Battery Temp", "Cell 1 Temp", "Cell 2 Temp"),
    "seplos": (
        "Battery Temp 1",
        "Battery Temp 2",
        "Battery Temp 3",
        "Battery Temp 4",
        "Environment Temperature",
        "MOSFET Temperature",
    ),
    "vip": ("BMS Temp 1", "BMS Temp 2", "Cell Temp 1", "Cell Temp 2"),
    # Renogy Pro reports `temp_sensors` as a count and returns that many
    # values; the probe wiring is not documented and we have no pack on the
    # bench, so no names are asserted here. The "Temperature N" fallback
    # names them until the order is confirmed against the DC Home app.
}

BINARY_SENSOR_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "chrg_mosfet",
        "name": "Charging Allowed",
        "device_class": "power",
        "icon": "mdi:battery-charging",
    },
    {
        "key": "dischrg_mosfet",
        "name": "Discharging Allowed",
        "device_class": "power",
        "icon": "mdi:battery-arrow-down",
    },
    {
        "key": "balancer",
        "name": "Balancing",
        "icon": "mdi:scale-balance",
    },
)

# Fields that a specific BMS family doesn't support — these entities
# simply won't be created when the coordinator's `family` slug matches.
# Keys apply across all entity types: scalar sensors (SENSOR_FIELDS),
# binary sensors (BINARY_SENSOR_FIELDS / OPTIONAL_BINARY_SENSOR_FIELDS),
# per-cell dynamics (cell_balance_state, cell_resistances), and info
# strings — anything keyed by the slug here is silently skipped.
#
# VIP (Roamer Gen 1): no active balancing hardware — the chipset neither
# reports balance current nor exposes a "balancing now" flag. Hiding
# those entities means they don't show up permanently-unavailable in
# HA, which confuses customers who think something's broken.
FAMILY_EXCLUDED_FIELDS: dict[str, frozenset[str]] = {
    "vip": frozenset({
        # Balancing — VIP chipset has no active balancer.
        "balance_current",
        "balancer",
        "cell_balance_state",
        # Cell resistances — not reported by VIP.
        "cell_resistances",
        # Info strings VIP doesn't expose (no type-0x03-equivalent frame
        # for device info). Device name comes from the BLE advert via
        # the VIP parser's `device_info()`; model is a static stub.
        "_info_fw_version",
        "_info_hw_version",
        "_info_serial",
    }),
    "jbd": frozenset({
        # JBD protocol doesn't surface these fields for the Fogstar Drift
        # Gen 2 (and most JBD-family BMSes). Creating the entities causes
        # them to sit permanently unavailable on the device page.
        "balance_current",
        "battery_health",
        # Per-cell resistance and per-cell balance state are JK-specific
        # capabilities not available in the JBD protocol.
        "cell_resistances",
        "cell_balance_state",
        # JBD device_info() doesn't return model/fw/hw/serial/name for
        # the Fogstar firmware — only hw_version comes back non-null.
        "_info_model",
        "_info_fw_version",
        "_info_serial",
        "_info_device_name",
    }),
    "renogy_pro": frozenset({
        # The Renogy Modbus register set carries no balancer telemetry and no
        # state of health, and device_info() returns only serial / name /
        # software version.
        "balance_current",
        "balancer",
        "battery_health",
        "cell_balance_state",
        "cell_resistances",
        "_info_model",
        "_info_hw_version",
    }),
}


def _is_field_excluded(family: str, key: str) -> bool:
    """True when a given field key shouldn't be exposed for this family."""
    return key in FAMILY_EXCLUDED_FIELDS.get(family, frozenset())

# Stage 2b — JK-specific extras exposed only when aiobmsble's sample carries
# the corresponding key. Stays dormant (no entity created) for BMS families
# that don't report these fields; auto-lights the moment data shows up.
OPTIONAL_BINARY_SENSOR_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "precharge_mosfet",
        "name": "Precharge MOSFET",
        "device_class": "power",
        "icon": "mdi:flash-outline",
    },
    {
        "key": "heater",
        "name": "Self-Heating",
        "device_class": "heat",
        "icon": "mdi:radiator",
    },
)

# Sample keys that hold a per-cell list; each list element becomes one entity.
# Presence of the key in the first sample decides whether we create them.
PER_CELL_OPTIONAL_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "cell_resistances",
        "name": "Cell {index} Resistance",
        "kind": "sensor",
        "unit": "mΩ",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:omega",
        "precision": 2,
    },
    {
        "key": "cell_balance_state",
        "name": "Cell {index} Balancing",
        "kind": "binary_sensor",
        "icon": "mdi:scale-balance",
    },
)


def _device_info(coordinator: BmsCoordinator, device_id: str, display_name: str) -> dict[str, Any]:
    return {
        "identifiers": {("sensa_nomad", f"bms_{device_id}")},
        "name": display_name,
        "manufacturer": "Sensa (via aiobmsble)",
        "model": coordinator._family.upper() + " BMS",
    }


def build_bms_static_sensor_entities(
    coordinator: BmsCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
) -> list[Any]:
    """Entities whose existence doesn't depend on sample contents.

    Scalar fields (voltage, current, etc.) + info strings (model, firmware).
    Safe to create before the coordinator has ever succeeded — they'll simply
    show Unknown until data flows.
    """
    return _build_bms_sensor_entities(
        coordinator, entry_id, device_id, display_name, include_dynamic=False
    )


def build_bms_dynamic_sensor_entities(
    coordinator: BmsCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
) -> list[Any]:
    """Entities whose count depends on `cell_voltages` / `temp_values` lists.

    Caller should only invoke once `coordinator.data` has those fields
    populated (first successful poll). Returns empty list otherwise.
    """
    data = coordinator.data or {}
    if not data.get("cell_voltages") and not data.get("temp_values"):
        return []
    return _build_bms_sensor_entities(
        coordinator, entry_id, device_id, display_name,
        include_dynamic=True, include_static=False,
    )


def build_bms_sensor_entities(
    coordinator: BmsCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
) -> list[Any]:
    """Compatibility wrapper — returns static + whatever dynamics are ready."""
    return _build_bms_sensor_entities(
        coordinator, entry_id, device_id, display_name,
        include_static=True, include_dynamic=True,
    )


def _build_bms_sensor_entities(
    coordinator: BmsCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
    include_static: bool = True,
    include_dynamic: bool = True,
) -> list[Any]:
    """Build sensor entities for a registered BMS.

    Base scalars are always created. Cell voltage and temperature entities are
    created dynamically from the shape of the first sample (length of
    `cell_voltages` / `temp_values` lists), so we don't hard-code cell/probe
    counts per battery.
    """
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    data = coordinator.data or {}

    class _BmsScalarSensor(CoordinatorEntity[BmsCoordinator], SensorEntity):
        _attr_has_entity_name = True

        def __init__(self, desc: dict[str, Any]) -> None:
            super().__init__(coordinator)
            self._desc = desc
            self._attr_unique_id = f"{entry_id}_bms_{device_id}_{desc['key']}"
            self._attr_name = desc["name"]
            if "unit" in desc:
                self._attr_native_unit_of_measurement = desc["unit"]
            if "icon" in desc:
                self._attr_icon = desc["icon"]
            if "device_class" in desc:
                self._attr_device_class = desc["device_class"]
            if "state_class" in desc:
                self._attr_state_class = desc["state_class"]
            if "precision" in desc:
                self._attr_suggested_display_precision = desc["precision"]

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def native_value(self) -> Any:
            return (self.coordinator.data or {}).get(self._desc["key"])

        @property
        def available(self) -> bool:
            return (
                self.coordinator.last_update_success
                and self.coordinator.data is not None
                and self._desc["key"] in self.coordinator.data
            )

    class _BmsCellVoltageSensor(CoordinatorEntity[BmsCoordinator], SensorEntity):
        _attr_has_entity_name = True
        _attr_device_class = "voltage"
        _attr_state_class = "measurement"
        _attr_native_unit_of_measurement = "V"
        _attr_suggested_display_precision = 3
        _attr_icon = "mdi:sine-wave"

        def __init__(self, index: int) -> None:
            super().__init__(coordinator)
            self._index = index
            self._attr_unique_id = f"{entry_id}_bms_{device_id}_cell_{index + 1}"
            self._attr_name = f"Cell {index + 1} Voltage"

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def native_value(self) -> Any:
            cells = (self.coordinator.data or {}).get("cell_voltages")
            if not isinstance(cells, (list, tuple)) or self._index >= len(cells):
                return None
            return cells[self._index]

    class _BmsTempSensor(CoordinatorEntity[BmsCoordinator], SensorEntity):
        _attr_has_entity_name = True
        _attr_device_class = "temperature"
        _attr_state_class = "measurement"
        _attr_native_unit_of_measurement = "°C"
        _attr_suggested_display_precision = 1
        _attr_icon = "mdi:thermometer"

        def __init__(self, index: int, label: str) -> None:
            super().__init__(coordinator)
            self._index = index
            slug = label.lower().replace(" ", "_")
            self._attr_unique_id = f"{entry_id}_bms_{device_id}_temp_{slug}"
            self._attr_name = label

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def native_value(self) -> Any:
            temps = (self.coordinator.data or {}).get("temp_values")
            if not isinstance(temps, (list, tuple)) or self._index >= len(temps):
                return None
            return temps[self._index]

    class _DerivedCellMinMax(CoordinatorEntity[BmsCoordinator], SensorEntity):
        _attr_has_entity_name = True
        _attr_device_class = "voltage"
        _attr_state_class = "measurement"
        _attr_native_unit_of_measurement = "V"
        _attr_suggested_display_precision = 3

        def __init__(self, kind: str) -> None:
            super().__init__(coordinator)
            assert kind in ("min", "max")
            self._kind = kind
            self._attr_unique_id = f"{entry_id}_bms_{device_id}_cell_v_{kind}"
            self._attr_name = f"{'Min' if kind == 'min' else 'Max'} Cell Voltage"
            self._attr_icon = "mdi:arrow-down-bold" if kind == "min" else "mdi:arrow-up-bold"

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def native_value(self) -> Any:
            cells = (self.coordinator.data or {}).get("cell_voltages")
            if not isinstance(cells, (list, tuple)) or not cells:
                return None
            fn = min if self._kind == "min" else max
            try:
                return fn(c for c in cells if c is not None)
            except ValueError:
                return None

    class _BmsInfoSensor(CoordinatorEntity[BmsCoordinator], SensorEntity):
        _attr_has_entity_name = True
        _attr_entity_category = EntityCategory.DIAGNOSTIC

        def __init__(self, desc: dict[str, Any]) -> None:
            super().__init__(coordinator)
            self._desc = desc
            self._attr_unique_id = (
                f"{entry_id}_bms_{device_id}_{desc['key'].lstrip('_')}"
            )
            self._attr_name = desc["name"]
            if "icon" in desc:
                self._attr_icon = desc["icon"]

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def native_value(self) -> Any:
            return (self.coordinator.data or {}).get(self._desc["key"])

        @property
        def available(self) -> bool:
            return (
                self.coordinator.last_update_success
                and self.coordinator.data is not None
                and self._desc["key"] in (self.coordinator.data or {})
            )

    family = coordinator._family
    entities: list[Any] = []
    if include_static:
        entities.extend(
            _BmsScalarSensor(desc) for desc in SENSOR_FIELDS
            if not _is_field_excluded(family, desc["key"])
        )
        entities.extend(
            _BmsInfoSensor(desc) for desc in INFO_STRING_FIELDS
            if not _is_field_excluded(family, desc["key"])
        )

    if include_dynamic:
        cell_voltages = data.get("cell_voltages") if isinstance(data.get("cell_voltages"), (list, tuple)) else []
        cell_count = int(data.get("cell_count") or len(cell_voltages or []) or 0)
        for i in range(cell_count):
            entities.append(_BmsCellVoltageSensor(i))
        if cell_count > 0:
            entities.append(_DerivedCellMinMax("min"))
            entities.append(_DerivedCellMinMax("max"))

        temp_values = data.get("temp_values") if isinstance(data.get("temp_values"), (list, tuple)) else []
        temp_labels = TEMP_LABELS.get(coordinator._family, ())
        for i, raw in enumerate(temp_values or []):
            if raw is None:
                continue
            label = temp_labels[i] if i < len(temp_labels) else f"Temperature {i + 1}"
            entities.append(_BmsTempSensor(i, label))

        # Stage 2b — per-cell resistance. Built only when the sample carries
        # the key; orphan cleanup prunes previous entries if the key goes
        # away on subsequent polls (e.g. aiobmsble parser change).
        resistances = data.get("cell_resistances")
        if (
            isinstance(resistances, (list, tuple))
            and resistances
            and not _is_field_excluded(family, "cell_resistances")
        ):

            class _BmsCellResistanceSensor(CoordinatorEntity[BmsCoordinator], SensorEntity):
                _attr_has_entity_name = True
                _attr_state_class = "measurement"
                _attr_native_unit_of_measurement = "mΩ"
                _attr_suggested_display_precision = 2
                _attr_icon = "mdi:omega"
                _attr_entity_category = EntityCategory.DIAGNOSTIC

                def __init__(self, index: int) -> None:
                    super().__init__(coordinator)
                    self._index = index
                    self._attr_unique_id = f"{entry_id}_bms_{device_id}_cell_r_{index + 1}"
                    self._attr_name = f"Cell {index + 1} Resistance"

                @property
                def device_info(self) -> dict[str, Any]:
                    return _device_info(coordinator, device_id, display_name)

                @property
                def native_value(self) -> Any:
                    vals = (self.coordinator.data or {}).get("cell_resistances")
                    if not isinstance(vals, (list, tuple)) or self._index >= len(vals):
                        return None
                    return vals[self._index]

            for i in range(len(resistances)):
                entities.append(_BmsCellResistanceSensor(i))

    return entities


def build_bms_binary_sensor_entities(
    coordinator: BmsCoordinator,
    entry_id: str,
    device_id: str,
    display_name: str,
) -> list[Any]:
    """Build binary sensors for flag-style BMS fields."""
    from homeassistant.components.binary_sensor import BinarySensorEntity
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    class _BmsFlagSensor(CoordinatorEntity[BmsCoordinator], BinarySensorEntity):
        _attr_has_entity_name = True

        def __init__(self, desc: dict[str, Any]) -> None:
            super().__init__(coordinator)
            self._desc = desc
            self._attr_unique_id = f"{entry_id}_bms_{device_id}_flag_{desc['key']}"
            self._attr_name = desc["name"]
            if "icon" in desc:
                self._attr_icon = desc["icon"]
            if "device_class" in desc:
                self._attr_device_class = desc["device_class"]

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def is_on(self) -> bool | None:
            value = (self.coordinator.data or {}).get(self._desc["key"])
            if value is None:
                return None
            return bool(value)

    class _BmsProblemSensor(CoordinatorEntity[BmsCoordinator], BinarySensorEntity):
        _attr_has_entity_name = True
        _attr_name = "Fault"
        _attr_device_class = "problem"
        _attr_icon = "mdi:alert-circle"

        def __init__(self) -> None:
            super().__init__(coordinator)
            self._attr_unique_id = f"{entry_id}_bms_{device_id}_fault"

        @property
        def device_info(self) -> dict[str, Any]:
            return _device_info(coordinator, device_id, display_name)

        @property
        def is_on(self) -> bool | None:
            data = self.coordinator.data or {}
            value = data.get("problem")
            if value is None:
                code = data.get("problem_code")
                if code is None:
                    return None
                try:
                    return int(code) != 0
                except (TypeError, ValueError):
                    return None
            return bool(value)

        @property
        def extra_state_attributes(self) -> dict[str, Any]:
            code = (self.coordinator.data or {}).get("problem_code")
            if code is None:
                return {}
            try:
                return {"problem_code": f"0x{int(code):08X}"}
            except (TypeError, ValueError):
                return {"problem_code": str(code)}

    family = coordinator._family
    entities: list[Any] = [
        _BmsFlagSensor(desc) for desc in BINARY_SENSOR_FIELDS
        if not _is_field_excluded(family, desc["key"])
    ]
    entities.append(_BmsProblemSensor())

    # Stage 2b — optional family-specific binaries. Only emit the entity if
    # the sample currently carries the key, so the HA device page stays
    # clean on BMS families that don't expose these fields.
    data = coordinator.data or {}
    for desc in OPTIONAL_BINARY_SENSOR_FIELDS:
        if desc["key"] in data and not _is_field_excluded(family, desc["key"]):
            entities.append(_BmsFlagSensor(desc))

    # Stage 2b — per-cell balance status. Unlike Stage 2's aggregate
    # `balancer` flag, this tells you which individual cells are actively
    # being bled. One binary sensor per cell, gated on list presence
    # AND family capability (VIP has no per-cell balance feedback).
    balance_state = data.get("cell_balance_state")
    if (
        isinstance(balance_state, (list, tuple))
        and balance_state
        and not _is_field_excluded(family, "cell_balance_state")
    ):

        class _BmsCellBalanceSensor(CoordinatorEntity[BmsCoordinator], BinarySensorEntity):
            _attr_has_entity_name = True
            _attr_icon = "mdi:scale-balance"
            _attr_entity_category = EntityCategory.DIAGNOSTIC

            def __init__(self, index: int) -> None:
                super().__init__(coordinator)
                self._index = index
                self._attr_unique_id = (
                    f"{entry_id}_bms_{device_id}_cell_balance_{index + 1}"
                )
                self._attr_name = f"Cell {index + 1} Balancing"

            @property
            def device_info(self) -> dict[str, Any]:
                return _device_info(coordinator, device_id, display_name)

            @property
            def is_on(self) -> bool | None:
                vals = (self.coordinator.data or {}).get("cell_balance_state")
                if not isinstance(vals, (list, tuple)) or self._index >= len(vals):
                    return None
                val = vals[self._index]
                return None if val is None else bool(val)

        for i in range(len(balance_state)):
            entities.append(_BmsCellBalanceSensor(i))

    return entities
