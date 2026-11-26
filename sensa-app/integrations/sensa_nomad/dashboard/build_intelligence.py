"""The Intelligence dashboard: the alarms / automation surface.

Split out of the old monolithic `dashboard.py`. The card helpers it calls stay in
the package root and are imported per call (see the note in each builder).
"""
from __future__ import annotations

from typing import Any


def _build_intelligence_yaml_data() -> dict[str, Any]:
    """Return the managed Nomad Intelligence dashboard YAML structure.

    A `panel + dashboard-shell` surface like the other panels. Tabs:
    **Alarms** (conditional â€” only while a managed alarm is live, appended
    first so the banner lands on it), **Scenes** (always, leads the
    always-present tabs so Intelligence lands on a positive behaviour surface),
    **Automations** (always), **Log** (always). The alarm store fires
    `sensa_nomad_panel_config_changed` on the 0â†”live boundary, so an open
    Intelligence panel re-fetches its config and the Alarms tab appears /
    disappears live.
    """
    # Late-bound on purpose. These live in the package root and are resolved
    # per CALL, exactly as they were when this was all one module: the tests
    # patch them on the package (`patch.object(dashboard, ...)`), and
    # `_dashboard_devices` is REBOUND by `set_dashboard_devices`. A module-level
    # import here would freeze the originals and silently ignore both.
    from ..const import DEPLOYMENT_MODE_APPLIANCE, SENSA_LOG_SOURCE_VEHICLE
    from . import (
        _SENSA_DEFAULT_LOGO,
        _build_alarms_tab,
        _build_automations_tab,
        _build_log_tab,
        _build_scenes_tab,
        _build_schengen_tab,
        _current_deployment_mode,
        _dealer_logo_url,
        _has_gps_source,
        _has_live_alarm,
    )
    appliance = _current_deployment_mode() == DEPLOYMENT_MODE_APPLIANCE
    tabs: list[dict[str, Any]] = []
    # Alarms â€” FIRST and only while a managed alarm is live (raised/snoozed),
    # so the banner's `#alarms` landing has a tab to select; drops away when
    # all are cancelled (live config refetch on the boundary event).
    if _has_live_alarm():
        tabs.append({
            "slug": "alarms",
            "label": "Alarms",
            "icon": "mdi:alarm-light",
            "card": _build_alarms_tab(),
        })
    # Scenes leads the always-present tabs, so Intelligence lands on it when
    # no alarm is live. Slug "scenes" since 2026-09-17 (was "modes"): the slug
    # is the URL hash (#scenes), and Richard wanted the address to say what
    # the tab says. Nothing else keys on it; the card, the entity ids and the
    # WS commands all still say mode.
    tabs.append({
        "slug": "scenes",
        "label": "Scenes",
        "icon": "mdi:tune-variant",
        "card": _build_scenes_tab(),
    })
    tabs.append({
        # Slug stays "automations" (entity ids / WS / deep-links unchanged); the
        # customer-facing LABEL is "Instincts" (was "Automations", then "Routines")
        # HA-jargon "Automations" stays internal.
        "slug": "automations",
        "label": "Instincts",
        "icon": "mdi:robot-outline",
        "card": _build_automations_tab(),
    })
    tabs.append({
        # "Routines" was freed from the old Automations tab (now "Instincts")
        # on 2026-07-20 and carried a placeholder note until this card landed:
        # customer-authored When / And If / Then Do automations, a thin
        # wrapper over native HA automations (routines.py) -- distinct from
        # Instincts' fixed, threshold-tunable curated catalog.
        "slug": "routines",
        "label": "Routines",
        "icon": "mdi:calendar-clock",
        # Full width, the same as Instincts: the two tile grids are one kind
        # of page and should read as one.
        "card": {
            "type": "custom:sensa-card-grid",
            "hide_header": True,
            "full_bleed": True,
            "cards": [
                {"type": "custom:sensa-routines-card", "full_width": True},
            ],
        },
    })
    # The VAN's messages only. Anything about the box itself (backups, updates,
    # licence, devices added or removed) goes to Settings -> System Log instead:
    # a customer opening Intelligence to see why their heater warned them should
    # not be scrolling past OTA lines to find it.
    tabs.append({
        "slug": "log",
        "label": "Log",
        "icon": "mdi:message-text-outline",
        "card": _build_log_tab(SENSA_LOG_SOURCE_VEHICLE),
    })
    # Schengen 90/180 usage tracker â€” only when a vehicle-GPS source exists
    # (the tracker stamps the day's country from the vehicle position). The
    # Enable toggle lives inside the tab.
    if _has_gps_source():
        tabs.append({
            "slug": "schengen",
            "label": "Schengen",
            "icon": "mdi:passport",
            "card": _build_schengen_tab(),
        })
    shell_card: dict[str, Any] = {
        "type": "custom:sensa-dashboard-shell",
        "logo": _dealer_logo_url() or _SENSA_DEFAULT_LOGO,
        "chips": [],
        "kiosk_hide_header": True,
        "kiosk_hide_sidebar": appliance,
        "header_show_trial": True,
        "tabs": tabs,
    }
    return {
        "title": "Nomad Intelligence",
        "views": [
            {
                "title": "Sensa",
                "path": "sensa",
                "icon": "mdi:robot-outline",
                "panel": True,
                "cards": [shell_card],
            }
        ],
    }
