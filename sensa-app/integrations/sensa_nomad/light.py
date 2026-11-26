"""Light platform for Sensa Nomad — customer-created Light Groups.

One `light.sensa_nomad_group_<slug>` per group in `light_groups.LightGroupStore`.
A group is a real light entity, not a saved snapshot, so Scenes, Routines, the
Home layout and `sensa-light-card` all take it with no special-casing: the card
requires only `entity` and every read is a generic light attribute.

Aggregation deliberately matches Home Assistant's own light-group semantics, so
a customer who has met an HA light group anywhere else is not surprised here:
on if ANY member is on, brightness and colour temperature are the mean over the
members that are ON (an off member has no brightness to average), hue is a
circular mean, and the supported colour modes are the union.

The colour-temperature RANGE is the exception, and is narrowed rather than
widened: the group's minimum is the highest of the members' minimums and its
maximum the lowest of their maximums. A union there would let the slider ask a
member for a Kelvin value it cannot produce, and the member would silently
clamp -- the group would then read back a temperature nothing is actually at.

A member is usually a `light.*` entity, but a plain light wired to a relay
channel and declared as lighting on the Device Type dropdown (a `switch.*`
entity) is a member too: it is a light to the customer and sits on the Lighting
tab beside the bulbs. It takes on/off and ignores brightness and colour, and it
contributes neither to the group's averages nor to its colour modes, so a group
of one strip and one switched light still offers the strip's full control.

Fan-out is one power-up ROUTINE per member, run concurrently, not a single
`light.turn_on` carrying the whole list: a member may sit behind a supply relay
and need its Power-On Delay and re-sends before it answers (see
`light_power_up`). Members are addressed individually on the wire anyway (this
is not a native Zigbee group), so a large group can show mild stagger.

The group is on only when EVERY reporting member is on, which is where this
departs from HA's own light groups: turning one member on must not light the
group's card.

Platform wiring follows `binary_sensor.py`: this module parks its
`async_add_entities` callback on the runtime dict and drains anything queued
before it registered, because the group store loads in `async_setup_entry`
AFTER the platforms have been forwarded.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SIGNAL_LIGHT_GROUPS_CHANGED
from .light_groups import DEFAULT_GROUP_ICON, light_group_entity_id

_LOGGER = logging.getLogger(__name__)


def _candidate_entities() -> list[dict[str, str]]:
    """Entities eligible for group membership (Zigbee lights and relay
    channels declared as lighting) as `{entity, name}`, or [] if the dashboard
    pipeline has not resolved any yet. Deferred import: `dashboard` pulls the
    catalog, and this module is imported at HA start.

    The NAME travels with the entity because only the integration knows it. A
    Z2M device the customer never renamed in Z2M reports its IEEE address as
    its HA friendly_name, so a picker reading the state machine offered
    `0x00158d0053494d1a` next to a card that said something readable."""
    try:
        from .dashboard import light_group_candidate_entities

        return light_group_candidate_entities()
    except Exception:
        _LOGGER.debug("group candidate lookup failed", exc_info=True)
        return []

# States that mean "this member has nothing to contribute", as opposed to a
# member that is genuinely off. Kept apart because an unavailable member must
# not drag the group's averages, and must not by itself make the group
# unavailable either -- one dead bulb does not make a room's lights unusable.
_NOT_REPORTING = (STATE_UNAVAILABLE, STATE_UNKNOWN, None, "")

# Colour modes that carry no colour information of their own. When members
# disagree about which mode they are in, these lose: a group of one plain
# dimmer and one colour bulb showing a colour should report the colour.
_DEPRIORITISED_MODES = (ColorMode.UNKNOWN, ColorMode.ONOFF, ColorMode.BRIGHTNESS)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the light platform.

    Adds nothing itself: every light entity we own is a customer-created
    group, and the store that holds them loads later in `async_setup_entry`.
    Park the callback and flush anything already queued.
    """
    runtime_data = hass.data[DOMAIN][entry.entry_id]
    runtime_data["vendor_add_lights"] = async_add_entities

    pending = runtime_data.pop("vendor_pending_lights", None)
    if pending:
        async_add_entities(pending)


def _mean_int(values: list[float]) -> int | None:
    if not values:
        return None
    return round(sum(values) / len(values))


def _mean_hue(hues: list[float]) -> float:
    """Circular mean of a list of hues in degrees.

    Hue is an angle, so the arithmetic mean of 350 and 10 would give 180
    (cyan) instead of 0 (red). Evenly opposed hues still average to an
    arbitrary answer; that is inherent to averaging a circle and is the same
    caveat HA documents for its own light groups.
    """
    x = sum(math.cos(math.radians(h)) for h in hues)
    y = sum(math.sin(math.radians(h)) for h in hues)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return 0.0
    # Round BEFORE the final wrap. Averaging 350 and 10 lands a hair below
    # zero, which `% 360` turns into 359.999999..., and rounding that gives
    # 360 -- a hue outside HA's 0-360 range for a pair of reds.
    return round(math.degrees(math.atan2(y, x)), 3) % 360


class SensaLightGroup(RestoreEntity, LightEntity):
    """One customer-created light group, as a single addressable light."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, group: dict[str, Any]) -> None:
        self._entry = entry
        self._slug = group["slug"]
        self._members: list[str] = list(group.get("members") or [])
        self._attr_unique_id = f"{entry.entry_id}_light_group_{self._slug}"
        self._attr_name = group.get("name") or self._slug
        self._attr_icon = group.get("icon") or DEFAULT_GROUP_ICON
        self.entity_id = light_group_entity_id(self._slug)
        self._unsub_members: Any = None
        # The colour this group was last told to show, while it is driving its
        # members. Restored across a restart with the switch position below.
        self._commanded_hs: tuple[float, float] | None = None
        # Whether the customer switched the GROUP on. The group's state follows
        # this, not its members -- see `is_on`.
        self._switched_on = False

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Sensa Nomad",
            "manufacturer": "Sensa",
            "model": "Nomad Integration",
        }

    # ---- member state ----------------------------------------------------

    def _member_states(self) -> list[Any]:
        """Every member's state object that is actually reporting."""
        if self.hass is None:
            return []
        out = []
        for entity_id in self._members:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in _NOT_REPORTING:
                out.append(state)
        return out

    def _on_states(self) -> list[Any]:
        return [s for s in self._member_states() if s.state == STATE_ON]

    def _attr_values(self, attr: str) -> list[Any]:
        """The given attribute across the members that are ON and report it.

        Only on-members: an off light's last brightness is not part of what
        the group is currently doing, and including it would make the dial
        read a value no light is showing.
        """
        out = []
        for state in self._on_states():
            value = (state.attributes or {}).get(attr)
            if value is not None:
                out.append(value)
        return out

    # ---- aggregated state ------------------------------------------------

    @property
    def available(self) -> bool:
        """Available while at least one member is reporting.

        An empty group is unavailable: it has nothing to control, and
        reporting it as a working light that does nothing is worse than
        showing it as not ready.
        """
        return bool(self._member_states())

    @property
    def is_on(self) -> bool:
        """On only when the customer switched the GROUP on. Nothing else.

        Not derived from the members at all, which is where two earlier rules
        both failed (Richard, 2026-09-17). Any-member-on lit the group when one
        light was switched on by itself ("the group should not come on just cos a
        member has been turned on"); every-member-on then lit it when the
        customer happened to switch them all on individually ("very wrong").
        Neither is the customer switching the group on, and the rule he settled
        on is exactly that: "group is only on when you turn group on".

        Deliberately no member veto either, not even "every light is now off".
        A group is a thing the customer switches, and a switch that reports
        someone else's doing is the whole class of bug this replaced.
        """
        return bool(self._members) and self._switched_on

    @property
    def brightness(self) -> int | None:
        return _mean_int([
            v for v in self._attr_values("brightness") if isinstance(v, (int, float))
        ])

    @property
    def color_temp_kelvin(self) -> int | None:
        return _mean_int([
            v for v in self._attr_values("color_temp_kelvin")
            if isinstance(v, (int, float))
        ])

    @property
    def min_color_temp_kelvin(self) -> int:
        """The warmest temperature EVERY colour-temp member can reach.

        Narrowed, not unioned -- see the module docstring.
        """
        values = [
            s.attributes.get("min_color_temp_kelvin")
            for s in self._member_states()
        ]
        values = [v for v in values if isinstance(v, (int, float))]
        if not values:
            return super().min_color_temp_kelvin
        return int(max(values))

    @property
    def max_color_temp_kelvin(self) -> int:
        """The coolest temperature EVERY colour-temp member can reach."""
        values = [
            s.attributes.get("max_color_temp_kelvin")
            for s in self._member_states()
        ]
        values = [v for v in values if isinstance(v, (int, float))]
        if not values:
            return super().max_color_temp_kelvin
        return int(min(values))

    def _member_hs(self) -> tuple[float, float] | None:
        """The colour the LIT members are showing, or None.

        Split out of `hs_color` because the two answer different questions.
        HA strips every colour attribute from a light that reports OFF
        (`light/__init__.py`: `color_mode = self.color_mode if _is_on else None`,
        and the colour attributes are only filled `if color_mode`), and this
        group is off unless EVERY member is on -- so a group with one member
        off published no colour at all, and the card's palette blanked about
        two seconds after a pick (Richard, 2026-09-17). This is published as an
        attribute as well, so the card can show what the strips are set to even
        while the group itself reads off.
        """
        pairs = [
            v for v in self._attr_values("hs_color")
            if isinstance(v, (list, tuple)) and len(v) == 2
        ]
        if not pairs:
            return None
        return (
            _mean_hue([float(p[0]) for p in pairs]),
            round(sum(float(p[1]) for p in pairs) / len(pairs), 3),
        )

    @property
    def hs_color(self) -> tuple[float, float] | None:
        return self._member_hs()

    @property
    def supported_color_modes(self) -> set[str]:
        """Union of the members' modes: anything any member can do, the
        group offers, and members that cannot do it ignore that part of the
        call. An empty union means on/off only.

        A mode a member is REPORTING counts as supported even if that member
        never declared it. HA raises for a lit light whose `color_mode` is
        outside this set (`__validate_color_mode`), and the member is the
        authority on what it is doing -- so trusting the declaration alone
        could make the group's own honest answer an error.
        """
        modes: set[str] = set()
        for state in self._member_states():
            attrs = state.attributes or {}
            member_modes = attrs.get("supported_color_modes")
            if isinstance(member_modes, (list, tuple, set)):
                modes.update(str(m) for m in member_modes)
            in_use = attrs.get("color_mode")
            if in_use and str(in_use) != ColorMode.UNKNOWN:
                modes.add(str(in_use))
        return modes or {ColorMode.ONOFF}

    @property
    def color_mode(self) -> str | None:
        """The most common mode among the on-members, with the modes that
        carry no colour deprioritised.

        The answer must always be one of `supported_color_modes`: HA RAISES
        `HomeAssistantError` for a lit light reporting a mode outside that set
        (`light/__init__.py::__validate_color_mode`). Returning a bare `onoff`
        when the members had not yet reported a mode was therefore a hard error
        on a colour group, not a harmless default.
        """
        if not self.is_on:
            return None
        supported = self.supported_color_modes
        modes = [str(m) for m in self._attr_values("color_mode")]
        if modes:
            informative = [m for m in modes if m not in _DEPRIORITISED_MODES]
            pool = informative or modes
            return max(set(pool), key=pool.count)
        # Nothing usable reported yet. Name a mode the group actually offers,
        # preferring one that can carry the colour we may be holding.
        for candidate in (ColorMode.HS, ColorMode.COLOR_TEMP, ColorMode.BRIGHTNESS):
            if candidate in supported:
                return candidate
        return next(iter(supported), ColorMode.ONOFF)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "group_slug": self._slug,
            # The Light Groups card reads membership straight off the entity,
            # so there is no second store or sensor for the frontend to get
            # out of step with.
            "member_entities": list(self._members),
            # ...and what the customer may ADD: registered Zigbee lighting
            # devices plus the relay channels declared as lighting, not every
            # `light.*` entity in HA. The heater's RGB status LED is a light
            # entity on a HEATER, and it was being offered as a group member
            # (2026-09-17). Resolved here, from the same tests the Lighting
            # tab uses, so the picker cannot drift.
            "candidate_entities": _candidate_entities(),
            # What the LIT members are showing. Published because HA strips
            # `hs_color` from a light reporting off, and this group is off
            # unless every member is on -- the card reads this so a group with
            # one member off still shows the colour its strips are set to.
            "member_hs_color": list(self._member_hs() or ()) or None,
            # The colour this group was last told to show, live only while the
            # group is driving its members. A member's own card reads it so it
            # does not claim orange while the strip is yellow.
            "commanded_hs_color": list(self._commanded_hs or ()) or None,
        }

    # ---- commands --------------------------------------------------------

    async def _restore_commanded(self) -> None:
        """Recover the group's colour across a restart.

        The group's STATE is derived from its members and needs no restoring,
        but the colour the customer set the GROUP to is the group's own -- and
        without it the first power press after a reboot would come back as a
        patchwork of whatever each member happened to hold.
        """
        last = await self.async_get_last_state()
        if last is not None:
            # The switch position is the group's own, so it has to come back
            # with the entity; deriving it from the members on boot is exactly
            # the mistake this rule exists to avoid.
            self._switched_on = last.state == STATE_ON
        hs = (last.attributes or {}).get("commanded_hs_color") if last else None
        if isinstance(hs, (list, tuple)) and len(hs) == 2:
            try:
                self._commanded_hs = (float(hs[0]), float(hs[1]))
            except (TypeError, ValueError):
                self._commanded_hs = None

    def _remember_commanded(self, kwargs: dict[str, Any]) -> None:
        """Record the colour this GROUP has been set to.

        KEPT across an off, and re-applied on the next switch-on, so a group
        comes back the way the customer left it (Richard, 2026-09-17: "when
        turn group back on it will be how it was"). That is what makes the
        customer-facing rule simple and true: change one light while its group
        is on and it does as you ask, and cycling the group puts the group's
        arrangement back.

        Published as `commanded_hs_color` so a MEMBER's own card can show the
        group's colour while the group is driving it, rather than claiming
        orange beside a yellow strip.
        """
        hs = kwargs.get("hs_color")
        if isinstance(hs, (list, tuple)) and len(hs) == 2:
            self._commanded_hs = (float(hs[0]), float(hs[1]))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power up every member, each by its own routine.

        NOT one `light.turn_on` carrying the whole member list any more. That
        is a single bare command per member, and an addressable controller is
        exactly the device it does not work for: it may be behind a supply
        relay, it needs its Power-On Delay to boot, and its first command is
        routinely lost while it rejoins Zigbee. A group containing one bulb and
        one pixel controller lit the bulb and left the controller dark
        (2026-09-17, proven from an MQTT capture).

        `light_power_up.async_power_up` is the ONE implementation of that
        sequence, so a member driven from its own card and the same member
        driven from a group behave identically. Members run concurrently: a
        controller inside its 15s retry window must not hold up the bulb beside
        it.

        Whatever HA validated into `kwargs` is passed straight through, rather
        than re-listed here: a per-attribute allowlist would silently drop each
        new light attribute HA adds.
        """
        self._remember_commanded(kwargs)
        self._switched_on = True
        self._publish()
        if not self._members:
            return
        from .light_power_up import async_power_up

        # A plain power press carries no colour, and the group must come back
        # the way it was left -- so its remembered colour rides along. Without
        # this, each member would resume ITS own colour and a group that had
        # been set yellow would come back as a patchwork.
        attrs = dict(kwargs)
        if "hs_color" not in attrs and self._commanded_hs is not None:
            attrs["hs_color"] = list(self._commanded_hs)

        await asyncio.gather(*(
            async_power_up(self.hass, entity_id, **attrs)
            for entity_id in self._members
        ), return_exceptions=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn every member off, cutting each one's supply relay unless that
        member is set to Leave Relay On. Same reasoning as turn_on: the group
        must do what each member's own card would do."""
        # The remembered colour SURVIVES an off: the group comes back the way
        # it was left, which is the whole promise made to the customer.
        self._switched_on = False
        self._publish()
        if not self._members:
            return
        from .light_power_up import async_power_down

        await asyncio.gather(*(
            async_power_down(self.hass, entity_id)
            for entity_id in self._members
        ), return_exceptions=True)

    # ---- lifecycle -------------------------------------------------------

    @callback
    def _publish(self) -> None:
        """Write the group's own state, now.

        A group is not polled, so nothing writes its state on its behalf after
        a service call: it has to do it itself. Its state used to be written
        only by the member state-change callback, which meant a group whose
        members were ALREADY on could not be switched on at all (Richard,
        2026-09-17): no member changed, so nothing wrote, so the group stayed
        off on screen and the customer could not turn it on.

        Called before the members are driven, not after, so the switch answers
        the press immediately rather than after an addressable controller's
        Power-On Delay and retry window.
        """
        if self.hass is None or self.entity_id is None:
            return
        self.async_write_ha_state()

    @callback
    def _resubscribe(self) -> None:
        """Watch exactly the current members. Called on add and after every
        membership change, so a light added to the group starts driving the
        group's state without a reload."""
        if self._unsub_members is not None:
            self._unsub_members()
            self._unsub_members = None
        if not self._members:
            return

        @callback
        def _on_member_change(event: Any) -> None:
            self.async_write_ha_state()

        self._unsub_members = async_track_state_change_event(
            self.hass, list(self._members), _on_member_change,
        )

    @callback
    def async_apply(self, group: dict[str, Any]) -> None:
        """Apply a renamed / re-membered version of this group."""
        self._attr_name = group.get("name") or self._slug
        self._attr_icon = group.get("icon") or DEFAULT_GROUP_ICON
        members = list(group.get("members") or [])
        if members != self._members:
            self._members = members
            self._resubscribe()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_commanded()
        self._resubscribe()

        @callback
        def _on_groups_changed(payload: Any) -> None:
            if not isinstance(payload, list):
                return
            for group in payload:
                if isinstance(group, dict) and group.get("slug") == self._slug:
                    self.async_apply(group)
                    return

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_LIGHT_GROUPS_CHANGED, _on_groups_changed,
            )
        )
        self.async_on_remove(self._unsubscribe_members)

    @callback
    def _unsubscribe_members(self) -> None:
        if self._unsub_members is not None:
            self._unsub_members()
            self._unsub_members = None
