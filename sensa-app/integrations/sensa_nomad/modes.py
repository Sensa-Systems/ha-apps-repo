"""Nomad Modes scene-YAML generation pipeline.

Mirror of `dashboard.py` but for the `/config/nomad/scenes.yaml` file
that holds the 8 managed Nomad Modes. Triggered by the
`SIGNAL_MODES_REGEN_REQUESTED` signal on every add / remove / rename /
enrich boundary, with a 2 s debounce upstream in `__init__.py`.

The generated scene set comes from three merged sources, in priority
order (later wins):

  1. Vendor hook:
     Every vendor module can expose
     `def get_mode_defaults(mode_slug, role_map) -> dict[str, dict]`
     returning `{entity_id: {state, attributes}}`. Used for anything
     that needs logic (lighting colour per mode, HVAC setpoints,
     MPPT current limits).

  2. Catalog fallback:
     `device_catalog.json` entries can carry a top-level `modes` block
     keyed by mode slug, values are role_map role → HA scene state:
       "modes": {"sleep": {"chrg_mosfet": "off"}, ...}
     For devices whose contribution is "flip one switch", no Python
     is needed.

  3. Customer override layer:
     Persisted via HA's Store helper; wins over (1) and (2) whenever
     the customer edits a mode via the sensa-mode-editor card.

Entity-id stability is guaranteed by running every role lookup through
HA's entity registry (same trick the BMS dashboard cards use), so a
device rename never leaves a scene referencing a stale slug.

Atomic writes (via `_io.atomic_write_text`) guarantee that a crash
mid-write never leaves `scenes.yaml` half-baked.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ._config_yaml import register_split_key, remove_split_key
from ._io import atomic_write_text, record_generated_checksum
from .const import (
    DOMAIN,
    MODES_FILE,
    MODES_MANAGED_BEGIN,
    MODES_MANAGED_END,
)
from .modes_catalog import (
    GATED_MODES,
    HOLD_MODE_SLUGS,
    LOCKED_MODE_SLUGS,
    MODES,
    ModeDef,
    custom_mode_slug,
    is_custom_slug,
)

_LOGGER = logging.getLogger(__name__)

_OVERRIDES_STORAGE_VERSION = 1
_OVERRIDES_STORAGE_KEY = f"{DOMAIN}.mode_overrides"

# Default startup mode on a fresh install: the Startup scene, which the
# customer fills with whatever the van should boot into. They can pick a
# different default from the Mode Editor.
_DEFAULT_STARTUP_MODE = "startup"
# Scenes that start OFF the Scenes card ("Include in Scenes card" unticked).
# Applied once per box (`home_defaults_applied`), so a later tick sticks.
_DEFAULT_HOME_HIDDEN = frozenset({"work", "standby", "service"})


def normalise_mode_order(
    slugs: list[Any], catalog: list[str] | None = None,
) -> list[str]:
    """Coerce a customer-supplied slug list into a full permutation of the
    catalog: keep known slugs in the given order (first occurrence wins),
    drop unknowns, then append every catalog slug not mentioned. `catalog`
    defaults to the preset slugs; `ModeOverrides` passes presets + custom."""
    if catalog is None:
        catalog = [m.slug for m in MODES]
    out: list[str] = []
    for raw in slugs:
        if not isinstance(raw, str):
            continue
        slug = raw.strip().lower()
        if slug in catalog and slug not in out:
            out.append(slug)
    out.extend(slug for slug in catalog if slug not in out)
    return out


# ---------------------------------------------------------------------------
# User-override persistence
# ---------------------------------------------------------------------------


class ModeOverrides:
    """Persistent layer of customer edits on top of the generated scene set.

    Storage shape (current):
        {
          "modes": {
            mode_slug: {
              "entities": {entity_id: {state: ..., attributes: {...}}},
              "excluded": [entity_id, ...],   # customer-deleted, subtracted
            },                                  # after the default merge
            ...
          },
          "startup_mode": "<slug>" | null,
          "mode_order": ["<slug>", ...] | null,   # customer tab/tile order
          "disabled": ["<slug>", ...],            # presets hidden by the customer
          "custom": [{"slug", "name", "icon"}, ...],  # customer-created scenes
          "labels": {"<slug>": {"name": ..., "icon": ...}},  # preset renames
        }

    Legacy shape (pre-startup-mode) is `{mode_slug: {...}, ...}` at the
    top level — `async_load` detects and migrates it transparently so
    existing installs upgrade without losing customer customisation.

    The storage key lives outside the config entry so renames of the
    entry (or reinstalls) preserve customer customisation.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, Any]](
            hass, _OVERRIDES_STORAGE_VERSION, _OVERRIDES_STORAGE_KEY,
        )
        self._data: dict[str, dict[str, Any]] = {}
        self._startup_mode: str | None = None
        # The slug of the most recently activated mode. Persisted so the
        # "Previous" startup option can restore the pre-restart mode.
        self._last_active_mode: str | None = None
        # Customer-chosen display order of the mode slugs (Scenes tabs, Home /
        # Cab tiles, Mobile pills). `None` = catalog order. Always a
        # permutation of the catalog slugs once set (see
        # `normalise_mode_order`), so consumers never see a missing mode.
        self._mode_order: list[str] | None = None
        # Presets the customer has hidden (still exist, still activatable by
        # a service / instinct; just not offered on tiles / pills / tabs).
        self._disabled: set[str] = set()
        # Scenes the customer has taken OFF the Habitation Home page's mode
        # bar (the "Include in Scenes card" tick in the scene popup). Stored
        # as the exclusions so every scene, presets and new ones alike, is on
        # Home until untucked, which is what the page showed before the tick
        # existed (2026-09-18).
        self._home_hidden: set[str] = set()
        self._home_defaults_applied = False
        # Customer-created scenes, in creation order: {slug, name, icon}.
        self._custom: list[dict[str, str]] = []
        # Display overrides for presets: slug -> {name?, icon?}. Slugs never
        # change, so a rename moves nothing.
        self._labels: dict[str, dict[str, str]] = {}
        # Hub feature gates this van has been TOLD are on (see GATED_MODES).
        # Persisted so a reboot with no signal keeps SOS where it was; never
        # told reads as off, which is how every gate ships.
        self._gates_on: set[str] = set()

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        if loaded is None:
            # Fresh install: seed the startup mode to the Startup scene so
            # the vehicle comes up in a known state on first boot. The
            # customer can change or clear it from the Mode Editor;
            # subsequent loads round-trip whatever they saved.
            self._startup_mode = _DEFAULT_STARTUP_MODE
            self._apply_home_defaults()
            await self.async_save()
            return
        if not isinstance(loaded, dict):
            return
        # New format carries the per-mode dict under "modes" and meta
        # fields alongside it. Legacy installs stored the per-mode dict
        # at the top level; detect that by the absence of the wrapper
        # keys and migrate in-memory (next save flushes the new shape).
        if "modes" in loaded or "startup_mode" in loaded:
            modes_block = loaded.get("modes")
            self._data = dict(modes_block) if isinstance(modes_block, dict) else {}
            startup = loaded.get("startup_mode")
            self._startup_mode = startup if isinstance(startup, str) and startup else None
            last = loaded.get("last_active_mode")
            self._last_active_mode = last if isinstance(last, str) and last else None
            disabled = loaded.get("disabled")
            self._disabled = {
                s for s in disabled if isinstance(s, str)
            } if isinstance(disabled, list) else set()
            home_hidden = loaded.get("home_hidden")
            self._home_hidden = {
                s for s in home_hidden if isinstance(s, str)
            } if isinstance(home_hidden, list) else set()
            custom = loaded.get("custom")
            self._custom = [
                {"slug": c["slug"], "name": str(c.get("name") or c["slug"]),
                 "icon": str(c.get("icon") or "mdi:star")}
                for c in custom
                if isinstance(c, dict) and isinstance(c.get("slug"), str)
                and is_custom_slug(c["slug"])
            ] if isinstance(custom, list) else []
            labels = loaded.get("labels")
            self._labels = {
                slug: {k: str(v) for k, v in spec.items()
                       if k in ("name", "icon") and isinstance(v, str) and v}
                for slug, spec in labels.items()
                if isinstance(slug, str) and isinstance(spec, dict)
            } if isinstance(labels, dict) else {}
            gates = loaded.get("feature_gates")
            self._gates_on = {
                g for g in gates if isinstance(g, str)
            } if isinstance(gates, list) else set()
            # Locked modes can never be hidden, whatever an older store says.
            self._disabled.difference_update(LOCKED_MODE_SLUGS)
            order = loaded.get("mode_order")
            self._mode_order = (
                normalise_mode_order(order, self.all_slugs())
                if isinstance(order, list) else None
            )
        else:
            self._data = loaded
        if not loaded.get("home_defaults_applied"):
            self._apply_home_defaults()
            await self.async_save()

    def _apply_home_defaults(self) -> None:
        self._home_hidden |= _DEFAULT_HOME_HIDDEN
        self._home_defaults_applied = True

    async def async_save(self) -> None:
        await self._store.async_save({
            "modes": self._data,
            "startup_mode": self._startup_mode,
            "last_active_mode": self._last_active_mode,
            "mode_order": self._mode_order,
            "disabled": sorted(self._disabled),
            "home_hidden": sorted(self._home_hidden),
            "home_defaults_applied": self._home_defaults_applied,
            "custom": [dict(c) for c in self._custom],
            "labels": {k: dict(v) for k, v in self._labels.items() if v},
            "feature_gates": sorted(self._gates_on),
        })

    def get_mode(self, mode_slug: str) -> dict[str, Any]:
        return dict(self._data.get(mode_slug) or {})

    def all(self) -> dict[str, dict[str, Any]]:
        return {slug: dict(v) for slug, v in self._data.items()}

    def get_startup_mode(self) -> str | None:
        return self._startup_mode

    async def async_set_startup_mode(self, mode_slug: str | None) -> None:
        """Persist the mode slug to activate on HA startup. `None` clears
        the setting — the integration won't fire any mode on the next
        restart. The sentinel ``"previous"`` restores the last-active mode
        (resolved at startup via `get_last_active_mode`)."""
        self._startup_mode = mode_slug if mode_slug else None
        await self.async_save()

    def get_last_active_mode(self) -> str | None:
        return self._last_active_mode

    # ---- the effective scene catalog: presets + custom ------------------

    def all_slugs(self) -> list[str]:
        """Every scene slug in catalog order: presets, then custom scenes in
        creation order. Ignores the customer's display order and hidden
        flags: this is "what exists", not "what's shown"."""
        return [m.slug for m in MODES] + [c["slug"] for c in self._custom]

    def get_mode_order(self) -> list[str]:
        """The display order of every scene slug: the customer's saved order
        (with anything created since appended), or catalog order when they've
        never reordered. Always every scene exactly once."""
        catalog = self.all_slugs()
        if self._mode_order is None:
            return catalog
        return normalise_mode_order(self._mode_order, catalog)

    async def async_set_mode_order(self, slugs: list[Any] | None) -> list[str]:
        """Persist the customer's tab/tile order. Unknown slugs are dropped and
        any scene missing from `slugs` is appended in catalog order, so a
        stale client can never hide a scene. `None` / empty resets to catalog
        order. Returns the effective order. Skips the write when unchanged."""
        catalog = self.all_slugs()
        new = normalise_mode_order(slugs, catalog) if slugs else None
        if new is not None and new == catalog:
            new = None
        if new == self._mode_order:
            return self.get_mode_order()
        self._mode_order = new
        await self.async_save()
        return self.get_mode_order()

    def get_mode_def(self, slug: str) -> ModeDef | None:
        """Look up any scene (preset or custom) by slug, with the customer's
        display overrides applied. None for an unknown slug."""
        for m in MODES:
            if m.slug == slug:
                label = self._labels.get(slug) or {}
                return ModeDef(
                    slug, label.get("name") or m.name, label.get("icon") or m.icon,
                )
        for c in self._custom:
            if c["slug"] == slug:
                return ModeDef(c["slug"], c["name"], c["icon"])
        return None

    def get_modes(self) -> list[ModeDef]:
        """Every scene, presets and custom, in the customer's display order,
        with display overrides applied. Includes hidden ones (see
        `is_enabled`); the scene pipeline generates all of them so a
        re-enabled preset is instantly usable."""
        out: list[ModeDef] = []
        for slug in self.get_mode_order():
            m = self.get_mode_def(slug)
            if m is not None:
                out.append(m)
        return out

    def is_available(self, slug: str) -> bool:
        """False for a preset whose Hub feature gate is off for this van:
        left out of every card and refused by `activate_mode`."""
        gate = GATED_MODES.get(slug)
        return gate is None or gate in self._gates_on

    async def async_set_feature_gate(self, key: str, on: bool) -> bool:
        """Record Hub's answer for a feature gate. Returns whether it changed,
        so the caller re-publishes the scene list only on a flip."""
        if on == (key in self._gates_on):
            return False
        if on:
            self._gates_on.add(key)
        else:
            self._gates_on.discard(key)
        await self.async_save()
        return True

    def is_enabled(self, slug: str) -> bool:
        return slug not in self._disabled

    def is_on_home(self, slug: str) -> bool:
        """Offered on the Habitation Home page's mode bar. Independent of
        `is_enabled` (Shown on tiles): a scene can be on the Cab / Mobile
        tiles yet off Home, and the Home bar shows a scene only when BOTH."""
        return slug not in self._home_hidden

    def is_custom(self, slug: str) -> bool:
        return any(c["slug"] == slug for c in self._custom)

    def modes_payload(self) -> list[dict[str, Any]]:
        """The list every mode card renders from, in display order. Mirrored
        onto `sensor.sensa_nomad_current_mode` as `attributes.modes`."""
        return [
            {
                "slug": m.slug,
                "name": m.name,
                "icon": m.icon,
                "enabled": self.is_enabled(m.slug),
                "home": self.is_on_home(m.slug),
                "custom": self.is_custom(m.slug),
                "locked": m.slug in LOCKED_MODE_SLUGS,
                "hold": m.slug in HOLD_MODE_SLUGS,
            }
            for m in self.get_modes()
            if self.is_available(m.slug)
        ]

    async def async_set_mode_enabled(self, slug: str, enabled: bool) -> bool:
        """Show / hide a scene on the tiles, pills and tabs. Locked presets
        (Drive, Parked) can't be hidden. Hiding the startup scene clears that
        choice so the van never boots into something invisible. Returns
        whether the flag changed."""
        if self.get_mode_def(slug) is None or slug in LOCKED_MODE_SLUGS:
            return False
        if enabled == (slug not in self._disabled):
            return False
        if enabled:
            self._disabled.discard(slug)
        else:
            self._disabled.add(slug)
            if self._startup_mode == slug:
                self._startup_mode = None
        await self.async_save()
        return True

    async def async_set_mode_home(self, slug: str, home: bool) -> bool:
        """Put a scene on, or take it off, the Habitation Home page's mode
        bar. Any scene, locked or not: Drive / Parked stay on the tiles so the
        instincts' choice is always visible somewhere, but Home is the
        customer's page. Returns whether the flag changed."""
        if self.get_mode_def(slug) is None:
            return False
        if home == (slug not in self._home_hidden):
            return False
        if home:
            self._home_hidden.discard(slug)
        else:
            self._home_hidden.add(slug)
        await self.async_save()
        return True

    async def async_create_mode(self, name: str, icon: str | None = None) -> ModeDef:
        """Create a customer scene. The slug is derived from the name once
        and never changes; it lands at the END of the display order."""
        clean = " ".join((name or "").split())[:40] or "New scene"
        slug = custom_mode_slug(clean, self.all_slugs())
        mode = {"slug": slug, "name": clean, "icon": (icon or "").strip() or "mdi:star"}
        self._custom.append(mode)
        if self._mode_order is not None:
            self._mode_order = [*self._mode_order, slug]
        await self.async_save()
        return ModeDef(slug, mode["name"], mode["icon"])

    async def async_delete_mode(self, slug: str) -> bool:
        """Delete a customer scene (presets can only be hidden). Drops its
        overrides, order slot, hidden flag and startup choice with it."""
        if not self.is_custom(slug):
            return False
        self._custom = [c for c in self._custom if c["slug"] != slug]
        self._data.pop(slug, None)
        self._labels.pop(slug, None)
        self._disabled.discard(slug)
        if self._mode_order is not None:
            self._mode_order = [s for s in self._mode_order if s != slug]
        if self._startup_mode == slug:
            self._startup_mode = None
        if self._last_active_mode == slug:
            self._last_active_mode = None
        await self.async_save()
        return True

    async def async_rename_mode(
        self, slug: str, name: str | None = None, icon: str | None = None,
    ) -> ModeDef | None:
        """Change a scene's display name and/or icon. Slugs never move, so
        this touches nothing generated. Empty name/icon = leave as is."""
        current = self.get_mode_def(slug)
        if current is None:
            return None
        clean_name = " ".join((name or "").split())[:40]
        clean_icon = (icon or "").strip()
        if not clean_name and not clean_icon:
            return current
        if self.is_custom(slug):
            for c in self._custom:
                if c["slug"] == slug:
                    if clean_name:
                        c["name"] = clean_name
                    if clean_icon:
                        c["icon"] = clean_icon
        else:
            label = dict(self._labels.get(slug) or {})
            if clean_name:
                label["name"] = clean_name
            if clean_icon:
                label["icon"] = clean_icon
            self._labels[slug] = label
        await self.async_save()
        return self.get_mode_def(slug)

    async def async_set_last_active_mode(self, mode_slug: str | None) -> None:
        """Record the most recently activated mode (for "Previous" startup).
        No-op write avoidance: skip the save when unchanged so a re-activation
        of the same mode doesn't churn the store."""
        new = mode_slug if mode_slug else None
        if new == self._last_active_mode:
            return
        self._last_active_mode = new
        await self.async_save()

    async def async_set_mode(
        self,
        mode_slug: str,
        *,
        entities: dict[str, dict[str, Any]] | None = None,
        excluded: list[str] | None = None,
    ) -> None:
        """Persist a customer edit.

        `entities` is the set of entity_ids → target state the customer
        wants in this mode's scene; `None` leaves the stored set untouched.

        `excluded` is the list of entity_ids the customer explicitly DELETED
        from the scene. It's subtracted in `_build_scenes_dict` AFTER the
        per-device catalog + vendor-hook merge, so a delete sticks even for an
        entity a device re-contributes on every regen (e.g. a BMS MOSFET flag).
        `None` leaves the stored exclusions untouched.
        """
        current = dict(self._data.get(mode_slug) or {})
        if entities is not None:
            current["entities"] = dict(entities)
        if excluded is not None:
            # An explicit delete WINS: drop the excluded entity_ids from
            # `entities` too. Without this, a caller that (wrongly) still carries
            # the entity in `entities` while also listing it in `excluded` would
            # have its delete silently undone — the `present` check below would
            # see the entity in `entities` and strip it back out of `excluded`,
            # so the row reappears on the next regen (the "deleted relay keeps
            # coming back / Starlink relay stayed in the Driving scene" report).
            ents = dict(current.get("entities") or {})
            for eid in excluded:
                ents.pop(eid, None)
            current["entities"] = ents
            # MERGE additively with any stored exclusions, then drop anything
            # still present in `entities` — a genuinely re-added entity (in
            # `entities`, NOT in this delete list) is no longer excluded. The
            # editor's `_removed` set is SESSION-LOCAL (empty on every reload)
            # and Save sends only THIS session's deletions, so a wholesale
            # replace would clear exclusions recorded in an earlier session.
            present = set(ents.keys())
            current["excluded"] = [
                e
                for e in dict.fromkeys(
                    [*(current.get("excluded") or []), *excluded]
                )
                if e and e not in present
            ]
        self._data[mode_slug] = current
        await self.async_save()

    async def async_prune_unregistered(self, hass: HomeAssistant) -> bool:
        """Drop override entities that no longer exist in HA's entity
        registry (their device was removed). Returns True if anything
        changed.

        Removal-path ONLY — do not call on add / restart, where an entity
        may be legitimately mid-registration and would be pruned by
        mistake. The caller invokes this right after a device purge, when
        the gone entities are already absent from the registry while every
        surviving device's entities remain. Keeps the store tidy and stops
        a re-added device's drifted `_2` entity_id from resurrecting stale
        state (the scene output is already protected by the registry
        filter in `_build_scenes_dict`; this is the housekeeping half)."""
        registered = _registered_entity_ids(hass)
        if registered is None:
            return False
        # Conservative for a DESTRUCTIVE op: treat an entity as present if
        # it's in the registry OR still has a live state, so a registry-less
        # entity (a YAML/template helper without a unique_id) is never
        # permanently deleted from the store. Only entities gone from BOTH —
        # i.e. a genuinely removed device's entities — are pruned.
        try:
            present = registered | set(hass.states.async_entity_ids())
        except Exception:  # noqa: BLE001
            present = registered
        changed = False
        for slug, mode in list(self._data.items()):
            new_mode = dict(mode)
            mode_changed = False

            entities = mode.get("entities")
            if isinstance(entities, dict):
                kept = {
                    eid: st for eid, st in entities.items() if eid in present
                }
                if len(kept) != len(entities):
                    new_mode["entities"] = kept
                    mode_changed = True

            # Reap the exclusion list the same way: a removed device's
            # entity_ids must not linger in `excluded`, or the list grows
            # unbounded across add/remove cycles AND a later device that reuses
            # a freed entity_id would be wrongly suppressed from the scene by a
            # stale exclusion the customer never meant for it.
            excluded = mode.get("excluded")
            if isinstance(excluded, list):
                kept_excl = [e for e in excluded if e in present]
                if len(kept_excl) != len(excluded):
                    new_mode["excluded"] = kept_excl
                    mode_changed = True

            if mode_changed:
                self._data[slug] = new_mode
                changed = True
        if changed:
            await self.async_save()
        return changed


# ---------------------------------------------------------------------------
# Vendor contribution resolver
# ---------------------------------------------------------------------------


def _registered_entity_ids(hass: HomeAssistant) -> set[str] | None:
    """Return every entity_id in HA's entity registry, or None when the
    registry can't be reached (test stubs).

    None means "can't determine" — callers treat it as "don't filter"
    (lenient, same posture as `_resolve_vendor_role_map`), so a missing
    registry never silently empties a scene."""
    try:
        from homeassistant.helpers import entity_registry as er
    except ImportError:
        return None
    try:
        ent_reg = er.async_get(hass)
        # Iterate `.values()` (not `.keys()`): the same access
        # `_resolve_vendor_role_map` uses, compatible with real HA's
        # EntityRegistryItems and the test fakes alike.
        return {ent.entity_id for ent in ent_reg.entities.values()}
    except Exception:  # noqa: BLE001
        return None


def _resolve_vendor_role_map(
    hass: HomeAssistant, device: dict[str, Any],
) -> dict[str, str]:
    """Reconstruct {role_slug: entity_id} for any Sensa-owned vendor
    device by scanning HA's entity registry.

    Generalises the BMS-specific resolver in `__init__._attach_bms_entity_prefixes`:
    for each known Sensa identifier prefix (`bms_`, `teltonika_`, `starlink_`,
    `victron_cerbo_`), walk entities whose unique_id carries that prefix
    and the backend device_id, and key them by slugified `original_name`.
    The resulting dict is the same role_map the BMS dashboard cards
    consume, so vendor contribution hooks can translate abstract role
    slugs (`chrg_mosfet`, `pack_voltage`, `soc`) into real entity_ids.

    Empty result if the device has no entities yet (freshly added but
    platform-setup hasn't completed) — callers treat that as "no
    contribution for this device this cycle".
    """
    try:
        from homeassistant.helpers import entity_registry as er
        from homeassistant.util import slugify as _ha_slugify
    except ImportError:
        return {}
    try:
        ent_reg = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return {}

    device_id = device.get("device_id")
    if not device_id:
        return {}

    # Every Sensa vendor identifier uses `(DOMAIN, "<prefix><device_id>")`.
    # We can just scan for the `_<prefix>_<device_id>_` pattern inside
    # unique_ids regardless of which prefix was actually used.
    prefixes = ("bms_", "teltonika_", "starlink_", "victron_cerbo_")
    markers = [f"_{p}{device_id}_" for p in prefixes]

    role_map: dict[str, str] = {}
    for ent in ent_reg.entities.values():
        uid = ent.unique_id or ""
        if not any(marker in uid for marker in markers):
            continue
        original = getattr(ent, "original_name", None) or ""
        if not original:
            continue
        role_slug = _ha_slugify(original)
        if not role_slug:
            continue
        role_map.setdefault(role_slug, ent.entity_id)
    return role_map


def _vendor_hook(integration: str):
    """Return a vendor module's `get_mode_defaults` callable, or None.

    Lazy import so the hook itself doesn't need to exist on vendors
    that don't implement it — absent hook is treated as "no
    contribution".
    """
    module_for_integration = {
        "sensa_bms_ble": "custom_components.sensa_nomad.vendors.bms_ble",
        "sensa_teltonika_api": "custom_components.sensa_nomad.vendors.teltonika_api",
        "sensa_starlink_grpc": "custom_components.sensa_nomad.vendors.starlink",
        "sensa_victron_mqtt": "custom_components.sensa_nomad.vendors.victron_mqtt",
    }
    module_name = module_for_integration.get(integration)
    if not module_name:
        return None
    try:
        import importlib
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001
        return None
    return getattr(module, "get_mode_defaults", None)


def _catalog_modes_block(device: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pull the catalog `modes` block for this device, if any.

    Finds the catalog entry that matches the device's integration +
    vendor_model, same lookup logic as `_enrich_from_catalog`.
    """
    try:
        from .device_catalog import PHYSICAL_DEVICE_CATALOG
    except ImportError:
        return {}

    integration = (
        device.get("integration_type")
        or device.get("integration")
        or ""
    )
    vendor_model = (
        device.get("vendor_model")
        or device.get("model")
        or ""
    )
    for entry in PHYSICAL_DEVICE_CATALOG:
        if entry.get("integration") != integration:
            continue
        # vendor_model match tightens the selection for multi-model
        # catalog entries (roamer gen1 vs gen2); absent vendor_model on
        # the device row falls back to integration-only.
        entry_model = entry.get("vendor_model") or ""
        if vendor_model and entry_model and entry_model != vendor_model:
            continue
        return dict(entry.get("modes") or {})
    return {}


# ---------------------------------------------------------------------------
# Scene dict assembly
# ---------------------------------------------------------------------------


def _build_scenes_dict(
    hass: HomeAssistant,
    devices: list[dict[str, Any]],
    overrides: ModeOverrides,
) -> dict[str, list[dict[str, Any]]]:
    """Assemble the full `{"scene": [...]}` dict for scenes.yaml.

    One scene per mode; each scene's `entities` section is the merged
    union of every registered device's contribution for that mode,
    with customer overrides applied last.

    Why one scene per mode (not per-device): HA's scene platform
    expects a single activate-by-id per named scene. A customer tapping
    "Night" should fire one `scene.turn_on`, not eight per-device sub-
    scenes. Per-mode single scenes also keep entity_ids
    (`scene.nomad_night`) stable no matter how the device set churns.
    """
    scenes: list[dict[str, Any]] = []

    # Resolved once: the set of entity_ids HA actually has. Used to drop a
    # removed device's orphaned override entities from the generated scene
    # (non-destructive — the stored override is untouched, so a transiently
    # missing entity returns next regen). None = registry unreachable →
    # don't filter (lenient).
    registered = _registered_entity_ids(hass)

    # Presets AND customer-created scenes (hidden ones included, so a
    # re-enabled scene is instantly usable and a hidden one still works
    # for a service / startup call).
    for mode in overrides.get_modes():
        mode_entities: dict[str, dict[str, Any]] = {}

        # 1. Vendor hook + 2. catalog fallback, merged per-device.
        for device in devices:
            integration = (
                device.get("integration_type")
                or device.get("integration")
                or ""
            )
            if not integration:
                continue

            role_map = _resolve_vendor_role_map(hass, device)
            if not role_map:
                # Entities not registered yet — skip this device this
                # cycle, next regen will pick it up.
                continue

            # Catalog fallback (weaker — vendor hook can override).
            catalog_block = _catalog_modes_block(device)
            mode_catalog = catalog_block.get(mode.slug) or {}
            for role, state in mode_catalog.items():
                entity_id = role_map.get(role)
                if not entity_id:
                    continue
                mode_entities[entity_id] = _normalise_scene_state(state)

            # Vendor hook (wins over catalog for same entity_id).
            hook = _vendor_hook(integration)
            if hook is not None:
                try:
                    contrib = hook(mode.slug, role_map) or {}
                except Exception:
                    _LOGGER.warning(
                        "modes: vendor hook for %s raised on mode %s",
                        integration, mode.slug, exc_info=True,
                    )
                    contrib = {}
                for entity_id, state in contrib.items():
                    mode_entities[entity_id] = _normalise_scene_state(state)

        # 3. Customer override layer (wins over everything).
        override = overrides.get_mode(mode.slug)
        override_entities = override.get("entities") or {}
        for entity_id, state in override_entities.items():
            if registered is not None and entity_id not in registered:
                # The owning device was removed — drop its orphan rather
                # than writing a dangling entity_id into scenes.yaml.
                continue
            mode_entities[entity_id] = _normalise_scene_state(state)

        # 4. Customer exclusions (delete-means-delete). Entity_ids the customer
        # explicitly removed in the editor are subtracted LAST, so a delete
        # sticks even against a catalog / vendor-hook contribution that would
        # otherwise re-add the entity on every regen (the recurring "I deleted
        # the Roamer battery rows but they keep coming back" report).
        for entity_id in override.get("excluded") or []:
            mode_entities.pop(entity_id, None)

        # HA's YAML scene platform derives entity_id from slugify(name),
        # NOT from the `id:` field (which is used for storage-mode
        # persistence tracking only). The scene name is built from the
        # stable SLUG (not `mode.name`) so the entity_id is always
        # `scene.nomad_<slug>` — the convention the dashboard tiles and
        # every downstream automation reference via
        # `modes_catalog.scene_entity_id`. This DECOUPLES the entity_id
        # from the customer-facing `mode.name`: a display rename (e.g.
        # "Driving" → "Drive") changes the tile label only and never
        # moves the scene entity, so nothing is orphaned. slug.title()
        # reproduces the previous names exactly ("driving" → "Driving"),
        # so existing installs see no entity_id change.
        scene = {
            "id": f"nomad_{mode.slug}",
            "name": f"Nomad {mode.slug.title()}",
            "icon": mode.icon,
            "entities": mode_entities,
        }
        scenes.append(scene)

    return {"scene": scenes}


def build_mode_entities(
    hass: HomeAssistant,
    devices: list[dict[str, Any]],
    overrides: ModeOverrides,
    mode_slug: str,
) -> dict[str, dict[str, Any]]:
    """The merged `{entity_id: {state, ...}}` for ONE scene.

    Exists because the Mode Editor cannot recover a scene's target states from
    the live `scene.nomad_<slug>` entity: HA exposes only the entity_id list,
    not what each entity is meant to be set to. That was harmless while a
    scene stored nothing but `state`, but once it stores a brightness and a
    colour, seeding the editor from the scene entity would show the lights'
    CURRENT values and quietly save those over the captured ones.

    Deliberately runs the whole `_build_scenes_dict` and picks one scene out,
    rather than reimplementing the three-layer merge for a single slug: the
    editor showing a different answer from the one written to scenes.yaml is
    exactly the bug this is here to prevent.
    """
    for scene in _build_scenes_dict(hass, devices, overrides).get("scene", []):
        if scene.get("id") == f"nomad_{mode_slug}":
            return scene.get("entities") or {}
    return {}


def resolve_light_types(
    hass: HomeAssistant, entity_ids: Any,
) -> dict[str, str]:
    """`{light entity_id: resolved Light Type}` for the lights in a scene.

    The Mode Editor needs this to decide which controls a light row may offer.
    It cannot work it out itself: the Light Type select is keyed on the light's
    HA DEVICE id, which a card never sees, and reading the light's own
    `supported_color_modes` instead is the trap `shared/light-type.ts` exists to
    stop -- a Skydance WZ5 in dim mode advertises colour it cannot drive, and
    the customer would save a colour that silently does nothing.

    So the answer is read straight off the select's `resolved` attribute, which
    is the one place it is computed. A light with no Light Type select (a plain
    Zigbee bulb) is simply absent from the map, and the editor falls back to
    offering everything the entity advertises.
    """
    from homeassistant.helpers import entity_registry as er

    from .const import light_type_select_entity_id

    out: dict[str, str] = {}
    try:
        registry = er.async_get(hass)
    except Exception:  # noqa: BLE001
        return out
    for entity_id in entity_ids or []:
        if not isinstance(entity_id, str) or not entity_id.startswith("light."):
            continue
        entry = registry.async_get(entity_id)
        device_id = getattr(entry, "device_id", None) if entry else None
        if not device_id:
            continue
        select = hass.states.get(light_type_select_entity_id(device_id))
        resolved = (select.attributes or {}).get("resolved") if select else None
        if isinstance(resolved, str) and resolved:
            out[entity_id] = resolved
    return out


def _normalise_scene_state(
    state: Any,
) -> dict[str, Any]:
    """Accept either a bare state string or a full `{state, ...}` dict
    and return the HA-scene-expected dict shape.

    Catalog entries can shorten `"chrg_mosfet": "off"` (bare state) or
    spell out `"chrg_mosfet": {"state": "off", "some_attr": ...}`. The
    full form is the HA canonical; the bare form is what most modes
    blocks will look like in practice.

    HA wants attributes as SIBLINGS of `state`, but the Mode Editor has
    always posted them nested under an `attributes` key (harmless while it
    only ever sent an empty one). Left alone, a populated nested dict writes
    a literal `attributes:` key into scenes.yaml and HA sets an attribute
    *named* `attributes` — the light comes back on at its old brightness and
    nothing says why. So the nested form is spliced up to siblings here,
    which is the one place both the editor and the vendor hooks pass through.
    An explicit sibling wins over the same key nested, since that is the
    canonical spelling.
    """
    if not isinstance(state, dict):
        return {"state": str(state)}
    out = dict(state)
    nested = out.pop("attributes", None)
    if isinstance(nested, dict):
        for key, value in nested.items():
            out.setdefault(key, value)
    return out


# ---------------------------------------------------------------------------
# Scene YAML writer + configuration.yaml registration
# ---------------------------------------------------------------------------


def _write_scenes_yaml(config_dir: str, scenes: dict[str, Any]) -> None:
    """Serialise the scenes dict and atomically write it to
    `{config_dir}/nomad/scenes.yaml`."""
    path = Path(config_dir) / MODES_FILE
    content = yaml.safe_dump(
        scenes.get("scene", []),
        sort_keys=False,
        allow_unicode=True,
    )
    atomic_write_text(path, content)
    record_generated_checksum(config_dir, MODES_FILE, content)
    _LOGGER.debug("Nomad modes: scenes YAML written to %s", path)


# LEGACY bare `scene: !include nomad/scenes.yaml` line — the pre-fix form
# that duplicated HA's default top-level `scene:` key (so HA discarded one
# scene file, the long-standing "duplicate key scene" bug). The current
# managed block uses the labelled `scene sensa:` key (below), so this pattern
# only ever matches the old broken form, never the new one. Stripped on every
# registration call so an install that upgraded from the broken form is
# migrated automatically.
_LEGACY_BARE_SCENE_PATTERN = re.compile(
    r"^scene:\s*!include\s+nomad/scenes\.yaml\s*\n?", re.MULTILINE,
)


def _ensure_scenes_registration(config_dir: str) -> bool:
    """Make sure configuration.yaml carries our managed scene block, using
    HA's **labelled split-config key** ``scene sensa: !include …`` — NOT a
    bare ``scene:`` and NOT a ``homeassistant: packages:`` wrapper:

    - A bare ``scene: !include …`` DUPLICATES HA's default
      ``scene: !include scenes.yaml`` top-level key. HA's YAML loader does NOT
      merge two identical mapping keys — it discards one (last-wins) and logs a
      "duplicate key scene" warning. That silently dropped EITHER the customer's
      UI scenes (empty HA Scenes editor) OR our nomad scenes. This was reported
      repeatedly and wrongly dismissed as "cosmetic"; it is a real bug.
    - A ``packages:`` wrapper isn't re-read by ``scene.reload``, so regen-time
      YAML edits never reached runtime.

    ``scene sensa:`` is still top-level scene config (so ``scene.reload`` re-reads
    it) AND a distinct key (so it MERGES with the default ``scene:`` instead of
    colliding) — HA maps ``scene <label>:`` to the ``scene`` domain, exactly as
    it does ``automation <label>:``.

    Returns True if configuration.yaml changed.
    """
    return register_split_key(
        config_dir,
        begin_marker=MODES_MANAGED_BEGIN,
        end_marker=MODES_MANAGED_END,
        domain="scene",
        label="sensa",
        file_path=MODES_FILE,
        legacy_patterns=[_LEGACY_BARE_SCENE_PATTERN],
    )


def _remove_managed_scene_block(config_dir: str) -> bool:
    """Strip our managed block from configuration.yaml (uninstall path)."""
    return remove_split_key(
        config_dir, begin_marker=MODES_MANAGED_BEGIN, end_marker=MODES_MANAGED_END,
    )


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------


async def async_create_or_update_sensa_modes(
    hass: HomeAssistant,
    devices: list[dict[str, Any]],
    overrides: ModeOverrides,
) -> bool:
    """Rebuild the scene YAML + ensure configuration.yaml is registered.

    Returns True if configuration.yaml changed (HA restart required to
    pick up the `scene: !include` directive for the first time). After
    the initial registration, this always returns False and
    `async_reload_scenes` picks up YAML changes without a restart.
    """
    config_dir = hass.config.config_dir
    scenes_dict = _build_scenes_dict(hass, devices, overrides)

    def _write_all() -> bool:
        _write_scenes_yaml(config_dir, scenes_dict)
        return _ensure_scenes_registration(config_dir)

    changed = await hass.async_add_executor_job(_write_all)
    return changed


async def async_reload_scenes(hass: HomeAssistant) -> None:
    """Ask HA to re-read the scenes YAML without a full restart."""
    try:
        await hass.services.async_call(
            "scene", "reload", {}, blocking=True,
        )
        _LOGGER.debug("Nomad modes: scene.reload called")
    except Exception:
        _LOGGER.debug(
            "Nomad modes: scene.reload failed — scenes will refresh on "
            "the next HA restart",
            exc_info=True,
        )


async def async_remove_sensa_modes(hass: HomeAssistant) -> None:
    """Uninstall path: wipe the managed block so configuration.yaml
    doesn't try to include a scenes file we no longer generate."""
    config_dir = hass.config.config_dir

    def _strip() -> None:
        _remove_managed_scene_block(config_dir)
        path = Path(config_dir) / MODES_FILE
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.debug(
                "Nomad modes: failed to delete %s on uninstall",
                path, exc_info=True,
            )

    await hass.async_add_executor_job(_strip)
