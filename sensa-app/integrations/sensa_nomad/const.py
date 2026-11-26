"""Constants for the Sensa Nomad integration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from homeassistant.const import Platform

DOMAIN = "sensa_nomad"
NAME = "Sensa Nomad"

# Integration slug for Zigbee devices surfaced via Zigbee2MQTT. These devices
# are owned by HA's shared `mqtt` integration (Z2M publishes them over MQTT);
# Sensa only enumerates + registers a backend row. Used by the discovery
# source, the register-only add branch, and the dashboard resolver. NB: this is
# NOT a catalog `accept_native` domain and has no BLE profile, so
# `_native_domain_for_integration` returns None for it — a Sensa-side removal
# must never delete the HA mqtt device (Z2M owns it).
INTEGRATION_ZIGBEE2MQTT = "zigbee2mqtt"

# Integration slug for an OBD2 adapter (the MeatPi WiCAN Pro) surfaced via its
# Home Assistant MQTT-discovery feed. Like Zigbee2MQTT, the HA device + sensor
# entities are owned by HA's shared `mqtt` integration (WiCAN publishes them);
# Sensa only enumerates + registers a backend row. NOT a catalog `accept_native`
# domain and no BLE profile, so `_native_domain_for_integration` returns None for
# it — a Sensa-side removal must never delete the HA mqtt device (WiCAN owns it).
INTEGRATION_WICAN = "wican"

# Z2M control plane. Pairing is driven from Nomad (the Discovery picker's "Pair
# Zigbee" button) by publishing to the Z2M bridge's MQTT request API, so the
# customer never opens the Z2M add-on. The base topic is Z2M's default; if an
# install customised it, this is the single place to change. Permit-join runs
# for the Zigbee maximum (254 s ≈ 4 min) — one bounded window, then it closes
# itself (leaving join open invites uninvited devices).
Z2M_BASE_TOPIC = "zigbee2mqtt"
Z2M_PERMIT_JOIN_TOPIC = f"{Z2M_BASE_TOPIC}/bridge/request/permit_join"
Z2M_PERMIT_JOIN_SECONDS = 254
# Pressing Pair while Z2M is restarting publishes permit-join to a topic with
# no subscriber. MQTT accepts it, the publish "succeeds", and the request is
# simply gone: the customer holds the button on the device, nothing joins, and
# nothing anywhere says why. Observed on the dev box 2026-09-19 at 09:21:05,
# eleven seconds before Z2M finished starting.
#
# Adopting a coordinator RESTARTS Z2M, and pressing Pair straight afterwards is
# the normal thing to do, so this is the common path rather than an edge. Wait
# for the bridge instead, up to roughly a Z2M cold start.
Z2M_PAIR_BRIDGE_WAIT_SECONDS = 5
Z2M_PAIR_BRIDGE_WAIT_ATTEMPTS = 18

# Z2M's own view of the network, read (never written) from two RETAINED bridge
# topics. Retained matters: a subscribe gets the current payload immediately,
# so nothing has to poll and a late-starting integration is not blind until the
# next Z2M restart.
#
# `bridge/devices` is the ONLY place the raw Zigbee identity of a device is
# visible to us. Z2M's HA discovery publishes `manufacturer = definition.vendor`
# and `model_id = definition.model`, i.e. the RESOLVED product, and it publishes
# NOTHING AT ALL for a device it cannot resolve. So a device whose Tuya batch
# code Z2M does not know is not merely mislabelled in HA, it is ABSENT from HA,
# and no amount of registry reading will find it. The bridge payload carries
# `model_id` (the raw Zigbee modelID, e.g. `TS0601`) and `manufacturer` (the raw
# manufacturerName, e.g. `_TZE204_8fffc3kb`) for every paired device, resolved
# or not. Verified against Z2M 2.14.1 (`dist/extension/bridge.js`, the
# `devices.push({...})` payload).
Z2M_BRIDGE_DEVICES_TOPIC = f"{Z2M_BASE_TOPIC}/bridge/devices"
Z2M_BRIDGE_INFO_TOPIC = f"{Z2M_BASE_TOPIC}/bridge/info"
# hass.data key for the parsed bridge cache both topics feed. A ROOT key of its
# own, not a member of `hass.data[DOMAIN]`: that dict is keyed by config-entry
# id, and a plain string key sitting among entry ids is exactly the kind of
# thing an entry-iterating loop trips over.
Z2M_BRIDGE_DATA_KEY = f"{DOMAIN}_z2m_bridge"
# A device whose Z2M interview FAILED joined the network but never told Z2M what
# it is, so Z2M publishes nothing for it and Sensa has no model, no image and no
# entities. Routine for battery sensors (Aqara above all): they fall asleep
# mid-interview. Z2M never retries on its own short of a re-pair, so we ask it to
# (`bridge/request/device/interview`) on this cadence while the device stays
# failed; the attempt that lands while the device is awake completes it.
# Observed on .116 2026-10-09: an Aqara leak sensor failed at `simpleDescRsp`.
Z2M_REINTERVIEW_TOPIC = f"{Z2M_BASE_TOPIC}/bridge/request/device/interview"
Z2M_REINTERVIEW_INTERVAL_SECONDS = 30
# Sent with the IEEEs that went FAILED -> SUCCESSFUL, so the integration can
# auto-add them exactly as if the customer's Pair press had worked first time.
SIGNAL_Z2M_INTERVIEW_RECOVERED = "sensa_nomad_z2m_interview_recovered"
# Z2M's model code for the Aqara Cube T1 Pro.
CUBE_T1_PRO_MODEL = "CTP-R01"
# The When picker's "either way" for that cube: a press of our own, not a Z2M
# action, standing for the two it sends when turned.
CUBE_TURN_PRESS = "turn"
CUBE_TURN_ACTIONS = ("rotate_left", "rotate_right")

# The "Zigbee has stopped" alarm. A root hass.data key for the same reason as
# the bridge cache above.
Z2M_BRIDGE_DOWN_DATA_KEY = f"{DOMAIN}_z2m_bridge_down_since"
Z2M_BRIDGE_DOWN_ALARM_KEY = "zigbee_bridge_offline"
# How long the bridge must stay down, AFTER Home Assistant is fully started,
# before we call it down. Everything about a restart looks identical to a dead
# bridge: the mqtt integration is still setting up, Z2M itself takes ~30s, and a
# coordinator that has just been repointed backs off between attempts. An alarm
# that fires whenever Sensa restarts is one the customer learns to ignore, which
# costs more than the alarm was ever worth.
#
# Fifteen minutes, not five, because the window that has to be survived is not
# an HA restart (the clock only starts once HA is running, and `hass.data` is
# empty on a fresh process anyway) but an OTA: HA comes back first and Z2M is
# then pulled and recreated behind it. Fifteen also puts the alarm after the
# Z2M watchdog's first three internal retries (1, 5, 15 min — `Z2M_WATCHDOG` in
# the compose file), so a blip the watchdog recovers from on its own never
# reaches the customer.
Z2M_BRIDGE_DOWN_GRACE_SECONDS = 900

# The zigbee-herdsman-converters version is the table that decides whether a
# Tuya batch code is recognised at all, so it is the RIGHT thing to compare, not
# the Z2M version that wraps it. Below this floor we tell the customer their
# identity table is behind; at or above it, an unrecognised device is a genuinely
# new batch and the report has to go upstream. Raise this when we validate a
# newer Z2M, never speculatively: claiming a floor we have not run is how a
# healthy box gets told to update for nothing.
Z2M_MIN_CONVERTERS_VERSION = "26.105.0"

# `device_subtype` sentinel for a Z2M device whose HA entities classify cleanly
# but whose model matches no catalog entry — a new Tuya batch on a product we do
# support. It exists so such a device reaches the picker's DISCOVERED group
# (addable) via its own filter rule instead of the debug-gated Others group,
# where the customer would never see it. Never a catalog model, so it can never
# collide with one from `known_zigbee_models()`.
ZIGBEE_UNRECOGNISED_SUBTYPE = "unrecognised_batch"

# Z2M's web frontend. 8080, NOT 8099: 8099 is the Sensa App's own server port,
# and the Container compose file publishes Z2M as `8080:8080`, so a frontend
# bound to anything else leaves that published port pointing at nothing and the
# Z2M page unreachable on the LAN.
#
# BASE_URL serves the page from a sub-path, which is what lets the Sensa-only
# support address carry it at `/z2m` on the hostname that ALREADY has a
# Cloudflare Access application in front of it, instead of needing a fourth
# name and a second Access application. A Cloudflare tunnel does not strip the
# path prefix before forwarding, so Z2M has to know its own base URL or every
# asset 404s. Local access moves to `http://<box>:8080/z2m` as well.
#
# `/z2m`, NOT `/zigbee`: Zigbee is the protocol and ZHA is a different
# integration entirely, so a `/zigbee` path reads as a route to something
# this is not. The page is Zigbee2MQTT's and the path says so.
#
# CHANGING THIS VALUE needs the self-heal below to know the old one, or a
# box already carrying the previous path keeps serving there while the
# tunnel routes the new one, and every request 404s.
#
# These two values are also written by the HAOS add-on's bash seed
# (`app/addons/sensa_app/rootfs/etc/cont-init.d/seed-zigbee2mqtt.sh`) and are
# matched by the licensing Function's ingress rule
# (`Sensa.Control.Licensing shared/cloudflare.py` `_Z2M_PATH`). bash cannot
# import this module, so `nomad/tests/test_z2m_frontend_seed.py` asserts the
# two seeds agree rather than leaving it to be noticed in the field.
Z2M_FRONTEND_PORT = 8080
Z2M_FRONTEND_BASE_URL = "/z2m"

# Bytes of randomness behind the Z2M frontend password. Z2M stores and compares
# it in clear, so its only defence is being unguessable; 32 bytes of urlsafe
# base64 is ~43 characters, which is fine to paste and hopeless to brute force.
#
# The page has NO login of its own by default, and it can pair, unpair and
# rewrite a customer's Zigbee network. That is why the remote route to it could
# not be published behind nothing: this token is what makes it safe to publish
# without a Cloudflare Access application in front of the hostname.
Z2M_FRONTEND_TOKEN_BYTES = 32

# Base URLs we have shipped and moved on from. The self-heal rewrites any of
# these to the current value; anything NOT in this list is somebody's own
# setting and is left alone. A list rather than a single "previous" because a
# box can be several releases behind and still has to land on the current path.
Z2M_FRONTEND_SUPERSEDED_BASE_URLS = ("/zigbee",)

# Dispatcher signals — internal pub/sub between vendor coordinators and
# top-level lifecycle handlers in `__init__.py`.
#
# SIGNAL_DASHBOARD_REGEN_REQUESTED: vendor modules fire this when they
# want the dashboards rebuilt outside of the add/remove path (e.g.
# post-enrichment display-name upgrade, post-poll capability detection).
# The listener debounces so a burst of enrichment renames produces a
# single rebuild.
SIGNAL_DASHBOARD_REGEN_REQUESTED = "sensa_nomad_dashboard_regen_requested"

# SIGNAL_MODES_REGEN_REQUESTED: fires on every add/remove/rename/enrich
# boundary that can change which entities should participate in which
# scene. Sibling of the dashboard signal, independent listener so one
# can be regenerated without the other. Same 2 s debounce so a burst
# of events produces one scene-YAML rewrite.
SIGNAL_MODES_REGEN_REQUESTED = "sensa_nomad_modes_regen_requested"

# SIGNAL_CURRENT_MODE_CHANGED: fires when the active Nomad Mode changes
# (i.e. `_async_handle_activate_mode` finishes a scene.turn_on). Carries
# one arg — the new mode slug, or "" for "no mode active". Listened to
# by the per-mode `binary_sensor.sensa_nomad_mode_<slug>_active`
# entities (so the Habitation Overview tile auto-tints via
# `state_color: true` + `--state-binary_sensor-active-color`) and by
# `sensor.sensa_nomad_current_mode` (which feeds the Habitation badge
# row). Sensors derive their state from the live signal value, so a
# stale subscriber never reports the wrong mode.
SIGNAL_CURRENT_MODE_CHANGED = "sensa_nomad_current_mode_changed"

# Broadcast from `_async_handle_set_startup_mode` after the
# customer's "default mode on HA startup" choice persists. Carries
# the new slug (or "" when cleared). Listened to by
# `sensor.sensa_nomad_current_mode` so its `startup_mode` attribute
# reflects the live setting; the Mode Editor webcard reads that
# attribute to seed its dropdown without an extra service round-trip.
SIGNAL_STARTUP_MODE_CHANGED = "sensa_nomad_startup_mode_changed"

# Broadcast whenever the scene CATALOG the customer sees changes: a
# reorder, a preset hidden / shown, a scene created / renamed / deleted.
# Carries `ModeOverrides.modes_payload()` (every scene in display order
# with enabled / custom / locked flags). Listened to by
# `sensor.sensa_nomad_current_mode`, which mirrors it as `attributes.modes`
# (+ `mode_order`); the Mode Editor tabs, Home/Cab tiles and Mobile pills
# all render from that attribute, so one edit reaches every surface
# without a dashboard rebuild.
SIGNAL_MODES_CHANGED = "sensa_nomad_modes_changed"

# Broadcast whenever the light-group catalog changes: a group created,
# renamed, re-membered, reordered or deleted. Carries
# `LightGroupStore.groups_payload()`. Each `light.sensa_nomad_group_<slug>`
# entity listens for its own slug and re-subscribes to the new member list,
# so adding a light to a group takes effect without a reload.
SIGNAL_LIGHT_GROUPS_CHANGED = "sensa_nomad_light_groups_changed"

# EVENT_PANEL_CONFIG_CHANGED: a hass *bus* event (NOT a dispatcher signal —
# it must reach the browser), fired when an open `<sensa-panel>` should
# re-fetch its shell config via the `sensa_nomad/panel_config` WS command.
# Fired at the end of the full dashboard rebuild, and directly by the
# home-layout select for an instant Home-tab swap without the rebuild.
EVENT_PANEL_CONFIG_CHANGED = "sensa_nomad_panel_config_changed"

# --- Cab navigation location source ----------------------------------------
# Which device the box treats as the van's GPS. This select is the SYSTEM
# DEFAULT (set on Settings -> Radar) and is what `resolve_nav_position` honours,
# so the Radar nearest-amenity sensors, Tracking and Schengen all follow it.
#
# The Cab Home / Navigation tab-strip dropdown offers `(System Default)` plus a
# per-screen OVERRIDE (client-side, localStorage), so a driver can put one
# screen on their own phone without changing what the box uses. That override
# resolves through the select's `resolved_entity` attribute rather than its own
# copy of the precedence -- see `webcards/src/shared/cab-location.ts`.
#
# A van may have no vehicle GPS at all: plenty of customers drive on a phone,
# drivers swap, and a partner's phone may not even be in the van. That is why
# the choice is a first-class customer setting and not an internal detail.
#
# Two fixed labels + one per selectable GPS tracker. "Auto" was "Auto (this
# screen)" until 2026-07-31, which was true when the select really was
# per-screen and merely confusing once it became the box-wide default.
CAB_LOCATION_AUTO = "Auto"
CAB_LOCATION_VEHICLE = "Vehicle GPS"
# What the Cab tab-strip dropdown shows for "use the system default". Lives
# here so the integration and the card cannot drift on the wording; it is NEVER
# one of the select entity's own options (a system setting cannot default to
# itself), only prepended client-side.
CAB_LOCATION_SYSTEM_DEFAULT = "(System Default)"
# entry.options key holding the person last picked on the Cab people bar, and
# its hass.data mirror for a cheap read. Durable in options so the choice
# survives a restart, like every other customer pick.
DRIVER_PERSON_OPTION = "driver_person"
DRIVER_PERSON_KEY = "driver_person_id"
# The location-source select's entity_id, shared by the select (options
# provider) and the frontend dropdown / map cards.
CAB_LOCATION_SELECT_ENTITY_ID = "select.sensa_nomad_cab_location_source"
# Vehicle tracker entity_id, the "Vehicle GPS" option's target + the HA Map.
CAB_VEHICLE_TRACKER_ENTITY_ID = "device_tracker.sensa_nomad_vehicle"
# The vehicle tracker's attribute carrying the receiver's own ground speed in
# mph. Written by the tracker, read by the movement detector (and so the Cab
# speedometer); one name so the two ends cannot drift.
TRACKER_SPEED_MPH_ATTR = "speed_mph"

# --- Auto-mode plausibility gate -------------------------------------------
# Auto's last resort is "the phone that reported most recently", which ranks on
# `last_updated` alone. In a van with no vehicle GPS that rule DECIDES, and it
# picks wrong in the obvious household case: a partner's phone sitting at home
# that just reported beats the driver's phone that reported thirty seconds ago.
# (`last_updated` also bumps on any state write, not only a new fix.)
#
# So a candidate is rejected when reaching it from the last accepted position
# would need an impossible speed. Physics rather than a fixed radius, because it
# needs no tuning and SELF-HEALS: the allowance grows with elapsed time, so a van
# trailered overnight or left off for a week is accepted again on its own.
#
# `hass.data[DOMAIN][NAV_LAST_FIX_KEY]` holds `(lat, lon, utc_timestamp)`.
NAV_LAST_FIX_KEY = "nav_last_fix"
# ~200 km/h. Generous on purpose: this is a nonsense filter, not a speed limit,
# and a ferry or a motorway tow must not trip it.
NAV_MAX_PLAUSIBLE_SPEED_MPS = 55.0
# Floor for GPS scatter and back-to-back polls, so two fixes seconds apart are
# never judged implausible over a few hundred metres of drift.
NAV_PLAUSIBILITY_FLOOR_M = 2000.0

# --- People (Settings -> People) -------------------------------------------
# `entry.options["people_members"]` = {mysensa_member_id: person_id}: which HA
# person each MySensa member was imported as.
#
# Its ONLY job is making a re-import idempotent. Without it a second "Download
# users from MySensa" would add a duplicate of everyone rather than refreshing
# the names and photos of the people it created the first time.
#
# There is no automatic join to fall back on: a member is {id, display_name,
# email_masked}, the email is masked before it reaches the box, and guessing by
# display name would quietly merge two different humans who share a first name.
# So the mapping is recorded at import time and nowhere else.
#
# A person the customer typed in by hand has no entry here, which is exactly
# right: they came from nowhere, so a MySensa download must not claim them.
PEOPLE_MEMBERS_OPTION = "people_members"

# Phones whose auto-made person the customer deleted. The seeder skips these,
# so a delete sticks instead of the person coming straight back.
PEOPLE_DECLINED_TRACKERS_OPTION = "people_declined_trackers"

# --- Sensa log (customer-facing message log + alert framework) -------------
# A customer-facing, non-technical message log — NOT a replacement for HA's
# system log / a debug-crash channel. Backend code and the alert engine write
# helpful, human-readable messages ("Low fresh water near a water source") that
# the customer reads on the Settings → Log tab. The store is an in-memory ring
# buffer (`collections.deque`) on hass.data, backed by a JSONL file for restart
# survival. See `_log.py` (store + writer) and `alerts.py` (rule engine).

# Customer-facing MESSAGE TYPES, ascending urgency — drives the Log tab's
# per-type filter and the push set below. The four customer types are
# informational / warning / error / alarm (`info` is shown as "Informational");
# `debug` is an internal diagnostic level only — captured to the store for
# support triage, never a customer "type", and hidden by default. `alarm` is the
# Log MIRROR of the managed-alarm subsystem (see `alarms.py`) — the actual
# sounding/notification of an alarm is owned there, NOT by the log push below.
# (The legacy `notification` level was retired 2026-06: it conflated severity
# with "also raise an HA notification" — that side-effect is now decided per
# type via SENSA_LOG_PUSH_LEVELS.)
SENSA_LOG_LEVELS: tuple[str, ...] = (
    "debug", "info", "warning", "error", "alarm",
)
# What an unrecognised level clamps to (`_log.record`). This was also the
# fresh-install default of `select.sensa_nomad_log_level`, the Log card's old
# minimum-severity dropdown; the per-type chips replaced that in 2026-06 and the
# entity was retired in 2026-08. The constant stays as the clamp target.
SENSA_LOG_LEVEL_DEFAULT = "info"
# Message types that ALSO raise a one-shot HA notification (persistent_notification
# + best-effort mobile push), since a driving customer isn't watching the Log
# tab. debug/info stay tab-only; `alarm` is deliberately EXCLUDED — the alarm
# subsystem raises its own sticky, re-asserting notification, so pushing here too
# would double-notify.
SENSA_LOG_PUSH_LEVELS: tuple[str, ...] = ("warning", "error")

# WHICH LOG an entry belongs to — orthogonal to its level. `vehicle` is the van
# talking about itself (low water, low battery, cold charging, an instinct
# firing) and surfaces on Intelligence → Log; `system` is the box talking about
# itself (backups, updates, licence, restore, devices added or removed) and
# surfaces on Settings → System Log. Two questions, two tabs, one store.
#
# The two tabs are a DISPLAY split: the store captures both, and the ring-buffer
# cap / retention window apply per source, so a chatty update day cannot evict
# the customer's vehicle history.
SENSA_LOG_SOURCE_VEHICLE = "vehicle"
SENSA_LOG_SOURCE_SYSTEM = "system"
SENSA_LOG_SOURCES: tuple[str, ...] = (
    SENSA_LOG_SOURCE_VEHICLE, SENSA_LOG_SOURCE_SYSTEM,
)
# READ-side default for an entry that carries no `source` — every entry written
# before the split, and any that a future producer forgets to tag. `vehicle` is
# where all of those are displayed TODAY, so an upgrade moves nothing and hides
# nothing. This is the only default: `sensa_log()` requires the keyword on the
# WRITE side, so a producer states which log it belongs to rather than
# inheriting an answer.
SENSA_LOG_SOURCE_DEFAULT = SENSA_LOG_SOURCE_VEHICLE

# Filesystem path (relative to HA config_dir) for the durable JSONL backing
# file. Parallel to the other Sensa-managed content under `nomad/`.
SENSA_LOG_PATH = "nomad/.sensa_log.jsonl"

# What we last TOLD the customer about their support contract (see
# `_support_notice.py`). Durable because the alternative -- comparing against the
# previous status in memory -- re-announces a lapse after every HA restart, and
# `sensa_log`'s own dedup map is in-memory and never rebuilt from the persisted
# log, so it cannot carry the "already said this" fact across a reboot either.
SENSA_SUPPORT_NOTICE_PATH = "nomad/.sensa_support_notice.json"
SENSA_SUPPORT_NOTICE_STORE_KEY = "support_notice_store"

# The external URL we last wrote into HA's core config from the remote-access
# hostname. Persisted so we can tell OUR value from one the customer typed, and
# never overwrite theirs. See _external_url.py.
SENSA_EXTERNAL_URL_PATH = "nomad/.sensa_external_url.json"
SENSA_EXTERNAL_URL_STORE_KEY = "external_url_store"

# What we last told the customer about a Radar Premium SEAT refusal, per member
# (see `_seat_notice.py`). Durable for exactly the reason above, and per-member
# because the answer differs by who the driver picker has selected: telling the
# customer once that Jo has no seat must not silence the same news about Sam.
SENSA_SEAT_NOTICE_PATH = "nomad/.sensa_seat_notice.json"
SENSA_SEAT_NOTICE_STORE_KEY = "seat_notice_store"
# Cold-charging protection: durable record of the charger states the protect
# engine changed, so restore survives an HA restart mid-cold-snap.
SENSA_COLD_CHARGE_PATH = "nomad/.sensa_cold_charge.json"
SENSA_COLD_CHARGE_STORE_KEY = "cold_charge_store"
# Miner autopilot: durable record of the miner relays it switched on and
# whether it cut the diesel heater, so a restart mid-session re-adopts the
# miners it owns and still knows to put the heater back (`crypto_autopilot.py`).
SENSA_CRYPTO_AUTOPILOT_PATH = "nomad/.sensa_crypto_autopilot.json"
SENSA_CRYPTO_AUTOPILOT_KEY = "crypto_autopilot"
# Ring-buffer cap — newest N entries kept in memory and on disk, PER SOURCE.
# This is a HARD backstop, not the customer's retention setting: the log can
# never grow past it however the retention window below is set, and
# `_write_jsonl` rewrites the whole file from the deques, so the backing file is
# capped too. Per source rather than shared because the system log is by far the
# chattier of the two, and one shared buffer would let a busy update-and-backup
# week evict the vehicle history the customer actually went looking for.
SENSA_LOG_MAX_LINES = 1000
# Runtime key under hass.data[DOMAIN] for the singleton SensaLogStore.
SENSA_LOG_STORE_KEY = "sensa_log_store"

# How far back the Log tab's history goes, in DAYS — the customer's choice, on
# Settings → System, under History at the bottom of the File Integrity panel
# (`number.sensa_nomad_log_retention`).
# Age-based rather than a count, because "1000 entries" is a meaningless unit to
# a van owner and a busy week and a quiet month look identical in it. The
# SENSA_LOG_MAX_LINES cap still applies underneath, so this only ever makes the
# window SHORTER than the ring buffer would.
#
# A free NUMBER of days rather than a fixed list, so the customer picks the
# window that suits them instead of the nearest preset (Richard, 2026-08-09).
#
# ZERO MEANS KEEP EVERYTHING. A sentinel rather than a second control, because
# the alternative is an "unlimited" checkbox that has to be kept in step with
# the box beside it; the field label states it, so it is discoverable rather
# than folklore. The ring-buffer cap still applies at 0, so "everything" has
# always meant "the newest SENSA_LOG_MAX_LINES entries".
SENSA_LOG_RETENTION_MIN_DAYS = 0
# ~10 years. Not a real constraint (the ring buffer bites first) — it exists so
# a slipped digit can't set a window we'd have to reason about.
SENSA_LOG_RETENTION_MAX_DAYS = 3650
# Fresh-install default, in days.
SENSA_LOG_RETENTION_DEFAULT_DAYS = 14
LOG_RETENTION_ENTITY_ID = "number.sensa_nomad_log_retention"
# Where the retention select publishes its resolved window (days, or None for
# "keep everything") so the log store can pick it up whatever the setup order.
#
# Load-bearing: platforms are forwarded at `async_setup_entry` line ~681 but the
# log store is only created at ~879 (via `_async_register_web_cards`), so the
# select's `async_added_to_hass` runs BEFORE the store exists and cannot hand the
# value over directly. Without this key the restored choice was silently dropped
# on every boot and nothing was ever pruned. Whichever side runs second reads it.
SENSA_LOG_RETENTION_DAYS_KEY = "log_retention_days"

# EVENT_SENSA_LOG_CHANGED: a hass *bus* event (NOT a dispatcher signal — it must
# reach the browser), fired when a new entry is appended so an open
# `custom:sensa-log-card` re-fetches via the `sensa_nomad/get_log` WS command.
# Mirrors EVENT_PANEL_CONFIG_CHANGED's reach-the-browser requirement.
EVENT_SENSA_LOG_CHANGED = "sensa_nomad_log_changed"

# --- Managed alarms (the alarm subsystem, parallel to the log) --------------
# An alarm is a STATEFUL, managed message: once raised it stays active and
# re-asserts (re-sounds) every N minutes until the customer cancels or snoozes
# it. Distinct from the append-only log — only an alarm's state transitions
# (raised / snoozed / cancelled) are mirrored into the log as `alarm`-type
# messages. See `_alarms.py` (store) + `alerts.py` (code-only producers).

# Active alarms are persisted here so they survive a restart while still live
# (unlike the in-memory log ring). A JSON object keyed by alarm `key`.
SENSA_ALARMS_PATH = "nomad/.sensa_alarms.json"
# Runtime key under hass.data[DOMAIN] for the singleton SensaAlarmStore.
SENSA_ALARMS_STORE_KEY = "sensa_alarms_store"
# Fired when the active-alarm set changes (raised / snoozed / cancelled / a
# re-sound) so an open Alarms card + the cross-panel banner re-fetch. A hass
# *bus* event (must reach the browser), mirroring EVENT_SENSA_LOG_CHANGED.
EVENT_SENSA_ALARMS_CHANGED = "sensa_nomad_alarms_changed"
# Default re-assert cadence (minutes) for an alarm raised without an explicit
# `resound_every`.
SENSA_ALARM_DEFAULT_RESOUND_MIN = 5
# Default snooze duration (minutes) when `snooze_alarm` is called without one.
SENSA_ALARM_DEFAULT_SNOOZE_MIN = 10
# How often the integration ticks the alarm store to re-sound / un-snooze.
SENSA_ALARM_TICK_SECONDS = 30

# --- Schengen 90/180 usage tracker (Intelligence → Schengen tab) ------------
# Visa-exempt visitors (UK post-Brexit, etc.) may spend at most LIMIT days of
# presence in any rolling WINDOW-day period across the Schengen area. We sample
# the vehicle tracker's country, stamp each calendar day in/out, and compute the
# rolling usage. See `_schengen.py` (store + maths) + `_schengen_geo.py`
# (country resolution) + the `custom:sensa-schengen-card`.
SENSA_SCHENGEN_PATH = "nomad/.sensa_schengen.json"
# Runtime key under hass.data[DOMAIN] for the singleton SensaSchengenStore.
SENSA_SCHENGEN_STORE_KEY = "sensa_schengen_store"
# Fired when the store changes (enabled toggle / day mark / range / clear / a
# sampler recompute) so an open Schengen card re-fetches via the
# `sensa_nomad/schengen_data` WS command. A hass *bus* event (must reach the
# browser), mirroring EVENT_SENSA_ALARMS_CHANGED.
EVENT_SENSA_SCHENGEN_CHANGED = "sensa_nomad_schengen_changed"
# Fired when a BLE capture changes state, takes a marker or finishes, so an
# open BLE Capture card re-reads `sensa_nomad/ble_capture_status`. A bus event,
# mirroring EVENT_SENSA_SCHENGEN_CHANGED.
EVENT_BLE_CAPTURE_CHANGED = "sensa_nomad_ble_capture_changed"
# The rule: LIMIT days allowed in any rolling WINDOW-day period. Stored in the
# store's settings (tunable later) but seeded from these.
SENSA_SCHENGEN_LIMIT_DAYS = 90
SENSA_SCHENGEN_WINDOW_DAYS = 180
# Backstop sample cadence. Day-granularity is all the rule needs, so a parked
# van only needs one in-Schengen stamp per day; movement (vehicle-tracker state
# change) samples instantly on top of this. ~30 min.
SENSA_SCHENGEN_SAMPLE_SECONDS = 1800
# Warn the customer (a `warning` log entry) once days-remaining drops to/below
# this while they are actually in the Schengen area.
SENSA_SCHENGEN_WARN_DAYS_REMAINING = 10

# --- Tracking (position history primitive) --------------------------------
#
# Tracking records where the van has been. It is the one feature on this box that
# deliberately keeps a continuous record of the vehicle's movements, so most of
# the constants below are privacy decisions rather than tuning ones. It uploads the
# record to Radar, which stores it; a separate mySensa app (family sharing, trip
# journals, a delayed public view) presents it back and reads the same data.
#
# CONSENT is the switch. Turning Tracking on is the customer opting in, and off means
# zero egress (nothing sampled or sent), so the van uploads nothing until it is on and
# a batch arriving at Radar is itself the opt-in. There is no separate web-app consent
# step: Radar authorises the upload purely on the van session.
#
# The PATH + STORE_KEY string VALUES keep their historical "followme" spelling so
# an in-flight upload buffer survives the rename to Tracking; only the NAMES changed.
SENSA_TRACKING_PATH = "nomad/.sensa_followme.json"
# Runtime key under hass.data[DOMAIN] for the singleton SensaTrackingStore.
SENSA_TRACKING_STORE_KEY = "sensa_followme_store"
# Fired when the store changes (enabled toggle / buffer change) so the switch
# re-reads rather than drifting from a pushed attribute.
EVENT_SENSA_TRACKING_CHANGED = "sensa_nomad_tracking_changed"

# Sampling and upload are DECOUPLED, which is the one real departure from
# radar_coordinator. Sampling is local and cheap; uploading is a round trip over
# a connection this vehicle loses constantly.
SENSA_TRACKING_SAMPLE_SECONDS = 60
# How often the van CONSIDERS uploading, not how often it does. RADAR decides the
# real interval and returns it as `next_upload_after_s` (300 normally, 60 while a
# share link is open), which `async_upload` honours by returning early until it is
# due. So the cadence stays server-controlled and this only bounds how finely the
# van can follow it.
#
# It must therefore be <= the SHORTEST cadence Radar can ask for. At the 300 it used
# to be, Radar's live-share 60 was silently unreachable: the tick was the real floor,
# the "live" map lagged five minutes, and both this repo and Radar carried comments
# claiming the value was already honoured.
SENSA_TRACKING_UPLOAD_SECONDS = 60

# --- Is the vehicle moving? ------------------------------------------------
#
# ONE derived signal, `binary_sensor.sensa_nomad_vehicle_moving`, folding the
# two sources a van can answer "am I moving?" with: the OBD2 adapter's road
# speed (authoritative when the engine is awake) and, when that is silent, the
# distance the resolved GPS has covered between samples. Two sources feeding one
# sensor rather than the same predicate written into two instincts: the
# Drive/Parked instincts, and anything later that wants the answer, all read the
# same entity. See `movement.py`.
#
# The thresholds exist to answer one question: "is this a journey, or is it
# shuffling the van down the driveway?" Distance carries more of that than time
# does, so ON is satisfied EITHER way and a driveway shuffle satisfies neither.
#
# All five are DEFAULTS. The customer tunes them on the "Drive mode when moving"
# instinct, and the detector re-reads that config on every sample -- so these are
# the starting point, not the rule. The instinct's `ConfigField` defaults are
# these same constants, so the two cannot drift.
#
# Speeds are MPH throughout the detector, because that is what every speed dial
# in the product shows and what the customer types into the instinct. The OBD2
# adapter reports km/h, so the conversion happens once, where that sensor is
# read.
SENSA_MOVEMENT_SAMPLE_SECONDS = 15
# ON, arm 1: road speed held above this for the sustain window.
SENSA_MOVEMENT_ON_MPH = 10.0
SENSA_MOVEMENT_ON_SECONDS = 60
# ON, arm 2: this far from where the van last came to rest, however slowly it
# got there. Manoeuvring on a pitch cannot reach it; leaving the site does.
SENSA_MOVEMENT_ON_DISTANCE_M = 500.0
# OFF: below this speed for this long. Five minutes so a fuel stop, a level
# crossing or a traffic queue does not read as "arrived". The speed floor is not
# customer-tunable: it is "not moving" with enough slack for GPS jitter, and a
# second speed knob next to the first would only invite setting stop above go.
SENSA_MOVEMENT_OFF_MPH = 2.0
SENSA_MOVEMENT_OFF_SECONDS = 300
# A GPS gap longer than this is a hole in the record, not a slow crawl: dividing
# the distance by it would invent a speed. The reference fix is reset instead.
SENSA_MOVEMENT_MAX_GPS_GAP_SECONDS = 600

MOVEMENT_BINARY_SENSOR_ENTITY_ID = "binary_sensor.sensa_nomad_vehicle_moving"
# Metres from where the van was when Sentry went on; read by the Geofence
# instinct by literal entity_id, so it is pinned (see sensor.py).
GEOFENCE_DISTANCE_ENTITY_ID = "sensor.sensa_nomad_geofence_distance"
# Runtime key under hass.data[DOMAIN] for the singleton detector.
RUNTIME_MOVEMENT_DETECTOR = "movement_detector"
# Fired when the detector's answer changes, so the binary sensor is pushed
# rather than polled off a second timer.
SIGNAL_MOVEMENT_CHANGED = "sensa_nomad_movement_changed"
# Fired after EVERY sample, so the GPS speed sensor (a live reading, not an
# edge) is written each tick. Separate from the CHANGED signal so the moving
# binary sensor keeps writing on transitions only.
SIGNAL_MOVEMENT_SAMPLED = "sensa_nomad_movement_sampled"
GPS_SPEED_SENSOR_ENTITY_ID = "sensor.sensa_nomad_gps_speed"

# Don't keep a fix unless the van has moved this far since the last kept one. A
# parked van records nothing. This is the single biggest lever on both data
# volume and privacy.
#
# 100 m since 2026-08-09 (Richard), down from the 200 m borrowed from
# `radar_coordinator`. That figure was tuned for "is it worth re-asking Radar for
# nearby amenities", which is a different question from "is this journey drawn
# accurately": at 200 m a town centre records almost nothing and the trip line cuts
# corners. Roughly doubles the points per journey, which the monthly partitioning
# and the per-van retention window were already sized for.
#
# Still deliberately NOT tiny: a parked van recording nothing is a privacy promise,
# not just a saving, and a threshold below GPS noise (~10-20 m) would have a
# stationary van drawing circles in a car park.
SENSA_TRACKING_MIN_MOVE_M = 100

# One fix every 30 minutes while stationary, so a journey can be shown to have
# ended where it ended without recording a week of identical points on a drive.
SENSA_TRACKING_HEARTBEAT_SECONDS = 1800

# While a Location Sharing link is open, somebody is actually watching this van, so
# both thresholds tighten. Radar tells us (`live_share_active` on the ingest reply)
# and also drops `next_upload_after_s` to 60.
#
# BOTH halves are needed. Uploading every minute achieves nothing if the van still
# only records a fix every 100 m: through a town that is still minutes of nothing,
# and the
# pin would sit still while the van moved. 50 m is roughly a few seconds at any
# speed, which is what makes "live" mean live.
#
# The cost is bounded and temporary: it applies only while a share is open, and the
# fleet's normal state is nobody sharing. That is why this is not simply the default.
SENSA_TRACKING_LIVE_MIN_MOVE_M = 50
SENSA_TRACKING_LIVE_HEARTBEAT_SECONDS = 300

# Roughly 72 hours of movement at the sample rate. Offline buffering is
# mandatory rather than nice to have: this is a campervan, and a weekend in a
# valley with no signal must not cost the customer the drive there. Oldest are
# dropped on overflow, and the drop is logged rather than silent.
SENSA_TRACKING_BUFFER_MAX = 5000

# Radar refuses a larger batch with a 413. Keep the two in step.
SENSA_TRACKING_BATCH_MAX = 500

# Back-off after a transient failure: 5, 10, 20, 40 minutes, capped at 60.
SENSA_TRACKING_BACKOFF_START_SECONDS = 300
SENSA_TRACKING_BACKOFF_MAX_SECONDS = 3600

# How often the van re-asserts its Tracking switch state to Radar, and how soon
# after a restart it does so the first time.
#
# The mySensa and Admin indicators read a stored flag on Radar, not the van, so
# whatever the van last said stands until it says otherwise. Reporting only on
# the flip made that flag a one-shot: a report lost to a dropped connection, or a
# switch that was already on before this leg existed, left "off" on the customer's
# screen for ever while the vehicle happily tracked. An upload re-asserts "on" as
# a side effect, but only a MOVING van uploads, so a parked one never self-corrected.
#
# So it is re-asserted on a heartbeat instead of trusted to a single event. Hourly,
# matching the licence check-in, because the ping carries NO location (see
# `TrackingCoordinator.async_report_state`) and is therefore cheap and outside the
# zero-egress-when-off rule. The first one is soon after start rather than an hour
# in, but not immediate: the Radar target and van token come from the App, which is
# still coming up while HA loads.
SENSA_TRACKING_STATE_REPORT_SECONDS = 3600
SENSA_TRACKING_STATE_REPORT_FIRST_SECONDS = 45

# Broadcast from `_async_compute_and_broadcast_eligibility` after each
# dashboard regen with the current eligible-entity list + per-rule
# traces. Listened to by `sensor.sensa_nomad_mode_eligibility` which
# stores both as state attributes for the Mode Editor's Add-entity
# dropdown and the (debug-only) Eligibility Rules panel. Two args:
# `(entities: list[str], rule_traces: list[dict])`.
SIGNAL_MODE_ELIGIBILITY_CHANGED = "sensa_nomad_mode_eligibility_changed"

# Filesystem path (relative to HA config_dir) for the generated
# scenes YAML that holds the 8 managed Nomad Modes. Parallel to the
# dashboard YAML directory so the Sensa-managed content lives under
# one roof and survives an uninstall cleanly.
MODES_FILE = "nomad/scenes.yaml"

# Managed-block markers inside `configuration.yaml` for the
# `scene sensa: !include nomad/scenes.yaml` directive (HA's split-config key —
# coexists with the default `scene: !include scenes.yaml` without a duplicate
# top-level `scene:` key; the earlier bare `scene:` collided and HA discarded one
# scene file). Same pattern as the dashboard/automation managed blocks — content
# between these markers is owned + rewritten by the integration; user-authored
# scene blocks outside the markers are left untouched.
MODES_MANAGED_BEGIN = "# BEGIN SENSA NOMAD MODES"
MODES_MANAGED_END = "# END SENSA NOMAD MODES"

# Generated curated automations (the Settings → Automations tab). Mirrors the
# scenes pipeline: a YAML file plus a managed block in configuration.yaml. The
# block uses HA's split-config key `automation sensa:` so it coexists with the
# default `automation: !include automations.yaml` (the customer's UI
# automations) without a duplicate top-level key.
AUTOMATIONS_FILE = "nomad/automations.yaml"
AUTOMATIONS_MANAGED_BEGIN = "# BEGIN SENSA NOMAD AUTOMATIONS"
AUTOMATIONS_MANAGED_END = "# END SENSA NOMAD AUTOMATIONS"

# Customer-authored Routines (Intelligence -> Routines tab): a thin When / And
# If / Then Do wrapper over native HA automations, distinct from the curated,
# fixed Instincts catalog above. Same split-config pattern: a managed YAML file
# plus a labelled `automation routines:` key, which coexists with BOTH the
# customer's default `automation:` key and our own `automation sensa:` key —
# HA merges every `automation <label>:` key into the single `automation`
# domain (see modes.py's `_build_managed_scene_block` docstring for the full
# reasoning, which applies identically here).
ROUTINES_FILE = "nomad/routines.yaml"
ROUTINES_MANAGED_BEGIN = "# BEGIN SENSA NOMAD ROUTINES"
ROUTINES_MANAGED_END = "# END SENSA NOMAD ROUTINES"

# Broadcast whenever the Routine Editor's eligible-entity sets (device +
# service groups) are recomputed. Same shape/purpose as
# SIGNAL_MODE_ELIGIBILITY_CHANGED above, kept as its own signal because the
# Routine Editor's eligibility groups are conceptually different from the
# Mode Editor's (read-oriented trigger/condition picking, not scene
# membership) even though both are computed from the same device list.
SIGNAL_ROUTINE_ELIGIBILITY_CHANGED = "sensa_nomad_routine_eligibility_changed"

# HA Store key for BLE MACs the user has explicitly ignored in the
# Bluetooth picker.  Persisted to .storage/sensa_nomad.ble_ignored.
BLE_IGNORED_STORAGE_KEY = "sensa_nomad.ble_ignored"
BLE_IGNORED_STORAGE_VERSION = 1

# Customer-facing short registration code (SENSA-XXXX-XXXX-XXXX). The device
# redeems it online once for a hardware-bound JWT; supersedes pasting the key.
CONF_REDEMPTION_CODE = "redemption_code"
CONF_BACKEND_URL = "backend_url"
CONF_PROFILE_NAME = "profile_name"
CONF_SCAN_SUBNET = "scan_subnet"
CONF_SELECTED_DEVICES = "selected_devices"
CONF_CUSTOMER_NAME = "customer_name"
CONF_CONTACT_EMAIL = "contact_email"

# Licence-status keys — keep in sync with the App's /api/license/status
# response shape (see Sensa.Control.App/routes/license.py).
LICENSE_VALID = "valid"
LICENSE_TRIAL = "trial"
LICENSE_READ_ONLY = "read_only"
LICENSE_DAYS_REMAINING = "trial_days_remaining"
LICENSE_PHONE_HOME_OK = "phone_home_ok"

# Runtime key under hass.data[DOMAIN][entry_id] — set to False while the
# licence is expired/revoked so writable platforms refuse service calls.
RUNTIME_LICENCE_OK = "licence_ok"
RUNTIME_LICENCE_STATUS = "licence_status"
# Runtime flag under hass.data[DOMAIN][entry_id] — set once the cloud has told
# us this hardware's trial slot is used (trial_exhausted) so the appliance
# self-provision loop stops re-requesting a trial it can never get. A paid
# licence is the recovery; cleared on reload (re-attempted once, harmless).
RUNTIME_TRIAL_BLOCKED = "trial_blocked"
# Runtime flag — an appliance trial POST is in flight, so the licence poller
# doesn't spawn a second overlapping request before the first lands.
RUNTIME_TRIAL_INFLIGHT = "trial_inflight"
# The App's /api/license/status `message` for a genuinely UNlicensed device (no
# JWT stored). Distinguished from a corrupted/invalid paid JWT, which returns
# "Licence invalid: ...": we self-provision a trial ONLY on the former, so a
# paid customer's verification failure surfaces for re-registration instead of
# being masked by a trial. Coupled to app/.../routes/license.py handle_get_status.
NO_LICENCE_MESSAGE = "No licence installed."

# How often the Nomad integration refreshes its view of the cached
# licence status from the App. Cheap local call — 15 min is plenty.
LICENCE_STATUS_REFRESH_SECONDS = 15 * 60

# Backup status cache. Populated by `_async_refresh_backup_status` polling
# the Sensa App's `/api/v1/backup/status` (which in turn calls Supervisor).
# A miss leaves the previous snapshot in place so the Settings → Backup
# card never empties on a transient Supervisor blip.
RUNTIME_BACKUP_STATUS = "backup_status"

# Which backup destinations the customer has switched on, as {kind: bool}.
#
# Keyed by KIND ("local" / "usb" / "cloud"), never by agent_id: a USB agent's id
# follows the stick's own label, so a preference keyed on it would leave an
# immortal row per stick anybody ever plugged in, would give a second stick no
# setting at all, and could not back a single "USB drive" control.
#
# A MISSING key means ON. Every box already in the field has no such option, and
# defaulting to off would silently stop backups on boxes that are working today.
BACKUP_DESTINATIONS_OPTION = "backup_destinations"
# Mirror of the option in hass.data, so the two callers that resolve the agent
# list do not have to be handed the config entry (see backup_config).
BACKUP_DESTINATIONS_KEY = "backup_destinations"
BACKUP_DESTINATION_LOCAL = "local"
BACKUP_DESTINATION_USB = "usb"
BACKUP_DESTINATION_CLOUD = "cloud"
BACKUP_DESTINATION_KINDS = (
    BACKUP_DESTINATION_LOCAL,
    BACKUP_DESTINATION_USB,
    BACKUP_DESTINATION_CLOUD,
)
BACKUP_STATUS_REFRESH_SECONDS = 15 * 60
SIGNAL_BACKUP_STATUS_CHANGED = "sensa_nomad_backup_status_changed"

# Staged-rollout / appliance OTA auto-apply. The owner pause toggle
# (switch.sensa_nomad_ota_auto_update) writes its on/off here so the backup
# poller can gate the auto-apply without resolving an entity_id; absent = ON
# (fail-open — a fresh appliance updates itself out of the box).
RUNTIME_OTA_AUTO_UPDATE = "ota_auto_update"
# The customer's pause switches for THIRD-PARTY device firmware, mirrored onto
# the runtime so an update entity can read the position without resolving an
# entity_id. Each reads OPEN (ON) when missing, exactly as the OTA flag does: a
# fresh boot before RestoreEntity has restored must not look like "paused".
RUNTIME_VICTRON_AUTO_UPDATE = "victron_auto_update"
RUNTIME_TELTONIKA_AUTO_UPDATE = "teltonika_auto_update"
# Teltonika splits router from managed switch, because the two are separate
# decisions: the router carries the van's internet and a customer may well want
# it held while the switch, which nothing depends on mid-journey, keeps current.
# The split matches the rollout pins (PIN_KEY_ROUTER / PIN_KEY_SWITCH), so pause
# granularity and ceiling granularity are the same granularity. The router keeps
# the original key and entity_id so a customer's restored position survives.
RUNTIME_TELTONIKA_SWITCH_AUTO_UPDATE = "teltonika_switch_auto_update"
VICTRON_AUTO_UPDATE_ENTITY_ID = "switch.sensa_nomad_victron_auto_update"
TELTONIKA_AUTO_UPDATE_ENTITY_ID = "switch.sensa_nomad_teltonika_auto_update"
TELTONIKA_SWITCH_AUTO_UPDATE_ENTITY_ID = (
    "switch.sensa_nomad_teltonika_switch_auto_update"
)
# Sensa's OWN ESP devices have one automatic-updates switch EACH, so a relay can
# be held while a proxy keeps current. Keys and entity ids come from
# `esp_update.esp_auto_update_key` / `esp_auto_update_entity_id`.
# Per-version retry window for the apply: once we've asked Supervisor to apply a
# desired version we don't re-ask for this long, so a failed/in-flight apply
# retries on the NEXT poll cycle rather than every tick (and a manual "Update
# now" can't double-fire with it). Derived to sit just under the poll interval
# so it tracks any retune of BACKUP_STATUS_REFRESH_SECONDS automatically.
OTA_APPLY_COOLDOWN_SECONDS = BACKUP_STATUS_REFRESH_SECONDS - 120

# Live container-OTA progress: the updater sidecar writes /ota/status, the App
# serves it as GET /api/v1/ota/status, and a short-lived fast poller mirrors it
# here so the backups-stale sensor's ota_* attributes drive the Admin card's
# progress bar. Container appliance only — a Supervisor update has no sidecar
# and never populates this.
# Host-OS update status (Layer 1, Container appliance): the on-host systemd agent
# writes /host-update/status.json, the App serves it as GET /host-update/status,
# and the backup poller mirrors it here so the backups-stale sensor's
# host_update_* attributes drive the Admin card's "Host OS updates" section. On a
# box with no host agent (HAOS/BYO) the App reports {"state": "unknown"}.
RUNTIME_HOST_UPDATE_STATUS = "host_update_status"

RUNTIME_OTA_STATUS = "ota_status"
OTA_STATUS_POLL_SECONDS = 2
OTA_STATUS_POLL_MAX_SECONDS = 600
# A terminal done/failed older than this is a previous update's history, not
# the outcome the customer is watching — read it as idle.
OTA_STATUS_STALE_SECONDS = 30 * 60
# The state taxonomy, enumerated from the settled end so a new updater PHASE
# can't silently break resume-on-setup or the fast watch (the card's
# _otaInFlight uses the same inversion). TERMINAL = an update ended (stops the
# watch + triggers the post-update refresh); SETTLED adds idle (nothing to
# resume). Derived, never duplicated — the watch stop-check drifting from the
# resume gate was a review finding.
OTA_STATES_TERMINAL = ("done", "failed")
OTA_STATES_SETTLED = OTA_STATES_TERMINAL + ("idle",)
# Refuse to START an update once the disk is this full (%). The pre-OTA snapshot
# writes a full HA backup to the SAME filesystem the update then pulls into, so
# on a nearly-full box we would consume the last of the space and only then have
# the updater refuse — and past 100% its status write silently fails (it shares
# the filesystem), leaving the card stuck on "Updating Sensa" forever with no way
# to report why (a dev Pi, 2026-07-11). Refuse while the box is still
# recoverable. The updater re-checks with real figures; this is the early bail.
OTA_MIN_FREE_DISK_PERCENT = 92
# Boot-time re-poll until the updater sidecar has resolved the channel-latest
# figure: it lands seconds-to-minutes after ITS boot (a first-boot pull can take
# minutes), usually AFTER our setup prime — and the 15-min backup poll is far
# too slow for the Admin card's Latest row, which would sit on "Checking...".
OTA_LATEST_PRIME_INTERVAL_SECONDS = 60
OTA_LATEST_PRIME_MAX_ATTEMPTS = 10

# Network signal cache. Populated by `_async_refresh_network_status` polling
# the Sensa App's `/api/v1/network/status` — a fresh reachability/RTT ping each
# cycle plus the LAST on-demand speed-test result. Feeds the Cab → Network
# signal-quality card's ping / up-down / speed-test sensors. Ping is cheap so
# the poll is frequent; the speed test is NEVER auto-run (it burns mobile data)
# — only the `sensa_nomad.run_speedtest` service / button triggers one.
RUNTIME_NETWORK_STATUS = "network_status"
NETWORK_STATUS_REFRESH_SECONDS = 60
SIGNAL_NETWORK_STATUS_CHANGED = "sensa_nomad_network_status_changed"

# Host resource snapshot (CPU / memory / disk %), sourced from the Sensa App
# add-on's `/api/v1/system/resources` and surfaced on the Settings → Support
# card. A coarse health readout, not a live graph.
RUNTIME_SYSTEM_RESOURCES = "system_resources"
SIGNAL_SYSTEM_RESOURCES_CHANGED = "sensa_nomad_system_resources_changed"

# Internet data usage per kind of traffic, from the Sensa App's
# `/api/v1/egress/usage`, for Settings -> Data usage. The same tick first posts
# the integration's own counts (`egress.py`) to the App.
RUNTIME_DATA_USAGE = "data_usage"
SIGNAL_DATA_USAGE_CHANGED = "sensa_nomad_data_usage_changed"
DATA_USAGE_REFRESH_SECONDS = 300

# Raspberry Pi board readings (temperature, fan, undervoltage, CPU clock, board
# power draw) from the Sensa App add-on's `/api/v1/system/hardware`, surfaced as
# diagnostic entities on the Sensa Core device.
#
# APPLIANCE ONLY, and the APP decides that, not us: the add-on holds the
# unfolded three-way deployment_mode, whereas `deployment_mode_family()` has
# already folded `enthusiast` into `appliance` by the time anything reaches
# `hass.data`, so this side genuinely cannot tell the two apart. A box that
# declines answers `{"supported": False}` and no entities are created.
RUNTIME_SYSTEM_HARDWARE = "system_hardware"
SIGNAL_SYSTEM_HARDWARE_CHANGED = "sensa_nomad_system_hardware_changed"
# Slower than the Support-card pollers: a temperature is not a live graph, and
# the host helper behind the power figures only republishes every 60s anyway, so
# polling faster would just re-read the same file.
SYSTEM_HARDWARE_REFRESH_SECONDS = 30

# Per-app (per-container) CPU/memory breakdown — Home Assistant, Sensa, and each
# other add-on — from the Sensa App add-on's `/api/v1/system/processes` (which
# reads Supervisor's per-container `/*/stats`). Surfaced beside Resources on the
# Settings → Support card.
RUNTIME_APP_USAGE = "app_usage"
SIGNAL_APP_USAGE_CHANGED = "sensa_nomad_app_usage_changed"
# Customer-selectable poll cadence shared by BOTH Support-card pollers (Resources
# + Usage by app) — one pill writes the select, both pollers re-read it each
# cycle (see __init__.py `_refresh_interval`). Default 10s keeps CPU visibly
# moving (the deliberate cadence the resources poller used before they were
# unified); the customer can raise it to cut the per-app Supervisor call volume.
APP_USAGE_REFRESH_SECONDS = 10
APP_USAGE_INTERVAL_ENTITY_ID = "select.sensa_nomad_app_usage_interval"
APP_USAGE_INTERVAL_OPTIONS = ["5", "10", "15", "30"]

# Compose-stack container list (name/state/version + allowed actions) for the
# Settings → Supervisor "Containers" panel — from the Sensa App add-on's
# `/api/v1/system/containers` (which reads the updater sidecar's `docker ps -a`).
# Container-appliance only; empty under Supervisor. Polled on a fixed cadence
# (containers change rarely) plus an immediate refresh after a control action.
RUNTIME_CONTAINERS = "containers"
SIGNAL_CONTAINERS_CHANGED = "sensa_nomad_containers_changed"
CONTAINERS_REFRESH_SECONDS = 30
# Give up re-arming the containers poller if deployment_mode never RESOLVES (App
# permanently unreachable) after this many consecutive unresolved ticks — a real
# container appliance resolves within a tick or two (HA depends_on the App), so
# this only caps a degraded/dead box (40 × 30s = 20 min). Reset once resolved.
CONTAINERS_MAX_UNCONFIRMED_TICKS = 40

# Host-OS (Layer 1) update maintenance window — the customer picks a 2-hour
# window; the manifest ships only the default. The on-host agent re-points the
# update timer to the chosen window. Appliance-only.
# Release channel this box follows. The channel is the tag on SENSA_STACK_IMAGE
# in the compose .env, which only the updater sidecar can write, so this select
# is the one way off a channel for a fielded box nobody can reach a shell on.
UPDATE_CHANNEL_SELECT_ENTITY_ID = "select.sensa_nomad_update_channel"
UPDATE_CHANNEL_OPTIONS = ["stable", "beta", "edge"]
UPDATE_CHANNEL_DEFAULT = "stable"

HOST_UPDATE_WINDOW_SELECT_ENTITY_ID = "select.sensa_nomad_host_update_window"
HOST_UPDATE_WINDOW_OPTIONS = [
    "23:00-01:00",
    "00:00-02:00",
    "01:00-03:00",
    "02:00-04:00",
    "03:00-05:00",
    "04:00-06:00",
    "05:00-07:00",
]
HOST_UPDATE_WINDOW_DEFAULT = "03:00-05:00"

# Appliance licence-recovery surface. On appliance the `owner` is a non-HA-admin
# and HA's Devices & Services (reconfigure / licence entry) is unreachable — so
# a read-only lockout would otherwise strand the customer with no way to enter a
# valid licence. These two entities back a Sensa-panel licence-entry card
# (Settings → Support, appliance only): the text holds the code, the button
# redeems it server-side. Neither is licence-gated (recovery must work in
# read-only); the button is the config-card's `save_button`.
LICENCE_CODE_ENTITY_ID = "text.sensa_nomad_licence_code"
ACTIVATE_LICENCE_ENTITY_ID = "button.sensa_nomad_activate_licence"

# Customer-set name for this system / vehicle ("Bertha", "Van 3"). Shown at the
# top of the Settings → Diagnostics tab's Resources panel, and carried up on every
# phone-home (a top-level `telemetry.system_name`, never under the consent-gated
# `diagnostics` section — naming your van is not a diagnostic), so
# the Hub fleet view can label a deployment with the name its owner uses instead
# of a bare licence id. Persisted in `entry.options["system_name"]`; the length
# cap is enforced on the entity (HA rejects a longer set_value), on the App, and
# in the card's input.
SYSTEM_NAME_ENTITY_ID = "text.sensa_nomad_system_name"
SYSTEM_NAME_MAX_LENGTH = 25

# Install mode, sourced from the Sensa App add-on's `deployment_mode` option and
# reported via /health. The add-on value is the three-way install mode (byo /
# enthusiast / appliance); the integration only cares about the two-way FAMILY
# (self-install vs appliance) for chrome + user pre-creation, so it FOLDS the
# value on ingest and stores the family (byo / appliance) under DEPLOYMENT_MODE_KEY
# — leaving every existing `== DEPLOYMENT_MODE_APPLIANCE` / `_is_appliance()` check
# untouched. Cached under `hass.data[DOMAIN][DEPLOYMENT_MODE_KEY]` at setup so the
# dashboard generator + first-run code don't pay a per-call HTTP round-trip. The
# device-side deployment-type enforcement lives in the App, not here.
DEPLOYMENT_MODE_KEY = "deployment_mode"
DEPLOYMENT_MODE_BYO = "byo"
DEPLOYMENT_MODE_APPLIANCE = "appliance"
DEPLOYMENT_MODE_DEFAULT = DEPLOYMENT_MODE_BYO

DEPLOYMENT_MODE_ENTHUSIAST = "enthusiast"

# Fold an add-on deployment_mode (byo / enthusiast / appliance) to the two-way
# family (byo self-install vs appliance): enthusiast + appliance are both a Sensa
# appliance.
_DEPLOYMENT_MODE_FAMILY = {
    DEPLOYMENT_MODE_BYO: DEPLOYMENT_MODE_BYO,
    DEPLOYMENT_MODE_ENTHUSIAST: DEPLOYMENT_MODE_APPLIANCE,
    DEPLOYMENT_MODE_APPLIANCE: DEPLOYMENT_MODE_APPLIANCE,
}


def deployment_mode_family(value: str | None) -> str | None:
    """Fold an add-on deployment_mode (byo / enthusiast / appliance) to the two-way
    family (``byo`` / ``appliance``). ``None`` for an absent / unrecognised value,
    so the caller can keep its previously-resolved mode rather than guess."""
    return _DEPLOYMENT_MODE_FAMILY.get((value or "").strip().lower())

# Sensa Radar's per-environment ORIGIN, sourced from the App add-on's `radar_url`
# option (pipeline: the `sensaRadarUrl` variable group -> add-on config ->
# SENSA_RADAR_URL -> /health). Cached under `hass.data[DOMAIN][RADAR_URL_KEY]`
# alongside deployment_mode, off the SAME /health read, so the dashboard generator
# can stamp it into the nav-map card without an HTTP round-trip (card configs are
# built synchronously).
#
# It is an ORIGIN, not an API base: `radar_client` appends RADAR_API_PREFIX and
# the nav-map card embeds `<origin>/?embed=1`. It must therefore be the origin
# serving the Radar WEB APP (which proxies /api/* through to the backend). Point
# it straight at the backend container and the API half works while the map
# iframe renders FastAPI's {"detail":"Not Found"}.
#
# Absent/empty = Radar is not configured for this environment; the nav map then
# falls back to the HA map rather than pointing at somebody else's Radar. No
# Radar URL is baked into the integration or the cards.
RADAR_URL_KEY = "radar_url"

# Dev only: skip TLS certificate verification when calling RADAR_URL_KEY. Read off
# the SAME /health payload as the origin, because the App is the one that knows:
# under Supervisor the integration cannot see the add-on's environment at all, and
# a box whose two halves disagreed about whether the origin is trusted would work
# through one caller and fail through the other.
#
# Absent (an App older than the option) reads as False, which is the safe way
# round: the worst case is the handshake failing exactly as it does today.
RADAR_INSECURE_TLS_KEY = "radar_insecure_tls"

# Sensa Hub (MySensa) ORIGIN for this box, off the Sensa App's /health: the
# configured `hub_url`, else the one the last phone-home handed down. Used for
# Hub's open weather API (the solar forecast, solar_forecast.py).
HUB_URL_KEY = "hub_url"

# The owner's on/off for the solar forecast: off by default, and while it is off
# nothing is fetched, so no position leaves the van for it.
SOLAR_SWITCH_ENTITY_ID = "switch.sensa_nomad_solar_forecast"
# The van's solar array rating in watts-peak. The forecast also needs this set.
SOLAR_ARRAY_RATING_ENTITY_ID = "number.sensa_nomad_solar_array_rating"
# The house battery's size in Ah, van-level (a Victron shunt under a Cerbo has
# no editable row). Turns the solar forecast's kWh into a battery %. 0 = not set.
BATTERY_CAPACITY_ENTITY_ID = "number.sensa_nomad_battery_capacity"
SOLAR_FORECAST_TODAY_ENTITY_ID = "sensor.sensa_nomad_solar_forecast_today"
SOLAR_FORECAST_TOMORROW_ENTITY_ID = "sensor.sensa_nomad_solar_forecast_tomorrow"
SOLAR_FORECAST_NEXT_HOUR_ENTITY_ID = "sensor.sensa_nomad_solar_forecast_next_hour"
SOLAR_FORECAST_ACCURACY_ENTITY_ID = "sensor.sensa_nomad_solar_forecast_accuracy"
# weather_sensors.py: weather, warnings, UV and air at the van, for the Weather card.
WEATHER_ENTITY_ID = "sensor.sensa_nomad_weather"
# The owner's consent to the weather fetch: off by default, and while it is off
# no position leaves the van for it. Turned on from the Weather card.
WEATHER_SWITCH_ENTITY_ID = "switch.sensa_nomad_weather_forecast"
# solar_accuracy.py: forecast against actual, by day, kept 30 days.
SOLAR_ACCURACY_STORAGE_KEY = "sensa_nomad.solar_accuracy"
SOLAR_ACCURACY_STORAGE_VERSION = 1

# Customer-adjustable search radius for the Radar "nearest amenity" sensors.
# Stored natively in KILOMETRES with `device_class: distance`, so HA converts the
# displayed value to miles by itself on an imperial system, the unit follows the
# HA system setting without the card, the entity or the coordinator each having
# to reimplement it. The coordinator converts to metres for the API call and
# falls back to `RADAR_SEARCH_RADIUS_M` when the entity is missing (an older
# build, or before the platform has set up).
RADAR_SEARCH_RADIUS_ENTITY_ID = "number.sensa_nomad_radar_search_radius"
# ~10 miles, matching the constant this replaced so existing boxes see no change
# in behaviour until the customer moves the slider.
RADAR_SEARCH_RADIUS_DEFAULT_KM = 16.0
RADAR_SEARCH_RADIUS_MIN_KM = 1.0
RADAR_SEARCH_RADIUS_MAX_KM = 80.0

# The Radar API version this integration speaks. It lives here, in code, next to
# the client written against that contract: a v2 with a different response shape
# needs new parsing code regardless, so putting the version in config could only
# ever break a working box, never upgrade one. Bump this one line when
# `radar_client` is taught v2.
RADAR_API_PREFIX = "/api/v1"

# Name of the dealer theme we have already auto-applied as the instance-wide
# default, persisted to the config entry's data. A dealer box should boot on
# the dealer's branding without anyone opening the picker, so the theme is
# applied once, the first time the App caches it. The marker must be DURABLE:
# the "theme just arrived" edge is computed against the in-memory runtime
# snapshot, which resets on every restart, so without this we would re-apply
# on each boot and silently stomp a customer who has since chosen a different
# theme. Absent = never auto-applied for this entry.
DEALER_THEME_APPLIED_KEY = "dealer_theme_applied"

# Boot-race recovery: if the add-on's /health is unreachable when the entry
# sets up (it's still booting), deployment_mode falls back to BYO and the
# appliance UI (hidden sidebar, switcher, per-user routing) never applies. We
# then re-check on a short timer until we get a confirmed reading, re-applying
# the panels if the mode flips. ~2 min of cover at 15s spacing catches a slow
# add-on boot without a manual reload.
DEPLOYMENT_MODE_WATCH_INTERVAL_SECONDS = 15
DEPLOYMENT_MODE_WATCH_MAX_ATTEMPTS = 8

# --- Appliance account model (see appliance.py + build-appliance.py) -------
# Home Assistant has no "restricted admin" tier — a real HA admin can
# always reach Developer Tools / Config / Add-ons via the API, companion
# app or a long-lived token, no matter what the frontend hides. So on a
# Sensa-shipped appliance NONE of the customer's logins is an HA admin;
# HA's own permission model then blocks every internal page for them.
#
#   hab/cab/mobile   — single-panel kiosk tablets (non-admin). On a CONTAINER
#                      appliance these three are the WHOLE customer account set,
#                      and Settings is open to all of them behind the Settings
#                      PIN below.
#   admin            — HAOS/Enthusiast ONLY. The customer's primary login (was
#                      "owner"; renamed 2026-07 as "admin" reads clearer to
#                      customers), a full HA administrator there so it can reach
#                      HA's native updates. The Container builder stopped
#                      creating it on 2026-09-03: once the PIN opened Settings to
#                      every login, a separate elevated username only cost the
#                      customer a log out and back in. The name + the allowlist
#                      below stay because HAOS still mints it, and because a
#                      Container box built before that date still has one.
#   sensa_support    — the ONLY HA admin/owner, held by Sensa = the support
#                      backdoor. Its password is never given to the customer.
APPLIANCE_ADMIN_USERNAME = "admin"
APPLIANCE_KIOSK_USERNAMES = ("hab", "cab", "mobile")
# Usernames allowed to use the Settings panel + privileged Sensa services
# in appliance mode WITHOUT a PIN. Anything not here (the kiosk tablets) needs a
# live Settings-PIN elevation, else it is refused even by direct URL /
# hand-crafted WS call. Real HA admins always pass too. HAOS only: on a
# Container appliance `admin` is retired and this is never consulted.
APPLIANCE_SETTINGS_USERNAMES = (APPLIANCE_ADMIN_USERNAME,)
SENSA_SUPPORT_USERNAME = "sensa_support"

# --- Settings PIN (elevation for a non-admin login) ------------------------
# The Settings dashboard is open to EVERY login (2026-09-01). A login that is
# not already a Sensa admin by role must enter this PIN, which elevates it to a
# full Sensa admin for a bounded idle window -- so a customer signed in on the
# companion app as `mobile` can do the admin jobs without logging out and back
# in as `admin`, which makes the companion app re-run most of its setup.
#
# The elevation is SERVER-side (see appliance.user_is_sensa_admin): a frontend
# reveal would show a Settings screen where every Admin action is refused.
# Kiosk logins are still not HA admins, so Developer Tools, add-ons and HA's
# own config pages stay closed to them however this resolves.
SETTINGS_PIN_STORAGE_KEY = "sensa_nomad.settings_pin"
SETTINGS_PIN_STORAGE_VERSION = 1
# Seeded on first run so the feature is live the moment it deploys, with a
# banner on Settings until the customer changes it. A fleet-wide default is
# deliberate: the threat model here is a passenger walking round the van, not
# an attacker, and there is nothing an unattended box could be told instead.
SETTINGS_PIN_DEFAULT = "1234"
SETTINGS_PIN_MIN_LEN = 4
SETTINGS_PIN_MAX_LEN = 8
# Sliding idle window. A tablet left on the Settings screen re-locks itself.
# Also the default "remember" window offered at the keypad — see below.
SETTINGS_PIN_IDLE_SECONDS = 15 * 60
# "Remember me" choices offered on the PIN keypad (seconds). 0 is "just this
# time": the elevation still lives for SETTINGS_PIN_IDLE_SECONDS server-side,
# but sensa-panel revokes it the moment the customer navigates off Settings
# (today's only behaviour, before this option existed) rather than letting a
# nav-away-and-back skip the keypad. Any other value REPLACES the sliding
# idle window with that fixed one for this elevation, so a quick 1-minute
# errand doesn't quietly inherit the full 15 minutes. Matched to
# sensa-panel.ts's PIN_REMEMBER_OPTIONS_S — the two must not drift apart, a
# value offered on one end that the other doesn't recognise is a silent
# mismatch, not an error either side would ever see.
SETTINGS_PIN_REMEMBER_OPTIONS_S = (0, 60, 5 * 60, 15 * 60)
# Brute-force brake. A 4-digit PIN is 10k combinations; 5 tries then a doubling
# lockout puts an exhaustive search into the days.
SETTINGS_PIN_MAX_ATTEMPTS = 5
SETTINGS_PIN_LOCKOUT_SECONDS = 60
SETTINGS_PIN_LOCKOUT_MAX_SECONDS = 15 * 60

# --- Remote support (customer-consented Sensa access) ----------------------
# The `sensa_support` HA admin user is pre-created INACTIVE by the master-image
# build script. The customer's `switch.sensa_nomad_remote_support` consent
# toggle activates it (fresh random password, reported to Sensa over the
# phone-home channel) for a bounded window, then it auto-disables. See
# remote-access.md §7.2. NONE of this touches the customer's own logins.
REMOTE_SUPPORT_STORAGE_KEY = "sensa_nomad.remote_support"
REMOTE_SUPPORT_STORAGE_VERSION = 1

# Why the box is about to go down, written on the way out and consumed by the
# next boot's System Log entry (`boot_log.py`). Deliberately its own tiny store:
# it has to be written and flushed in the moment before a restart.
SHUTDOWN_INTENT_STORAGE_KEY = "sensa_nomad.shutdown_intent"
SHUTDOWN_INTENT_STORAGE_VERSION = 1
EVENT_SENSA_REMOTE_SUPPORT_CHANGED = "sensa_nomad_remote_support_changed"
# Default access window (hours) when the customer enables support. Exposed as
# `number.sensa_nomad_remote_support_window_hours` so it can be tuned per unit.
REMOTE_SUPPORT_DEFAULT_WINDOW_HOURS = 24
REMOTE_SUPPORT_MIN_WINDOW_HOURS = 1
REMOTE_SUPPORT_MAX_WINDOW_HOURS = 168  # a hard 7-day safety cap
# How often the expiry tick checks whether an open window has elapsed.
REMOTE_SUPPORT_TICK_SECONDS = 60
REMOTE_SUPPORT_WINDOW_NUMBER_ENTITY_ID = (
    "number.sensa_nomad_remote_support_window_hours"
)

# The customer's own remote-access switch (`switch.sensa_nomad_remote_access`),
# distinct from remote SUPPORT above: this one decides whether the box answers
# on its public `van-<hash>` hostname at all, whereas that one decides whether a
# Sensa engineer has a login. Default OFF. The App holds the setting; this key
# caches its last answer in `hass.data[DOMAIN]` so the switch can render between
# licence-status polls. See remote_access.py.
REMOTE_ACCESS_STATE_KEY = "remote_access_enabled"
EVENT_SENSA_REMOTE_ACCESS_CHANGED = "sensa_nomad_remote_access_changed"
REMOTE_ACCESS_SWITCH_ENTITY_ID = "switch.sensa_nomad_remote_access"

# Logins that still have the password the image was built with, as the App last
# reported them on `/api/license/status`. Cached in `hass.data[DOMAIN]` so both
# remote switches can refuse to open a door that the box would then hold shut
# anyway. `None` (or the key absent) means COULD NOT ASK and must never be read
# as "clean". See default_logins.py.
# Whether the Cerbo Remote Console is proxied through this box, cached from the
# App's /api/license/status. The App owns the decision (see its
# routes/cerbo_console.py); this is only where the HA side parks the answer.
CERBO_CONSOLE_STATE_KEY = "cerbo_console"
DEFAULT_LOGINS_STATE_KEY = "default_logins"
EVENT_SENSA_DEFAULT_LOGINS_CHANGED = "sensa_nomad_default_logins_changed"

PURCHASE_URL = "https://shop.sensa.systems"

# The licence agreement this system runs under. Linked, never bundled: the
# published page is the agreement and carries its own version, so a copy shipped
# in the image would be a second, ageing answer to "what did I agree to". The
# repo's LICENSE.md points at the same place for the same reason.
LICENCE_TERMS_URL = "https://sensa.systems/legal/control-terms/"

# License tier — CURRENT_TIER is hardcoded to VISION until the vendor-entity
# gating below is wired to the validated licence. When that lands, it is read
# from `editions["nomad"]`.
#
# The identifiers were renamed lite -> base and plus -> pro to match the
# customer-facing names. A signed JWT is never re-issued, so every licence in
# the field still carries the old identifier until it is replaced: both
# spellings must keep resolving, forever. Compare tiers through
# `normalise_tier` — never `==` a raw claim value. Mirrors
# `core/sensa_core/licensing/limits.py` (the integration is a standalone HA
# custom component and cannot import sensa_core).
TIER_BASE = "base"
TIER_PRO = "pro"
TIER_VISION = "vision"
# Vision plus on-device conversation: a superset of Vision, never instead of it.
TIER_AI = "ai"

# Retired identifiers, still carried by every licence issued before the rename.
TIER_LITE = "lite"
TIER_PLUS = "plus"

_TIER_ALIASES = {TIER_LITE: TIER_BASE, TIER_PLUS: TIER_PRO}

CURRENT_TIER = TIER_VISION


def normalise_tier(tier: str | None) -> str:
    """Canonical tier identifier, folding the retired lite/plus names."""
    key = (tier or "").strip().lower()
    return _TIER_ALIASES.get(key, key)

def teltonika_device_name(mac: str = "") -> str:
    """Build a stable Teltonika device name from MAC suffix (last 6 chars)."""
    clean = mac.replace(":", "").replace("-", "").lower()
    if len(clean) >= 6:
        return f"Teltonika {clean[-6:]}"
    return "Teltonika Device"


def teltonika_devices_from_registry(hass, entry) -> dict[str, dict[str, Any]]:
    """Recover Teltonika device_id -> a minimal device dict from the registry.

    The router's four config entities (Username / Password text, SSL switch,
    Save Router Config button) are built from the backend snapshot
    (`coordinator.data`). That snapshot is momentarily empty right after an
    OTA, while the Sensa App add-on is still starting, so on the reload that
    follows an upgrade those entities aren't rebuilt and become registry
    ORPHANS: they read `unavailable`, `text.set_value` against them silently
    no-ops, and the (also-orphaned) Save button can't read the credentials.
    Re-entering the username/password then appears to save (the picker pane
    collapses) but nothing persists.

    This recovers the still-present routers from the persistent registries so
    every platform can re-create their config entities even with an empty
    snapshot. Mirrors the relay registry-union in text.py / switch.py.

    Keyed off the `_config_username` unique_id, then filtered to entries whose
    device still carries the `(DOMAIN, teltonika_<device_id>)` identifier — the
    Teltonika discriminator (Cerbo/Asustor share the `_config_username` uid, so
    the unique_id alone is not enough) AND the safety against resurrecting a
    forgotten router (device removal purges the device, so a miss = skip). The
    MAC is recovered best-effort for a correct device name; a miss is harmless
    because HA restores each entity's entity_id from the registry by unique_id.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    result: dict[str, dict[str, Any]] = {}
    # One try/except around the whole walk, like the relay registry-union in
    # text.py: a registry hiccup (or the partial test stub) must never break
    # platform setup — degrade to whatever was recovered so far.
    try:
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        prefix = f"{entry.entry_id}_"
        suffix = "_config_username"
        for ent in ent_reg.entities.values():
            if ent.config_entry_id != entry.entry_id:
                continue
            uid = ent.unique_id or ""
            if not uid.startswith(prefix) or not uid.endswith(suffix):
                continue
            device_id = uid[len(prefix):-len(suffix)]
            if not device_id or device_id in result:
                continue
            device = dev_reg.async_get_device(
                identifiers={(DOMAIN, f"teltonika_{device_id}")}
            )
            if device is None:
                # Not a Teltonika (Cerbo/Asustor reuse `_config_username`), or
                # the router was forgotten — don't materialise entities.
                continue
            mac = ""
            for conn_type, conn_val in device.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    mac = conn_val
                    break
            result[device_id] = {
                "device_id": device_id,
                "mac_address": mac,
                "integration_type": "sensa_teltonika_api",
            }
    except Exception:  # noqa: BLE001 — never let the recovery break setup
        return result
    return result


def teltonika_model_label(model: str) -> str:
    """A Teltonika model as a brand-prefixed label, without doubling it.

    "RUTX11" -> "Teltonika RUTX11". "Teltonika TSW2XX Switch" -> unchanged,
    because a router self-reports a bare model while a TSW switch reports one
    that already carries the brand. The blanket prefix produced "Teltonika
    Teltonika TSW2XX Switch" on the bench switch (seen in HA's device registry,
    2026-09-18). The coordinator's enrichment had the rule and this did not,
    which is the same predicate written twice; both call this now.
    """
    text = (model or "").strip()
    if not text:
        return "Teltonika"
    return text if text.lower().startswith("teltonika") else f"Teltonika {text}"


def teltonika_friendly_name(model: str, mac: str, data: dict) -> str:
    """Build the customer-facing HA device name for a Teltonika.

    Pre-poll (no real model in `data`): legacy MAC-only tag so the HA
    Devices & Services card has SOMETHING readable while the router
    is being authenticated. Post-poll: "Teltonika RUTX11 (a3b1c2)" —
    self-reported model from /system/device/status, plus the MAC tag
    in brackets so a fleet of identical models stays disambiguated.
    Customer's HA-side rename (`name_by_user`) always wins; this is
    only the integration-supplied default.
    """
    has_real_model = bool(
        data.get("device_model") and data.get("device_model") != "RutOS Device"
    )
    if not has_real_model:
        return teltonika_device_name(mac)
    clean = mac.replace(":", "").replace("-", "").lower()
    mac_tag = clean[-6:] if len(clean) >= 6 else ""
    label = teltonika_model_label(model)
    if mac_tag:
        return f"{label} ({mac_tag})"
    return label


# Backend URL is no longer a fixed constant — it depends on the
# repo-hash prefix Supervisor assigns to the `sensa_app` add-on, which
# varies per distribution channel. Live lookup lives in
# `backend_url.resolve_backend_url(hass)`; the fallback constant for
# bare-Core / Supervisor-unreachable cases is
# `backend_url.FALLBACK_BACKEND_URL`. The old `DEFAULT_BACKEND_URL`
# name was removed to make the upgrade path obvious.
DEFAULT_PROFILE_NAME = "Default"
# Both segments of the Sensa network: data (.1) and IoT (.2).
DEFAULT_SCAN_SUBNET = "192.168.1.0/24,192.168.2.0/24"
# Largest DERIVED scan range we will honour, in addresses. A range wider than
# this is clamped to the /24 around the host instead of being scanned in full.
#
# Load-bearing, not a tidy-up: both subnet sweeps enumerate every address in the
# range (`network.hosts()` in `config_flow.py`) against roughly 7-second budgets,
# so honouring a real /16 would mean 65,534 probes and would time the whole
# discovery source out. 512 admits a /23 and everything smaller, which covers
# every domestic and vehicle network we have seen, including the /28 an iPhone
# hotspot hands out.
SCAN_SUBNET_MAX_ADDRESSES = 512
# How often the derived scan range may be recomputed, in seconds.
#
# `async_get_adapters` is NOT cached by HA: each call opens a socket to resolve
# the source address and then enumerates every interface. The discovery sensor
# ticks every ~30s, so re-deriving per tick would put that on a hot path to catch
# something that changes when the van changes network. Five minutes detects a
# move quickly enough that a customer re-running a scan sees the new range.
SCAN_SUBNET_REDERIVE_SECONDS = 300

# Single source of truth for the Teltonika SSL default. RutOS 7+ routers ship
# with HTTPS-only /api. Anywhere in the integration that needs a fallback —
# API client constructor, coordinator startup, UI switch entity, Save Router
# Config button — imports this constant. Do NOT reintroduce inline `False`
# fallbacks; the fix has regressed once already because five call sites each
# had their own default. See tests/test_teltonika_ssl_defaults.py.
TELTONIKA_DEFAULT_SSL = True

# Every Teltonika is connected as `admin`: firmware management and SSH both
# need it, so the customer is asked only for the password. On a Teltonika the
# SSH root password is the admin password by default.
TELTONIKA_USERNAME = "admin"

# How often Sensa reads a router's GPS position, in seconds: the customer's
# "GPS refresh" on the router's row. The GPS module itself reports
# continuously; this is the only interval that decides how fresh the van's
# position is in Sensa. Read on its own timer so it is independent of the
# 30 s status poll in both directions. Default 1 s because GPS drives a
# speedo (Richard, 2026-10-01): BP-RUTX11 gives a new fix every second and
# answers /gps/position/status in about 80 ms (measured that day).
TELTONIKA_GPS_REFRESH_DEFAULT = 1
TELTONIKA_GPS_REFRESH_MIN = 1
TELTONIKA_GPS_REFRESH_MAX = 300

# The Sensa Simulator advertises every fake device from this locally-administered
# OUI (`02:5E:00`, the simulator's `network_config._SENSA_LAA_OUI`). Real hardware
# carries a global vendor OUI, so a MAC starting with this CANNOT be real kit and
# the test is safe to use as a "this is simulated" predicate with no false
# positives. Single source of truth: `config_flow` (ESPHome MAC-tag suffix),
# `sensor._synthesise_freshjet_child` and `sensor._mark_sim_name` (BLE picker rows)
# all import this rather than each spelling `"025E00"` inline — a duplicated
# predicate is this repo's recurring bug. Compare against a MAC with separators
# stripped and upper-cased.
SIM_MAC_OUI = "025E00"

def _relay_device_id_safe(device_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", str(device_id).lower())


def relay_channel_name_entity_id(device_id: str, n: int) -> str:
    """Deterministic entity_id for a relay channel name text entity."""
    return f"text.sensa_relay_{_relay_device_id_safe(device_id)}_{n}_name"


def relay_channel_enabled_entity_id(device_id: str, n: int) -> str:
    """Deterministic entity_id for a relay channel enabled switch entity."""
    return f"switch.sensa_relay_{_relay_device_id_safe(device_id)}_{n}_enabled"


def relay_device_name_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a relay device-name text entity."""
    return f"text.sensa_relay_{_relay_device_id_safe(device_id)}_device_name"


def relay_channel_assignment_entity_id(device_id: str, n: int) -> str:
    """Deterministic entity_id for a relay channel "Works with" select entity."""
    return f"select.sensa_relay_{_relay_device_id_safe(device_id)}_{n}_assignment"


def relay_channel_image_entity_id(device_id: str, n: int) -> str:
    """Deterministic entity_id for a relay channel image (picture URL) text entity."""
    return f"text.sensa_relay_{_relay_device_id_safe(device_id)}_{n}_image"


# entry.options keys for the per-port config, mirroring `relay_labels` /
# `relay_assignments`. Nested {device_id: {port_id: value}}.
SWITCH_PORT_LABELS_OPTION = "switch_port_labels"
SWITCH_PORT_ASSIGNMENTS_OPTION = "switch_port_assignments"


# --- network-switch ports ---------------------------------------------------
# The same "name it, say what it drives" config a relay channel gets, for the
# ports of a managed network switch. Keyed on the device's own PORT ID
# ("port1", "sfp1") rather than an index: a relay controller has a fixed 8
# channels, but a switch's port list is whatever the unit reports, and the id
# is the stable thing the customer also sees on the physical socket.


def switch_port_name_entity_id(device_id: str, port_id: str) -> str:
    """Deterministic entity_id for a switch-port name text entity."""
    return (
        f"text.sensa_netsw_{_relay_device_id_safe(device_id)}"
        f"_{_relay_device_id_safe(port_id)}_name"
    )


def switch_port_assignment_entity_id(device_id: str, port_id: str) -> str:
    """Deterministic entity_id for a switch-port "Device Type" select entity."""
    return (
        f"select.sensa_netsw_{_relay_device_id_safe(device_id)}"
        f"_{_relay_device_id_safe(port_id)}_assignment"
    )


# Service classes eligible for a "Home Layout" placement control — a per-device
# select choosing which column of the Home tab shows the device's compact card
# (Off / Left / Middle / Right). Heaters (air + hydronic), lighting controllers
# and air conditioners each get one. One definition shared by
# switch/select/sensor/dashboard/__init__ so adding a kind is a one-line change.
HEATER_SERVICE_CLASSES = ("air_heater", "hydronic_heater")
LIGHTING_SERVICE_CLASSES = ("lighting",)
AC_SERVICE_CLASSES = ("air_conditioner",)
LEVELLING_SERVICE_CLASSES = ("levelling",)


# The two Home surfaces that each offer a per-device placement select. A device
# eligible for a Home Layout control gets ONE select PER surface, so a customer
# can show it on the Habitation home, the Cab home, both, or neither — each with
# its own column choice. Each surface carries its own entry.options ledger
# ({device_id: "left"|"middle"|"right"}; absent id = "Off") and its own
# unique_id suffix (mutually non-suffixing so the registry backstop can tell them
# apart). One definition drives switch/select/sensor/dashboard/__init__.
HOME_LAYOUT_SURFACES: tuple[dict[str, str], ...] = (
    {
        "key": "hab",
        "label": "Habitation",
        "option": "home_layout_placement_hab",
        "uid_suffix": "_hab_home_layout",
        # Sort ORDER within the chosen column, its own ledger + uid suffix
        # ({device_id: int}; absent id = HOME_LAYOUT_ORDER_DEFAULT).
        "order_option": "home_layout_order_hab",
        "order_uid_suffix": "_hab_home_order",
    },
    {
        "key": "cab",
        "label": "Cab",
        "option": "home_layout_placement_cab",
        "uid_suffix": "_cab_home_layout",
        "order_option": "home_layout_order_cab",
        "order_uid_suffix": "_cab_home_order",
    },
)

# Sort order WITHIN a column. The placement select says which column a device's
# card goes in; this says where it sits among the others in that column. Lower
# first. Ties (the default, until the customer sets anything) fall back to the
# device NAME so the order is stable and alphabetical rather than dependent on
# registration order — otherwise two untouched devices could swap places on a
# rebuild for no visible reason.
#
# uid suffixes must not be suffixes of one another across BOTH lists (the
# registry backstops match with `endswith`): "_hab_home_layout" /
# "_cab_home_layout" / "_hab_home_order" / "_cab_home_order" all differ in the
# final word, so none can be claimed by another.
HOME_LAYOUT_ORDER_MIN = 1
HOME_LAYOUT_ORDER_MAX = 20
HOME_LAYOUT_ORDER_DEFAULT = 1

# The placement select's customer-facing option labels and the mapping to the
# stored column value. "Off" removes the device from that surface's home layout.
HOME_LAYOUT_OFF_LABEL = "Off"
HOME_LAYOUT_COLUMNS = ("left", "middle", "right")
HOME_LAYOUT_PLACEMENT_OPTIONS = ["Off", "Left", "Middle", "Right"]
HOME_LAYOUT_LABEL_TO_COLUMN: dict[str, str | None] = {
    "Off": None, "Left": "left", "Middle": "middle", "Right": "right",
}
HOME_LAYOUT_COLUMN_TO_LABEL: dict[str, str] = {
    "left": "Left", "middle": "Middle", "right": "Right",
}

# Legacy boolean "Show on Home Layout" switch ledgers (pre-column-select). Read
# ONCE at setup to migrate any already-ticked device into the HABITATION
# placement ledger at the "right" column (its historic placement), then never
# touched. See `_migrate_home_layout_options` in __init__.
HOME_LAYOUT_LEGACY_OPTIONS = (
    "home_layout_heaters", "home_layout_lights", "home_layout_acs",
)

# unique_id suffixes of the legacy Show-on-Home SWITCH entities (heater / light
# / AC) AND the single-surface placement SELECT that briefly existed before the
# hab/cab split. The control is a per-surface select now, so these are reaped
# from the registry once at setup (`_cleanup_legacy_home_layout_switches`) and
# on a device removal, or a restored unavailable entity would linger forever.
HOME_LAYOUT_LEGACY_SWITCH_SUFFIXES = (
    "_home_layout", "_home_layout_light", "_home_layout_ac",
)


def home_layout_surface_for(surface_key: str) -> dict[str, str] | None:
    """The surface spec for a key ("hab"/"cab"), or None."""
    for surface in HOME_LAYOUT_SURFACES:
        if surface["key"] == surface_key:
            return surface
    return None


def home_layout_placement_select_entity_id(
    device_id: str, surface_key: str,
) -> str:
    """Deterministic entity_id for a device's per-surface placement select."""
    return (
        f"select.sensa_{_relay_device_id_safe(device_id)}"
        f"_{surface_key}_home_layout"
    )


# "Temperature Sensor Source": what an Autoterm Air heater regulates on in
# Temperature mode (2026-09-24). The two Autoterm options map onto the
# controller firmware's own "Temperature Source" select; any other value is a
# van temperature sensor's entity_id, whose reading Nomad writes to the
# firmware's "Sensa Temperature" number. Options-as-truth in
# `entry.options[HEATER_TEMP_SOURCE_OPTION][device_id]`; absent reads as the
# controller (the panel), which is how the heater behaved before.
HEATER_TEMP_SOURCE_OPTION = "heater_temp_source"
HEATER_TEMP_SOURCE_UID_SUFFIX = "_heater_temp_source"
HEATER_TEMP_SOURCE_CONTROLLER = "autoterm_controller"
HEATER_TEMP_SOURCE_HEATER = "autoterm_heater"
# Our option id -> the controller firmware's select option (the contract in
# Sensa.Control.Firmware autoterm_uart TEMP_SOURCE_OPTIONS). A sensor choice
# is FIRMWARE_TEMP_SOURCE_SENSA.
HEATER_TEMP_SOURCE_FIRMWARE: dict[str, str] = {
    HEATER_TEMP_SOURCE_CONTROLLER: "Autoterm Controller",
    HEATER_TEMP_SOURCE_HEATER: "Autoterm Heater",
}
FIRMWARE_TEMP_SOURCE_SENSA = "Sensa Sensor"
# Customer-facing labels; a sensor option is labelled by its friendly name.
# Which heaters are offered the choice is `heater_temp_source.eligible`: the
# controller must expose the firmware's Temperature Source entity.
HEATER_TEMP_SOURCE_LABELS: dict[str, str] = dict(HEATER_TEMP_SOURCE_FIRMWARE)


# "Heater Model" (2026-09-24): which Air model the Air D controller is fitted
# to, because the extended diagnostics frame's field map differs between the
# Air 2D and Air 4D (`autoterm_diag.MODELS`). Options-as-truth in
# `entry.options[HEATER_MODEL_OPTION][device_id]`; unset means "not chosen",
# and nothing is decoded rather than decoded with a guessed map. The Baltica
# has one model and no choice.
HEATER_MODEL_OPTION = "heater_model"
HEATER_MODEL_UID_SUFFIX = "_heater_model"
HEATER_MODEL_UNSET = "not_set"


def heater_model_select_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a heater's Heater Model select."""
    return f"select.sensa_{_relay_device_id_safe(device_id)}_heater_model"


# "Network Setup" (2026-10-26): who runs a Teltonika router's network.
# Self managed = the customer's own setup, which Sensa reads but never
# measures against a layout; the two Nomad managed choices name the layout
# (`network_profile.LAYOUT_FILES`). Options-as-truth in
# `entry.options[ROUTER_SETUP_OPTION][device_id]`, chosen in Configure Router
# (never drawn as a dropdown). No default: unset is "Not set" and behaves as
# Self managed, so a router is never measured against a layout nobody chose.
ROUTER_SETUP_OPTION = "router_setup"
ROUTER_SETUP_UID_SUFFIX = "_router_setup"
ROUTER_SETUP_UNSET = "not_set"
ROUTER_SETUP_SELF = "self"
ROUTER_SETUP_SINGLE = "single"
ROUTER_SETUP_SECURE = "secure"
ROUTER_SETUP_LABELS: dict[str, str] = {
    ROUTER_SETUP_UNSET: "Not set",
    ROUTER_SETUP_SELF: "Self managed",
    ROUTER_SETUP_SINGLE: "Nomad managed: single network",
    ROUTER_SETUP_SECURE: "Nomad managed: secure",
}


# How many wired devices the customer has on each network (data, iot, cctv),
# per router: `entry.options[ROUTER_WIRED_OPTION][device_id]`, set in
# Configure Router and read by the socket plan (vendors/teltonika_setup.py).
ROUTER_WIRED_OPTION = "router_wired"
# What the customer arranged on Configure Router's board, per router:
# `entry.options[ROUTER_LAYOUT_OPTION][device_id]` = {sockets: {port: network},
# switches: {port: model}, devices: {MAC: port}}. Absent = Sensa's own plan.
ROUTER_LAYOUT_OPTION = "router_layout"


def router_setup_select_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a router's Network Setup select."""
    return f"select.sensa_{_relay_device_id_safe(device_id)}_router_setup"


def heater_temp_source_select_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a heater's Temperature Sensor Source select."""
    return f"select.sensa_{_relay_device_id_safe(device_id)}_heater_temp_source"


# Tyre pressure (TPMS) wheel positions (2026-09-26). No BLE TPMS format says
# which wheel a sensor is on, so the customer picks it per sensor. Options-as-
# truth in `entry.options[TYRE_POSITION_OPTION][device_id]`; one sensor per
# position. The ids are stable wire values: the Tyres card lays out by them.
TYRE_POSITION_OPTION = "tyre_positions"
TYRE_POSITION_UNSET = "not_set"
TYRE_POSITION_LABELS: dict[str, str] = {
    TYRE_POSITION_UNSET: "Not set",
    "front_left": "Front left",
    "front_right": "Front right",
    "rear_left": "Rear left",
    "rear_right": "Rear right",
    "rear_left_inner": "Rear left inner",
    "rear_right_inner": "Rear right inner",
    "spare": "Spare",
    "trailer_left": "Trailer left",
    "trailer_right": "Trailer right",
    "motorcycle_front": "Motorcycle front",
    "motorcycle_rear": "Motorcycle rear",
}
TYRE_POSITIONS: tuple[str, ...] = tuple(
    p for p in TYRE_POSITION_LABELS if p != TYRE_POSITION_UNSET
)


# Sensa Fan Temperature Control autopilot (2026-09-25). Each fan follows one van
# temperature sensor: off below its Start temperature, rising to 100% at its
# Max temperature. While Sleep mode is on, speed is capped at the device's
# Night Cap, except at or above the fan's Critical temperature. Options-as-
# truth in `entry.options[FAN_AUTOPILOT_OPTION][device_id]`, a flat dict of
# `fan_<n>_source`, `fan_<n>_start` / `_max` / `_critical` (degC) and
# `night_cap` (%). A fan whose source is FAN_SOURCE_MANUAL is left alone, so
# a fresh device is manual until the customer picks a sensor.
FAN_AUTOPILOT_OPTION = "fan_autopilot"
FAN_CHANNELS: tuple[int, ...] = (1, 2)
FAN_SOURCE_MANUAL = "manual"
FAN_SOURCE_LABELS: dict[str, str] = {FAN_SOURCE_MANUAL: "Manual (no autopilot)"}
# Per-fan numbers: key -> (label, min, max, step, default). Temperatures in degC;
# HA shows them in the customer's unit because the entity has a temperature
# device class.
FAN_TEMP_NUMBERS: dict[str, tuple[str, float, float, float, float]] = {
    "start": ("Start Temperature", 5.0, 60.0, 0.5, 25.0),
    "max": ("Max Temperature", 5.0, 70.0, 0.5, 35.0),
    "critical": ("Critical Temperature", 5.0, 90.0, 0.5, 45.0),
}
FAN_NIGHT_CAP_DEFAULT = 40.0


# Sensa Climate Control (2026-10-05, Pro and above): one target temperature,
# held by the customer's nominated primary heater and primary air conditioner
# judged by one nominated temperature sensor (`climate_control.py`).
# Options-as-truth in `entry.options[CLIMATE_CONTROL_OPTION]`: `enabled`,
# `target` (degC), `heater` / `secondary` / `third` / `ac` (device_id) and `sensor`
# (entity_id), each
# CLIMATE_CONTROL_NONE or absent when not nominated.
CLIMATE_CONTROL_OPTION = "climate_control"
CLIMATE_CONTROL_NONE = "none"
CLIMATE_CONTROL_TARGET_DEFAULT = 21.0
# (min, max, step) in degC; HA shows it in the customer's unit.
CLIMATE_CONTROL_TARGET_RANGE: tuple[float, float, float] = (10.0, 30.0, 0.5)
CLIMATE_CONTROL_SWITCH_ENTITY_ID = "switch.sensa_nomad_climate_control"
CLIMATE_CONTROL_TARGET_ENTITY_ID = "number.sensa_nomad_climate_control_target"
CLIMATE_CONTROL_STATUS_ENTITY_ID = "sensor.sensa_nomad_climate_control"
# The battery % at or above which an air conditioner that can heat heats off
# shore power (`ac_heat_soc`; 0 = shore power only).
CLIMATE_CONTROL_AC_HEAT_SOC_ENTITY_ID = "number.sensa_nomad_climate_control_ac_heat_soc"
CLIMATE_CONTROL_AC_HEAT_SOC_DEFAULT = 80.0
# How far (degC) under the target a diesel heater relights (`heater_restart`):
# a glow-plug start draws a lot, so not for every one-degree dip.
CLIMATE_CONTROL_HEATER_RESTART_ENTITY_ID = "number.sensa_nomad_climate_control_heater_restart"
CLIMATE_CONTROL_HEATER_RESTART_DEFAULT = 2.0
# The cannot-reach alarm: minutes without getting closer to the target
# (`alarm_minutes`), and how far (degC) the van may drift further from it than
# when heating or cooling began before alarming at once (`alarm_drift`).
CLIMATE_CONTROL_ALARM_MINUTES_ENTITY_ID = "number.sensa_nomad_climate_control_alarm_minutes"
CLIMATE_CONTROL_ALARM_MINUTES_DEFAULT = 30.0
CLIMATE_CONTROL_ALARM_DRIFT_ENTITY_ID = "number.sensa_nomad_climate_control_alarm_drift"
CLIMATE_CONTROL_ALARM_DRIFT_DEFAULT = 3.0
# Below this diesel tank % a heater is not used, and a low-fuel alarm is
# raised (`low_fuel`). Each heater slot's tank is its own pick (`<slot>_tank`).
CLIMATE_CONTROL_LOW_FUEL_ENTITY_ID = "number.sensa_nomad_climate_control_low_fuel"
CLIMATE_CONTROL_LOW_FUEL_DEFAULT = 10.0
# Nomination key -> (select entity_id, select name).
CLIMATE_CONTROL_SELECTS: dict[str, tuple[str, str]] = {
    "heater": ("select.sensa_nomad_climate_control_heater", "Climate Control Heater"),
    "secondary": (
        "select.sensa_nomad_climate_control_secondary_heater",
        "Climate Control Secondary Heater",
    ),
    "third": (
        "select.sensa_nomad_climate_control_third_heater",
        "Climate Control Third Heater",
    ),
    "ac": ("select.sensa_nomad_climate_control_air_conditioner", "Climate Control Air Conditioner"),
    "sensor": ("select.sensa_nomad_climate_control_sensor", "Climate Control Temperature Sensor"),
    "outside": (
        "select.sensa_nomad_climate_control_outside_sensor",
        "Climate Control Outside Temperature Sensor",
    ),
    "heater_tank": ("select.sensa_nomad_climate_control_heater_tank", "Climate Control Heater Diesel Tank"),
    "secondary_tank": (
        "select.sensa_nomad_climate_control_secondary_heater_tank",
        "Climate Control Secondary Heater Diesel Tank",
    ),
    "third_tank": (
        "select.sensa_nomad_climate_control_third_heater_tank",
        "Climate Control Third Heater Diesel Tank",
    ),
    # A Baltica picked as a heater (`climate_control.BALTICA_WARM_*` /
    # `BALTICA_OFF_*`): what it does when the room is warm, and when
    # SensaClimate is turned off.
    "baltica_warm": (
        "select.sensa_nomad_climate_control_baltica_when_warm",
        "Climate Control Baltica When Warm",
    ),
    "baltica_off": (
        "select.sensa_nomad_climate_control_baltica_when_off",
        "Climate Control Baltica When Off",
    ),
}


def fan_autopilot_key(channel: int, key: str) -> str:
    """The ledger key for one fan's setting (`fan_1_start`, `fan_2_source`...)."""
    return f"fan_{channel}_{key}"


def fan_source_select_entity_id(device_id: str, channel: int) -> str:
    """Deterministic entity_id for a fan's Temperature Sensor select."""
    return f"select.sensa_{_relay_device_id_safe(device_id)}_fan_{channel}_temp_source"


def fan_autopilot_number_entity_id(device_id: str, ledger_key: str) -> str:
    """Deterministic entity_id for a fan controller's autopilot number."""
    return f"number.sensa_{_relay_device_id_safe(device_id)}_{ledger_key}"


# Per fan, beside the settings above: `fan_<n>_auto` (default True) is whether
# the autopilot may drive that fan right now. The fan card turns it off when the
# customer moves the speed, so a hand-set speed is not undone within a minute,
# and its Auto button turns it back on. `fan_<n>_name` is the customer's name
# for the fan ("Roof fan"); empty means "Fan <n>".
def fan_auto_switch_entity_id(device_id: str, channel: int) -> str:
    """Deterministic entity_id for a fan's Auto (autopilot allowed) switch."""
    return f"switch.sensa_{_relay_device_id_safe(device_id)}_fan_{channel}_auto"


def fan_name_text_entity_id(device_id: str, channel: int) -> str:
    """Deterministic entity_id for a fan's customer-facing name."""
    return f"text.sensa_{_relay_device_id_safe(device_id)}_fan_{channel}_name"


# Sensa DC Meter 6: one INA226 per 12V circuit. The customer's name for each
# circuit lives on the entry, `DC_CIRCUIT_LABELS_OPTION[device_id]["circuit_<n>"]`,
# the same nested shape as `relay_labels`; empty means "Circuit <n>".
DC_METER_CHANNELS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
DC_CIRCUIT_LABELS_OPTION = "dc_circuit_labels"


def dc_circuit_name_entity_id(device_id: str, n: int) -> str:
    """Deterministic entity_id for a DC meter circuit's customer-facing name."""
    return f"text.sensa_{_relay_device_id_safe(device_id)}_circuit_{n}_name"


def home_layout_order_number_entity_id(
    device_id: str, surface_key: str,
) -> str:
    """Deterministic entity_id for a device's per-surface sort-order number."""
    return (
        f"number.sensa_{_relay_device_id_safe(device_id)}"
        f"_{surface_key}_home_order"
    )


def home_layout_order_for(
    options: dict[str, Any], surface: dict[str, str], device_id: str,
) -> int:
    """The stored sort order for a device on a surface, else the default.

    One reader for the entity, the Devices row and the dashboard builder, so
    they cannot disagree about what an unset order means.
    """
    raw = (options.get(surface.get("order_option") or "") or {}).get(device_id)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return HOME_LAYOUT_ORDER_DEFAULT


# --- Zigbee2MQTT vibration-sensor sensitivity ------------------------------
# 1 = most sensitive, 21 = least. The DJT11LM's `sensitivity` attribute is
# WRITE-ONLY: Zigbee2MQTT documents that it cannot be read (`/get`) back, so
# the MQTT `number.*_sensitivity` entity it publishes never carries a value and
# sits at `unknown` for ever, whether or not the write landed. The customer sets
# the slider, it snaps straight back to blank, and the control reads as broken.
#
# So Sensa REMEMBERS what was last written, in this ledger ({device_id: int}).
# The ledger is the value: it is what the Devices row renders, and it survives a
# restart and a reload. An ABSENT id means nobody has set it yet -- which is
# genuinely different from "set to 21", so it reads None (blank) rather than
# defaulting to a number we would be inventing. "Could not ask" is not "told
# no", and we have never asked the sensor.
VIBRATION_SENSITIVITY_OPTION = "vibration_sensitivity"
VIBRATION_SENSITIVITY_MIN = 1
VIBRATION_SENSITIVITY_MAX = 21
VIBRATION_SENSITIVITY_UID_SUFFIX = "_vibration_sensitivity"


def vibration_sensitivity_number_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a vibration sensor's sensitivity number."""
    return (
        f"number.sensa_{_relay_device_id_safe(device_id)}"
        f"_vibration_sensitivity"
    )


def vibration_sensitivity_for(
    options: dict[str, Any], device_id: str,
) -> int | None:
    """The last sensitivity written for this device, or None if never set.

    One reader for the entity and the Devices row, so they cannot disagree
    about what an unset sensitivity means.
    """
    raw = (options.get(VIBRATION_SENSITIVITY_OPTION) or {}).get(device_id)
    if raw is None:
        return None
    try:
        return max(
            VIBRATION_SENSITIVITY_MIN, min(VIBRATION_SENSITIVITY_MAX, int(raw)),
        )
    except (TypeError, ValueError):
        return None


# Per-kind cosmetics (icon + fallback display name) for the placement select,
# keyed by service class. The mechanism is otherwise kind-independent — one
# select entity, one ledger — so a new kind is just another entry here.
HOME_LAYOUT_KINDS: tuple[dict[str, Any], ...] = (
    {
        "key": "heater",
        "classes": HEATER_SERVICE_CLASSES,
        "icon": "mdi:home-thermometer",
        "fallback_name": "Heater",
    },
    {
        "key": "light",
        "classes": LIGHTING_SERVICE_CLASSES,
        "icon": "mdi:home-lightbulb",
        "fallback_name": "Light",
    },
    {
        "key": "ac",
        "classes": AC_SERVICE_CLASSES,
        "icon": "mdi:home-thermometer-outline",
        "fallback_name": "Air Conditioner",
    },
    {
        # Van level (2026-08-07). Its gyro used to be placed AUTOMATICALLY on
        # both home surfaces; it is now opt-in like every other kind, so a
        # customer who wants the park-up gyro on a home screen picks a column
        # for it. Existing installs are deliberately NOT seeded (Richard: "level
        # should no longer be placed automatically and only added if selected in
        # devices"), so a van that had it must re-select it.
        "key": "levelling",
        "classes": LEVELLING_SERVICE_CLASSES,
        "icon": "mdi:spirit-level",
        "fallback_name": "Van Level",
    },
)


def home_layout_kind_for(service_class: str | None) -> dict[str, Any] | None:
    """The Home-layout opt-in spec for a service class, or None when that class
    has no Show-on-Home toggle."""
    for kind in HOME_LAYOUT_KINDS:
        if service_class in kind["classes"]:
            return kind
    return None


# --- Header temperature chip (Settings -> Devices opt-in) ------------------
#
# "Show Temperature In Header": a per-device switch putting that device's
# temperature reading in the Sensa header bar, beside the Mode chip.
#
# Before 2026-09-03 the header showed EVERY environment temperature
# automatically (`dashboard._temperature_chips`), with no customer control: a
# van with a cabin probe, a fridge probe, an outside probe and a Cerbo room
# sensor filled the strip and overflowed on a narrow tablet. It is now opt-in
# per device, so the customer picks however many fit their screen.
#
# Deliberately a BOOLEAN and deliberately NOT per-surface, exactly like
# HEADER_POWER_CHIP below: `_habitation_chips` is ONE shared list feeding the
# Habitation, Cab and Settings headers, so a hab/cab split would be a choice
# with no effect.
#
# OFF by default and existing installs are deliberately NOT seeded (the same
# call made for Van Level on the Home layouts): the header carries the Mode
# chip until devices are picked.
HEADER_TEMP_CHIP_OPTION = "header_temperature_chips"
HEADER_TEMP_CHIP_ICON = "mdi:thermometer"

# Must not be a suffix of, or have as a suffix, any other per-device unique_id
# suffix (the platform registry backstops match with `endswith`). Checked
# against HEADER_POWER_CHIP_UID_SUFFIX, the HOME_LAYOUT suffixes and the relay
# "_enabled" tail by `tests/test_header_temp_chip.py`.
HEADER_TEMP_CHIP_UID_SUFFIX = "_header_temp_chip"

# The category, not a class list: every environment device (cabin / fridge /
# outside probes, Govee and Ruuvi tags, a Cerbo room sensor) is offered the
# control. Gating on the CLASS rather than on a live reading keeps the switch
# creatable before the vendor coordinator has registered any entities, exactly
# as HOME_LAYOUT_KINDS and HEADER_POWER_CHIP_CLASSES do; a device that turns
# out to report no temperature simply contributes no chip.
HEADER_TEMP_CHIP_CATEGORY = "environment"
HEADER_TEMP_CHIP_FALLBACK_NAME = "Sensor"


def header_temp_chip_switch_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a device's "Show Temperature In Header"
    switch."""
    return (
        f"switch.sensa_{_relay_device_id_safe(device_id)}_header_temp_chip"
    )


def header_temp_chip_eligible(service_class: str | None) -> bool:
    """Whether a device's service class is offered the header temperature
    chip."""
    from .vendors.service_classes import category_of

    if not service_class:
        return False
    return category_of(service_class) == HEADER_TEMP_CHIP_CATEGORY


def header_temp_chip_enabled(
    options: dict[str, Any], device_id: str,
) -> bool:
    """Whether a device's temperature is opted in to the header.

    One reader for the switch, the Devices row and the header builder, so the
    three can never disagree. The ledger stores only the ids that are ON
    (absent = off), so options stay minimal and a removed device leaves nothing
    behind but a dead id.
    """
    ledger = options.get(HEADER_TEMP_CHIP_OPTION) or []
    try:
        return device_id in ledger
    except TypeError:
        return False


# --- Temperature flags: Temperature Watch / Fridge / Freezer ----------------
# (Settings -> Devices opt-ins; 2026-09-03, split three ways 2026-09-04)
#
# Three per-device switches on every environment device's Devices row, each
# a ledger of device_ids in entry.options, each feeding ONE instinct in
# instincts_catalog.py:
#
#   Temperature Watch -> "Temperature watch": a rise or fall of X degC inside
#                        Y minutes (a cabin heating up, a compressor failing),
#   Fridge            -> "Fridge watch": drifting above a safe fridge temp,
#   Freezer           -> "Freezer watch": drifting above a safe freezer temp.
#
# Flag a device here instead of picking it on the instinct's own config, so
# any number of devices can be watched; each instinct discovers every device
# carrying its flag dynamically and never needs a picker or a slot count.
# Same eligibility as HEADER_TEMP_CHIP above (`header_temp_chip_eligible`,
# every "environment" device), reused rather than duplicated: the features
# are deliberately offered to the identical device set.
#
# ONE table drives the switch class, the Devices row fields, the orphan
# sweep, the post-setup add path and the dashboard resolver, so a fourth flag
# is one row here plus its instinct, and the three can never drift apart.


@dataclass(frozen=True)
class TemperatureFlag:
    """One per-device temperature opt-in: its options ledger, unique_id
    suffix, Devices-row label and icon.

    `uid_suffix` must not be a suffix of, or have as a suffix, any other
    per-device unique_id suffix (the platform registry backstops match with
    `endswith`); `tests/test_header_temp_chip.py` checks every flag.
    """

    key: str
    option: str
    uid_suffix: str
    label: str
    icon: str

    def switch_entity_id(self, device_id: str) -> str:
        """Deterministic entity_id for a device's switch for this flag."""
        return (
            f"switch.sensa_{_relay_device_id_safe(device_id)}{self.uid_suffix}"
        )

    def unique_id(self, entry_id: str, device_id: str) -> str:
        return f"{entry_id}_{device_id}{self.uid_suffix}"

    def enabled(self, options: dict[str, Any], device_id: str) -> bool:
        """Whether a device carries this flag.

        One reader for the switch, the Devices row and
        `dashboard._flagged_temperature_entities`, so the three can never
        disagree. Options-as-truth like HEADER_TEMP_CHIP_OPTION: the ledger
        holds only the ids that are ON (absent = off).
        """
        ledger = options.get(self.option) or []
        try:
            return device_id in ledger
        except TypeError:
            return False


TEMPERATURE_WATCH_FLAG = TemperatureFlag(
    key="temperature_watch",
    option="temperature_watch_devices",
    uid_suffix="_temperature_watch",
    label="Temperature Watch",
    icon="mdi:thermometer-alert",
)
FRIDGE_FLAG = TemperatureFlag(
    key="fridge",
    option="fridge_devices",
    uid_suffix="_fridge_flag",
    label="Fridge",
    icon="mdi:fridge-outline",
)
FREEZER_FLAG = TemperatureFlag(
    key="freezer",
    option="freezer_devices",
    uid_suffix="_freezer_flag",
    label="Freezer",
    icon="mdi:snowflake",
)
TEMPERATURE_FLAGS: tuple[TemperatureFlag, ...] = (
    TEMPERATURE_WATCH_FLAG, FRIDGE_FLAG, FREEZER_FLAG,
)
TEMPERATURE_FLAG_FALLBACK_NAME = "Sensor"

# The "Temperature watch" instinct needs "the reading Y minutes ago", which
# no HA trigger can see, so `sensor.SensaTemperatureWatchSensor` samples every
# device flagged Temperature Watch and publishes each one's change over the
# routine's window (degC, keyed by entity_id) under this attribute. The
# generated automation's template triggers read it; the two ends share these
# names so they cannot drift.
TEMPERATURE_WATCH_SENSOR_ENTITY_ID = "sensor.sensa_nomad_temperature_watch"
TEMPERATURE_WATCH_CHANGES_ATTR = "changes"

# The first flag's values under their original names, derived rather than
# retyped: still imported by the tests and by callers that only ever meant
# the Temperature Watch flag.
TEMPERATURE_WATCH_OPTION = TEMPERATURE_WATCH_FLAG.option
TEMPERATURE_WATCH_ICON = TEMPERATURE_WATCH_FLAG.icon
TEMPERATURE_WATCH_UID_SUFFIX = TEMPERATURE_WATCH_FLAG.uid_suffix
TEMPERATURE_WATCH_FALLBACK_NAME = TEMPERATURE_FLAG_FALLBACK_NAME


def temperature_watch_switch_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a device's "Temperature Watch" switch."""
    return TEMPERATURE_WATCH_FLAG.switch_entity_id(device_id)


def temperature_watch_enabled(options: dict[str, Any], device_id: str) -> bool:
    """Whether a device is flagged Temperature Watch (see TemperatureFlag)."""
    return TEMPERATURE_WATCH_FLAG.enabled(options, device_id)


# --- Header power chip (Settings -> Devices opt-in) -------------------------
#
# "Show Power In Header": a per-device switch putting that device's live power
# draw in the Sensa header bar, beside the Mode and temperature chips. Built
# for the Starlink dish, whose ~20-60 W is the single biggest discretionary
# load in most vans, so a customer running off battery wants it in their eyeline
# rather than a tab away on Habitation -> Network.
#
# Deliberately a BOOLEAN and deliberately NOT per-surface, exactly like the
# header temperature chip above. `_habitation_chips` is one shared list
# feeding the Habitation, Cab and Settings headers, so a hab/cab split here
# would be a choice with no effect -- see [[feedback_dont_invent_options]].
#
# OFF by default (opt-in, same call as the Home Layout controls): a header that
# filled itself with every power reading a van can produce would be noise.
HEADER_POWER_CHIP_OPTION = "header_power_chips"
HEADER_POWER_CHIP_ICON = "mdi:flash"

# Which service classes are offered the control, and the name to fall back on
# when the switch has to be rebuilt from the registry or the ledger with no
# backend row to read a display_name from. The role-map slug the chip reads is
# "power" (`dashboard._network_entity(device, "power")`), which today only a
# Starlink dish publishes; gating on the CLASS rather than on a live role-map
# keeps the switch creatable before the vendor coordinator has registered any
# entities, exactly as HOME_LAYOUT_KINDS does.
HEADER_POWER_CHIP_CLASSES: dict[str, str] = {
    "satellite_internet": "Starlink",
}

# Must not be a suffix of, or have as a suffix, any other per-device unique_id
# suffix (the platform registry backstops match with `endswith`). Checked
# against the HOME_LAYOUT / HEADER_TEMP_CHIP suffixes above and the relay
# "_enabled" tail: none of them ends in "_header_power_chip" and it ends in
# none of them.
HEADER_POWER_CHIP_UID_SUFFIX = "_header_power_chip"


def header_power_chip_switch_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a device's "Show Power In Header" switch."""
    return (
        f"switch.sensa_{_relay_device_id_safe(device_id)}_header_power_chip"
    )


def header_power_chip_eligible(service_class: str | None) -> bool:
    """Whether a device's service class is offered the header power chip."""
    return (service_class or "") in HEADER_POWER_CHIP_CLASSES


def header_power_chip_fallback_name(service_class: str | None) -> str:
    """Entity name to use when there is no backend row to read a name from."""
    return HEADER_POWER_CHIP_CLASSES.get(service_class or "", "Device")


def header_power_chip_enabled(
    options: dict[str, Any], device_id: str,
) -> bool:
    """Whether a device's power reading is opted in to the header.

    One reader for the switch, the Devices row and the header builder, so the
    three can never disagree. The ledger stores only the ids that are ON
    (absent = off), so options stay minimal and a removed device leaves nothing
    behind but a dead id.
    """
    ledger = options.get(HEADER_POWER_CHIP_OPTION) or []
    try:
        return device_id in ledger
    except TypeError:
        return False


# --- Header Short Name (Settings -> Devices) --------------------------------
#
# A shorter label for a device's header chip, so a customer can fit more chips
# ("Fridge" for "Govee H5075 Fridge Probe"). Offered on every device that can
# put a chip in the header (power or temperature), stored per device in
# `entry.options[HEADER_SHORT_NAME_OPTION]` as {device_id: name}. Blank = the
# chip keeps the device's display name.
#
# Must not be a suffix of, or have as a suffix, any other per-device unique_id
# suffix: none of the header chip / home layout suffixes ends in
# "_header_short_name" and it ends in none of them.
HEADER_SHORT_NAME_OPTION = "header_short_names"
HEADER_SHORT_NAME_UID_SUFFIX = "_header_short_name"


def header_short_name_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a device's "Header Short Name" field."""
    return f"text.sensa_{_relay_device_id_safe(device_id)}_header_short_name"


def header_chip_eligible(service_class: str | None) -> bool:
    """Whether a device can put a chip in the header at all (power or
    temperature), and so is offered a Header Short Name. Derived from the two
    chip gates rather than listing classes, so a class gaining a chip gains
    the name field with it."""
    return header_power_chip_eligible(service_class) or header_temp_chip_eligible(
        service_class
    )


def header_short_name(options: dict[str, Any], device_id: str) -> str:
    """The device's Header Short Name, or "" when none is set. The one reader
    for the text entity and both chip builders."""
    names = options.get(HEADER_SHORT_NAME_OPTION) or {}
    if not isinstance(names, dict):
        return ""
    return str(names.get(device_id) or "").strip()


def led_strip_select_entity_id(ha_device_id: str) -> str:
    """Deterministic entity_id for an addressable LED controller's strip-type
    (colour-order) select.

    Keyed on the **HA device id** of the Z2M controller (the same id the
    dashboard resolves the controller's `light` / `_scene` entities from), so a
    `light.*` registry event maps straight to its select with no Sensa↔HA id
    translation.
    """
    return f"select.sensa_led_strip_{_relay_device_id_safe(ha_device_id)}"


# Addressable-LED-controller "LED Strip" preset names — the options the
# `sensa-led-card` strip dropdown offers. Each name maps to a colour-channel
# order inside the card (webcards/src/sensa-led-card.ts `STRIP_PRESETS`):
# "Standard" → no remap (rgb); a named strip → the byte order its IC actually
# uses (e.g. WS2812B/SK6812 strips are GRB, so green/red read swapped under a
# plain RGB send). KEEP THIS LIST IN SYNC WITH THE CARD'S `STRIP_PRESETS` names.
# LED strip profiles, loaded from `led_strip_profiles.json` beside this file.
# DATA, not code: adding a strip is a JSON edit, the same way a device is.
#
# A profile names a pair the customer would otherwise have to look up on a reel
# or in a datasheet: the LIGHT BEAD SEQUENCE (which order the strip consumes its
# colour bytes) and the CHIP TYPE (its driver IC). The controller can be told
# both -- the GL-SPI-206P/WZ-SPI exposes them as settable enums -- so picking a
# profile configures the DEVICE rather than compensating for it in our card.
#
# Pixel count is deliberately NOT part of a profile: it depends on how long a
# run the customer cut, not on which strip it is, so it stays the device's own
# `lightpixel_number_set` number.
LED_STRIP_PROFILES_FILE = "led_strip_profiles.json"

# The sentinel that reveals the controller's own two pickers instead of applying
# a preset. Not in the JSON: it is a UI mode, not a strip.
LED_STRIP_CUSTOM: str = "custom"
LED_STRIP_CUSTOM_LABEL: str = "Custom"


def _load_led_strip_profiles() -> dict[str, Any]:
    """The profile table, or a minimal built-in if the file is unreadable.

    Never raises: this is imported at integration setup, and a malformed JSON
    must not take Nomad down. The fallback keeps "Standard", so a lighting
    controller still works with no remap rather than losing its select.
    """
    import json
    import os

    path = os.path.join(os.path.dirname(__file__), LED_STRIP_PROFILES_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not data.get("profiles"):
            raise ValueError("no profiles")
        return data
    except Exception:  # noqa: BLE001 - a bad file must not break setup
        return {
            "bead_sequences": ["RGB"],
            "chip_types": [],
            "profiles": [
                {"id": "standard", "name": "Standard", "bead_sequence": "RGB",
                 "chip_type": None},
            ],
        }


LED_STRIP_PROFILE_DATA: dict[str, Any] = _load_led_strip_profiles()

# Every profile, keyed by its **id** — the stable value the select stores.
#
# The id, never the label. A label is prose: it gets shortened, corrected and
# re-worded, and a stored label turns every one of those into a silent data
# migration. The ids are `standard`, `ledspace_splash_rgb`, and so on, and they
# do not change; the label is looked up for display and nothing else.
LED_STRIP_PROFILES: dict[str, dict[str, Any]] = {
    str(p.get("id")): p
    for p in LED_STRIP_PROFILE_DATA.get("profiles", [])
    if p.get("id")
}

# id -> what the customer reads. Published by the select as a `labels`
# attribute, because HA's own SelectEntity has no value/label split: its state
# IS one of `options`, so the UI has to do the lookup. Our device row does.
LED_STRIP_LABELS: dict[str, str] = {
    pid: str(p.get("name") or pid) for pid, p in LED_STRIP_PROFILES.items()
}

# The vocabularies the controller accepts. A value outside these is silently
# ignored by Z2M, so they are the validation for both the JSON and Custom.
LED_BEAD_SEQUENCES: list[str] = list(
    LED_STRIP_PROFILE_DATA.get("bead_sequences", [])
)
LED_CHIP_TYPES: list[str] = list(LED_STRIP_PROFILE_DATA.get("chip_types", []))

# Everything a box might already have STORED, mapped to the id it means now.
#
# Until 2026-09-16 the select stored the display LABEL, so a customer's saved
# value is a long product name; a label that has since been shortened is in the
# profile's `aliases`. Both fold to the id here, which is what makes the switch
# to ids a rename nobody notices rather than a fleet-wide reset to Standard.
LED_STRIP_ALIASES: dict[str, str] = {
    **{
        str(p.get("name")): str(p.get("id"))
        for p in LED_STRIP_PROFILE_DATA.get("profiles", [])
        if p.get("id") and p.get("name")
    },
    **{
        str(alias): str(p.get("id"))
        for p in LED_STRIP_PROFILE_DATA.get("profiles", [])
        if p.get("id")
        for alias in (p.get("aliases") or [])
    },
    LED_STRIP_CUSTOM_LABEL: LED_STRIP_CUSTOM,
}


def normalise_led_strip_profile(stored: str | None) -> str | None:
    """The profile ID a stored choice means, or None when it names nothing we
    ship. ONE reader: the restore path, and anything that later reads a saved
    value, must not each decide for themselves what an old label meant."""
    if not stored:
        return None
    if stored in LED_STRIP_PROFILES or stored == LED_STRIP_CUSTOM:
        return stored
    return LED_STRIP_ALIASES.get(stored)


def led_strip_profile_label(profile_id: str | None) -> str:
    """What to SHOW for a stored id. Falls back to the id itself, so an
    unlabelled profile reads as something rather than as blank."""
    if not profile_id:
        return ""
    if profile_id == LED_STRIP_CUSTOM:
        return LED_STRIP_CUSTOM_LABEL
    return LED_STRIP_LABELS.get(profile_id, profile_id)


# Profile ids plus Custom, in file order. The select's options — ids, so the
# stored value survives any amount of re-wording.
LED_STRIP_OPTIONS: list[str] = [*LED_STRIP_PROFILES, LED_STRIP_CUSTOM]
LED_STRIP_DEFAULT: str = "standard"


def light_type_select_entity_id(ha_device_id: str) -> str:
    """Deterministic entity_id for a lighting controller's "Light Type" select.

    Keyed on the **HA device id** of the Z2M light (the same id the dashboard
    resolves the controller's `light` entity from), so a `light.*` registry event
    maps straight to its select with no Sensa↔HA id translation. Mirrors
    `led_strip_select_entity_id`.
    """
    return f"select.sensa_light_type_{_relay_device_id_safe(ha_device_id)}"


# Power-On Delay — how long a relay-fed lighting controller is given to boot
# after its supply relay closes, before the card sends the first `light.turn_on`.
#
# The controller is a computer: the relay only restores its power, and a command
# that lands before it has rejoined Zigbee is simply lost. How long that takes is
# a property of the CUSTOMER'S hardware (controller, strip length, relay), so it
# is theirs to set on the device's row rather than a constant baked into a card.
#
# Seconds, one decimal. The default was 0.1 s until 2026-09-16, which was short
# enough that the first command was routinely lost and the customer saw a lit
# button over a dark strip until a retry landed.
LIGHT_POWER_ON_DELAY_DEFAULT_S: float = 0.5
LIGHT_POWER_ON_DELAY_MIN_S: float = 0.0
LIGHT_POWER_ON_DELAY_MAX_S: float = 10.0
LIGHT_POWER_ON_DELAY_STEP_S: float = 0.1
LIGHT_POWER_ON_DELAY_UID_PREFIX: str = "_power_on_delay_"


# The customer's display order for the Lights tab, as a LIST of card entity
# ids (group lights and individual lights in one list, because they sit in one
# grid and a customer dragging a card does not care which kind it is).
#
# A list, not a per-card index: the order IS a permutation, and two parallel
# index fields drift the moment a card is added. Anything not in the list is
# appended in the builder's own order, so a card added after the last drag
# still shows up rather than vanishing.
LIGHTING_CARD_ORDER_OPTION: str = "lighting_card_order"


def lighting_card_order(options: dict[str, Any]) -> list[str]:
    """The stored display order, or [] when the customer has never dragged."""
    stored = options.get(LIGHTING_CARD_ORDER_OPTION)
    if not isinstance(stored, list):
        return []
    return [str(item) for item in stored if isinstance(item, str) and item]


def sort_by_lighting_card_order(
    cards: list[dict[str, Any]], order: list[str],
) -> list[dict[str, Any]]:
    """`cards` in the customer's order, keyed on each card's `entity`.

    Unknown ids in the order are ignored and any card the order does not
    mention is appended in the order it came in. So a stale order can never
    hide a light, which is the failure mode that matters: a customer who
    added a light after their last drag must still see it.
    """
    by_entity: dict[str, dict[str, Any]] = {}
    for card in cards:
        entity = str(card.get("entity") or "")
        if entity and entity not in by_entity:
            by_entity[entity] = card
    ordered: list[dict[str, Any]] = []
    for entity in order:
        card = by_entity.get(entity)
        if card is not None and card not in ordered:
            ordered.append(card)
    ordered.extend(card for card in cards if card not in ordered)
    return ordered


def light_power_on_delay_number_entity_id(ha_device_id: str) -> str:
    """Deterministic entity_id for a lighting controller's Power-On Delay.

    Keyed on the **HA device id**, matching `light_type_select_entity_id` and
    `led_strip_select_entity_id`, so the dashboard wires the card from the same
    id it resolves the controller's `light` entity from.
    """
    return f"number.sensa_power_on_delay_{_relay_device_id_safe(ha_device_id)}"


# "Leave Relay On" — whether turning the light OFF also cuts its supply relay.
#
# Off (the default) is the behaviour since 2026-09-08: the relay follows the
# controller both ways, so nobody has to switch a channel separately from the
# light it feeds. On keeps the relay powered, so the controller stays booted and
# answers instantly, at the cost of its standby draw. A van that uses its
# lighting all evening wants that; one parked for a week does not.
LIGHT_LEAVE_RELAY_ON_UID_PREFIX: str = "_leave_relay_on_"


def light_leave_relay_on_switch_entity_id(ha_device_id: str) -> str:
    """Deterministic entity_id for a lighting controller's "Leave Relay On".

    Keyed on the HA device id, like the Power-On Delay beside it.
    """
    return f"switch.sensa_leave_relay_on_{_relay_device_id_safe(ha_device_id)}"


def light_leave_relay_on_unique_id(entry_id: str, ha_device_id: str) -> str:
    """The switch's unique_id. ONE spelling, shared by the entity, the
    Devices-row resolver and the late-add dedup."""
    return f"{entry_id}{LIGHT_LEAVE_RELAY_ON_UID_PREFIX}{ha_device_id}"


def light_power_on_delay_unique_id(entry_id: str, ha_device_id: str) -> str:
    """The Power-On Delay number's unique_id. ONE spelling, shared by the
    entity, the Devices-row resolver and the late-add dedup, so none of the
    three can drift into looking for an id nothing creates."""
    return f"{entry_id}{LIGHT_POWER_ON_DELAY_UID_PREFIX}{ha_device_id}"


# How a lighting card should RENDER — the three behaviours every "Light Type"
# choice ultimately resolves to:
#   • "Colour"        — show everything the entity supports.
#   • "Tunable White" — brightness + colour-temperature, NO colour mixer.
#   • "White"         — brightness only (no colour mixer, no colour-temp).
# KEEP THIS LIST IN SYNC WITH THE CARDS' `LIGHT_TYPE_*` constants
# (webcards/src/sensa-light-card.ts + sensa-led-card.ts).
LIGHT_TYPE_OPTIONS: list[str] = [
    "Colour",
    "Tunable White",
    "White",
]
LIGHT_TYPE_DEFAULT: str = "Colour"

# The default choice: follow whatever Zigbee2MQTT reports the controller is set
# to (`vendors.zigbee2mqtt.light_mode_for_model`). Kept as its own option rather
# than seeding the select with the detected value, so the choice stays LIVE — a
# customer who changes the mode on the hardware sees the software follow, with
# nothing to re-pick. An explicit pick is an override and stays put.
LIGHT_TYPE_AUTO: str = "Auto"

# The five output modes a Skydance WZ5 offers. ONE table, because these four
# facts about a mode must never drift apart: the customer-facing label, the RUN
# LED colour the unit lights, the render behaviour it implies, and the Z2M
# models that report it.
#
# Verified against the WZ5 manual, which states the colours outright: "(White:
# DIM, Yellow: CCT, Red: RGB, Green: RGBW, Blue: RGB+CCT)" - the same colours
# the match-button hold durations produce (8s DIM / 10s CCT / 12s RGB / 14s
# RGBW / 16s RGB+CCT).
#
# NOTE the two collapses, which are different and both deliberate:
#   * FIVE modes -> THREE render behaviours (RGB, RGBW and RGB+CCT all drive
#     colour; the extra white/CCT channel is the strip's wiring, not a different
#     card layout), so `behaviour` is what the cards read; and
#   * FIVE modes -> FIVE distinct LED colours, so anything showing the customer
#     *which mode* the unit is in must read `led`, NOT `behaviour`. Keying a
#     five-colour indicator off the three behaviours would paint RGB, RGBW and
#     RGB+CCT identically.
#
# `manufacturer_names` is the SECOND identity key, for the batch Z2M cannot
# resolve. A WZ5's mode IS its Zigbee identity (the match button rewrites the
# Tuya `manufacturerName`, which is what makes Z2M pick a different definition),
# so when a batch's code is unknown to zigbee-herdsman-converters the model id
# falls back to a generic `TS0601` and names nothing, while the raw code still
# names the mode exactly. Adding a customer's code restores Auto detection, the
# RUN LED colour and the product image for that batch with no code change.
# PUT IT IN THE CATALOG, on the WZ5 entry for that mode
# (`device_catalog.json` -> `discovery.zigbee.manufacturer_names`): the picker
# reads it there for the image, and `vendors.zigbee2mqtt._wz5_manufacturer_map`
# folds it in here through the entry's Zigbee model, so one edit serves every
# surface. This tuple's own `manufacturer_names` still work and win on a clash;
# they exist for a code that belongs to a mode with no catalog entry. On
# 2026-09-07 the two lists were kept by hand and disagreed: six controllers
# showed the right photo while the log kept calling them unrecognised. Empty
# until a batch is reported: a code we have not seen on a real device is a
# guess, and a guess in this table renders the wrong controls on a strip that
# cannot drive them.
WZ5_MODE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "label": "DIM (white LED)",
        "led": "white",
        "behaviour": "White",
        "models": ("wz5_dim_1", "wz5_dim_2"),
        "manufacturer_names": (),
    },
    {
        "label": "CCT (yellow LED)",
        "led": "yellow",
        "behaviour": "Tunable White",
        "models": ("wz5_cct",),
        "manufacturer_names": (),
    },
    {
        "label": "RGB (red LED)",
        "led": "red",
        "behaviour": "Colour",
        "models": ("wz5_rgb", "wz5_rgb_1"),
        "manufacturer_names": (),
    },
    {
        "label": "RGBW (green LED)",
        "led": "green",
        "behaviour": "Colour",
        "models": ("wz5_rgbw",),
        "manufacturer_names": (),
    },
    {
        "label": "RGB+CCT (blue LED)",
        "led": "blue",
        "behaviour": "Colour",
        "models": ("wz5_rgbcct",),
        "manufacturer_names": (),
    },
)

# label -> render behaviour, derived so it cannot fall out of step with the
# table above. Ordered as the match button cycles the modes.
WZ5_MODE_LABELS: dict[str, str] = {
    spec["label"]: spec["behaviour"] for spec in WZ5_MODE_SPECS
}
# label -> RUN LED colour, for any surface showing which mode the unit is in.
WZ5_MODE_LED_COLOURS: dict[str, str] = {
    spec["label"]: spec["led"] for spec in WZ5_MODE_SPECS
}


def nas_power_switch_entity_id(device_id: str) -> str:
    """Deterministic entity_id for an Asustor NAS power switch.

    The single switch per NAS: OFF = clean SSH shutdown, ON = Wake-on-LAN.
    Pinned on the entity and referenced by the Settings → Support card, same
    per-device pinned-id pattern as the relay / van-level helpers."""
    return f"switch.sensa_nas_{_relay_device_id_safe(device_id)}_power"


def nas_config_text_entity_id(device_id: str, key: str) -> str:
    """Deterministic entity_id for an Asustor NAS config text entity.

    `key` is one of host / ssh_username / ssh_password / ssh_key / wol_mac."""
    return f"text.sensa_nas_{_relay_device_id_safe(device_id)}_{key}"


def nas_save_button_entity_id(device_id: str) -> str:
    """Deterministic entity_id for an Asustor NAS save-config button."""
    return f"button.sensa_nas_{_relay_device_id_safe(device_id)}_save_config"


# --- IP camera (catalog `ip_camera`, integration `sensa_rtsp_camera`) --------
# The Pro tier's one camera, the grey-waste dump camera, shown live on the
# Habitation Dump tab through Frigate's restream (frigate_seed.py). Pinned ids
# so the tab card and the picker's config pane name the entities without a
# registry lookup.
IP_CAMERA_INTEGRATION = "sensa_rtsp_camera"
IP_CAMERA_IDENTIFIER_PREFIX = "ip_camera_"
IP_CAMERA_DEFAULT_STREAM_PATH = "stream1"


def ip_camera_entity_id(device_id: str) -> str:
    """Deterministic entity_id for an IP camera's live `camera` entity."""
    return f"camera.sensa_camera_{_relay_device_id_safe(device_id)}"


def ip_camera_config_text_entity_id(device_id: str, key: str) -> str:
    """Deterministic entity_id for an IP camera config text entity.

    `key` is one of stream_path / username / password."""
    return f"text.sensa_camera_{_relay_device_id_safe(device_id)}_{key}"


def ip_camera_save_button_entity_id(device_id: str) -> str:
    """Deterministic entity_id for an IP camera's Apply button."""
    return f"button.sensa_camera_{_relay_device_id_safe(device_id)}_apply"


def velit_climate_entity_id(device_id: str) -> str:
    """Deterministic entity_id for a Velit Rooftop AC's climate entity.

    `climate.py` pins this on the `VelitRooftopACClimate` entity so the
    dashboard's Climate-tab card can reference it without resolving the entity
    registry (same per-device pinned-id pattern as the relay / van-level
    helpers). One climate entity per air-conditioner device."""
    return f"climate.sensa_nomad_velit_{_relay_device_id_safe(device_id)}"


def velit_sensor_entity_id(device_id: str, key: str) -> str:
    """Deterministic entity_id for a Velit Rooftop AC telemetry sensor.

    The climate entity is one control that carries current/target temperature,
    fan speed, etc. as ATTRIBUTES. These companion `sensor.*` entities expose a
    few of those readings as standalone, history-recorded numbers so they can be
    graphed or used as automation triggers. They share the climate entity's HA
    device (same `velit_rooftop_ac_{device_id}` device_info identifier). Pinned
    so a re-add doesn't orphan a user's automations / history. `key` is one of
    `current_temperature` / `target_temperature` / `fan_speed`."""
    return f"sensor.sensa_nomad_velit_{_relay_device_id_safe(device_id)}_{key}"


def velit_binary_sensor_entity_id(device_id: str, key: str) -> str:
    """Deterministic entity_id for a Velit Rooftop AC binary sensor (e.g. the
    standalone power on/off state). Same device as the climate entity; pinned so
    automations survive a re-add. `key` is e.g. `power`."""
    return f"binary_sensor.sensa_nomad_velit_{_relay_device_id_safe(device_id)}_{key}"


# --- Van Level (levelling-service-class) entity IDs ----------------------
# Per-device entities use the same safe-slug helper as relays. Geometry
# numbers (track / wheelbase / shim tolerance) are *singletons* — one
# vehicle, one set of numbers — and live under the Sensa Nomad hub device,
# so they don't take a device_id suffix.

# One dropdown per logical axis: the lid axis that points that way, a trailing
# "-" when it points the opposite way. Replaced separate axis and +/- sign
# selects 2026-10-29.
VAN_LEVEL_AXIS_OPTIONS: tuple[str, ...] = ("X", "X-", "Y", "Y-", "Z", "Z-")
VAN_LEVEL_LOGICAL_AXES: tuple[str, ...] = ("forward", "left", "up")
# What the customer reads for each logical axis.
VAN_LEVEL_LOGICAL_LABELS: dict[str, str] = {
    "forward": "Front", "left": "Left", "up": "Up",
}
# Default per-axis (logical → physical) mapping for an identity install: the
# device sits flat with its +X pointing forward, +Y left, +Z up.
VAN_LEVEL_DEFAULT_AXIS_MAP: dict[str, str] = {
    "forward": "X", "left": "Y", "up": "Z",
}


def van_level_geometry_entity_id(key: str) -> str:
    """Deterministic entity_id for a singleton vehicle-geometry number."""
    return f"number.sensa_nomad_{key}"


# --- Miner autopilot (Crypto tab, Controls panel) -------------------------
# Singleton settings for the "mine on spare solar" engine. One van, one set of
# thresholds, so they live on the Sensa Nomad hub device with fixed ids the
# Crypto card binds to. The numbers hold values only; every rule between them
# (stop must sit below start, the hysteresis band) lives in
# `crypto_autopilot.py`, written once.
CRYPTO_AUTOPILOT_SWITCH_ENTITY_ID = "switch.sensa_nomad_crypto_autopilot"
CRYPTO_HEATER_OFF_SWITCH_ENTITY_ID = "switch.sensa_nomad_crypto_heater_off"
CRYPTO_ROOM_SENSOR_SELECT_ENTITY_ID = "select.sensa_nomad_crypto_room_sensor"
CRYPTO_AUTOPILOT_SENSOR_ENTITY_ID = "sensor.sensa_nomad_crypto_autopilot"
# The select's "no temperature guard" choice. A deliberate empty pick, distinct
# from a chosen probe that has since gone away (that is a fault, and pauses).
CRYPTO_ROOM_NOT_MONITORED = "Not monitored"
# key -> (name, icon, min, max, step, default, unit)
CRYPTO_AUTOPILOT_NUMBERS: dict[str, tuple[str, str, float, float, float, float, str]] = {
    "soc_start": ("Mining Start Battery Level", "mdi:battery-high", 50.0, 100.0, 5.0, 80.0, "%"),
    "soc_stop": ("Mining Stop Battery Level", "mdi:battery-low", 20.0, 95.0, 5.0, 50.0, "%"),
    "solar_per_miner": ("Solar Per Miner", "mdi:solar-power", 50.0, 400.0, 10.0, 150.0, "W"),
    "room_max": ("Mining Maximum Room Temperature", "mdi:home-thermometer", 15.0, 35.0, 1.0, 24.0, "°C"),
}


def crypto_autopilot_number_entity_id(key: str) -> str:
    """Deterministic entity_id for one miner-autopilot threshold."""
    return f"number.sensa_nomad_crypto_{key}"


# The van's Radar vehicle profile (height / width / length / weight / max
# gradient). The KEYS are Radar's own profile keys and travel to the map embed
# untranslated, so this list and Radar's `VehicleProfile` must stay in step.
VEHICLE_PROFILE_KEYS: tuple[str, ...] = (
    "height_m",
    "width_m",
    "length_m",
    "weight_t",
    "max_grade_pct",
)


def vehicle_profile_entity_id(key: str) -> str:
    """Deterministic entity_id for a vehicle-profile number."""
    return f"number.sensa_nomad_vehicle_{key}"


# Customer-selectable pictures for the Cab → Levelling tilt + top-down cards.
# Singletons (one van), held in `entry.options["level_images"][slot]`. Default
# to the Sensa van artwork shipped in the webcards bundle (served from
# `/local/sensa/images/`); the customer overrides each from the levelling
# sensor's row on Settings → Devices (`mode: "image"` config fields, moved there
# 2026-08-10 when the Settings → Level tab was deleted). An empty stored value
# means "use the Sensa default".
LEVEL_IMAGE_SLOTS: tuple[str, ...] = ("top", "side", "rear")
LEVEL_IMAGE_DEFAULTS: dict[str, str] = {
    "top": "/local/sensa/images/van-top.webp",
    "side": "/local/sensa/images/van-side.webp",
    "rear": "/local/sensa/images/van-rear.webp",
}
LEVEL_IMAGE_LABELS: dict[str, str] = {
    "top": "Top-down view",
    "side": "Side view (pitch)",
    "rear": "Rear view (roll)",
}


def level_image_entity_id(slot: str) -> str:
    """Deterministic entity_id for a singleton levelling-picture text entity."""
    return f"text.sensa_nomad_level_image_{slot}"


def van_level_offset_entity_id(device_id: str, axis: str) -> str:
    """Deterministic entity_id for a per-device pitch/roll offset number."""
    return f"number.sensa_van_level_{_relay_device_id_safe(device_id)}_{axis}_offset"


def van_level_axis_select_entity_id(device_id: str, logical_axis: str) -> str:
    """Deterministic entity_id for a per-device axis-mapping select.

    `logical_axis` is one of `forward` / `left` / `up`; state is one of
    VAN_LEVEL_AXIS_OPTIONS (`X`, `X-`, `Y`, `Y-`, `Z`, `Z-`).
    """
    return (
        f"select.sensa_van_level_{_relay_device_id_safe(device_id)}_"
        f"{logical_axis}_axis"
    )


def van_level_sign_select_entity_id(device_id: str, logical_axis: str) -> str:
    """Entity_id of the retired +/- sign select. Only read once, to fold an
    old "-" into the axis select's value on upgrade."""
    return (
        f"select.sensa_van_level_{_relay_device_id_safe(device_id)}_"
        f"{logical_axis}_sign"
    )


def van_level_orientated_sensor_entity_id(device_id: str, axis: str) -> str:
    """Deterministic entity_id for the orientation-mapped pitch/roll sensor."""
    return (
        f"sensor.sensa_van_level_{_relay_device_id_safe(device_id)}_"
        f"{axis}_orientated"
    )


def van_level_calibrated_sensor_entity_id(device_id: str, axis: str) -> str:
    """Deterministic entity_id for the offset-applied pitch/roll sensor."""
    return (
        f"sensor.sensa_van_level_{_relay_device_id_safe(device_id)}_"
        f"{axis}_calibrated"
    )


def van_level_reset_button_entity_id(device_id: str) -> str:
    """Deterministic entity_id for the per-device reset-offsets button."""
    return (
        f"button.sensa_van_level_{_relay_device_id_safe(device_id)}_reset_offsets"
    )


def van_level_calibrate_button_entity_id(device_id: str) -> str:
    """Deterministic entity_id for the per-device calibrate-now button."""
    return (
        f"button.sensa_van_level_{_relay_device_id_safe(device_id)}_calibrate_now"
    )


# Wheel labels for the per-wheel shim sensors. Order matters: the
# lowest-wheel sensor's state value is one of these strings.
VAN_LEVEL_WHEELS: tuple[str, ...] = ("FL", "FR", "RL", "RR")


def van_level_shim_sensor_entity_id(device_id: str, wheel: str) -> str:
    """Deterministic entity_id for a per-wheel shim height sensor.

    `wheel` is one of `FL` / `FR` / `RL` / `RR` (case-insensitive); the
    entity_id uses the lowercase form.
    """
    return (
        f"sensor.sensa_van_level_{_relay_device_id_safe(device_id)}_"
        f"shim_{wheel.lower()}_cm"
    )


def van_level_shim_max_sensor_entity_id(device_id: str) -> str:
    """Deterministic entity_id for the max-shim-across-wheels sensor."""
    return (
        f"sensor.sensa_van_level_{_relay_device_id_safe(device_id)}_shim_max_cm"
    )


def van_level_lowest_wheel_sensor_entity_id(device_id: str) -> str:
    """Deterministic entity_id for the lowest-wheel name sensor."""
    return (
        f"sensor.sensa_van_level_{_relay_device_id_safe(device_id)}_lowest_wheel"
    )


PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.CLIMATE,
    Platform.DEVICE_TRACKER,
    # Customer-created Light Groups only; we own no per-device lights.
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TEXT,
    # A registered Cerbo GX's Venus OS firmware. Sensa's own stack
    # update is NOT an update entity (it is the appliance OTA button).
    Platform.UPDATE,
]


# The customer's name for the AC a FreshJet bridge is paired with. The AC has
# no device record of its own, so its name lives in the bridge's `config`.
FRESHJET_AC_NAME_KEY = "ac_name"


def custom_ac_name(device: dict | None) -> str:
    """The customer's name for a bridge's paired AC, or "" for the default."""
    return str(((device or {}).get("config") or {}).get(FRESHJET_AC_NAME_KEY) or "").strip()
