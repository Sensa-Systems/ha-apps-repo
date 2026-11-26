"""Configure Home Assistant's own backup engine on a Container appliance.

Sensa does not ship a backup engine; HA's is the engine (see
`docs/system-design/backups.md` § 2). What was missing on Container is that nobody
ever *configured* it. Verified on-device 2026-08-08, a Container appliance shipped
with `automatic_backups_configured: false`, `schedule.recurrence: "never"`,
`retention: {copies: null, days: null}` and `agents: {}`, so the box took a backup
only when somebody pressed a button and never pruned one. 4.4 GB of tars had
accumulated on the boot media, which is the same filesystem the OTA low-disk guard
protects (`OTA_MIN_FREE_DISK_PERCENT`).

This module reconciles HA's stored backup config to the Sensa policy. It is
idempotent and cheap, so it runs at setup and again on the backup poller tick,
which means a box that was provisioned before this shipped self-heals.

**Container appliance only.** On HAOS the `admin` login is a full HA administrator
with Supervisor's own backup UI, and on BYO the customer owns their Home Assistant
outright. Writing either of their backup settings from under them would be wrong,
so the gate is `appliance and not is_supervised(hass)`, the same predicate as the
Supervisor and Backup tabs.

**Every backup is encrypted, including the local copy** (Richard, 2026-08-08:
encryption is a key selling point). An earlier revision left the local copy in the
clear on the reasoning that it sits on the same card as the plaintext `/config` it
was made from, so encrypting it protects against nobody already holding the card.
That reasoning was too clever. A backup is only useful once it is somewhere else,
and every route off the box (download, USB, cloud) turns "the local copy" into a
copy that has left the vehicle. Encrypting it costs one config field, and the
key-loss risk it was avoiding barely applies: a local backup dies with its card
whether or not it is encrypted.

HA models encryption as a **per-agent** `protected` flag and re-encrypts per agent
on upload (`manager.py:560-592`), so this stays the right mechanism when USB and
cloud arrive; the choice is simply to protect every agent rather than only the
remote ones.

**The key therefore has to be recoverable, and today it only half is.** It lives
in the App's `/data/backups.json` and the only copy off the box is the emergency
kit the customer downloads. Until escrow lands (`backups-roadmap.md` Phase 4) a
customer who loses the kit and the card loses the backups they had moved
elsewhere. That is the accepted trade for the encryption guarantee, and it is why
Phase 4 matters.

**HA's own retention only ever prunes AUTOMATIC backups.** Both of its retention
paths filter on `backup.with_automatic_settings` (`config.py:415`, `:750`), so a
backup made with a plain `backup.create` is never swept. That is why `run_backup`
routes through the manager with `automatic=True` (see `async_create_named_backup`)
and why the pre-OTA snapshot does not: it is the rollback point, and it must not
be swept out from under an update that is still being written.

**So Sensa owns the AGE rule, because HA's cannot reach the backups that actually
fill the card** (Richard, 2026-08-17). A pre-OTA snapshot is created with
`automatic=False` and was therefore kept for ever: one ~1.5 GB tar per version
applied, unbounded, on the same filesystem the OTA disk guard protects, so the
box eventually stops updating on the space its own updates consumed.
`async_purge_aged_backups` sweeps every ON-VEHICLE backup older than
`BACKUP_MAX_AGE`, whatever made it, and runs as a step of the nightly. HA's
`retention.copies` stays configured as a backstop that runs inside the manager on
every create, so a box whose integration never got as far as the nightly still
prunes something; it is DERIVED from the age window rather than set beside it.

**The cloud is exempt, and not by choice.** The vehicle's credential carries no
delete permission (see `async_delete_from_every_destination`), so an offsite copy
is not ours to sweep from here; it ages out server-side on the storage account's
own lifecycle rule.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import (
    BACKUP_DESTINATION_CLOUD,
    BACKUP_DESTINATION_KINDS,
    BACKUP_DESTINATION_LOCAL,
    BACKUP_DESTINATION_USB,
    BACKUP_DESTINATIONS_KEY,
    DEPLOYMENT_MODE_APPLIANCE,
    DEPLOYMENT_MODE_KEY,
    DOMAIN,
    SENSA_LOG_SOURCE_SYSTEM,
)

_LOGGER = logging.getLogger(__name__)

# HA Core's own on-disk agent. `agent_id` is f"{domain}.{unique_id}" and
# `CoreLocalBackupAgent` is domain "backup" / unique_id "local".
LOCAL_AGENT_ID = "backup.local"

# 03:00 keeps the historical Sensa cadence (the HAOS master image seeds an
# automation at the same time), so a customer moving between flavours sees no
# change. Deliberately not customer-configurable: what IS configurable is the
# host-update window, and the backup moves out of the way of that (below).
BACKUP_TIME = time(3, 0)


def _parse_window(window: str | None) -> tuple[time, time] | None:
    """"HH:MM-HH:MM" -> (start, end), or None if it is not one."""
    if not isinstance(window, str) or "-" not in window:
        return None
    start_raw, _, end_raw = window.partition("-")
    try:
        start_h, _, start_m = start_raw.strip().partition(":")
        end_h, _, end_m = end_raw.strip().partition(":")
        return time(int(start_h), int(start_m or 0)), time(int(end_h), int(end_m or 0))
    except (TypeError, ValueError):
        return None


def _within(moment: time, start: time, end: time) -> bool:
    """Is `moment` inside [start, end)? Handles a window that wraps midnight.

    HALF-OPEN on purpose. "01:00-03:00" ends exactly when the backup would
    start, and that is not a clash: the update window has closed. Treating it as
    closed would push the backup to 03:00 anyway (the window end IS 03:00) and
    make the rule look like it had done nothing.
    """
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    # Wraps midnight ("23:00-01:00"): inside means after the start OR before the
    # end, not between them.
    return moment >= start or moment < end


def scheduled_backup_time(window: str | None) -> time:
    """When the nightly backup should run, given the host-update window.

    **03:00 always, unless 03:00 falls inside the update window; then the END of
    that window** (Richard, 2026-08-09). Two full backups and a stack update
    competing for the same disk is how the 03:00 run on 2026-08-09 was refused
    with "Backup manager busy" and silently lost a night: the shipped default
    window is 03:00-05:00, so on a default box the backup was scheduled inside
    the update window from the day it was configured.

    Moving to the END of the window rather than before the start is deliberate:
    a backup taken after the update reflects the system the customer is now
    running, and if the update went badly that is the state worth capturing. The
    pre-OTA snapshot already covers the before-picture.

    An unparseable or absent window means no constraint, so 03:00 stands. Fails
    OPEN in the sense that matters: a box whose window we cannot read still
    takes a backup, at the ordinary time.
    """
    parsed = _parse_window(window)
    if parsed is None:
        return BACKUP_TIME
    start, end = parsed
    if not _within(BACKUP_TIME, start, end):
        return BACKUP_TIME
    return end


def current_backup_time(hass: HomeAssistant) -> time:
    """`scheduled_backup_time` for the window this box is actually set to.

    Read from the select's live state, never from a cached copy: the customer
    changes the window on Settings -> Supervisor, and the whole point of this is
    that the backup follows it without anyone re-deriving anything.
    """
    from .const import HOST_UPDATE_WINDOW_DEFAULT, HOST_UPDATE_WINDOW_SELECT_ENTITY_ID

    state = hass.states.get(HOST_UPDATE_WINDOW_SELECT_ENTITY_ID)
    raw = state.state if state is not None else None
    if raw in (None, "", "unknown", "unavailable"):
        # The select has not registered yet (early setup). Use the shipped
        # default rather than 03:00: the default window CONTAINS 03:00, so
        # assuming "no window" here would schedule the backup straight into it
        # and then move it on the next poll, which is a worse first impression
        # than being right immediately.
        raw = HOST_UPDATE_WINDOW_DEFAULT
    return scheduled_backup_time(raw)


# How long a backup is kept ON THE VEHICLE. A real tar on the test box was 1.4 GB
# with the database included, so three nights is ~4.2 GB against a 32 GB card
# floor (the smallest we ship, per build-guide-container.md).
#
# An AGE and not a COUNT, because a count could only ever bound the backups HA
# will sweep, and the ones that filled the card were the ones it will not: a
# pre-OTA snapshot per version applied, kept for ever. Age reaches all of them
# with one rule, and it is also the rule a customer can hold in their head
# ("three days of backups") without knowing what made each one.
BACKUP_MAX_AGE = timedelta(days=3)

# What HA's own retention is set to. DERIVED, never a second figure typed beside
# the first: the nightly run is one automatic backup per day, so "keep N days"
# and "keep N automatic copies" are the same policy said twice, and the day
# somebody widened the window without touching this the copies rule would have
# silently kept winning.
#
# Kept at all because it is a BACKSTOP with a different failure mode: HA runs it
# inside the manager at the end of every create, so it still prunes on a box
# where our nightly never fires. It cannot replace the sweep (it never sees a
# `automatic=False` backup) and the sweep cannot replace it (it needs our timer).
RETENTION_COPIES = BACKUP_MAX_AGE.days

# The FLOOR under the age rule: whichever of "three days" and "three copies"
# leaves more, wins (Richard, 2026-09-15). Age alone is the right rule on a van
# that runs every night and the wrong one on a box that has been off for a
# fortnight, where the sweep would take the window literally, find everything
# aged, and leave the customer with a single recovery point. Counted per
# DESTINATION rather than once overall, for the same reason the age exemption
# below is: a delete removes the backup from every on-vehicle destination at
# once, so a stick that has been out of the van since Tuesday needs its own
# three and cannot borrow the disk's.
BACKUP_MIN_COPIES = 3

# The recorder database is most of the tar. Kept IN: a restore that silently
# dropped every graph and history would surprise a customer far more than the
# disk cost, and retention now bounds the growth that made it a question. Revisit
# for the cloud destination, where bytes cost money rather than card space.
INCLUDE_DATABASE = True

# How long to wait for a backup we must not race. A real 1.6 GB tar took ~10
# minutes on the test box, so this is generous on purpose: a timeout means we
# report "no usable snapshot" and the caller carries on, which is the right
# failure but a wasteful one to hit for a slow SD card.
#
# NB this exceeds the OTA apply cooldown (`OTA_APPLY_COOLDOWN_SECONDS`, ~13 min),
# so the poller can attempt a second apply while the first is still waiting on
# its snapshot. That is safe rather than tidy: HA's manager refuses a concurrent
# create ("Backup manager busy"), which surfaces as a caught exception, a False
# return and an update that proceeds without claiming a snapshot it lacks.
BACKUP_COMPLETION_TIMEOUT_SECONDS = 30 * 60

# Customer-facing backup names (Richard, 2026-08-08). HA's default is
# "Automatic backup <ha version>", which says nothing about why a backup exists;
# these say what made it, which is what someone scanning the list needs to know.
SCHEDULED_BACKUP_NAME = "Overnight backup"
MANUAL_BACKUP_NAME = "Manual backup"


def pre_ota_backup_name(version: str) -> str:
    """Name for the snapshot taken before an update, e.g. the rollback point."""
    return f"Before update to {version}" if version else "Before update"


def is_container_appliance(hass: HomeAssistant) -> bool:
    """True on a Sensa Container appliance, false on HAOS/Enthusiast and BYO.

    The integration folds `enthusiast` into `appliance` on ingest, so the stored
    deployment mode alone cannot tell a HAOS box from a Container one and
    `is_supervised` is the only discriminator available here.
    """
    if hass.data.get(DOMAIN, {}).get(DEPLOYMENT_MODE_KEY) != DEPLOYMENT_MODE_APPLIANCE:
        return False
    from .appliance import is_supervised

    return not is_supervised(hass)


def _manager(hass: HomeAssistant) -> Any | None:
    """Return HA's backup manager, or None if it isn't available.

    Fails CLOSED on purpose: if we cannot reach the manager we change nothing,
    which leaves today's behaviour rather than risking a half-applied config.
    """
    try:
        from homeassistant.components.backup.const import DATA_MANAGER
    except ImportError:  # pragma: no cover - real HA always has it
        return None
    return hass.data.get(DATA_MANAGER)




def destination_kind(agent_id: str) -> str:
    """"local" | "usb" | "cloud" for an agent id, or "" if it is not ours.

    A third-party HA backup agent (the customer's own Google Drive, say) is
    neither ours to switch nor ours to drop, so it resolves to "" and is left
    entirely alone. `async_backup_agent_ids` already excludes anything outside
    our domain; the preference only ever REMOVES from the list we build, never
    adds to it or reorders it.
    """
    aid = str(agent_id or "")
    if aid == LOCAL_AGENT_ID:
        return BACKUP_DESTINATION_LOCAL
    if aid.startswith(f"{DOMAIN}.usb"):
        return BACKUP_DESTINATION_USB
    if aid.startswith(f"{DOMAIN}.cloud"):
        return BACKUP_DESTINATION_CLOUD
    return ""


# What a system that has never been configured backs up to.
#
# Local and USB ON, cloud OFF (Richard, 2026-08-11: "the default should be
# local and usb on a new system"). The two on-vehicle destinations cost the
# customer nothing to have; the cloud costs them a gigabyte of their data
# allowance and puts their whole system offsite, which is a thing to opt into
# rather than discover. The Schedule panel offers it in one tap.
DESTINATION_DEFAULTS = {
    BACKUP_DESTINATION_LOCAL: True,
    BACKUP_DESTINATION_USB: True,
    BACKUP_DESTINATION_CLOUD: False,
}


def backup_destination_prefs(hass: HomeAssistant) -> dict[str, bool]:
    """The customer's on/off choices, over `DESTINATION_DEFAULTS`.

    **Absent means the default**, which is the upgrade path: no box in the
    field has this option, and defaulting the on-vehicle destinations to off
    would silently stop backups on every one of them. Unreadable means the
    default too -- an unreadable setting must never be the reason a backup does
    not happen.
    """
    prefs = {
        kind: DESTINATION_DEFAULTS.get(kind, True) for kind in BACKUP_DESTINATION_KINDS
    }
    try:
        stored = hass.data.get(DOMAIN, {}).get(BACKUP_DESTINATIONS_KEY) or {}
        for kind, enabled in stored.items():
            if kind in prefs:
                prefs[kind] = bool(enabled)
    except Exception:
        _LOGGER.debug("backup destinations: preference unreadable", exc_info=True)
    return prefs


def destination_enabled(hass: HomeAssistant, kind: str) -> bool:
    """Is this destination switched on? Unknown kinds are always on."""
    if kind not in BACKUP_DESTINATION_KINDS:
        return True
    return backup_destination_prefs(hass).get(kind, True)


async def async_backup_agent_ids(hass: HomeAssistant) -> list[str]:
    """Every destination a backup should be written to, local first.

    **The roadmap assumed HA's scheduler would drive this and it no longer
    does.** Sensa fires the nightly backup itself (so it can skip a redundant
    one), passing an explicit `agent_ids`, so a USB agent added only to the
    stored config would receive nothing. This is the one place the list is
    decided, and both the stored config and our own create read it.

    Local always leads and is never dropped: a stick is an EXTRA copy, and a
    box whose only destination was a stick somebody has taken out would take no
    backup at all.

    Fails back to local alone on any error. An extra destination is a bonus; a
    backup that does not happen because we could not enumerate the bonus is not.
    """
    prefs = backup_destination_prefs(hass)
    # Read straight out of prefs, which already carries every kind over
    # DESTINATION_DEFAULTS. A second default here would be the same rule
    # written twice, and the two would drift the day one of them changed.
    agents = [LOCAL_AGENT_ID] if prefs[BACKUP_DESTINATION_LOCAL] else []
    manager = _manager(hass)
    if manager is None:
        return agents
    try:
        for agent_id in manager.backup_agents:
            aid = str(agent_id)
            if aid == LOCAL_AGENT_ID or not aid.startswith(f"{DOMAIN}."):
                continue
            # The `True` is for an agent of OURS whose kind we do not
            # recognise, not a destination default: it has no switch on the
            # card, so excluding it would drop a destination silently.
            if prefs.get(destination_kind(aid), True):
                agents.append(aid)
    except Exception:
        _LOGGER.debug("could not enumerate backup agents", exc_info=True)
        # Fail OPEN to local: an enumeration we could not do must not be the
        # reason a backup goes nowhere.
        return [LOCAL_AGENT_ID]
    # May be EMPTY, and that is passed through rather than repaired to local.
    # The customer is allowed to switch every destination off; quietly writing
    # to local anyway would make the control a lie. Callers refuse and say so.
    return agents


async def async_agent_ids_for_kinds(
    hass: HomeAssistant, kinds: list[str] | tuple[str, ...]
) -> list[str]:
    """Agent ids for an EXPLICIT set of destination kinds, local first.

    The manual half of "where does this backup go". `async_backup_agent_ids`
    answers the SCHEDULED question by reading the customer's stored preference;
    this one is handed the choice directly, because the two are now separate
    controls: the buttons in the Schedule panel govern the nightly run, the
    buttons beside "Back up now" govern one press of it.

    Deliberately reads the same agent list and applies the same ordering as
    `async_backup_agent_ids`, so a destination cannot be reachable by one route
    and not the other. Unknown kinds are ignored rather than refused: the card
    sends what it rendered, and a stale bundle naming something we have since
    dropped should take a backup, not fail to.
    """
    wanted = {str(kind) for kind in (kinds or ())}
    agents = [LOCAL_AGENT_ID] if BACKUP_DESTINATION_LOCAL in wanted else []
    manager = _manager(hass)
    if manager is None:
        return agents
    try:
        for agent_id in manager.backup_agents:
            aid = str(agent_id)
            if aid == LOCAL_AGENT_ID or not aid.startswith(f"{DOMAIN}."):
                continue
            if destination_kind(aid) in wanted:
                agents.append(aid)
    except Exception:
        _LOGGER.debug("could not enumerate backup agents", exc_info=True)
        return agents
    return agents


def _desired(
    password: str, backup_time: time, agent_ids: list[str] | None = None
) -> dict[str, Any] | None:
    """Build the config we want, or None if HA's API doesn't look as expected."""
    try:
        from homeassistant.components.backup.config import ScheduleRecurrence
    except ImportError:  # pragma: no cover
        return None
    # `is None`, NOT falsy: an EMPTY list means the customer switched every
    # destination off, and `or` would read that as "not supplied" and put local
    # back — writing a destination into HA's stored config that they had just
    # turned off.
    agents = list([LOCAL_AGENT_ID] if agent_ids is None else agent_ids)
    return {
        "automatic_backups_configured": True,
        "create_backup": {
            "agent_ids": agents,
            "include_database": INCLUDE_DATABASE,
            "password": password,
            # HA's default is "Automatic backup <ha version>", which tells the
            # customer nothing about WHY a backup exists. This name is the one
            # HA's own scheduler uses for the nightly run.
            "name": SCHEDULED_BACKUP_NAME,
        },
        "retention": {"copies": RETENTION_COPIES, "days": None},
        # **HA's own scheduler is deliberately OFF, and Sensa fires the nightly
        # backup itself.** HA's scheduler calls the manager directly and offers
        # no pre-run hook, so there is nowhere to ask "has a backup just been
        # taken?" before it starts. That question is the whole point: an update
        # takes a full pre-OTA snapshot minutes before the nightly slot, and
        # running a second identical backup on top of it costs ~10 minutes of
        # disk on a Pi to duplicate what is already there.
        #
        # Safe because retention does NOT hang off the schedule, checked on the
        # box against 2026.8.1: `delete_backups_exceeding_configured_count` runs
        # inside the manager at the end of EVERY create, so `retention.copies`
        # still prunes when Sensa creates the backup. (The separate daily timer
        # handles `retention.days`, which is None here.)
        "schedule": {
            "recurrence": ScheduleRecurrence.NEVER,
            "days": [],
            # Still stored, so HA's own UI and anything reading the config sees
            # the time we actually use rather than a stale 03:00.
            "time": backup_time,
        },
        # EVERY destination protected, not just the ones that leave the
        # vehicle: the local copy is encrypted too (Richard, 2026-08-08), and a
        # stick is the copy most likely to be lost in a car park.
        "agents": {a: {"protected": True} for a in agents},
    }


def _already_applied(
    config: Any, password: str, backup_time: time, agent_ids: list[str] | None = None
) -> bool:
    """True when the stored config already matches the policy.

    Compared field by field rather than wholesale because HA owns the dataclass
    shapes and gains fields between versions; an equality check on the whole
    object would rewrite the store on every tick after any HA upgrade.
    """
    try:
        schedule = config.schedule
        create = config.create_backup
        # `is None`, not falsy — see `_desired`. An empty list is a real answer.
        wanted = list([LOCAL_AGENT_ID] if agent_ids is None else agent_ids)
        # EVERY desired agent, not the local one. Hardcoding local here meant
        # that with Local switched off the comparison could never match and the
        # reconcile rewrote HA's stored config every 15 minutes for ever -- a
        # box that looks perfectly correct while writing its store 96 times a
        # day.
        agents_ok = all(
            (a := config.agents.get(aid)) is not None and a.protected is True
            for aid in wanted
        )
        return (
            bool(config.automatic_backups_configured)
            # Sensa owns the trigger now, so HA's scheduler must be OFF; a
            # box still set to "daily" would fire its own backup as well as
            # ours and the skip check would be bypassed by one of the two.
            and str(schedule.recurrence) == "never"
            and schedule.time == backup_time
            and config.retention.copies == RETENTION_COPIES
            and list(create.agent_ids) == wanted
            and create.password == password
            and create.name == SCHEDULED_BACKUP_NAME
            and bool(create.include_database) is INCLUDE_DATABASE
            and agents_ok
        )
    except AttributeError:
        # An HA version whose config shape we don't recognise. Treat as
        # "not applied" is wrong (we'd rewrite every tick), so treat as applied
        # and leave the box alone.
        _LOGGER.debug("backup config: unrecognised HA config shape, leaving alone")
        return True


def _has_own_agents(manager: Any) -> bool:
    """True when Home Assistant currently has at least one agent of ours.

    The single test for "is the agent list trustworthy right now". HA deletes
    and re-adds every agent for a domain when it reloads that platform, so an
    empty list means either "all gone" or "mid-reload" and nothing can tell them
    apart. Both the prune and the reconcile refuse to act on it, and they share
    this predicate so they cannot drift.
    """
    try:
        return any(str(a).startswith(f"{DOMAIN}.") for a in manager.backup_agents)
    except (AttributeError, TypeError):
        return False


def _prune_stale_agent_config(manager: Any) -> bool:
    """Drop stored per-agent config for Sensa agents that no longer exist.

    `BackupConfig.update()` MERGES the agents map and has no way to remove an
    entry (checked against HA 2026.8.1), so every stick label ever plugged into
    the box would keep its entry for ever. The test box carries three of them,
    left by an earlier revision that scanned `/media` and found HA's own
    `cdrom`, `floppy` and `usb` directories.

    Scoped hard, because this reaches past the public API into the stored data:

    * ONLY keys in our own `sensa_nomad.` namespace. HA's local agent and any
      third-party agent (a customer's Google Drive) are none of our business,
      and dropping their settings would be a data loss we were never asked for.
    * ONLY when the agent is genuinely absent from the live agent list.

    Nothing is lost by pruning: the per-agent config holds `protected` and
    `retention`, both of which the policy sets afresh, while the customer's own
    on/off preference is keyed by KIND and stored separately, so an unplugged
    stick keeps its setting exactly as before.
    """
    try:
        stored = manager.config.data.agents
        live = set(manager.backup_agents)
    except AttributeError:
        # An HA whose shape we do not recognise. Leaving stale entries alone
        # costs a few bytes; guessing at an unknown structure could cost the
        # customer their agent settings.
        return False
    # **"No agents at all" is NOT "every agent departed".**
    #
    # Home Assistant deletes and re-adds every agent for a domain when it
    # reloads that platform, and the integration reloads for all sorts of
    # reasons. Sampling the list inside that window sees zero of ours and, taken
    # at face value, means "the customer's stick and cloud have both gone".
    # Acting on it deleted the cloud destination's stored settings MID-BACKUP
    # and rewrote the target list to nothing (test box, 2026-08-10, 13:05:58),
    # which killed a 900 MB upload that was half an hour in.
    #
    # There is no way to tell the two apart from an empty list, so the empty
    # case does nothing at all. The cost is that a genuinely departed stick
    # keeps its stored settings until some agent of ours is present again, which
    # is a few stale bytes; the cost the other way is destroying a live backup.
    if not _has_own_agents(manager):
        return False
    stale = [
        aid
        for aid in stored
        if aid.startswith(f"{DOMAIN}.") and aid not in live
    ]
    if not stale:
        return False
    for aid in stale:
        stored.pop(aid, None)
    try:
        manager.store.save()
    except Exception:
        _LOGGER.debug("backup config: could not save after pruning", exc_info=True)
    _LOGGER.info(
        "backup config: dropped stored settings for %d departed destination(s): %s",
        len(stale),
        ", ".join(sorted(stale)),
    )
    return True


async def async_apply_backup_config(hass: HomeAssistant, client: Any) -> bool:
    """Reconcile HA's backup config to the Sensa policy. Returns True if changed.

    Safe to call repeatedly: it writes only when the stored config differs, so
    the poller tick is a no-op on a box that is already correct.

    The encryption key comes from the App (it owns `/data/backups.json` and
    generates the key on first use), so this needs the API client. If the key
    cannot be fetched we change NOTHING rather than configure a schedule with no
    encryption: a half-applied policy that silently drops the guarantee is worse
    than retrying on the next poll.
    """
    if not is_container_appliance(hass):
        return False
    manager = _manager(hass)
    if manager is None:
        # The backup integration hasn't set up yet. The poller retries.
        return False
    password = await _async_encryption_key(client)
    if not password:
        return False
    backup_time = current_backup_time(hass)
    agent_ids = await async_backup_agent_ids(hass)
    # Refuse to write "nowhere" when the reason is that nothing has LOADED yet.
    #
    # An empty list is a legitimate answer when the customer has switched every
    # destination off, and `_desired` passes it through deliberately. It is NOT
    # legitimate when a platform reload has momentarily emptied the agent list,
    # which is what put "to nowhere" into HA's stored config during a running
    # backup (test box, 2026-08-10).
    #
    # Told apart by the SAME rule the prune uses: if none of our agents is
    # registered, the list cannot be trusted. An earlier version tested the
    # customer's PREFERENCES instead and was wrong on a box with no cloud
    # entitlement, where the absent `cloud` key defaults to enabled and would
    # have made "switch everything off" impossible to honour.
    #
    # The trade-off, stated: on a box with local off and genuinely no stick and
    # no cloud, this leaves the previous config in place rather than writing
    # nothing. That keeps backups going somewhere, which is the safer way to be
    # wrong.
    if not agent_ids and not _has_own_agents(manager):
        _LOGGER.debug(
            "backup config: no agents registered yet, leaving stored config alone"
        )
        return False
    desired = _desired(password, backup_time, agent_ids)
    if desired is None:
        return False
    # Runs whether or not the policy itself needs a write: a stick being
    # unplugged changes nothing about the desired config, and that is exactly
    # when its leftover entry wants clearing.
    pruned = _prune_stale_agent_config(manager)
    try:
        if _already_applied(manager.config.data, password, backup_time, agent_ids):
            return pruned
        manager.config.update(**desired)
    except Exception:
        _LOGGER.warning("backup config: could not apply Sensa policy", exc_info=True)
        return False
    # Names the destinations rather than claiming "local agent": since a stick
    # can be one of them, a fixed string would misreport where backups go.
    _LOGGER.info(
        "backup config: applied Sensa policy (daily %s, keep %d, encrypted, to %s)",
        backup_time.strftime("%H:%M"),
        RETENTION_COPIES,
        ", ".join(agent_ids) or "nowhere (every destination switched off)",
    )
    return True


async def _async_encryption_key(client: Any) -> str | None:
    """The per-device backup encryption key from the App, or None.

    The App generates it on first use and it is the same key the customer
    downloads as the emergency kit, so a backup encrypted here is openable with
    the kit they already have.
    """
    if client is None:
        return None
    try:
        kit = await client.async_get_backup_emergency_kit()
    except (ConnectionError, TimeoutError):
        return None
    except Exception:
        _LOGGER.debug("backup config: emergency kit unavailable", exc_info=True)
        return None
    key = (kit or {}).get("encryption_key") if isinstance(kit, dict) else None
    return key.strip() if isinstance(key, str) and key.strip() else None


# Customer-facing names for HA's backup agents. `backup.local` is HA Core's own
# on-disk agent; the USB and cloud agents Sensa adds later get their labels here
# too, so the Backup list never has to know how an agent is implemented.
AGENT_LABELS = {
    LOCAL_AGENT_ID: "This device",
    # `_SensaCloudBackupAgent.name` (cloud_backup.py). Named here as well because
    # this is looked up from an agent ID alone, with no way back to the agent
    # object; without it the row for a copy Sensa holds offsite was captioned
    # "cloud", the mangled tail of `sensa_nomad.cloud`. The USB agent is
    # deliberately NOT listed: its label is the stick's own volume name, read off
    # the live agent (see `async_backup_destination_states`).
    f"{DOMAIN}.cloud": "Sensa cloud",
}



# Customer-facing names and icons for the three destination kinds. The USB name
# is replaced by the stick's own label when one is mounted.
DESTINATION_META = {
    BACKUP_DESTINATION_LOCAL: ("This device", "mdi:harddisk"),
    BACKUP_DESTINATION_USB: ("USB drive", "mdi:usb-flash-drive"),
    BACKUP_DESTINATION_CLOUD: ("Cloud", "mdi:cloud-outline"),
}


def cloud_backup_allowed(hass: HomeAssistant) -> bool:
    """True when this licence includes cloud backup.

    Phase 6 fills the field in; until it does this is always False and the Cloud
    pill never renders. **Fails CLOSED**, unlike the licence `control_locked`
    gate which deliberately fails open: failing open on a CONTROL protects an
    entitled customer from a cold-boot lockout, while failing open on a FEATURE
    flag would advertise a destination that does not exist and cannot be built.
    """
    from .const import RUNTIME_LICENCE_STATUS

    try:
        for runtime in (hass.data.get(DOMAIN) or {}).values():
            if not isinstance(runtime, dict):
                continue
            status = runtime.get(RUNTIME_LICENCE_STATUS) or {}
            if bool((status.get("cloud_backup") or {}).get("enabled")):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _usb_warning(hass: HomeAssistant) -> str:
    """Any caveat about the mounted stick itself, for the USB pill.

    Runs on an executor because it reads `/proc`. Returns "" for anything it
    cannot determine: a warning invented from a failed read would be worse than
    none.
    """
    try:
        from .backup import small_file_warning, usb_backup_dirs

        for path in usb_backup_dirs():
            warning = small_file_warning(path.parent)
            if warning:
                return warning
    except Exception:
        _LOGGER.debug("destination states: media caveat unreadable", exc_info=True)
    return ""


async def async_backup_destination_states(hass: HomeAssistant) -> list[dict[str, Any]]:
    """One entry per destination, for the Backup card's pills.

    Everything here is derived from data that genuinely exists. The three
    signals, read off HA 2026.8.1 on the test box:

      * `ManagerBackup.agents` -- destinations currently HOLDING a backup;
      * `ManagerBackup.failed_agent_ids` -- asked at create time and FAILED;
      * `agent_errors` from `async_get_backups()` -- COULD NOT BE ASKED just now.

    The distinction that matters most is "could not ask" versus "said no". A
    destination we could not reach renders neutral, never as a failure -- the
    same rule the licence pill follows (degrading to a neutral "Unknown") and
    the OTA card ("Couldn't check" rather than a stale version).

    `pending` is deliberately NOT `failed`: a stick plugged in after the last
    backup appears in neither map, and colouring that red would tell somebody
    their brand-new stick had broken.
    """
    if not is_container_appliance(hass):
        return []
    prefs = backup_destination_prefs(hass)
    manager = _manager(hass)

    present: dict[str, list[str]] = {}
    if manager is not None:
        try:
            for agent_id in manager.backup_agents:
                kind = destination_kind(str(agent_id))
                if kind:
                    present.setdefault(kind, []).append(str(agent_id))
        except Exception:
            _LOGGER.debug("destination states: agents unreadable", exc_info=True)

    known = True
    # Per destination: the NEWEST backup it actually holds, and its name.
    #
    # **"Holding" is asked of EVERY backup, not just the newest one.** It used to
    # read the newest backup's agent list, which was right only while every
    # backup went everywhere. Once destinations became switchable that broke on
    # the first mixed run: after a cloud-only backup, the newest backup lived in
    # the cloud alone, so Local and USB dropped to "pending" and the card told
    # the customer there was "no backup yet" on a disk holding four of them and a
    # stick holding two (test box, 2026-08-10). The question a pill answers is
    # "is my backup here?", which is about the destination, not about whichever
    # backup happens to be newest.
    holds: dict[str, tuple[str, str]] = {}
    failed: set[str] = set()
    unreachable: set[str] = set()
    if manager is None:
        known = False
    else:
        try:
            backups, agent_errors = await manager.async_get_backups()
            unreachable = {str(a) for a in (agent_errors or {})}
            newest = None
            newest_date = ""
            for backup in (backups or {}).values():
                date = str(getattr(backup, "date", "") or "")
                name = str(getattr(backup, "name", "") or "")
                for agent_id in (getattr(backup, "agents", None) or {}):
                    aid = str(agent_id)
                    if aid not in holds or date > holds[aid][0]:
                        holds[aid] = (date, name)
                if newest is None or date > newest_date:
                    newest, newest_date = backup, date
            # FAILED stays keyed to the newest backup, deliberately: it means
            # "the last attempt to write here did not land", which is a
            # statement about the most recent run and nothing else.
            if newest is not None:
                failed = {str(a) for a in (getattr(newest, "failed_agent_ids", None) or ())}
        except Exception:
            known = False
            _LOGGER.debug("destination states: backups unreadable", exc_info=True)

    # How full each destination is. Blocking (statvfs), so off the loop.
    try:
        from .backup import destination_capacities

        capacities = await hass.async_add_executor_job(destination_capacities)
    except Exception:
        _LOGGER.debug("destination states: capacity unreadable", exc_info=True)
        capacities = {}

    at = current_backup_time(hass)
    out: list[dict[str, Any]] = []
    for kind in BACKUP_DESTINATION_KINDS:
        if kind == BACKUP_DESTINATION_CLOUD and not cloud_backup_allowed(hass):
            # Not merely off: this licence has no such destination, so offering
            # a switch for it would be a control that can never do anything.
            continue
        label, icon = DESTINATION_META[kind]
        warning = ""
        ids = present.get(kind, [])
        enabled = bool(prefs.get(kind, True))
        if ids and kind == BACKUP_DESTINATION_USB:
            # Name it after the stick, so two sticks are told apart. The AGENT's
            # own name ("USB stick (SUNDATA)"), not `agent_label`, which knows
            # nothing of this agent and would fall back to mangling the id into
            # "usb sundata".
            try:
                agent = manager.backup_agents[ids[0]] if manager else None
                label = str(getattr(agent, "name", "") or "") or agent_label(ids[0])
            except Exception:  # noqa: BLE001
                label = agent_label(ids[0])
            warning = await hass.async_add_executor_job(_usb_warning, hass)
        entry: dict[str, Any] = {
            "kind": kind,
            "label": label,
            "icon": icon,
            "enabled": enabled,
            "present": bool(ids),
        }
        capacity = capacities.get(kind) or {}
        if capacity:
            # Only what the card renders, so the attribute stays small: the
            # total for the label and the percentage for the pill.
            entry["total_bytes"] = capacity.get("total_bytes")
            entry["percent_used"] = capacity.get("percent_used")
        if ids and kind == BACKUP_DESTINATION_USB and warning:
            # Carried ALONGSIDE the state, never as the state: a FAT32 stick
            # that is holding backups is genuinely working, and colouring it
            # red would say otherwise. What it needs is a caveat, not a verdict.
            entry["warning"] = warning
        if not enabled:
            entry["state"] = "off"
        elif not known or any(a in unreachable for a in ids):
            # No claim can be made, so none is made -- and no timestamp either.
            entry["state"] = "unknown"
        elif not ids:
            entry["state"] = "unavailable"
        elif any(a in failed for a in ids):
            entry["state"] = "failed"
        elif any(a in holds for a in ids):
            entry["state"] = "ok"
            # The newest backup THIS destination holds, not the newest overall.
            # After a cloud-only run they are different, and showing the global
            # newest date against Local would date a backup it does not have.
            held = max((holds[a] for a in ids if a in holds), default=("", ""))
            # "Holds", never "written at": the only time available is the
            # BACKUP's date, and for a remote destination an upload can land
            # much later than the backup was made.
            entry["holds_at"] = held[0]
            entry["holds_name"] = held[1]
        else:
            entry["state"] = "pending"
        if enabled and kind != BACKUP_DESTINATION_CLOUD:
            # Local and USB share the one nightly run, so they share its time.
            # Cloud gets NO cadence until Phase 6 ships a runner that honours
            # one; printing "Weekly" now would be a promise nothing keeps.
            entry["cadence"] = f"Nightly at {at.strftime('%H:%M')}"
        out.append(entry)
    return out


async def async_destinations_all_off(hass: HomeAssistant) -> bool:
    """Cheap version of `destinations_all_off`, for a caller that must not wait
    on a network round trip.

    `enabled` is the stored preference; `present` is read straight off
    `manager.backup_agents`, which is an in-memory dict HA already built when
    the agent platform set up -- no request goes anywhere. This is everything
    the "nothing is being backed up" warning needs.

    Contrast `async_backup_destination_states`, which additionally reads
    per-destination HOLD state (ok/failed/pending) via `manager.async_get_backups()`
    -- for the cloud agent that means listing the container and fetching every
    metadata blob over the internet. Calling that just to decide whether to
    switch a preference made the "turn a destination off" service take several
    seconds on the test box's mobile link (2026-08-11): the toggle was blocked
    on a full Azure listing it never needed.
    """
    prefs = backup_destination_prefs(hass)
    manager = _manager(hass)
    present: set[str] = set()
    if manager is not None:
        try:
            for agent_id in manager.backup_agents:
                kind = destination_kind(str(agent_id))
                if kind:
                    present.add(kind)
        except Exception:
            _LOGGER.debug("destinations_all_off: agents unreadable", exc_info=True)
    return not any(prefs.get(kind) and kind in present for kind in BACKUP_DESTINATION_KINDS)


def destinations_all_off(states: list[dict[str, Any]]) -> bool:
    """True when nothing is both switched on AND actually there.

    Deliberately not "every toggle is off": the case that loses a customer's
    backups is Local off, USB on, and the stick in their pocket.
    """
    return not any(d.get("enabled") and d.get("present") for d in states)


def backup_schedule_state(hass: HomeAssistant) -> dict[str, Any]:
    """What HA's backup engine is ACTUALLY set to do, for the Backup card.

    The card used to render its own config defaults ("Next backup at 03:00
    tomorrow", "Daily at 03:00") whether or not anything was scheduled, which on
    a Container box was a flat lie for months: nothing was scheduled at all and
    the card still promised a nightly run.

    `next_automatic_backup` is computed by HA's own scheduler, so the time shown
    is the one that will actually fire, including its catch-up-after-a-missed-run
    behaviour. Never recompute it here.

    Empty dict when unavailable, so the card falls back rather than inventing.
    """
    manager = _manager(hass)
    if manager is None:
        return {}
    try:
        data = manager.config.data
        agent = data.agents.get(LOCAL_AGENT_ID)
        # OUR schedule, not HA's. HA's scheduler is deliberately off (Sensa fires
        # the nightly backup itself so it can skip a redundant one), so
        # `schedule.recurrence` reads "never" and `next_automatic_backup` is
        # None. Reporting those would have the card tell the customer nothing is
        # scheduled while a backup runs every night.
        next_at = next_scheduled_backup(hass)
        at = current_backup_time(hass)
        return {
            "schedule_recurrence": "daily",
            "schedule_time": at.strftime("%H:%M"),
            "schedule_next": next_at.isoformat() if next_at else None,
            # OURS, not `data.retention.copies`, for the same reason as the days
            # below: HA's copies rule is a backstop that only ever sees automatic
            # backups, and the floor the card promises is the one the sweep
            # actually holds over every on-vehicle backup whatever made it.
            "retention_copies": BACKUP_MIN_COPIES,
            # OUR policy, not `data.retention.days`, which is deliberately None:
            # HA's age rule would only ever reach the automatic backups, and the
            # sweep this number describes reaches every on-vehicle backup
            # whatever made it. Reading HA's field would report "no age limit"
            # on a box that expires backups every night.
            "retention_days": BACKUP_MAX_AGE.days,
            # Whether backups are actually being encrypted, rather than whether
            # a key merely exists somewhere.
            "encrypted": bool(agent.protected) if agent is not None else False,
        }
    except AttributeError:
        _LOGGER.debug("backup schedule: unrecognised HA config shape", exc_info=True)
        return {}


# Why the last create failed, in words a customer can act on. Set by
# `async_create_named_backup` and read by the service so the UI stops saying
# "the encryption key or backup engine was unavailable" for every cause: that
# message named the wrong thing for a busy engine and sent Richard hunting a key
# problem that did not exist (2026-08-08).
_LAST_FAILURE: dict[str, str] = {}


def last_backup_failure() -> str:
    """The reason the last backup attempt failed, or a generic fallback."""
    return _LAST_FAILURE.get("reason") or "the backup could not be started"


def last_backup_failure_code() -> str:
    """A machine-readable cause for the last failure, or "".

    Only one caller needs to tell the causes apart, and it matters: "the engine
    is busy" is a NOT-YET-FINISHED backup, whereas everything else here is a
    failed one. The pre-OTA path is allowed to proceed past a failed backup (a
    permanently failing one must not wedge updates for ever) but must NOT
    proceed past an unfinished one, because the thing being written is the very
    rollback point the update would truncate. See `busy`.
    """
    return _LAST_FAILURE.get("code", "")


def _fail(name: str, reason: str, *, code: str = "", exc: bool = False) -> bool:
    _LAST_FAILURE["reason"] = reason
    _LAST_FAILURE["code"] = code
    _LOGGER.warning("backup '%s': %s", name, reason, exc_info=exc)
    return False


def clear_backup_failure() -> None:
    """Forget the last attempt, so a reader can only ever see THIS one.

    Matters because the record is module-level: a caller that branches on
    `last_backup_failure_code()` would otherwise be able to read a code left by
    some earlier, unrelated backup and defer an update for a reason that has
    already resolved itself.
    """
    _LAST_FAILURE.pop("reason", None)
    _LAST_FAILURE.pop("code", None)


def backup_in_progress(hass: HomeAssistant) -> bool:
    """True while HA's backup engine is busy creating a backup.

    The App lists backups by scanning `/config/backups`, so a tar that is still
    being WRITTEN shows up as a finished row, with a size that is really just
    how far it has got. HA's own agent only adds a backup to its list once the
    write completes (`components/backup/backup.py:86`), so "on disk but not
    known to the engine, while the engine is creating" is a reliable way to spot
    the row being written right now.

    Matters because the row carried a delete button: deleting a backup
    mid-write is a good way to corrupt the thing you were trying to protect.
    """
    manager = _manager(hass)
    if manager is None:
        return False
    try:
        return str(manager.state) == "create_backup"
    except AttributeError:
        return False


def agent_label(agent_id: str) -> str:
    """A customer-facing name for a backup agent id.

    Unknown agents fall back to the agent's own name rather than a guess: a
    third-party HA backup agent (the customer's own Google Drive, say) is a
    legitimate destination we simply have no label for, and showing its id is
    honest where inventing "Cloud" would not be.
    """
    if agent_id in AGENT_LABELS:
        return AGENT_LABELS[agent_id]
    return agent_id.split(".", 1)[-1].replace("_", " ").strip() or agent_id


async def async_backup_locations(hass: HomeAssistant) -> dict[str, list[str]]:
    """Map backup id -> customer-facing location names, from HA's own manager.

    Read from the manager rather than assumed, so the Backup list shows where a
    backup ACTUALLY is. Today that is one entry per backup ("This device"), but
    the moment a USB or cloud agent exists the same code reports both without
    changing, and a backup that only ever reached one destination cannot be
    shown as if it reached both.

    Empty dict when the manager is unavailable or the flavour is not Container:
    the card then shows nothing rather than a fabricated location.
    """
    if not is_container_appliance(hass):
        return {}
    manager = _manager(hass)
    if manager is None:
        return {}
    try:
        backups, _agent_errors = await manager.async_get_backups()
    except Exception:
        _LOGGER.debug("backup locations: could not list backups", exc_info=True)
        return {}
    out: dict[str, list[str]] = {}
    for backup_id, backup in (backups or {}).items():
        try:
            agent_ids = list(backup.agents)
        except (AttributeError, TypeError):
            continue
        if agent_ids:
            out[str(backup_id)] = [agent_label(a) for a in agent_ids]
    return out


async def async_backups_not_on_disk(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Backups Home Assistant knows about that have no local file.

    The Backup list is built by the App, which lists `*.tar` under
    `/config/backups`. That is the right source for a box whose backups are on
    its own disk, and it CANNOT see one that only ever went to the cloud or a
    stick: the first successful cloud-only backup was invisible in the UI and so
    could not be restored (test box, 2026-08-10, `b4cecf48`).

    Rows are shaped exactly like the App's so the card needs no new branch, and
    the SIZE is taken from the agent that actually holds it: `ManagerBackup`
    carries a per-agent size, and a backup can differ between destinations
    because HA re-encrypts per agent.

    Empty on any flavour without the manager, so the list degrades to the App's
    own answer rather than disappearing.
    """
    if not is_container_appliance(hass):
        return []
    manager = _manager(hass)
    if manager is None:
        return []
    try:
        backups, _agent_errors = await manager.async_get_backups()
    except Exception:
        _LOGGER.debug("backup list: manager backups unreadable", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for backup_id, backup in (backups or {}).items():
        try:
            agents = dict(backup.agents)
        except (AttributeError, TypeError):
            continue
        if not agents or LOCAL_AGENT_ID in agents:
            # On disk already, so the App has it and its figures win.
            continue
        size = next(
            (
                int(getattr(status, "size", 0) or 0)
                for status in agents.values()
                if int(getattr(status, "size", 0) or 0) > 0
            ),
            0,
        )
        out.append(
            {
                "slug": str(backup_id),
                "name": str(getattr(backup, "name", "") or "Backup"),
                "date": str(getattr(backup, "date", "") or ""),
                "type": "partial",
                "size_mb": round(size / (1024 * 1024), 1) if size else None,
                "pinned": False,
            }
        )
    return out


async def async_backup_location_kinds(hass: HomeAssistant) -> dict[str, list[str]]:
    """Map backup id -> destination KINDS (`local` / `usb` / `cloud`).

    A parallel to `async_backup_locations`, which returns customer-facing
    labels. The card needs the kind to pick an icon, and deriving it by matching
    the label text would be the same predicate written twice: the labels are
    customer-facing and change (a USB label carries the stick's own volume
    name), so a matcher would break the first time somebody renamed one.

    A third-party agent has no Sensa kind and is reported as an empty string, so
    the card can still show it with a neutral fallback rather than dropping a
    destination the backup genuinely reached.
    """
    if not is_container_appliance(hass):
        return {}
    manager = _manager(hass)
    if manager is None:
        return {}
    try:
        backups, _agent_errors = await manager.async_get_backups()
    except Exception:
        _LOGGER.debug("backup kinds: could not list backups", exc_info=True)
        return {}
    out: dict[str, list[str]] = {}
    for backup_id, backup in (backups or {}).items():
        try:
            agent_ids = list(backup.agents)
        except (AttributeError, TypeError):
            continue
        if agent_ids:
            out[str(backup_id)] = [destination_kind(str(a)) for a in agent_ids]
    return out


def held_offsite_only(holders: set[str], on_vehicle: Iterable[str]) -> bool:
    """True when NO on-vehicle destination holds this backup, so it cannot be
    deleted from here.

    ONE predicate, read by the delete refusal AND by the stamp that tells the
    card to hide its delete control. Written twice they would drift, and the way
    that shows up is a delete button that can only ever fail, which is exactly
    how it was reported: "if they cant delete it then a delete button shouldnt
    be offerred" (Richard, 2026-08-15).

    An empty `holders` is "could not ask", never "nowhere", so it answers False:
    refusing a delete because the agent list was unreadable would be a new way
    to get stuck.
    """
    return bool(holders) and not (holders & set(on_vehicle))


async def async_offsite_only_backups(hass: HomeAssistant) -> set[str]:
    """Backup ids that live only offsite. The card hides delete for these.

    The cloud is deliberately out of the vehicle's reach (see
    `async_delete_from_every_destination` for why the credential carries no
    delete permission), so the row for a cloud-only copy has nothing a delete
    could act on. Reported from the engine rather than inferred from the
    location chips, because the chips are a display of `destination_kind` and
    the refusal is a test against the agent ids: the two agree today and would
    have to be kept agreeing by hand for ever.

    Empty set when the manager cannot be read, which fails OPEN -- the button
    stays, the customer presses it, and the service answers for itself.
    """
    if not is_container_appliance(hass):
        return set()
    manager = _manager(hass)
    if manager is None:
        return set()
    try:
        backups, _agent_errors = await manager.async_get_backups()
    except Exception:
        _LOGGER.debug("backup offsite: could not list backups", exc_info=True)
        return set()
    on_vehicle = await async_on_vehicle_agent_ids(hass)
    out: set[str] = set()
    for backup_id, backup in (backups or {}).items():
        try:
            holders = {str(agent) for agent in (getattr(backup, "agents", None) or {})}
        except TypeError:
            continue
        if held_offsite_only(holders, on_vehicle):
            out.add(str(backup_id))
    return out


# How often a copy goes OFFSITE.
#
# Weekly, not nightly (Richard, 2026-08-11). A backup is ~1 GB and the vans that
# most need an offsite copy are the ones on mobile data, where a nightly upload
# costs the customer their allowance and takes about an hour of it. Four copies
# spanning a month is a better answer to "the van was stolen" than thirty copies
# of the same fortnight, and it pairs with the 28-day lifecycle rule on the
# storage account so the two together mean "roughly four, about a month".
CLOUD_BACKUP_INTERVAL = timedelta(days=7)


async def async_last_cloud_copy_at(hass: HomeAssistant) -> datetime | None:
    """When the cloud last received a copy, or None if it holds none / cannot tell.

    Read from what the destination ACTUALLY holds rather than from a counter we
    keep, so it cannot drift: a copy deleted server-side by the lifecycle rule,
    or an upload that failed after we ticked a box, both correct themselves on
    the next question.
    """
    manager = _manager(hass)
    if manager is None:
        return None
    try:
        backups, _agent_errors = await manager.async_get_backups()
    except Exception:
        _LOGGER.debug("cloud cadence: backups unreadable", exc_info=True)
        return None
    newest = ""
    for backup in (backups or {}).values():
        agents = getattr(backup, "agents", None) or {}
        if not any(
            destination_kind(str(agent)) == BACKUP_DESTINATION_CLOUD for agent in agents
        ):
            continue
        date = str(getattr(backup, "date", "") or "")
        newest = max(newest, date)
    if not newest:
        return None
    try:
        # stdlib, not `dt_util.parse_datetime`: this is a plain ISO string that
        # we wrote, and the HA helper is not in the test stub, so depending on
        # it here would have made every cadence decision a MagicMock.
        return datetime.fromisoformat(newest)
    except ValueError:
        return None


async def async_cloud_copy_due(hass: HomeAssistant) -> bool:
    """True when the cloud holds no copy newer than `CLOUD_BACKUP_INTERVAL`.

    **The one cloud cadence rule, asked by both paths.** The nightly run uses it
    to decide whether tonight includes an offsite copy, and "Back up now" uses
    it to refuse a second upload in the same week. One predicate rather than a
    schedule rule plus a separate manual quota, which is this repo's recurring
    bug and would let the two disagree about what week it is.

    Keyed on the AGE OF WHAT IS THERE, never on the day of the week. A van is
    away, out of signal or switched off for days at a time, so "upload on a
    Sunday" means a van that was asleep on Sunday gets no offsite copy that week
    and nothing ever notices. Asking the destination what it already holds
    self-heals: a box that has been offline for a fortnight uploads on the first
    night it can.

    Answers TRUE when it cannot tell. An extra upload costs data; a missing
    offsite copy costs the whole system, and this is the failure that only
    shows up on the day everything else has already gone wrong.
    """
    from homeassistant.util import dt as dt_util

    held = await async_last_cloud_copy_at(hass)
    if held is None:
        return True
    try:
        return (dt_util.now() - held) >= CLOUD_BACKUP_INTERVAL
    except TypeError:
        # A naive timestamp from an older backup: not worth guessing a timezone.
        return True


async def async_nightly_agent_ids(hass: HomeAssistant) -> list[str]:
    """Where tonight's scheduled backup goes.

    Everything on the vehicle, every night, plus the cloud on the nights a
    weekly copy is due.
    """
    on_vehicle = await async_on_vehicle_agent_ids(hass)
    every = await async_backup_agent_ids(hass)
    cloud = [
        agent
        for agent in every
        if destination_kind(agent) == BACKUP_DESTINATION_CLOUD
    ]
    if cloud and await async_cloud_copy_due(hass):
        # Order follows `async_backup_agent_ids`: local leads, cloud last.
        return [agent for agent in every if agent in on_vehicle or agent in cloud]
    return on_vehicle


async def async_on_vehicle_agent_ids(hass: HomeAssistant) -> list[str]:
    """Destinations that are ON THE VEHICLE: everything except the cloud.

    For the pre-update rollback point, which has to be written before the update
    can start. Uploading it offsite first is an hour of waiting on a mobile link
    (997 MB at the 1.2 Mbps the test box measured, 2026-08-11) for a copy that
    could never be the rollback anyway: rolling back means reading it straight
    back off this box, and a customer sitting on a stalled update is not going
    to wait to pull a gigabyte down again.

    The nightly backup still goes everywhere. Offsite matters for the van being
    stolen or burnt out; it does nothing for an update that went wrong.

    Falls back to the full list if the customer has switched every on-vehicle
    destination off, because a rollback point somewhere beats none.
    """
    every = await async_backup_agent_ids(hass)
    on_vehicle = [
        agent for agent in every if destination_kind(agent) != BACKUP_DESTINATION_CLOUD
    ]
    return on_vehicle or every


async def async_create_named_backup(
    hass: HomeAssistant,
    client: Any,
    *,
    name: str,
    automatic: bool,
    wait: bool = False,
    agent_ids: list[str] | None = None,
) -> bool:
    """Create an ENCRYPTED backup with our own name. Returns False on any miss.

    Driven through the manager rather than a backup service, for two reasons
    that both bit us:

      * `backup.create` hardcodes `password=None` inside HA
        (`components/backup/services.py`), so it can NEVER encrypt whatever the
        stored config says. It wrote the pre-update rollback point in the clear.
      * `backup.create_automatic` encrypts, but takes its name from the stored
        config, so every backup came out called "Overnight backup" regardless of
        what actually made it.

    `automatic` decides whether HA's retention may sweep it. True for the manual
    button (a routine copy, safe to age out); False for the pre-update snapshot,
    which is the rollback point and must outlive the retention window.

    `on_vehicle_only` keeps the copy off the cloud. See
    `async_on_vehicle_agent_ids` for why the pre-update snapshot sets it.

    Never raises: a failed backup must not block an update or throw at a
    customer pressing a button, and the caller decides how to report it.
    """
    # The record describes the LATEST attempt, so clear it before making one.
    clear_backup_failure()
    if not is_container_appliance(hass):
        return False
    if agent_ids is None:
        agent_ids = await async_backup_agent_ids(hass)
    if not agent_ids:
        # The customer has switched every destination off. Refused HERE, before
        # the manager is touched, so the reason reaching the card is the real
        # one rather than whatever HA says about an empty agent list.
        return _fail(
            name,
            "every backup destination is switched off",
            code="no_destinations",
        )
    manager = _manager(hass)
    if manager is None:
        return False
    password = await _async_encryption_key(client)
    if not password:
        # No key means we would write it in the clear. Skip instead.
        return _fail(name, "the encryption key could not be read from the Sensa App")
    # Subscribe BEFORE starting, or a fast backup could finish before we listen.
    finished: asyncio.Event | None = None
    outcome = {"ok": False}
    unsub = None
    if wait:
        finished = asyncio.Event()

        # CAVEAT: `CreateBackupEvent` carries no backup id (reason / stage /
        # state only), so this cannot tell OUR backup's completion from a
        # concurrent one. The realistic collision is an update running across
        # 03:00 while the nightly backup fires. The consequence is bounded: we
        # would return early believing ours finished. Accepted rather than
        # solved, because the alternative is not to wait at all, which is the
        # bug this exists to fix.
        def _on_event(event: Any) -> None:
            state = getattr(event, "state", None)
            if state is None:
                return
            text = str(state)
            if text in ("completed", "failed"):
                outcome["ok"] = text == "completed"
                if finished is not None:
                    finished.set()

        try:
            unsub = manager.async_subscribe_events(_on_event)
        except Exception:  # noqa: BLE001
            # We cannot watch, so we could never confirm the backup finished.
            # Refuse rather than silently degrade to the old behaviour of
            # claiming a snapshot we did not verify, which is exactly the defect
            # this wait was added to fix.
            return _fail(
                name, "the backup engine could not be watched, so completion is unconfirmable"
            )

    try:
        await manager.async_create_backup(
            # Resolved, never the constant: a stick plugged in must receive the
            # nightly backup too, and Sensa passes this list explicitly.
            agent_ids=agent_ids,
            include_addons=None,
            include_all_addons=False,
            include_database=INCLUDE_DATABASE,
            include_folders=None,
            include_homeassistant=True,
            name=name,
            password=password,
            with_automatic_settings=automatic,
        )
        if finished is not None:
            # `async_create_backup` returns once the backup is INITIATED, not
            # written: the reader/writer hands back an asyncio.Task. Returning
            # there let the OTA recreate containers and restart HA while the tar
            # was still being written, so the rollback point came out TRUNCATED
            # (141 MB of a 1.6 GB backup, "tar: invalid tar magic", observed on
            # the box 2026-08-08). Wait for the manager to say it finished.
            await asyncio.wait_for(finished.wait(), BACKUP_COMPLETION_TIMEOUT_SECONDS)
            if not outcome["ok"]:
                return _fail(name, "Home Assistant reported the backup failed")
    except TimeoutError:
        # Fail OPEN: the caller (an OTA) must never be wedged by a slow backup.
        # Returning False is honest, it says we do not have a usable snapshot,
        # and the caller decides what to do about that.
        return _fail(
            name,
            f"the backup did not finish within {BACKUP_COMPLETION_TIMEOUT_SECONDS} seconds",
        )
    except Exception as exc:  # noqa: BLE001 - never block the caller on a backup
        text = str(exc)
        if "busy" in text.lower():
            # HA refuses a second concurrent backup. This is the common case and
            # deserves its own words: the generic message sent Richard looking
            # for an encryption-key fault that did not exist.
            #
            # The CODE is load-bearing for the pre-OTA caller, which must treat
            # this as "wait", not "carry on": see `last_backup_failure_code`.
            return _fail(name, "a backup is already running", code="busy", exc=True)
        return _fail(name, f"Home Assistant refused the backup ({text})", exc=True)
    finally:
        if unsub is not None:
            unsub()
    _LAST_FAILURE.pop("reason", None)
    _LAST_FAILURE.pop("code", None)
    return True


async def async_create_pre_ota_backup(hass: HomeAssistant, client: Any, version: str) -> bool:
    """The rollback point taken before an update: encrypted, outside retention.

    `wait=True` is load-bearing here. The update recreates containers and
    restarts HA, so returning before the tar is written produces a truncated,
    unusable rollback point, which is the one backup that exists precisely for
    when an update goes wrong. ota.md's "apply can never precede its snapshot"
    only holds if we wait for the snapshot to finish, not merely to start.
    """
    return await async_create_named_backup(
        hass,
        client,
        name=pre_ota_backup_name(version),
        automatic=False,
        wait=True,
        agent_ids=await async_on_vehicle_agent_ids(hass),
    )


# Where the last upload-progress event is kept, so the Backup card can say WHICH
# destination is being written and how far through it is.
UPLOAD_PROGRESS_KEY = "sensa_nomad_backup_upload_progress"


def record_agent_upload_progress(
    hass: HomeAssistant, agent_id: str, uploaded: int, total: int
) -> None:
    """Remember how far one destination has got.

    Only ONE is kept, the most recent, because Home Assistant uploads to agents
    one after another rather than at once: a second entry would describe a
    destination that has already finished.

    Public because Home Assistant only raises `UploadBackupEvent` for uploads IT
    performs. An agent that does its own transfer (the Sensa cloud one, which
    streams to Blob Storage itself) has to say where it has got to, or the card
    keeps rendering whichever destination finished last -- which is exactly what
    it did: "Sending to usb sundata... 100%" for the whole of a cloud upload
    that had another hour and a half to run (test box, 2026-08-11).
    """
    hass.data[UPLOAD_PROGRESS_KEY] = {
        "agent_id": agent_id,
        "label": agent_label(agent_id),
        "kind": destination_kind(agent_id),
        "uploaded_bytes": int(uploaded),
        "total_bytes": int(total),
    }


def _record_upload_progress(hass: HomeAssistant, event: Any) -> None:
    """Record an upload-progress event raised by Home Assistant's own writer."""
    try:
        agent_id = str(event.agent_id)
        uploaded = int(event.uploaded_bytes)
        total = int(event.total_bytes)
    except (AttributeError, TypeError, ValueError):
        return
    record_agent_upload_progress(hass, agent_id, uploaded, total)


@callback
def clear_upload_progress(hass: HomeAssistant) -> None:
    """Forget it, so a finished backup does not leave a stale percentage."""
    hass.data.pop(UPLOAD_PROGRESS_KEY, None)


# Where Home Assistant stages a tar it is not writing to a local agent.
#
# With Local switched OFF (a cloud-only or USB-only box) the Core writer has no
# local agent path to write to, so it builds the tar HERE and uploads from it.
# Nothing was looking at this directory: the App lists `/config/backups`, so a
# cloud-only backup showed no in-progress row and no growing size, and the card
# fell back to a bare "Backing up..." with no percentage for the whole write
# (observed on the test box 2026-08-10).
STAGING_DIR = Path("/config/tmp_backups")


def staged_backup_bytes(staging: Path = STAGING_DIR) -> int:
    """Size of the tar being staged for upload, or 0.

    Keyed on the most recently MODIFIED file, not the largest. A run that failed
    part way can leave a tar behind, and a stale COMPLETE one is bigger than the
    one currently growing: taking the largest would start the bar high and then
    send it backwards as the real backup caught up. The file being written is
    the one whose mtime keeps moving.

    Blocking, so callers hand it to an executor.
    """
    try:
        stats = [(p.stat().st_mtime, p.stat().st_size) for p in staging.glob("*.tar")]
    except OSError:
        return 0
    if not stats:
        return 0
    return max(stats)[1]


def backup_upload_progress(hass: HomeAssistant) -> dict[str, Any]:
    """The destination currently being written, and how far through, or {}.

    Empty once the backup ends, which is what stops the card claiming an upload
    is still running after everything has finished.
    """
    if not backup_in_progress(hass):
        return {}
    state = hass.data.get(UPLOAD_PROGRESS_KEY)
    if not isinstance(state, dict):
        return {}
    total = state.get("total_bytes") or 0
    uploaded = state.get("uploaded_bytes") or 0
    if total > 0 and uploaded >= total:
        # This destination is DONE. The run is not, so something else is being
        # written -- and naming a finished destination is worse than naming
        # none, because it reads as a stall on a transfer that has in fact
        # moved on. The card falls back to its plain in-progress line until the
        # next destination reports.
        return {}
    out = dict(state)
    if total > 0:
        # Clamped: a total that lags the uploaded count would otherwise render
        # as more than a whole.
        out["percent"] = max(0, min(100, round(uploaded * 100 / total)))
    return out


def async_subscribe_backup_events(
    hass: HomeAssistant, on_change: Any, on_started: Any = None
) -> Any:
    """Call back when a backup STARTS and when it finishes. Returns an unsubscribe.

    HA's create services return once a backup is INITIATED, not written (the
    reader/writer hands back an `asyncio.Task`), so refreshing the Backup list
    straight after "Back up now" reads the state from before the backup existed.
    The list then sat empty until the next 15-minute poll and the customer
    watched their backup vanish (reported on the box 2026-08-08).

    **IN_PROGRESS matters as much as the terminal states**, which this missed
    until 2026-08-09. Only COMPLETED and FAILED fired, so nothing refreshed when
    a backup BEGAN and the new row waited for the next 15-minute poller tick:
    average seven and a half minutes of a 17-minute backup with nothing on
    screen, which reads exactly like a backup that is not happening. `on_started`
    is what lets the caller poll fast for the duration and stop again afterwards.

    Returns a no-op unsubscribe when the manager is unavailable, so the caller
    can always register it for teardown.
    """
    manager = _manager(hass)
    if manager is None:
        return lambda: None
    try:
        from homeassistant.components.backup.manager import (
            CreateBackupEvent,
            CreateBackupState,
        )
    except ImportError:  # pragma: no cover
        return lambda: None
    try:
        from homeassistant.components.backup.manager import UploadBackupEvent
    except ImportError:  # pragma: no cover - older HA without upload events
        UploadBackupEvent = None

    def _handle(event: Any) -> None:
        # Upload progress arrives on the SAME subscription as the create events,
        # as a separate class. Recorded rather than acted on: it is what turns a
        # flat "Backing up" into "Uploading to Sensa cloud, 42%", which matters
        # because the upload phase runs long AFTER the local tar is finished and
        # looks identical to a stall from the outside.
        if UploadBackupEvent is not None and isinstance(event, UploadBackupEvent):
            _record_upload_progress(hass, event)
            return
        if not isinstance(event, CreateBackupEvent):
            return
        # A backup has just begun: refresh so the row appears at once, and tell
        # the caller so it can watch the size while it grows.
        if event.state == CreateBackupState.IN_PROGRESS:
            on_change()
            if on_started is not None:
                on_started()
            return
        # Both COMPLETED and FAILED refresh: a failure changes what the customer
        # should see too.
        if event.state in (CreateBackupState.COMPLETED, CreateBackupState.FAILED):
            # Dropped on the way out, or a finished backup keeps reporting the
            # last percentage it happened to see.
            clear_upload_progress(hass)
            on_change()

    try:
        return manager.async_subscribe_events(_handle)
    except Exception:
        _LOGGER.debug("backup events: could not subscribe", exc_info=True)
        return lambda: None


# -- the nightly run, which Sensa fires itself --------------------------------
#
# A backup taken within this long before the nightly slot makes the nightly run
# pointless: it would be a second full copy of a system that has not changed
# since. One hour is Richard's figure (2026-08-09) and it is comfortably wider
# than the real case it exists for, an update whose pre-OTA snapshot lands
# minutes before the slot.
SKIP_IF_BACKUP_WITHIN = timedelta(hours=1)


async def async_latest_backup_at(hass: HomeAssistant) -> datetime | None:
    """When the most recent backup was taken, from HA's own list.

    Every backup counts, not just ours: a pre-OTA snapshot is the case this
    exists for, but a customer who pressed "Back up now" at ten to three has
    equally already got tonight's copy.

    None when the manager cannot be read, which the caller treats as "no idea,
    so take one" -- an extra backup is a far smaller failure than a missing one.
    """
    manager = _manager(hass)
    if manager is None:
        return None
    try:
        backups, _agent_errors = await manager.async_get_backups()
    except Exception:
        _LOGGER.debug("scheduled backup: could not list backups", exc_info=True)
        return None
    latest: datetime | None = None
    for backup in (backups or {}).values():
        raw = getattr(backup, "date", None)
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        if latest is None or when > latest:
            latest = when
    return latest


# A database Home Assistant set aside after it corrupted. `recorder/util.py`
# renames the live file to `<dbfile>.corrupt.<isotime>` and starts a fresh one,
# and then nothing ever removes it.
#
# It matters here because it is INVISIBLE and EXPENSIVE. HA's backup exclusions
# are `home-assistant_v2.db` and `home-assistant_v2.db-wal` by exact name, so a
# `.corrupt.` copy matches neither -- it is gzipped and encrypted into every
# backup, every night, and it stays there even with `include_database` off. On
# the test box one was 1.4 GB against a 3.8 GB backup payload: over a third of
# every backup, and a third of the ~19 minutes, spent on a dead file
# (2026-08-09).
#
# The glob is `*.db.corrupt.*` rather than the default database name, because
# `recorder` takes a `db_url` and the name is not guaranteed.
DEAD_DB_GLOB = "*.db.corrupt.*"

# HA writes a rename per retry, so a single corruption event leaves one real
# file and a handful of empty ones. Only the real ones cost anything.
DEAD_DB_MIN_BYTES = 1024 * 1024


async def async_dead_databases(hass: HomeAssistant) -> list[tuple[str, int]]:
    """Corrupt leftover databases in the config directory, biggest first.

    (name, bytes) pairs. Empty when there are none, or when the directory
    cannot be read -- this is advisory, and a failure to look must never stop a
    backup being taken.
    """

    def _scan() -> list[tuple[str, int]]:
        from pathlib import Path

        try:
            found = [
                (p.name, p.stat().st_size)
                for p in Path(hass.config.path()).glob(DEAD_DB_GLOB)
                if p.is_file()
            ]
        except OSError:
            return []
        return sorted(
            [f for f in found if f[1] >= DEAD_DB_MIN_BYTES],
            key=lambda f: f[1],
            reverse=True,
        )

    try:
        return await hass.async_add_executor_job(_scan)
    except Exception:
        _LOGGER.debug("dead database scan failed", exc_info=True)
        return []


async def _async_warn_about_dead_databases(hass: HomeAssistant) -> None:
    """Tell the customer their backups are carrying a dead database.

    Once a week at most, not once a night: nothing about this changes between
    backups, and a warning that repeats daily is one people learn to ignore.
    """
    dead = await async_dead_databases(hass)
    if not dead:
        return
    from ._log import sensa_log

    total = sum(size for _name, size in dead)
    gb = total / (1024**3)
    _LOGGER.warning(
        "backup: %d leftover corrupt database file(s) totalling %.1f GB are being "
        "included in every backup: %s",
        len(dead),
        gb,
        ", ".join(name for name, _ in dead),
    )
    sensa_log(
        hass,
        "warning",
        (
            f"Every backup is carrying {gb:.1f} GB of a database that was set aside "
            "after a past fault. It is no longer used, but it still gets copied into "
            "each backup, making them larger and slower. Contact Sensa support to "
            "have it removed."
        ),
        source=SENSA_LOG_SOURCE_SYSTEM,
        context={"files": [name for name, _ in dead], "bytes": total},
        dedup_key="backup_dead_database",
        cooldown=timedelta(days=7),
    )


async def async_warn_about_media(hass: HomeAssistant) -> None:
    """Tell the customer about a stick that cannot hold a large backup.

    Written to the System Log because it is the box talking about itself, and
    because the pill on the Backup card is only seen by somebody who goes
    looking. A stick is plugged in once and then forgotten, so the moment it is
    noticed is the only moment we can be sure of reaching anyone.

    Deliberately a `warning` and not an alarm: nothing is broken, backups are
    being written, and the stick will keep working until a backup passes 4 GB.
    Deduped per stick with a long cooldown, so a stick left in permanently does
    not say the same thing every half minute.
    """
    if not is_container_appliance(hass):
        return

    def _scan() -> list[tuple[str, str]]:
        from .backup import small_file_warning, usb_backup_dirs

        found: list[tuple[str, str]] = []
        for path in usb_backup_dirs():
            warning = small_file_warning(path.parent)
            if warning:
                found.append((path.parent.name, warning))
        return found

    try:
        found = await hass.async_add_executor_job(_scan)
    except Exception:
        _LOGGER.debug("media warning: scan failed", exc_info=True)
        return
    from ._log import sensa_log

    for name, warning in found:
        sensa_log(
            hass,
            "warning",
            f"USB stick \"{name}\": {warning}",
            source=SENSA_LOG_SOURCE_SYSTEM,
            # NO notification. A `warning` normally raises one
            # (`SENSA_LOG_PUSH_LEVELS`), which is right for "your backup
            # failed" and wrong for this: the stick works, backups are landing
            # on it, and nothing needs doing today. Interrupting somebody to
            # say a working destination might matter in a year is how a
            # customer learns to dismiss Sensa's notifications unread. It stays
            # in the System Log, and the Backup card carries the same caveat
            # under the destination pills where it is relevant.
            notify=False,
            context={"medium": name},
            dedup_key=f"backup_media_small_files:{name}",
            cooldown=timedelta(days=7),
        )


def _purge_candidates(
    backups: dict[str, Any],
    on_vehicle: Iterable[str],
    *,
    now: datetime,
    max_age: timedelta,
    min_copies: int = BACKUP_MIN_COPIES,
) -> list[str]:
    """Which backup ids the nightly sweep may delete. Pure, so it is testable.

    Split out from `async_purge_aged_backups` deliberately: everything that
    decides whether a backup lives or dies is here, with no engine and no clock
    of its own, because this is the one piece of the backup story that DESTROYS
    something and the rules it applies are the whole safety argument.

    Four ways a backup survives, and each is a case that would otherwise cost
    somebody their recovery point:

    * **It is not old enough.** The only rule anybody asked for.
    * **It is held offsite ONLY.** The cloud is not ours to delete from
      (`async_delete_from_every_destination`), so listing one here would produce
      a delete that can only fail. Asked with `held_offsite_only`, the same
      predicate the delete button and its hidden control use.
    * **It is among the `min_copies` NEWEST an on-vehicle destination holds.**
      A box that has been switched off for a week has nothing inside the window,
      and a sweep that took the age rule literally would hand the customer a van
      with one recovery point, or none. Whichever of the age and the count keeps
      MORE, keeps: three days of a box that runs every night is three copies
      anyway, so the floor only bites after downtime, which is exactly when
      losing the history would hurt. Per DESTINATION rather than one overall,
      because a delete removes the backup from every on-vehicle destination at
      once: with a single global exemption, a stick that had been out of the van
      since Tuesday would be emptied by a purge running against a disk that is
      fully up to date.
    * **Its date cannot be read.** "Could not tell" is never "old". A missing,
      malformed or naive timestamp keeps the backup, which costs disk; guessing
      costs the backup.
    """
    fleet = list(on_vehicle)
    newest_per_agent: dict[str, list[tuple[str, str]]] = {}
    aged: list[str] = []
    for backup_id, backup in (backups or {}).items():
        try:
            holders = {str(agent) for agent in (getattr(backup, "agents", None) or {})}
        except TypeError:
            continue
        if not holders:
            # **"Could not tell where it lives" keeps it, and that is the
            # OPPOSITE polarity to the delete button.** `held_offsite_only`
            # answers False for an empty set on purpose, so a customer pressing
            # delete is never blocked by an unreadable agent list. Fail-open
            # there means "go ahead"; here it would mean "destroy it", so the
            # empty case is answered before that predicate is ever asked.
            continue
        if held_offsite_only(holders, fleet):
            continue
        raw = str(getattr(backup, "date", "") or "")
        for agent in holders & set(fleet):
            # Every candidate is collected and the newest `min_copies` taken
            # below rather than a running top-N kept here: this is the rule that
            # decides what SURVIVES, and a hand-rolled incremental sort that is
            # subtly wrong loses backups silently.
            newest_per_agent.setdefault(agent, []).append((raw, str(backup_id)))
        try:
            age = now - datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            # Unreadable, or naive against an aware `now`. Keep it.
            continue
        if age > max_age:
            aged.append(str(backup_id))
    keep = {
        backup_id
        for held in newest_per_agent.values()
        for _date, backup_id in sorted(held, reverse=True)[: max(min_copies, 1)]
    }
    return [backup_id for backup_id in aged if backup_id not in keep]


async def async_purge_aged_backups(hass: HomeAssistant) -> int:
    """Delete on-vehicle backups past `BACKUP_MAX_AGE`. Returns how many went.

    The age is a ceiling on what may GO, never a floor on what is kept: the
    newest `BACKUP_MIN_COPIES` per destination survive whatever their age, so a
    van parked up for a fortnight comes back to three recovery points, not one.

    A step of the nightly run rather than a rule hung off the create, so it
    happens on a night the backup itself was skipped: a box that skipped because
    one had just been taken still has a day's worth of expiry to do, and tying
    the sweep to a successful create would mean the one thing that never gets
    tidied is the box that keeps colliding with its own updates.

    Never raises. It runs on a timer with nobody watching, and a failure to
    delete is a disk cost, never a reason to lose the backup that ran before it.
    """
    if not is_container_appliance(hass):
        return 0
    manager = _manager(hass)
    if manager is None:
        return 0
    if backup_in_progress(hass):
        # Not urgent enough to race the engine. A night's delay costs a tar; a
        # delete competing with a create for the same filesystem costs an
        # afternoon working out what happened.
        _LOGGER.debug("backup purge: skipped, a backup is being written")
        return 0
    from homeassistant.util import dt as dt_util

    try:
        backups, _agent_errors = await manager.async_get_backups()
    except Exception:
        # Could not ask, so nothing is deleted. The whole sweep fails CLOSED,
        # unlike most of this module: everything else here fails open because
        # the cost is a redundant backup, and here the cost is a lost one.
        _LOGGER.debug("backup purge: could not list backups", exc_info=True)
        return 0
    doomed = _purge_candidates(
        backups or {},
        # The customer's enabled destinations, which is also the list the delete
        # itself will act on. Read once and shared so the decision and the delete
        # cannot disagree about what "on the vehicle" means.
        await async_on_vehicle_agent_ids(hass),
        now=dt_util.now(),
        max_age=BACKUP_MAX_AGE,
    )
    if not doomed:
        return 0
    gone = 0
    for backup_id in doomed:
        ok, reason = await async_delete_from_every_destination(hass, backup_id)
        if ok:
            gone += 1
        else:
            # Per backup, and only at debug: a stick pulled out mid-sweep fails
            # every remaining delete, and a warning each would bury the night's
            # real news. The summary below is the line worth reading.
            _LOGGER.debug("backup purge: could not delete %s (%s)", backup_id, reason)
    _LOGGER.info(
        "backup purge: removed %d of %d backup(s) older than %d days",
        gone,
        len(doomed),
        BACKUP_MAX_AGE.days,
    )
    return gone


async def async_run_scheduled_backup(hass: HomeAssistant, client: Any) -> None:
    """The nightly backup, skipped when one has just been taken.

    Never raises: this runs on a timer with nobody to report to, so every
    outcome ends in the customer's log instead.
    """
    if not is_container_appliance(hass):
        return
    from homeassistant.util import dt as dt_util

    from ._log import sensa_log

    # Checked BEFORE the skip test, so it is still reported on a night the
    # backup itself is skipped: the waste is a property of the config
    # directory, not of whether we happened to take a copy of it tonight.
    await _async_warn_about_dead_databases(hass)

    if not await async_backup_agent_ids(hass):
        # WARNING, not info. The two other nightly skips are info because the
        # system IS saved; this one means it is not.
        sensa_log(
            hass,
            "warning",
            (
                "No backup was taken tonight: every backup destination is "
                "switched off. Turn one on under Settings -> Backup."
            ),
            source=SENSA_LOG_SOURCE_SYSTEM,
            dedup_key="backup_no_destinations",
            cooldown=timedelta(hours=12),
        )
        _LOGGER.warning("scheduled backup skipped: no destinations enabled")
        return

    latest = await async_latest_backup_at(hass)
    if latest is not None:
        try:
            age = dt_util.now() - latest
        except TypeError:
            # A naive timestamp from an older backup. Not worth guessing a
            # timezone over; treat it as "cannot tell" and back up.
            age = None
        if age is not None and timedelta(0) <= age < SKIP_IF_BACKUP_WITHIN:
            # Said out loud, because a customer looking for tonight's
            # "Overnight backup" and not finding one needs to know their system
            # IS backed up, not wonder whether the schedule is broken.
            sensa_log(
                hass,
                "info",
                (
                    "Tonight's backup was skipped: a backup was already taken at "
                    f"{latest.astimezone().strftime('%H:%M')}, so the system is "
                    "already saved."
                ),
                source=SENSA_LOG_SOURCE_SYSTEM,
                dedup_key="backup_skipped_recent",
                cooldown=timedelta(hours=12),
            )
            _LOGGER.info(
                "scheduled backup skipped: one was taken %s ago", age
            )
            return

    if backup_in_progress(hass):
        # The collision this whole area exists because of. Nothing to do but
        # say so: the backup that IS running covers tonight.
        sensa_log(
            hass,
            "info",
            "Tonight's backup was skipped: another backup was already running.",
            source=SENSA_LOG_SOURCE_SYSTEM,
            dedup_key="backup_skipped_busy",
            cooldown=timedelta(hours=12),
        )
        return

    # `wait=True`, unlike the manual button. The button has a card watching it
    # and can report a failure the moment the service returns; a 05:00 timer has
    # nobody, so if this returned at INITIATION a backup that failed while
    # writing would leave no trace anywhere the customer can see. Waiting costs
    # nothing here: it is a timer callback with nothing queued behind it.
    if not await async_create_named_backup(
        hass,
        client,
        name=SCHEDULED_BACKUP_NAME,
        automatic=True,
        wait=True,
        agent_ids=await async_nightly_agent_ids(hass),
    ):
        sensa_log(
            hass,
            "error",
            f"Tonight's backup failed: {last_backup_failure()}",
            source=SENSA_LOG_SOURCE_SYSTEM,
            dedup_key="backup_scheduled_failed",
            cooldown=timedelta(hours=12),
        )


def next_scheduled_backup(hass: HomeAssistant) -> datetime | None:
    """When our own nightly timer will next fire.

    Computed here because HA's scheduler is off, so `next_automatic_backup` is
    None and the Backup card would otherwise have nothing to show. This replaced
    reading HA's value; the old rule ("never recompute it here") applied while
    HA owned the trigger, and it does not any more.
    """
    from homeassistant.util import dt as dt_util

    try:
        now = dt_util.now()
        at = current_backup_time(hass)
        candidate = now.replace(
            hour=at.hour, minute=at.minute, second=0, microsecond=0
        )
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    except Exception:  # noqa: BLE001 - the card falls back rather than inventing
        return None


# -- failures reaching the customer -------------------------------------------
#
# The Sensa log (Intelligence -> Log) is where a customer finds out what their
# van has been doing. Until now a backup only ever wrote "Backup completed."
# there, so a backup that DIDN'T happen was invisible: it went to Home
# Assistant's system log, which on an appliance the customer cannot open.
#
# The 03:00 run on 2026-08-09 is the case that made this necessary. It fired
# exactly on time, collided with the pre-OTA snapshot for an update that started
# at 02:51, and was refused with "Backup manager busy: create_backup". HA logged
# that, re-armed for the following night and moved on. Nothing told the customer,
# and nothing in Sensa knew.
#
# **A log handler rather than the manager's event stream, and that is forced.**
# `async_initiate_backup` raises the busy refusal BEFORE it emits any
# `CreateBackupEvent` (read on the box against 2026.8.1), so an event subscriber
# cannot see the one failure we actually hit. HA's scheduler catches the raise
# and logs it, so the logger is the only place every scheduled failure -- busy
# or otherwise -- reliably appears.

_HA_BACKUP_LOGGER = "homeassistant.components.backup"

# One entry per cause per half hour. A wedged engine can fail every scheduled
# run, and a customer log that fills with the same sentence is one nobody reads.
_FAILURE_COOLDOWN = timedelta(minutes=30)


def _failure_sentence(text: str) -> tuple[str, str]:
    """(customer sentence, dedup key) for one of HA's backup errors.

    Named causes, not a catch-all. "the backup could not be started" was the old
    message for everything and it sent us hunting an encryption-key fault that
    did not exist; the same mistake in the customer's log would have them ring
    up about the wrong thing.
    """
    lowered = text.lower()
    if "busy" in lowered:
        return (
            (
                "The scheduled backup did not run because another backup was "
                "already in progress. The next one will run as normal."
            ),
            "backup_failed_busy",
        )
    if "password" in lowered or "decrypt" in lowered:
        return (
            "A backup could not be encrypted with this system's key.",
            "backup_failed_password",
        )
    if "space" in lowered or "no space left" in lowered:
        return (
            "A backup could not be written because the system is out of space.",
            "backup_failed_space",
        )
    return (f"A backup did not complete: {text}", "backup_failed_other")


def async_watch_backup_failures(hass: HomeAssistant) -> Any:
    """Mirror Home Assistant's own backup errors into the Sensa log.

    Returns an unsubscribe, always, so the caller can register it for teardown
    without checking anything.

    Attaches to HA's logger rather than parsing anything: the RECORD is the
    signal, and the text only picks which sentence the customer gets. A change
    in HA's wording therefore costs a less specific message, never a missed
    failure.
    """
    from ._log import sensa_log

    logger = logging.getLogger(_HA_BACKUP_LOGGER)

    class _SensaBackupFailureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Never raise out of a log handler: an exception here surfaces as
            # "--- Logging error ---" on every backup error and would make a
            # failure we are trying to report harder to see, not easier.
            try:
                text = record.getMessage()
            except Exception:  # noqa: BLE001
                return
            sentence, key = _failure_sentence(text)
            # `sensa_log` must run on the loop, and a log record can be emitted
            # from any thread (HA's backup work runs in executors).
            try:
                hass.loop.call_soon_threadsafe(
                    lambda: sensa_log(
                        hass,
                        "error",
                        sentence,
                        source=SENSA_LOG_SOURCE_SYSTEM,
                        context={"detail": text},
                        dedup_key=key,
                        cooldown=_FAILURE_COOLDOWN,
                    )
                )
            except Exception:  # noqa: BLE001, S110
                pass

    handler = _SensaBackupFailureHandler(level=logging.ERROR)
    logger.addHandler(handler)

    def _remove() -> None:
        logger.removeHandler(handler)

    return _remove


# -- restore ------------------------------------------------------------------
#
# The box is alive and the customer wants to undo something (backups-roadmap.md
# Phase 3). This does NOT solve hardware replacement: that needs a source off
# the box, which arrives with the USB destination.
#
# Restore is the one backup operation that is not merely slow but DESTRUCTIVE
# and self-interrupting. HA's Core writer does not restore anything inline: it
# validates the password, writes `.HA_RESTORE`, and calls `homeassistant.restart`
# blocking, so the tar is unpacked by the restore module during the NEXT startup
# (`manager.py`, read on the test box against 2026.8.1). Two consequences drive
# the shape of everything below:
#
#   * The call never returns normally. Whatever awaits it is still awaiting when
#     the process goes away, so a UI that waited on the service call could not
#     tell "restore failed" from "the box did as it was told".
#   * Everything that can be checked has to be checked BEFORE the point of no
#     return, because afterwards there is nobody left to report to.
#
# Hence the split: `async_prepare_restore` does every check that can fail and
# raises a reason the customer can act on, and `async_apply_restore` is the
# irreversible half, started as a background task so the service call returns.


class RestoreNotPossible(Exception):
    """The restore was refused before anything was changed.

    Carries a customer-facing sentence, not a stack trace: the whole point of
    the pre-flight is that a refusal here is recoverable and worth explaining.
    """


@dataclass(frozen=True)
class RestorePlan:
    """Everything the irreversible half needs, resolved while it was still safe."""

    backup_id: str
    name: str
    agent_id: str
    password: str | None
    restore_database: bool


def backup_engine_state(hass: HomeAssistant) -> str:
    """HA's backup manager state, or "" when it cannot be read.

    `idle` is the only state that accepts new work; the manager raises on
    anything else. Read rather than tracked, so a backup started outside Sensa
    (HA's own scheduler) is seen too.
    """
    manager = _manager(hass)
    if manager is None:
        return ""
    try:
        return str(manager.state)
    except AttributeError:
        return ""


def restore_in_progress(hass: HomeAssistant) -> bool:
    """True once a restore has been handed to HA and before the box restarts.

    Surfaced to the Backup card so the state survives a page reload during the
    window between pressing Restore and Home Assistant going away.
    """
    return backup_engine_state(hass) == "restore_backup"


def _restore_agent(backup: Any) -> tuple[str | None, bool]:
    """Which destination to read the backup from, and whether it is encrypted.

    Prefers the local agent, because on a Container appliance that is the copy
    already on the card and needs no download. The fallback to any other agent
    holding it is what makes this keep working when the USB and cloud
    destinations arrive, rather than needing to be revisited then.

    `protected` is a per-AGENT fact, not a per-backup one: HA re-encrypts on
    upload, so the same backup can be in the clear locally and encrypted on a
    stick. Reading it off the agent we actually chose is the only way the
    password decision can be right for both.
    """
    try:
        agents = dict(backup.agents)
    except (AttributeError, TypeError):
        return None, False
    failed = set(getattr(backup, "failed_agent_ids", None) or ())
    candidates = [a for a in agents if a not in failed]
    if not candidates:
        return None, False
    agent_id = LOCAL_AGENT_ID if LOCAL_AGENT_ID in candidates else candidates[0]
    return agent_id, bool(getattr(agents[agent_id], "protected", False))


def nested_config_mounts(config_dir: str) -> list[str]:
    """Storage mounted INSIDE the configuration directory, or [].

    Home Assistant restores by emptying the configuration directory and then
    extracting the backup over it, and `shutil.rmtree` cannot remove a mount
    point: it raises `OSError: [Errno 16] Resource busy`. That happens AFTER the
    directory has been emptied, so the box is left with neither its old
    configuration nor the backup's.

    That is not theoretical. The Z2M data volume used to be mounted at
    `/config/zigbee2mqtt`, and pressing Restore on the test box wiped it and
    then failed on exactly that path (2026-08-11). The mount has since moved out
    of `/config`, but a mount nobody has thought about can be added again by a
    compose change, so the check stays: it costs one read of `/proc/mounts` and
    it is the difference between a refusal and a destroyed system.

    Blocking, so callers hand it to an executor. Unreadable `/proc/mounts` means
    "could not ask", which returns [] rather than blocking a restore on a
    machine whose mounts we cannot see.
    """
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    root = config_dir.rstrip("/") + "/"
    found: set[str] = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        # /proc/mounts escapes the separators it would otherwise collide with.
        point = parts[1].replace("\\040", " ").replace("\\011", "\t")
        if point.startswith(root):
            found.add(point)
    return sorted(found)


async def _async_backup_holders(hass: HomeAssistant, backup_id: str) -> set[str]:
    """Agent ids currently holding a backup, or an empty set if we cannot tell.

    Empty means "could not ask", and every caller treats that as "carry on":
    refusing a delete because the list was unreadable would be a new way to get
    stuck.
    """
    manager = _manager(hass)
    if manager is None:
        return set()
    try:
        backup, _agent_errors = await manager.async_get_backup(backup_id)
    except Exception:
        _LOGGER.debug("delete_backup: could not read %s", backup_id, exc_info=True)
        return set()
    try:
        return {str(agent) for agent in (getattr(backup, "agents", None) or {})}
    except TypeError:
        return set()


async def async_delete_from_every_destination(
    hass: HomeAssistant, backup_id: str
) -> tuple[bool, str]:
    """Delete a backup from EVERY destination holding it. (ok, reason).

    The App can only unlink the tar under `/config/backups`, which is the whole
    story for a backup that is on this disk and nowhere else -- and no story at
    all for one that is not. A cloud-only backup (the App cannot see it; it is
    merged into the list by `async_backups_not_on_disk`) made the App 404, which
    reads as "already gone", so the card refreshed and the row was still there.
    Two taps, three taps, forever: "i cant delete backup Before update to
    1.0.0-edge.4828" (test box, 2026-08-11).

    Home Assistant's manager knows every agent, so it is the one thing that can
    delete a copy wherever it lives. `BackupNotFound` from an agent that never
    had it is not an error there, which is what makes this safe to call for a
    backup held in only one place.

    **The cloud is deliberately out of reach.** The vehicle's credential carries
    no delete permission, because the offsite copy exists for the van being
    stolen and a token on the van that could erase it would hand a thief the one
    copy they cannot otherwise touch. Cloud copies age out server-side instead
    (the lifecycle rule in `Sensa.Control.Licensing/infra/modules/
    storage-backups.bicep`). So this deletes from the on-vehicle destinations
    and says so, rather than asking the cloud and relaying a 403 the customer
    can do nothing about.

    Container-appliance only, the same rule as restore: on HAOS and BYO the
    engine is not ours to drive.
    """
    if not is_container_appliance(hass):
        return False, ""
    manager = _manager(hass)
    if manager is None:
        return False, ""
    agent_ids = await async_on_vehicle_agent_ids(hass)
    holders = await _async_backup_holders(hass, backup_id)
    if held_offsite_only(holders, agent_ids):
        # Only in the cloud. Refused with the reason, not attempted: the 403 it
        # would earn describes our security model, not anything the customer got
        # wrong, and it left an un-deletable row on the card (test box,
        # 2026-08-11). The card hides its delete control for these rows off the
        # SAME predicate, so this is the backstop rather than the notice.
        return False, (
            "this copy is held offsite by Sensa and cannot be deleted from the "
            "vehicle. It is removed automatically once it is old enough"
        )
    try:
        agent_errors = await manager.async_delete_backup(backup_id, agent_ids=agent_ids)
    except Exception as exc:  # noqa: BLE001 - the caller decides how to report
        _LOGGER.warning("delete_backup: engine delete failed for %s: %s", backup_id, exc)
        return False, str(exc)
    if agent_errors:
        # PARTIAL, and said so BOTH ways round. HA deletes from every agent at
        # once, so by the time one of them fails the others are already gone --
        # a flat "Delete failed" therefore tells the customer the opposite of
        # what happened, and they retry expecting nothing to have changed.
        #
        # It also matters that this reads as "could not", not "refused". A dead
        # SAS and a mobile link that dropped mid-request arrive here as the same
        # shape, and one of them comes good on its own: the first attempt on the
        # test box was a DNS timeout and the second a 403 (2026-08-11). Naming
        # the destination and inviting a retry is true of either.
        which = ", ".join(agent_label(str(agent)) for agent in agent_errors)
        detail = "; ".join(
            f"{agent_label(str(agent))}: {err}" for agent, err in agent_errors.items()
        )
        return False, (
            f"the copy in {which} could not be deleted ({detail}). "
            "Every other copy has been removed, so trying again retries only this one"
        )
    return True, ""


async def async_prepare_restore(
    hass: HomeAssistant, client: Any, *, backup_id: str
) -> RestorePlan:
    """Check everything that can be checked, or raise a reason why not.

    Deliberately strict where the create path is forgiving. A backup that fails
    to be created costs the customer a backup; a restore that goes wrong costs
    them the van's whole configuration, and the box will have restarted by the
    time anyone finds out.
    """
    if not is_container_appliance(hass):
        # Restore drives HA's engine directly, which is only ours to drive on a
        # box we ship. On HAOS the `admin` login is a full HA administrator with
        # Supervisor's own restore UI, and on BYO the Home Assistant is the
        # customer's own. The Backup tab is Container-only for the same reason.
        raise RestoreNotPossible("restoring is only available on a Sensa appliance")
    manager = _manager(hass)
    if manager is None:
        raise RestoreNotPossible("Home Assistant's backup engine is not available")
    # BEFORE anything else that could be mistaken for the reason: a restore that
    # cannot finish still empties the configuration directory on its way to
    # failing, so this one has to be a refusal, never a discovery made halfway
    # through. See `nested_config_mounts`.
    nested = await hass.async_add_executor_job(
        nested_config_mounts, hass.config.config_dir
    )
    if nested:
        raise RestoreNotPossible(
            "this system stores data inside its configuration folder ("
            + ", ".join(nested)
            + "), which a restore has to be able to clear. Restoring would erase "
            "the current configuration and then stop, so it has been refused. "
            "Please contact support"
        )
    backup_id = (backup_id or "").strip()
    if not backup_id:
        raise RestoreNotPossible("no backup was named")
    state = backup_engine_state(hass)
    if state and state != "idle":
        # Say which, because "busy" while a backup is being written is a wait,
        # and "busy" while a restore is already running is a very different
        # thing to tell somebody.
        if state == "create_backup":
            raise RestoreNotPossible(
                "a backup is being created; wait for it to finish and try again"
            )
        if state == "restore_backup":
            raise RestoreNotPossible("a restore is already running")
        raise RestoreNotPossible(f"the backup engine is busy ({state})")
    try:
        backup, _agent_errors = await manager.async_get_backup(backup_id)
    except Exception as exc:
        raise RestoreNotPossible(f"the backup could not be read ({exc})") from exc
    if backup is None:
        # The list can be stale (it refreshes on the poller), so a row the
        # customer can still see may name a backup that has since been pruned.
        raise RestoreNotPossible("that backup is no longer on this device")
    agent_id, protected = _restore_agent(backup)
    if agent_id is None:
        raise RestoreNotPossible("that backup is not held anywhere this device can read")
    password: str | None = None
    if protected:
        password = await _async_encryption_key(client)
        if not password:
            # Every Sensa backup is encrypted, so this is the common failure
            # mode, and it is recoverable: the App holds the key, so a restore
            # attempted while it is restarting simply needs retrying.
            raise RestoreNotPossible(
                "the encryption key could not be read from the Sensa App, "
                "so the backup cannot be unlocked"
            )
    # Ask the backup what it contains rather than assuming our own policy: HA's
    # writer refuses a restore that includes neither Home Assistant nor the
    # database, and asking it to restore a database that was never in the tar
    # is a promise nothing can keep. `include_database` is True today, so this
    # normally resolves to True, but a backup taken under a different policy (or
    # by HA itself) still restores correctly.
    return RestorePlan(
        backup_id=backup_id,
        name=str(getattr(backup, "name", "") or backup_id),
        agent_id=agent_id,
        password=password,
        restore_database=bool(getattr(backup, "database_included", False)),
    )


async def async_apply_restore(hass: HomeAssistant, plan: RestorePlan) -> None:
    """The irreversible half. Raises with a customer-facing reason on failure.

    `restore_addons` and `restore_folders` are passed as None because the Core
    writer REFUSES them outright ("Addons and folders are not supported in core
    restore"); on Container there are no Supervisor add-ons to restore anyway.
    Every keyword is required by HA's signature, so none of them can be left to
    a default that might change under us.
    """
    manager = _manager(hass)
    if manager is None:
        raise RestoreNotPossible("Home Assistant's backup engine is not available")
    try:
        await manager.async_restore_backup(
            plan.backup_id,
            agent_id=plan.agent_id,
            password=plan.password,
            restore_addons=None,
            restore_database=plan.restore_database,
            restore_folders=None,
            restore_homeassistant=True,
        )
    except Exception as exc:
        raise RestoreNotPossible(_restore_failure_reason(exc)) from exc


# Why the last restore failed AFTER the customer committed to it, in words they
# can act on. The pre-flight reports its refusals through the service call, but
# once the restore is handed to HA the service has already returned, so a
# failure past that point has nowhere to go: the box does not restart, the card
# sits on "Restoring..." and the customer is told nothing. This is how it gets
# back to them, stamped onto the backup status the card already reads.
_LAST_RESTORE_FAILURE: dict[str, str] = {}


def last_restore_failure() -> str:
    """The reason the last committed restore failed, or "" if none has."""
    return _LAST_RESTORE_FAILURE.get("reason", "")


def note_restore_failure(reason: str) -> None:
    _LAST_RESTORE_FAILURE["reason"] = reason


def clear_restore_failure() -> None:
    """Forget a previous failure, so a fresh attempt starts with a clean card."""
    _LAST_RESTORE_FAILURE.pop("reason", None)


def _restore_failure_reason(exc: Exception) -> str:
    """Turn an HA backup exception into something a customer can act on."""
    text = str(exc) or exc.__class__.__name__
    code = getattr(exc, "error_code", "")
    if code == "password_incorrect" or "password" in text.lower():
        # The key is the box's own, so this means the backup was made under a
        # different key: a restore of somebody else's tar, or one from before a
        # rebuild. Worth naming, because retrying will never help.
        return "the backup could not be unlocked with this device's encryption key"
    if "busy" in text.lower():
        return "the backup engine is busy; try again in a few minutes"
    return f"Home Assistant refused the restore ({text})"
