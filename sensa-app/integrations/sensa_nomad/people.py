"""People: read and write Home Assistant's own `person` collection.

The customer names the humans who live in the van and binds each one to their
phone. Home Assistant already models exactly that (a person has a name, a
picture, and the `device_tracker`s that are their devices), so this module does
NOT invent a parallel record: it drives HA's own person collection, and a
person created here is indistinguishable from one created in HA's UI.

Why it exists at all: on an **appliance** the customer is not an HA admin, so
HA's person UI is unreachable and a locked-down box could otherwise never gain
a person. On BYO the customer could use HA directly, but the Settings tab is
the same code either way.

Two rules are load-bearing:

* **Drive HA's collection in-process, never hand-write `.storage`.** HA
  lazy-loads that store and caches it in memory, so a hand-written file is
  silently clobbered by the next write HA makes itself. This mirrors
  `admin.py` (which calls the auth provider's own methods) and
  `dashboard/config_yaml.py::async_set_sidebar_order` (which uses the frontend
  store API and whose docstring spells the trap out).

* **System logins are not people.** A Sensa appliance image ships five HA
  accounts (`const.APPLIANCE_*`): the customer's elevated `admin`, the three
  kiosk tablets, and `sensa_support`, which is SENSA'S OWN remote-support
  backdoor. HA creates a `person` alongside at least the onboarding account, so
  an unfiltered list would offer the support login as a family member and let a
  phone be bound to it. :func:`async_list_people` hides them and the write paths
  refuse them, so filtering is not merely cosmetic.

Everything imports `homeassistant.components.person` LAZILY, inside the
function that needs it. That is the house style for optional/late HA internals
(the auth provider, `frontend_storage`, `entity_registry` are all imported this
way) and it keeps the test suite free of another `conftest.py` stub.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .appliance import _hass_username, _is_appliance
from .const import (
    APPLIANCE_ADMIN_USERNAME,
    APPLIANCE_KIOSK_USERNAMES,
    DOMAIN,
    PEOPLE_DECLINED_TRACKERS_OPTION,
    PEOPLE_MEMBERS_OPTION,
    SENSA_SUPPORT_USERNAME,
)

_LOGGER = logging.getLogger(__name__)

# The HA accounts a Sensa image creates. None of them is a person: three are
# kiosk tablets, one is the customer's elevated login, and `sensa_support` is
# Sensa's remote-support backdoor. Built from const.py so there is ONE list of
# appliance usernames in the codebase, not a second copy that drifts.
SYSTEM_USERNAMES: frozenset[str] = frozenset(
    (*APPLIANCE_KIOSK_USERNAMES, APPLIANCE_ADMIN_USERNAME, SENSA_SUPPORT_USERNAME)
)

# HA's person component keys.
_PERSON_DOMAIN = "person"
_CONF_ID = "id"
_CONF_NAME = "name"
_CONF_USER_ID = "user_id"
_CONF_DEVICE_TRACKERS = "device_trackers"
_CONF_PICTURE = "picture"


class PeopleError(Exception):
    """A person operation could not be completed."""


# ---------------------------------------------------------------------------
# Access to HA's collections
# ---------------------------------------------------------------------------


def _collections(hass: HomeAssistant) -> tuple[Any | None, Any | None]:
    """`(yaml_collection, storage_collection)`, either possibly None.

    `hass.data["person"]` is a tuple whose SECOND slot is the storage-backed
    collection. It gained a third slot (the entity component) in later cores, so
    this indexes rather than unpacks: `[1]` is the storage collection under both
    the 2-tuple and 3-tuple shapes, and a future 4-tuple would not break it
    either. Returns `(None, None)` when the person component has not set up,
    which callers surface as "no people" rather than raising.
    """
    try:
        data = hass.data.get(_PERSON_DOMAIN)
    except Exception:  # noqa: BLE001 (never break a caller over hass.data)
        return None, None
    if not isinstance(data, (tuple, list)) or len(data) < 2:
        return None, None
    return data[0], data[1]


def _storage(hass: HomeAssistant) -> Any:
    """The storage-backed person collection, or raise.

    Only storage-backed persons are editable. A YAML-configured person is
    surfaced read-only by :func:`async_list_people` and refused here, exactly as
    HA's own UI does.
    """
    _yaml, storage = _collections(hass)
    if storage is None:
        raise PeopleError(
            "Home Assistant's person component is not available on this system."
        )
    return storage


def _items(collection: Any) -> list[dict[str, Any]]:
    try:
        return [dict(item) for item in (collection.async_items() or [])]
    except Exception:
        _LOGGER.debug("people: async_items() failed", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


async def _system_user_ids(hass: HomeAssistant) -> set[str]:
    """User ids of the accounts a Sensa image creates.

    Resolved by USERNAME, the same discriminator `appliance.user_is_sensa_staff`
    uses, because HA has no role or group concept to hang this on. Keying on
    `is_admin` would be wrong in both directions: on BYO the customer IS the HA
    admin, and on a Container appliance the `admin` login deliberately is not.

    Fails OPEN (an empty set, so nothing is hidden) rather than closed: a failed
    auth lookup must not blank the customer's People tab.

    On an APPLIANCE the rule is stronger: **every** HA account is one the master
    image created. The customer never creates HA users at runtime (the old
    runtime provisioning in `tablet_users.py` is gone), and a person added on
    this tab is never linked to a login, so a person carrying ANY `user_id` on
    an appliance is a Sensa account rather than a family member.

    That backstop is load-bearing, because the username list alone is not
    enough. The onboarding account's username is a BUILD-TIME parameter
    (`build-appliance.py::onboarding_create_user`) and its person is named from
    the display name ("Sensa Admin"), so a box onboarded under any username the
    list does not happen to contain would show Sensa's own account as a family
    member. That is exactly what shipped.
    """
    try:
        users = await hass.auth.async_get_users()
    except Exception:
        _LOGGER.debug("people: user lookup failed; suppressing nothing", exc_info=True)
        return set()

    try:
        appliance = _is_appliance(hass)
    except Exception:  # noqa: BLE001 (fall back to the named-account rule)
        appliance = False

    found: set[str] = set()
    for user in users or []:
        user_id = getattr(user, "id", None)
        if not user_id:
            continue
        # BYO: only the accounts we can name. The customer's own HA users are
        # legitimately people there, so a blanket rule would hide the household.
        if appliance or _hass_username(user) in SYSTEM_USERNAMES:
            found.add(str(user_id))
    return found


# ---------------------------------------------------------------------------
# The MySensa household
# ---------------------------------------------------------------------------


async def async_fetch_members(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The customer's MySensa household, or [].

    Served from the App's local cache, which the licence phone-home fills. The
    licence already identifies the customer to MySensa, so the box needs no
    second credential and the customer is never asked to sign in.

    Reading a cache means the People tab opens instantly and still works with
    the van offline: whatever the last phone-home brought down is what the
    Download button applies.
    """
    import socket

    import aiohttp

    from .backend_url import resolve_backend_url

    try:
        base = resolve_backend_url(hass)
    except Exception:  # noqa: BLE001
        return []
    url = f"{base.rstrip('/')}/api/v1/people/members"
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session, session.get(url) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:
        _LOGGER.debug("people: household lookup failed", exc_info=True)
        return []
    members = (data or {}).get("members")
    return members if isinstance(members, list) else []


# ---------------------------------------------------------------------------
# The MySensa member join
# ---------------------------------------------------------------------------


def member_map(hass: HomeAssistant) -> dict[str, str]:
    """`{mysensa_member_id: person_id}` from the config entry options.

    Exists so a re-import UPDATES the person it created last time instead of
    adding a duplicate. Scans entries the way `device_tracker` does, because a
    caller here holds a `hass` and not the entry.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        candidate = (entry.options or {}).get(PEOPLE_MEMBERS_OPTION) or {}
        if isinstance(candidate, dict) and candidate:
            return {str(k): str(v) for k, v in candidate.items() if k and v}
    return {}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _entity_id_for(hass: HomeAssistant, person_id: str) -> str:
    """The person's entity_id, best effort.

    Person entities carry the person's collection id as their `unique_id`, so
    the registry answers this directly. Best effort on purpose: the People tab
    does not need an entity_id to manage a person, so a miss returns "" rather
    than dropping the row.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        return (
            er.async_get(hass).async_get_entity_id(
                _PERSON_DOMAIN, _PERSON_DOMAIN, person_id
            )
            or ""
        )
    except Exception:  # noqa: BLE001 (registry lookups are advisory here)
        return ""


def _row(
    hass: HomeAssistant,
    item: dict[str, Any],
    *,
    members: dict[str, str],
    readonly: bool,
) -> dict[str, Any]:
    person_id = str(item.get(_CONF_ID) or "")
    trackers = item.get(_CONF_DEVICE_TRACKERS) or []
    return {
        "person_id": person_id,
        "entity_id": _entity_id_for(hass, person_id),
        "name": str(item.get(_CONF_NAME) or ""),
        "picture": str(item.get(_CONF_PICTURE) or ""),
        "device_trackers": [str(t) for t in trackers if t],
        # Which MySensa member this person came from, if any. Blank for someone
        # the customer typed in by hand, which is the offline-only case.
        "member_id": next(
            (m for m, p in members.items() if p == person_id),
            "",
        ),
        # A YAML-configured person cannot be edited (HA owns the file). Listed
        # anyway so the customer is not left wondering where they went.
        "readonly": readonly,
    }


async def async_list_people(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every person the customer may see, system logins removed.

    Sorted by name so the tab does not reshuffle between polls.
    """
    yaml_collection, storage = _collections(hass)
    if storage is None and yaml_collection is None:
        return []

    system_ids = await _system_user_ids(hass)
    members = member_map(hass)

    rows: list[dict[str, Any]] = []
    for item in _items(storage) if storage is not None else []:
        if str(item.get(_CONF_USER_ID) or "") in system_ids:
            continue
        rows.append(_row(hass, item, members=members, readonly=False))
    for item in _items(yaml_collection) if yaml_collection is not None else []:
        if str(item.get(_CONF_USER_ID) or "") in system_ids:
            continue
        rows.append(_row(hass, item, members=members, readonly=True))

    rows.sort(key=lambda r: (r["name"].lower(), r["person_id"]))
    return rows


async def async_person_is_manageable(hass: HomeAssistant, person_id: str) -> bool:
    """True if `person_id` is a storage-backed, non-system person.

    The write paths call this FIRST. Hiding a system login from the list is a UI
    courtesy; this is the actual gate, so a hand-crafted service call cannot
    rename Sensa's support account or bind a phone to it.
    """
    if not person_id:
        return False
    _yaml, storage = _collections(hass)
    if storage is None:
        return False
    item = next(
        (i for i in _items(storage) if str(i.get(_CONF_ID) or "") == person_id),
        None,
    )
    if item is None:
        return False
    return str(item.get(_CONF_USER_ID) or "") not in await _system_user_ids(hass)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def current_driver_person_id(hass: HomeAssistant) -> str:
    """The person last picked on the Cab people bar, or "".

    Mirrored into hass.data from entry.options at setup, so this is a cheap
    synchronous read on a path that runs on every position resolve.
    """
    from .const import DRIVER_PERSON_KEY

    # getattr: this is read from an entity attribute getter, which must never
    # raise, and a partial hass stub has no `data`.
    store = getattr(hass, "data", None) or {}
    return str((store.get(DOMAIN) or {}).get(DRIVER_PERSON_KEY, "") or "")


def _clean_trackers(device_trackers: Any) -> list[str]:
    if not isinstance(device_trackers, (list, tuple)):
        return []
    seen: list[str] = []
    for raw in device_trackers:
        eid = str(raw or "").strip()
        if eid and eid.startswith("device_tracker.") and eid not in seen:
            seen.append(eid)
    return seen


async def async_upsert_person(
    hass: HomeAssistant,
    *,
    person_id: str | None = None,
    name: str,
    picture: str | None = None,
    device_trackers: Any = None,
) -> str:
    """Create a person, or update the one named by `person_id`. Returns its id.

    `picture` is only written when supplied: `""` clears it, `None` leaves it
    alone.

    **`device_trackers` must ALWAYS be sent on an update, even when the caller
    did not supply any.** HA's person UPDATE_SCHEMA declares
    `vol.Optional(CONF_DEVICE_TRACKERS, default=list)`, and voluptuous INJECTS
    that default when the field is absent; `_update_data` then merges
    `{**item, **update_data}`, so an omitted field does not mean "leave alone",
    it means "set to []". Renaming a person therefore silently unbound them from
    their device, and the seeder -- correctly seeing an unowned GPS device --
    created a second person for it. That is the "it adds a new row" bug.

    So an update with no explicit trackers carries the person's CURRENT ones
    forward.
    """
    storage = _storage(hass)

    clean_name = str(name or "").strip()
    if not clean_name:
        raise PeopleError("A person needs a name.")

    payload: dict[str, Any] = {_CONF_NAME: clean_name}
    if picture is not None:
        payload[_CONF_PICTURE] = str(picture).strip() or None
    if device_trackers is not None:
        payload[_CONF_DEVICE_TRACKERS] = _clean_trackers(device_trackers)

    if person_id:
        if not await async_person_is_manageable(hass, person_id):
            raise PeopleError("That person cannot be edited.")
        if _CONF_DEVICE_TRACKERS not in payload:
            # Carry the existing links forward; see the docstring. Omitting the
            # field would clear them.
            existing = next(
                (
                    i
                    for i in _items(_collections(hass)[1])
                    if str(i.get(_CONF_ID) or "") == person_id
                ),
                {},
            )
            payload[_CONF_DEVICE_TRACKERS] = _clean_trackers(
                existing.get(_CONF_DEVICE_TRACKERS) or []
            )
        try:
            await storage.async_update_item(person_id, payload)
        except Exception as exc:
            raise PeopleError(f"Could not update that person: {exc}") from exc
        return person_id

    # A new person is never linked to an HA login. Creating one would mean
    # creating an account, which this product deliberately never does at
    # runtime (see appliance.py: the image's build script owns accounts).
    payload.setdefault(_CONF_DEVICE_TRACKERS, [])
    try:
        created = await storage.async_create_item(payload)
    except Exception as exc:
        raise PeopleError(f"Could not add that person: {exc}") from exc
    return str((created or {}).get(_CONF_ID) or "")


async def async_delete_person(hass: HomeAssistant, person_id: str) -> None:
    """Remove a person. Refuses anything the customer may not manage."""
    storage = _storage(hass)
    if not await async_person_is_manageable(hass, person_id):
        raise PeopleError("That person cannot be removed.")
    try:
        await storage.async_delete_item(person_id)
    except Exception as exc:
        raise PeopleError(f"Could not remove that person: {exc}") from exc


# ---------------------------------------------------------------------------
# Seeding from the phones already on the box
# ---------------------------------------------------------------------------


def _person_trackers(hass: HomeAssistant) -> list[tuple[str, str]]:
    """The GPS devices that belong to a PERSON, not to the van.

    Only HA Companion trackers (`platform == "mobile_app"`) qualify. The
    Companion app is installed on a human's phone by definition, so it is the
    one signal that reliably says "this device belongs to somebody".

    Everything else is the VAN's kit: our own `sensa_nomad` vehicle tracker, a
    router's or telematics box's GPS (`mqtt`), the simulator's fake GPS. Those
    used to get a person each, from when a person's phone WAS the GPS, which
    produced nonsense rows like a person called "Van Simulator GPS (SIM)". The
    van is not a member of the household.

    A phone running Traccar or OwnTracks rather than the Companion app is not
    auto-detected, which is deliberate: it is indistinguishable from a telematics
    box at this level, and inventing a person for the van is worse than the
    customer assigning a real one by hand on the People tab. Both remain
    selectable as a GPS SOURCE either way -- this only governs who gets a person.
    """
    from homeassistant.helpers import entity_registry as er

    from .select import _selectable_gps_trackers

    ent_reg = er.async_get(hass)
    out: list[tuple[str, str]] = []
    for eid, label in _selectable_gps_trackers(hass):
        ent = ent_reg.async_get(eid)
        if ent is not None and ent.platform == "mobile_app":
            out.append((eid, label))
    return out


async def async_seed_from_trackers(hass: HomeAssistant, entry) -> int:
    """Guarantee a person for every PHONE on the van. Returns how many were
    created.

    Phones only, never the van's own GPS -- see :func:`_person_trackers`.

    A phone whose person the customer deleted is remembered in
    `PEOPLE_DECLINED_TRACKERS_OPTION` and skipped, so a delete sticks.

    A device already bound to somebody is left alone, so this can never
    duplicate a person the customer has set up or renamed: the binding is what
    it checks, never the name.

    Built on the same `_selectable_gps_trackers` the Cab location picker and the
    per-person phone dropdown use, narrowed to Companion phones, so all three
    agree on what counts as a device and only this one decides who gets a person.
    """
    try:
        trackers = _person_trackers(hass)
    except Exception:
        _LOGGER.debug("people: tracker enumeration failed", exc_info=True)
        return 0
    if not trackers:
        return 0

    assigned = {
        t
        for person in await async_list_people(hass)
        for t in (person.get("device_trackers") or [])
    }
    assigned.update((entry.options or {}).get(PEOPLE_DECLINED_TRACKERS_OPTION) or [])

    created = 0
    for entity_id, label in trackers:
        if entity_id in assigned:
            continue
        try:
            await async_upsert_person(hass, name=label, device_trackers=[entity_id])
        except PeopleError as exc:
            _LOGGER.debug("people: could not add a person for %s: %s", entity_id, exc)
            continue
        created += 1
    return created


async def async_prune_dead_trackers(hass: HomeAssistant) -> int:
    """Drop device links pointing at entities that no longer exist.

    A deleted device otherwise leaves the person bound to a dead entity_id
    while the tab's dropdown, finding no matching option, quietly displays
    "Not set". The record and the screen then disagree, and the customer has no
    way to see it, let alone fix it.

    **Keyed on the ENTITY REGISTRY, never on `_selectable_gps_trackers`.** That
    list requires live coordinates for anything that is not a Companion phone,
    so a van GPS that is merely asleep or offline drops out of it. Pruning from
    that list would strip the binding off every temporarily-offline GPS, and
    `async_seed_from_trackers` would then create a SECOND person for it the
    moment it woke up. Registry membership is the only question that means
    "this device is gone".

    Fails SAFE: an unreadable or empty registry prunes nothing. Removing every
    customer's device links because a lookup hiccuped is far worse than leaving
    a stale one for another cycle.

    Returns how many people were changed.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        known = set(er.async_get(hass).entities.keys())
    except Exception:
        _LOGGER.debug("people: registry read failed; pruning nothing", exc_info=True)
        return 0
    if not known:
        return 0

    changed = 0
    for person in await async_list_people(hass):
        if person.get("readonly"):
            continue
        current = list(person.get("device_trackers") or [])
        live = [t for t in current if t in known]
        if len(live) == len(current):
            continue
        try:
            await async_upsert_person(
                hass,
                person_id=person["person_id"],
                # Unchanged; `async_upsert_person` requires a name and must not
                # be allowed to rewrite the customer's.
                name=person["name"],
                device_trackers=live,
            )
        except PeopleError as exc:
            _LOGGER.debug(
                "people: could not prune %s: %s", person.get("person_id"), exc
            )
            continue
        changed += 1
    return changed


async def async_sync_imported_people(hass: HomeAssistant) -> int:
    """Re-apply the cached MySensa name + photo to the people who came from it.

    A person the customer imported is a MIRROR of that MySensa member, so
    changing their photo or name in MySensa should reach the van on its own.
    Without this the import was a one-shot copy and the box drifted silently:
    the household said one thing, the People tab another, and nothing on screen
    explained why.

    Keyed on `PEOPLE_MEMBERS_OPTION` (member -> person), so it touches ONLY
    people that came from MySensa. Someone the customer typed in by hand has no
    entry, is theirs to manage, and is never overwritten. That is the whole
    distinction the mapping exists to record.

    Device links are NOT touched: the phone was assigned on this box and MySensa
    knows nothing about it.

    Returns how many people changed. Idempotent, so it is safe to run on every
    phone-home: a person whose name and photo already match is skipped, and no
    write means no dashboard churn.
    """
    mapping = member_map(hass)
    if not mapping:
        return 0
    members = {
        str(m.get("id") or ""): m for m in await async_fetch_members(hass)
    }
    if not members:
        # Could not ask, or nothing cached. NOT the same as "the household is
        # empty", so nothing is cleared on the strength of it.
        return 0

    by_id = {p["person_id"]: p for p in await async_list_people(hass)}
    changed = 0
    for member_id, person_id in mapping.items():
        member = members.get(member_id)
        person = by_id.get(person_id)
        if member is None or person is None or person.get("readonly"):
            continue

        name = str(member.get("display_name") or "").strip()
        if not name:
            continue
        # ABSENT means "no photo information", EMPTY means "they removed it".
        raw_picture = member.get("avatar_local")
        picture = None if raw_picture is None else str(raw_picture)

        if name == person.get("name") and (
            picture is None or picture == (person.get("picture") or "")
        ):
            continue  # already in step; do not write and churn the dashboard

        try:
            await async_upsert_person(
                hass, person_id=person_id, name=name, picture=picture,
            )
        except PeopleError as exc:
            _LOGGER.debug("people: could not sync %s: %s", person_id, exc)
            continue
        changed += 1
    return changed


async def async_remove_van_gps_people(hass: HomeAssistant) -> int:
    """Remove people the old seeder created for the VAN's own GPS.

    Boxes seeded before phones-only got a person per GPS device, so a router's
    or a simulator's GPS became a "person". They were never household members
    and the van is not one, so they go.

    Conservative on purpose, because a person record may have been made the
    customer's since. One is removed ONLY when it still looks exactly as the
    seeder left it:

      * every device it holds belongs to the VAN (no Companion phone), and
      * it carries no MySensa member, and
      * its name is still the tracker's own label.

    Rename it, assign it a phone, or link it to a member and it is yours; this
    leaves it alone. Removing something the customer has adopted would be far
    worse than leaving one stale row for them to delete by hand.

    Returns how many were removed.
    """
    try:
        from .select import _selectable_gps_trackers

        labels = {eid: label for eid, label in _selectable_gps_trackers(hass)}
        person_eids = {eid for eid, _ in _person_trackers(hass)}
    except Exception:
        _LOGGER.debug("people: tracker enumeration failed", exc_info=True)
        return 0
    if not labels:
        return 0

    removed = 0
    for person in await async_list_people(hass):
        if person.get("readonly") or person.get("member_id"):
            continue
        trackers = [t for t in (person.get("device_trackers") or []) if t]
        # No devices at all: somebody the customer added. Not ours to remove.
        if not trackers or any(t in person_eids for t in trackers):
            continue
        # Still named after the device the seeder named it after?
        if person.get("name") not in {labels.get(t) for t in trackers}:
            continue
        try:
            await async_delete_person(hass, person["person_id"])
        except PeopleError as exc:
            _LOGGER.debug("people: could not remove %s: %s", person["name"], exc)
            continue
        _LOGGER.info(
            "People: removed %r, which was a person for the van's own GPS",
            person.get("name"),
        )
        removed += 1
    return removed
