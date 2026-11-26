"""Shared dashboard constants: paths, the managed-block markers, the panel table.

Split out of the old monolithic `dashboard.py` so the YAML writer and the sidebar
admin can import them without dragging in the card builders. No logic here, and
nothing mutable: everything in this module is a literal.
"""
from __future__ import annotations

from pathlib import Path

MANAGED_BLOCK_START = "# BEGIN SENSA MANAGED DASHBOARD"


MANAGED_BLOCK_END = "# END SENSA MANAGED DASHBOARD"


DASHBOARD_DIR = Path("nomad/dashboards")


# Lovelace dashboards are GONE â€” the frontend is fully on `panel_custom`
# panels now (see PANELS / build_panel_config below). `DASHBOARDS` is
# kept as an empty list so the few remaining iterators (sidebar order,
# managed-block helpers) no-op without needing per-call guards.
DASHBOARDS: list[dict[str, str]] = []


# Every dashboard YAML file we ever shipped â€” deleted from /config on
# setup (migration off Lovelace) and on uninstall. Includes the old
# production dashboards, the Test1/Test2 sandboxes, and the stale name.
_LEGACY_DASHBOARD_FILES: tuple[str, ...] = (
    "nomad-habitation.yaml",
    "nomad-cab.yaml",
    "nomad-mobile.yaml",
    "nomad-settings.yaml",
    "nomad-test.yaml",
    "nomad-test-trial.yaml",
)


# Lovelace dashboard keys + panel paths to purge from each user's
# sidebar panelOrder (legacy `nomad-*` dashboards we no longer register,
# plus a defensive sweep of the live panel paths so re-ordering is
# idempotent).
_LEGACY_DASHBOARD_KEYS: tuple[str, ...] = (
    "nomad-habitation", "nomad-cab", "nomad-mobile", "nomad-settings",
    "nomad-test", "nomad-test-trial", "nomad-test3",
)


# Custom panels â€” the panel_custom migration of the four dashboards.
# `path` is the panel's frontend_url_path; deliberately distinct from
# the `nomad-*` Lovelace dashboard keys so the two don't collide in
# HA's single panel namespace (the Lovelace dashboards stay registered
# as hidden URL fallbacks). `key` is what `build_panel_config` and the
# frontend switcher use to identify a dashboard.
PANELS: list[dict[str, str]] = [
    {"key": "habitation", "title": "Habitation", "icon": "mdi:sofa-outline",
     "path": "sensa-habitation"},
    {"key": "cab", "title": "Cab", "icon": "mdi:steering",
     "path": "sensa-cab"},
    {"key": "mobile", "title": "Mobile", "icon": "mdi:cellphone",
     "path": "sensa-mobile"},
    {"key": "intelligence", "title": "Intelligence", "icon": "mdi:robot-outline",
     "path": "sensa-intelligence"},
    {"key": "settings", "title": "Settings", "icon": "mdi:cog-outline",
     "path": "sensa-settings"},
]


# Default panel a fresh user lands on (set into frontend.user_data_*).
DEFAULT_PANEL_PATH = "sensa-habitation"


# The `admin` login manages the box, so it lands on Settings (Richard 2026-07-07).
SETTINGS_PANEL_PATH = "sensa-settings"


# Per-user default landing panel for the appliance kiosk tablet users
# (created by the build script â€” tools/build-appliance.py).
# Each tablet signs in as its own user, so baking the landing panel into
# that user's frontend.user_data_* makes the Cab tablet open on the Cab
# panel and the Habitation tablet on Habitation â€” HA's native per-user
# default, no third-party sidebar add-on. Writing this UI preference is
# cosmetic (not a lockdown), so it stays in the product, gated on the
# appliance flag. Users not in this map (owner, BYO accounts) get
# DEFAULT_PANEL_PATH. Keep keys in sync with const.APPLIANCE_KIOSK_USERNAMES.
TABLET_USER_DEFAULT_PANEL: dict[str, str] = {
    "hab": "sensa-habitation",
    "cab": "sensa-cab",
    "mobile": "sensa-mobile",
}


# HA's built-in dashboard panels we hide from the appliance sidebar so the
# customer only ever sees the Sensa Nomad panels â€” values are HA panel
# url_paths, written into each user's frontend.user_data_* `sidebar.hiddenPanels`.
# Settings (`config`) and Developer Tools (`developer-tools`) are deliberately
# NOT here: HA already hides those from the non-admin owner/kiosk users by its
# own permission model, and the support-backdoor admin must keep them. Applied
# in appliance mode only (see `async_set_sidebar_order`).
HA_DEFAULT_HIDDEN_PANELS: frozenset[str] = frozenset(
    {
        "lovelace",       # Overview
        "energy",         # Energy
        "history",        # History
        "logbook",        # Logbook / Activity
        "map",            # Map
        "media-browser",  # Media
        "todo",           # To-do lists
        # HA 2026.x auto-generated dashboards, registered by the `frontend`
        # component itself (show_in_sidebar=False, so hiding is moot; REMOVAL
        # is what matters). `home` is the frontend's DEFAULT_PANEL: whenever a
        # user's default_panel isn't in hass.panels (a Sensa panel not yet
        # registered during an HA restart) getDefaultPanel() falls back to
        # `home`, so a customer landed on HA's own Home dashboard (/home/0).
        # Removing it closes that door; once removed the frontend re-routes a
        # stranded tab back to the Sensa default panel on the next panel update.
        "home",           # Home (new default dashboard)
        "security",       # Security
        "climate",        # Climate
        "maintenance",    # Maintenance
    }
)
