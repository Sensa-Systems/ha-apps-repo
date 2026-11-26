"""Customer-created Light Groups.

A Light Group is a NOUN: a named set of light entities exposed as one
real `light.*` entity of our own (`light.py`). Because it is an entity and
not a saved snapshot, everything that already takes a light takes a group
for free -- Scenes, Routines, the Home layout, `sensa-light-card`.

That is deliberately the opposite of a Scene, which is a VERB: a stored
write of values with no identity and no undo. The distinction is the one
every platform surveyed draws (a Hue scene carries a required `group`; a
Matter scene-table row is keyed by `(GroupID, SceneID)`), and collapsing
the two is the failure mode they all avoid. So there is no separate "light
scene" here: a group goes INTO a scene, and the Scene editor captures the
brightness / colour on top.

Storage mirrors `modes.ModeOverrides` closely, including the two rules that
matter:

  * The store key lives OUTSIDE the config entry, so a remove + re-add of
    the integration (or a reinstall) keeps the customer's groups.
  * A slug is derived from the name ONCE and never moves on a rename. The
    slug is baked into `light.sensa_nomad_group_<slug>`, so a rename that
    re-slugged would orphan every scene, routine and layout referencing it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}.light_groups"

# Customer-created groups get a slug under this prefix so they can never
# collide with anything else and are recognisable in an entity_id.
GROUP_SLUG_PREFIX = "group_"
_SLUG_MAX = 40
_NAME_MAX = 40
DEFAULT_GROUP_ICON = "mdi:lightbulb-group"


def light_group_entity_id(slug: str) -> str:
    """HA entity_id of a group's light entity.

    Written once because the producer (`light.SensaLightGroup`) and every
    reader (delete's registry cleanup, the dashboard builder) drifting apart
    would leave a group that exists in the store but points at nothing.
    """
    return f"light.sensa_nomad_{slug}"


def light_group_slug(name: str, taken: Any) -> str:
    """Derive a stable, unique slug for a group from its name. Derived ONCE
    at creation and never changed by a rename."""
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    base = base[:_SLUG_MAX] or "lights"
    existing = set(taken)
    slug = f"{GROUP_SLUG_PREFIX}{base}"
    n = 2
    while slug in existing:
        slug = f"{GROUP_SLUG_PREFIX}{base}_{n}"
        n += 1
    return slug


def is_group_slug(slug: str) -> bool:
    return isinstance(slug, str) and slug.startswith(GROUP_SLUG_PREFIX)


def _clean_name(name: Any) -> str:
    return " ".join(str(name or "").split())[:_NAME_MAX]


# Domains a group member may live in. `switch.` is here for a plain light
# wired to a relay channel and declared as lighting on the Device Type
# dropdown: it is a light to the customer, it sits on the Lighting tab beside
# the Zigbee bulbs, and leaving it out meant the one kind of light nearly every
# van has could not join a group. The group drives it on/off and it simply
# ignores the brightness and colour the dimmable members take.
MEMBER_DOMAINS = ("light.", "switch.")


def _clean_members(members: Any) -> list[str]:
    """Coerce a customer-supplied member list: `light.*` / `switch.*`
    entity_ids only, de-duplicated, order preserved, never a group (no
    nesting)."""
    out: list[str] = []
    if not isinstance(members, list):
        return out
    for raw in members:
        if not isinstance(raw, str):
            continue
        entity_id = raw.strip()
        if not entity_id.startswith(MEMBER_DOMAINS):
            continue
        if entity_id.startswith("light.sensa_nomad_" + GROUP_SLUG_PREFIX):
            continue
        if entity_id not in out:
            out.append(entity_id)
    return out


class LightGroupStore:
    """Persistent catalog of customer-created light groups.

    Storage shape:
        {"groups": [{"slug", "name", "icon", "members": [entity_id, ...]}, ...]}

    List order IS display order, so a reorder is a permutation of this list
    rather than a parallel order field that could drift out of step with it.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, Any]](hass, _STORAGE_VERSION, _STORAGE_KEY)
        self._groups: list[dict[str, Any]] = []

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        if not isinstance(loaded, dict):
            return
        raw = loaded.get("groups")
        if not isinstance(raw, list):
            return
        groups: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or not is_group_slug(item.get("slug")):
                continue
            slug = item["slug"]
            groups.append({
                "slug": slug,
                "name": _clean_name(item.get("name")) or slug,
                "icon": str(item.get("icon") or DEFAULT_GROUP_ICON),
                "members": _clean_members(item.get("members")),
            })
        self._groups = groups

    async def async_save(self) -> None:
        await self._store.async_save({
            "groups": [dict(g, members=list(g["members"])) for g in self._groups],
        })

    # ---- reads -----------------------------------------------------------

    def all_slugs(self) -> list[str]:
        return [g["slug"] for g in self._groups]

    def get(self, slug: str) -> dict[str, Any] | None:
        for g in self._groups:
            if g["slug"] == slug:
                return dict(g, members=list(g["members"]))
        return None

    def groups_payload(self) -> list[dict[str, Any]]:
        """Every group in display order. Broadcast on
        `SIGNAL_LIGHT_GROUPS_CHANGED` and mirrored by each group entity."""
        return [dict(g, members=list(g["members"])) for g in self._groups]

    # ---- writes ----------------------------------------------------------

    async def async_create(
        self, name: str, icon: str | None = None, members: Any = None,
    ) -> dict[str, Any]:
        """Create a group. The slug is derived from the name once and never
        changes; the group lands at the END of the display order."""
        clean = _clean_name(name) or "New group"
        slug = light_group_slug(clean, self.all_slugs())
        group = {
            "slug": slug,
            "name": clean,
            "icon": (icon or "").strip() or DEFAULT_GROUP_ICON,
            "members": _clean_members(members),
        }
        self._groups.append(group)
        await self.async_save()
        return dict(group, members=list(group["members"]))

    async def async_delete(self, slug: str) -> bool:
        if self.get(slug) is None:
            return False
        self._groups = [g for g in self._groups if g["slug"] != slug]
        await self.async_save()
        return True

    async def async_rename(
        self, slug: str, name: str | None = None, icon: str | None = None,
    ) -> dict[str, Any] | None:
        """Change a group's display name and/or icon. Slugs never move, so
        this touches no entity_id. Empty name/icon = leave as is."""
        clean_name = _clean_name(name)
        clean_icon = (icon or "").strip()
        for g in self._groups:
            if g["slug"] != slug:
                continue
            if clean_name:
                g["name"] = clean_name
            if clean_icon:
                g["icon"] = clean_icon
            if clean_name or clean_icon:
                await self.async_save()
            return dict(g, members=list(g["members"]))
        return None

    async def async_set_members(self, slug: str, members: Any) -> list[str] | None:
        """Replace a group's membership. Returns the effective list, or None
        for an unknown slug. Skips the write when unchanged."""
        clean = _clean_members(members)
        for g in self._groups:
            if g["slug"] != slug:
                continue
            if clean != g["members"]:
                g["members"] = clean
                await self.async_save()
            return list(clean)
        return None

    async def async_set_order(self, slugs: Any) -> list[str]:
        """Persist the customer's display order. Unknown slugs are dropped
        and any group missing from `slugs` is appended in creation order, so
        a stale client can never hide a group."""
        known = {g["slug"]: g for g in self._groups}
        ordered: list[dict[str, Any]] = []
        if isinstance(slugs, list):
            for raw in slugs:
                g = known.get(raw) if isinstance(raw, str) else None
                if g is not None and g not in ordered:
                    ordered.append(g)
        ordered.extend(g for g in self._groups if g not in ordered)
        if ordered != self._groups:
            self._groups = ordered
            await self.async_save()
        return self.all_slugs()

    async def async_prune_members(self, known_entity_ids: Any) -> list[str]:
        """Drop member ids whose entity no longer exists (a device removed
        under the group's feet). Returns the slugs whose membership changed,
        so the caller can refresh just those entities.

        Called with the FULL set of known light entity_ids. An empty set is
        treated as "could not ask" and prunes nothing: that distinction is
        the difference between a customer deleting a light and the registry
        not being ready yet, and pruning on the latter would silently empty
        every group on a slow boot.
        """
        known = {e for e in known_entity_ids if isinstance(e, str)}
        if not known:
            return []
        changed: list[str] = []
        for g in self._groups:
            kept = [m for m in g["members"] if m in known]
            if kept != g["members"]:
                _LOGGER.debug(
                    "Light group %s: pruning %s",
                    g["slug"], [m for m in g["members"] if m not in known],
                )
                g["members"] = kept
                changed.append(g["slug"])
        if changed:
            await self.async_save()
        return changed
