"""Factory reset: return a commissioned box to the state it shipped in.

Backs Settings -> System -> "Factory Reset". Two customers, one action: a dealer
or installer who has finished commissioning and testing a van and is about to
hand it over, and an owner who wants to start again from scratch.

It is NOT `sensa-image-clean` with a button on it. That script strips a bench box
immediately before it is cloned to a master card, and it deliberately KEEPS the
config entry, the generated dashboards and the customer logins, because a golden
image needs them. Here those ARE the testing residue, so they are exactly what
has to go. The only overlap is the recorder database, the Sensa log and the
alarms.

The **licence** goes too (Richard, 2026-10-16): the App deletes
`licensing.json`, so the box comes back on the activation screen and needs a key
redeemed again.

What survives, and why:

  * The **customer logins and Wi-Fi**, so the box is reachable the moment it
    finishes restarting. A reset that locks the installer out of the tablet they
    are holding is not a reset, it is a recovery job.
  * The **software version and channel**. That is which build the box runs, not
    anything a dealer did to it.

Remote access is switched OFF: whether the van is reachable from the internet
is the new owner's choice, never one inherited from the bench.

The reset removes the config entry, and the box rebuilds from that. On an
appliance the add-on's seeded `sensa_nomad:` key makes `async_setup` kick a fresh
import flow on the next start, so it comes back on its own; on BYO the customer
re-adds the integration from Devices & Services, which is what starting fresh
means there.

TRAP, do not "tidy" this: the generated `nomad/scenes.yaml`,
`nomad/automations.yaml` and `nomad/routines.yaml` are NOT deleted, even though
they are pure testing residue. configuration.yaml carries `!include` directives
for them, and Home Assistant refuses to start when an included file is missing:
a reset that deletes them bricks the box on the restart that follows. They are
regenerated from the catalog on the next setup, and the customer's Instinct
picks that shaped them live in the config entry's options, which IS deleted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api.client import SensaApiClient
from .const import (
    CONF_BACKEND_URL,
    DOMAIN,
    OTA_STATES_SETTLED,
    SENSA_ALARMS_PATH,
    SENSA_COLD_CHARGE_PATH,
    SENSA_CRYPTO_AUTOPILOT_PATH,
    SENSA_EXTERNAL_URL_PATH,
    SENSA_LOG_PATH,
    SENSA_SCHENGEN_PATH,
    SENSA_SEAT_NOTICE_PATH,
    SENSA_SUPPORT_NOTICE_PATH,
    SENSA_TRACKING_PATH,
)

_LOGGER = logging.getLogger(__name__)

# Per-box state files under `/config/nomad/`. Nothing `!include`s these (they
# are plain JSON/JSONL state), so unlike the generated YAML above they are safe
# to delete outright. Each one is either the bench's own history (the Sensa log,
# live alarms, buffered track points) or a one-shot notice the new owner should
# see for themselves.
_STATE_FILES: tuple[str, ...] = (
    SENSA_LOG_PATH,
    SENSA_ALARMS_PATH,
    SENSA_SUPPORT_NOTICE_PATH,
    SENSA_SEAT_NOTICE_PATH,
    SENSA_EXTERNAL_URL_PATH,
    SENSA_COLD_CHARGE_PATH,
    SENSA_CRYPTO_AUTOPILOT_PATH,
    SENSA_SCHENGEN_PATH,
    SENSA_TRACKING_PATH,
)

# The customer's dashboard shaping, kept in `.storage` rather than the config
# entry, so removing the entry alone brings every panel back as the bench left
# it: card placement and sizes, tab order, Instinct order, the scene catalog
# (order, names, hidden and custom scenes and their contents), light groups and
# routines. Deleting the files is what puts the dashboards back to as-built.
_DASHBOARD_STORES: tuple[str, ...] = (
    f"{DOMAIN}.grid_layout",
    f"{DOMAIN}.tab_order",
    f"{DOMAIN}.instinct_order",
    f"{DOMAIN}.mode_overrides",
    f"{DOMAIN}.light_groups",
    f"{DOMAIN}.routine_overrides",
)


async def _async_check_not_updating(client: SensaApiClient) -> None:
    """Refuse a reset while a software update is in flight.

    Same rule as the integrity check, for the same reason: the updater rewrites
    the very files and containers a reset is tearing down, and the two racing
    leaves a box that is neither the old version nor the new one. A backend we
    cannot reach is "could not ask", never "no update running", so it does NOT
    block: the customer is more likely holding a box with a stopped add-on than
    one mid-update.
    """
    try:
        state = (await client.async_get_ota_status() or {}).get("state")
    except Exception:
        _LOGGER.debug("factory reset: could not read OTA state", exc_info=True)
        return
    if state and state not in OTA_STATES_SETTLED:
        raise HomeAssistantError(
            "A software update is in progress. Wait for it to finish, then "
            "run the factory reset again."
        )


async def _async_delete_all_backups(client: SensaApiClient) -> int:
    """Delete every stored backup, pinned ones included.

    Retention only ever prunes AUTOMATIC backups, so an installer's manual
    "before I broke it" snapshot would otherwise survive the handover, and a
    backup is a whole copy of the bench's Home Assistant, which the new owner
    could open and restore. That leak is the reason `sensa-image-clean` exists;
    a reset that left backups behind would reintroduce it on the one path where
    the box is changing hands.
    """
    try:
        status = await client.async_get_backup_status()
    except Exception:
        _LOGGER.warning(
            "factory reset: could not list backups, none were deleted",
            exc_info=True,
        )
        return 0

    slugs = [
        str(item.get("slug"))
        for item in (status.get("backups") or [])
        if item.get("slug")
    ]
    deleted = 0
    for slug in slugs:
        try:
            await client.async_delete_backup(slug=slug)
        except Exception:
            _LOGGER.warning(
                "factory reset: could not delete backup %s", slug, exc_info=True,
            )
            continue
        deleted += 1
    _LOGGER.warning(
        "factory reset: deleted %d of %d backup(s)", deleted, len(slugs),
    )
    return deleted


def _delete_state_files(config_dir: str) -> int:
    removed = 0
    rels = _STATE_FILES + tuple(f".storage/{key}" for key in _DASHBOARD_STORES)
    for rel in rels:
        path = Path(config_dir) / rel
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _LOGGER.warning("factory reset: could not remove %s: %s", path, exc)
            continue
        _LOGGER.info("factory reset: removed %s", path)
        removed += 1
    return removed


async def _async_purge_history(hass: HomeAssistant) -> None:
    """Empty the recorder database, which is the bench's own sensor history.

    `recorder.purge` rather than deleting the file: the database is open and
    being written to, so unlinking it underneath a running recorder leaves a
    half-emptied database and a WAL that outlives it. `keep_days: 0` drops
    everything; `repack` reclaims the space, which on an SD card is the point.
    """
    try:
        await hass.services.async_call(
            "recorder", "purge", {"keep_days": 0, "repack": True}, blocking=True,
        )
    except Exception:
        _LOGGER.warning("factory reset: recorder purge failed", exc_info=True)


async def _async_nerd_mode_off(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Turn the box-wide Nerd Mode switch off, found by its unique id."""
    try:
        from homeassistant.helpers import entity_registry as er

        entity_id = er.async_get(hass).async_get_entity_id(
            "switch", DOMAIN, f"{entry.entry_id}_sensa_nomad_nerd_mode",
        )
        if entity_id:
            await hass.services.async_call(
                "switch", "turn_off", {"entity_id": entity_id}, blocking=True,
            )
    except Exception:
        _LOGGER.warning("factory reset: Nerd Mode could not be turned off", exc_info=True)


async def _async_remove_native_entries(hass: HomeAssistant) -> int:
    """Remove every HA config entry in a domain Sensa cooperates with.

    Ruuvi, Govee, Mopeka, Victron BLE, Shelly, ESPHome boards and the rest live
    in Home Assistant's own integrations, and many have no Sensa device row, so
    removing our entry never reaches them. They survived a reset and kept their
    readings on the Climate tab (box .226, 2026-10-13: two Ruuvi tags still
    showing with an empty device list).
    """
    try:
        from . import _native_cooperate_domains

        domains = sorted(_native_cooperate_domains())
    except Exception:
        _LOGGER.warning("factory reset: native domains unknown", exc_info=True)
        return 0

    removed = 0
    for domain in domains:
        for ha_entry in list(hass.config_entries.async_entries(domain)):
            try:
                await hass.config_entries.async_remove(ha_entry.entry_id)
            except Exception:
                _LOGGER.warning(
                    "factory reset: could not remove %s entry %s",
                    domain, ha_entry.title, exc_info=True,
                )
                continue
            removed += 1
    _LOGGER.warning("factory reset: removed %d native entr(ies)", removed)
    return removed


# One-shot marker the reset leaves for the next boot: the box comes back in
# Parked (Richard, 2026-10-16) instead of the Startup scene a fresh mode store
# would pin. Read and removed by `_async_schedule_startup_mode_activation`.
POST_RESET_STORE_KEY = f"{DOMAIN}.post_reset"
POST_RESET_MODE = "parking"


async def async_factory_reset(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Run the reset, then restart Home Assistant.

    Ordering is load-bearing. Everything that needs the App or the config entry
    runs FIRST; the config entry goes next (which drains every registered device
    through the same per-device remove pipeline the Discovery tab uses, and
    strips the dashboards and themes); the on-disk sweep runs LAST, once nothing
    is left running to rewrite what it deletes.

    Best-effort throughout, and deliberately so: a half-finished reset the
    customer can simply run again beats one that raises out halfway and leaves a
    box with its devices gone and its dashboards still up.
    """
    backend_url = entry.data.get(CONF_BACKEND_URL)
    client = SensaApiClient(hass, backend_url) if backend_url else None

    _LOGGER.warning("Factory reset: starting")

    if client is not None:
        await _async_check_not_updating(client)
        await _async_delete_all_backups(client)
        # Remote access OFF, through the switch's own path so the App stops the
        # tunnel and tells the cloud. Its setting lives in the App's
        # remote_access.json, which the App's scrub does not touch, so without
        # this a reset handed the next owner a van reachable from the internet.
        try:
            from . import remote_access

            await remote_access.async_set_enabled(hass, entry, False)
        except Exception:
            _LOGGER.warning("factory reset: remote access could not be turned off", exc_info=True)

    await _async_purge_history(hass)

    # Nerd Mode OFF while its switch still exists. It restores its last state,
    # so the last state saved must be off, or the new switch (same entity id)
    # could come back on.
    await _async_nerd_mode_off(hass, entry)

    # Removing the entry runs `async_remove_entry`: per-device teardown (BLE
    # coordinators, adopted native entries, the backend device rows), the bulk
    # backend clear, the dashboards, the bundled themes and the Z2M serial. HA
    # drops the device and entity registry rows itself, and the entity states
    # our entities restore go with them, which is what actually clears the
    # bench's restored values, not deleting `.storage/core.restore_state` (Home
    # Assistant rewrites that on the shutdown this reset is about to trigger).
    try:
        await hass.config_entries.async_remove(entry.entry_id)
    except Exception:
        _LOGGER.exception(
            "factory reset: removing the config entry failed; the box will "
            "come back with its devices still registered",
        )

    # After the entry, never before: the App's scrub empties the device list, and
    # `async_remove_entry` drains only the rows it can read. Scrubbing first left
    # that teardown nothing to do (.177's relays kept their ESPHome entries).
    if client is not None:
        try:
            await client.async_post_factory_reset()
        except Exception:
            _LOGGER.warning(
                "factory reset: the App's own state could not be scrubbed; "
                "the image library and cached dealer assets may survive",
                exc_info=True,
            )

    await _async_remove_native_entries(hass)

    removed = await hass.async_add_executor_job(
        _delete_state_files, hass.config.config_dir,
    )
    _LOGGER.warning("factory reset: removed %d state file(s)", removed)

    try:
        from homeassistant.helpers.storage import Store

        await Store(hass, 1, POST_RESET_STORE_KEY).async_save(
            {"mode": POST_RESET_MODE},
        )
    except Exception:
        _LOGGER.warning("factory reset: could not mark the Parked start", exc_info=True)

    # Back to the shipped PIN. It is a credential the installer chose and told
    # nobody, so leaving it would hand the new owner a box they cannot get into
    # Settings on.
    try:
        from . import settings_pin

        await settings_pin.async_reset_pin(hass)
    except Exception:
        _LOGGER.warning("factory reset: PIN reset failed", exc_info=True)

    _LOGGER.warning("Factory reset: complete, restarting Home Assistant")
    # The Sensa log is one of the things a reset wipes, so this reason is the
    # FIRST entry the fresh box writes, not a survivor of the old one.
    from .boot_log import async_record_shutdown_intent

    await async_record_shutdown_intent(hass, "factory reset")
    await hass.services.async_call("homeassistant", "restart", {}, blocking=False)


async def async_handle_factory_reset(hass: HomeAssistant, call: Any) -> None:
    """Service entry point. Kicks the reset off in the background and returns.

    The reset removes its own config entry, which unloads the integration this
    handler is running inside. Awaiting it here would have the service call
    cancelled halfway through, so the work goes on a task owned by `hass` rather
    than by the entry, and the card gets its acknowledgement immediately.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("There is nothing set up to reset.")

    hass.async_create_background_task(
        async_factory_reset(hass, entries[0]),
        name="sensa_factory_reset",
    )
