"""The Setup Wizard: a few things a new owner should know and decide.

Armed when the config flow creates the Sensa entry. That is both a brand new
system and the first boot after a factory reset, because the reset removes the
entry and the box re-installs it through the same flow. Cleared when the
wizard's Finish is pressed, and never re-armed by anything else.

The steps are built here, from the same card builders Settings uses, so a
choice made in the wizard is the same entity, the same words and the same
control as the one in Settings. There is no second place a setting lives.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    ACTIVATE_LICENCE_ENTITY_ID,
    DOMAIN,
    EVENT_PANEL_CONFIG_CHANGED,
    HOST_UPDATE_WINDOW_SELECT_ENTITY_ID,
    LICENCE_CODE_ENTITY_ID,
    SETTINGS_PIN_DEFAULT,
)

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}.setup_wizard"
_PENDING = "setup_wizard_pending"

ADMIN_LOCK_ENTITY = "switch.sensa_nomad_admin_lock"


# What each diagnostics switch sends, for the wizard's (i) popup. Keyed 1:1 to
# diagnostics_report.CATEGORIES; worded as the privacy policy words it.
_DIAG_DETAIL: dict[str, str] = {
    "devices": (
        "Which types of device you have, such as the make and model, so we "
        "can see which devices our customers use most. Never the names you "
        "have given them."
    ),
    "unavailable": (
        "Which types of device have stopped responding, such as the make and "
        "model, so we can spot a fault early. Never the names you have given "
        "them."
    ),
    "errors": (
        "Recent error messages. Passwords and coordinates are removed before "
        "they are sent, but a message can include the names you have given "
        "your devices."
    ),
    "resources": "How hard the processor, memory and storage are working.",
    "uptime": "How long Sensa has been running, so we can spot restarts.",
    "solar": (
        "How much charge the solar forecast expected against what your panels "
        "made each day, with your panel rating and your vehicle's position "
        "rounded to about 10 km. Only while the solar forecast is on."
    ),
}


def _store(hass: HomeAssistant) -> Store:
    return Store(hass, 1, STORE_KEY)


async def async_arm(hass: HomeAssistant) -> None:
    """Show the wizard on this box. Called by the config flow as it creates
    the entry. A failure is logged, never raised: the entry must still be
    created, and a missing wizard is the lesser fault."""
    try:
        await _store(hass).async_save({"pending": True})
    except Exception:
        _LOGGER.warning("Setup Wizard: could not arm it", exc_info=True)
        return
    hass.data.setdefault(DOMAIN, {})[_PENDING] = True
    _LOGGER.info("Setup Wizard: armed")


async def async_load(hass: HomeAssistant) -> None:
    """Read the store into memory, before the panels are registered, so the
    very first panel config already knows whether to show the wizard."""
    try:
        data = await _store(hass).async_load()
    except Exception:
        _LOGGER.warning("Setup Wizard: could not read its store", exc_info=True)
        data = None
    hass.data.setdefault(DOMAIN, {})[_PENDING] = bool(
        isinstance(data, dict) and data.get("pending"),
    )


def is_pending(hass: HomeAssistant) -> bool:
    return bool(hass.data.get(DOMAIN, {}).get(_PENDING))


async def async_complete(hass: HomeAssistant) -> None:
    """Finish was pressed. Every answer was already written to its own entity
    as it was made, so all this records is that the wizard is done."""
    await _store(hass).async_save({"pending": False})
    hass.data.setdefault(DOMAIN, {})[_PENDING] = False
    _LOGGER.info("Setup Wizard: finished")
    # Every open screen re-reads its panel config, so the wizard closes on
    # all of them, not just the one that pressed Finish.
    hass.bus.async_fire(EVENT_PANEL_CONFIG_CHANGED, {})


def build_steps(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The wizard's pages, for the entities and flavour this box has.

    A page whose control this box lacks is left out, so the step counter never
    counts a page that is not there.
    """
    from .appliance import _is_appliance, is_supervised
    from .dashboard.build_settings import (
        _DIAGNOSTICS_SWITCHES,
        nav_units_card,
        navigation_card,
        solar_card,
        update_switch_groups,
        vehicle_details_card,
        weather_card,
    )
    from .switch import _DIAG_CATEGORY_META

    # The Settings PIN and the HA unit system are changed on the System tab,
    # which only a Container appliance has (`_pin_scope` in __init__).
    container = _is_appliance(hass) and not is_supervised(hass)

    steps: list[dict[str, Any]] = [
        {
            "id": "welcome",
            "title": "Welcome to Sensa",
            "icon": "mdi:hand-wave",
            # The website's homepage hero, also the sign-in background.
            "image": "/local/sensa/branding/welcome-hero.webp",
            "text": [
                ("A few things you should know about your system, and a few "
                "choices to make. It takes a couple of minutes, and you can "
                "change any of it later in Settings."),
                ("Sensa brings everything in your van together in one place: "
                "power, water, heating, lights and the rest, on every screen "
                "and on your phone. If you ever need a hand, our help centre "
                "is at help.sensa.systems."),
            ],
        },
    ]

    # Entering a licence. The same two entities Settings uses (write the code,
    # press Activate), so there is one way a licence gets onto a box.
    if (
        hass.states.get(LICENCE_CODE_ENTITY_ID) is not None
        and hass.states.get(ACTIVATE_LICENCE_ENTITY_ID) is not None
    ):
        steps.append({
            "id": "licence",
            "title": "Your licence",
            "icon": "mdi:license",
            "text": [
                ("Enter the licence code from your Sensa order e-mail, then "
                "press Activate. You need to be online to activate it. If you "
                "do not have a code yet, press Next and add it later in "
                "Settings."),
            ],
            "licence": {
                "sensor": "sensor.sensa_nomad_licence",
                "code_entity": LICENCE_CODE_ENTITY_ID,
                "activate_button": ACTIVATE_LICENCE_ENTITY_ID,
            },
        })

    # After the licence (Richard): the diagnostics ride its check-in.
    steps.append({
        "id": "diagnostics",
        "title": "Help us make Sensa better",
        "icon": "mdi:stethoscope",
        "text": [
            ("When you are online, Sensa can share these with us so we can "
            "spot problems early and keep improving it. We recommend leaving "
            "them all on. Passwords and your exact location are never "
            "shared."),
        ],
        # The wizard draws the switches itself: the Settings card brings its
        # own heading, an "Off by default" line and a Send button.
        "toggles": {
            "items": [
                {"entity": entity, "label": _DIAG_CATEGORY_META[key][0],
                 "icon": _DIAG_CATEGORY_META[key][1],
                 "detail": _DIAG_DETAIL[key]}
                for key, entity in _DIAGNOSTICS_SWITCHES.items()
            ],
            # Under every row's (i) popup. Says what the privacy policy
            # says (Nomad, Diagnostics sharing); change both together.
            "info_note": (
                "Sent about every 15 minutes while your system is online, "
                "and kept for 180 days. Full details are in our privacy "
                "policy at sensa.systems/legal/privacy-policy."
            ),
        },
    })

    steps += [
        {
            "id": "tracking",
            "title": "Tracking",
            "icon": "mdi:map-marker-path",
            "text": [
                ("Tracking records where your van goes, so you can see and "
                "manage your journeys in MySensa. Share your location with "
                "friends and family or make a trip public to your followers "
                "using our FollowMe feature."),
            ],
            "cards": [navigation_card(units=False)],
        },
    ]

    # One window for Sensa updates, system updates and the backup: updates
    # install inside it and the nightly backup is kept out of it (ota.md,
    # backups.md). Appliance only, where the select exists.
    from . import dashboard as _dashboard

    update_groups = update_switch_groups(
        hass, getattr(_dashboard, "_dashboard_devices", None),
    )
    if hass.states.get(HOST_UPDATE_WINDOW_SELECT_ENTITY_ID) is not None:
        steps.append({
            "id": "maintenance",
            "title": "Updates and backups",
            "icon": "mdi:calendar-clock",
            "text": [
                ("Choose a two-hour window when your van is usually parked and "
                "nobody needs the screens. Sensa installs software updates "
                "during it, and takes its nightly backup outside it."),
            ],
            # The two everyday groups; Nerd Mode on Settings -> System splits
            # them back out one by one.
            "toggles": {
                "items": [
                    item for item in (
                        {
                            "entities": update_groups["sensa"],
                            "label": "Sensa updates",
                            "icon": "mdi:update",
                            "detail": (
                                "Sensa's own software: the operating system, "
                                "Nomad, and the firmware in Sensa devices."
                            ),
                        },
                        {
                            "entities": update_groups["vendor"],
                            "label": "Vendor firmware",
                            "icon": "mdi:chip",
                            "detail": (
                                "Firmware for other makers' equipment that "
                                "Sensa looks after, such as a Victron Cerbo or "
                                "a Teltonika router. Sensa only installs "
                                "versions it has approved."
                            ),
                        },
                    ) if item["entities"]
                ],
            },
            "cards": [
                {
                    "type": "custom:sensa-config-card",
                    "fields": [
                        {
                            "entity": HOST_UPDATE_WINDOW_SELECT_ENTITY_ID,
                            "name": "Maintenance window",
                            "control": "select",
                        },
                    ],
                },
            ],
        })

    if container and hass.states.get(ADMIN_LOCK_ENTITY) is not None:
        steps.append({
            "id": "pin",
            "title": "Settings PIN",
            "icon": "mdi:lock-outline",
            "text": [
                ("Settings can ask for a PIN, so nobody else in the van can "
                "change them. Turn on Admin Lock to use it."),
                (f"The PIN starts as {SETTINGS_PIN_DEFAULT}. If you turn Admin "
                "Lock on, choose your own."),
            ],
            "cards": [
                {
                    "type": "custom:sensa-config-card",
                    "fields": [
                        {
                            "entity": ADMIN_LOCK_ENTITY,
                            "name": "Admin Lock",
                            "control": "switch",
                        },
                    ],
                },
            ],
            "pin": True,
        })

    steps.append({
        "id": "vehicle",
        "title": "Your vehicle and units",
        "icon": "mdi:rv-truck",
        "text": [
            ("How you like distances and temperatures shown, and the size of "
            "your van for routing."),
        ],
        "cards": [nav_units_card(), vehicle_details_card()],
        # HA's own unit system, which Settings changes on the System tab.
        "temperature": container,
    })

    # Both forecasts are off until switched on here or on Settings -> Vehicle
    # (Richard, 2026-10-24).
    steps.append({
        "id": "weather",
        "title": "Weather",
        "icon": "mdi:weather-partly-cloudy",
        "text": [
            ("Sensa can forecast the weather where you are, and how much "
            "charge your solar panels should make. Both are off until you "
            "turn them on."),
        ],
        "cards": [solar_card(), weather_card()],
    })

    # Last, on every box: what to do next, and Next takes them there.
    steps.append({
        "id": "done",
        "title": "Thank you",
        "icon": "mdi:check-circle-outline",
        "text": [
            ("Your system is ready. Now add your devices: press Next to go to "
            "Settings, Devices, where Sensa finds what is fitted in your van."),
        ],
        # Where Finish lands: the Settings panel, on its Devices tab.
        "go": "/sensa-settings#discovery",
        "button": "Next",
    })
    return steps
