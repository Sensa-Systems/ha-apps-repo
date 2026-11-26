"""Sensa branding for Home Assistant's web app manifest.

HA is already an installable PWA: `homeassistant.components.frontend` serves a
generated `/manifest.json` with `display: standalone`, its own icon set and a
service worker at scope `/`. What it does NOT do is know it is running as a
Sensa appliance, so an installed desktop app appears as "Home Assistant" with
the HA icon and lands on HA's own default page.

This module rebrands that manifest. It is deliberately small, because the hook
it depends on is fragile in two ways worth stating plainly:

* `frontend.add_manifest_json_key` is public but UNDOCUMENTED (nothing on
  developers.home-assistant.io). Treat it as unstable across HA upgrades, and
  never let a failure here abort integration setup.
* The manifest is ONE module-level object, last-writer-wins. There is no
  per-integration layering, so every key we set is set for the whole instance.

That second point is why this is appliance-only. On a BYO install the customer's
Home Assistant runs plenty besides Sensa, and renaming the whole thing to
"Sensa" is not ours to do.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .appliance import _is_appliance
from .dashboard._constants import DEFAULT_PANEL_PATH

_LOGGER = logging.getLogger(__name__)

APP_NAME = "Sensa"

# --sensa-bg-2, resolved from themes/sensa-colours.yaml. Deliberately a literal
# hex and NOT read from the theme: HA populates the manifest's theme_color from
# the default theme's `app-header-background-color`, which in our theme is the
# string "var(--sensa-bg-2)". HA does no CSS-variable resolution, so relying on
# the theme puts an unresolvable string in the manifest and in the
# <meta name="theme-color"> tag HA renders from the same value.
BRAND_COLOUR = "#0b1830"

# Served by HA's built-in /local/ mount from /config/www/sensa/, which the App
# add-on populates from the webcards bundle. Same origin as HA, which is what
# the manifest needs. A 404 here silently fails the browser's install criteria,
# so these paths are worth checking on the box after a deploy.
_ICON_BASE = "/local/sensa/images"

ICONS: list[dict[str, str]] = [
    {
        "src": f"{_ICON_BASE}/sensa-icon-192.png",
        "sizes": "192x192",
        "type": "image/png",
        "purpose": "any",
    },
    {
        "src": f"{_ICON_BASE}/sensa-icon-512.png",
        "sizes": "512x512",
        "type": "image/png",
        "purpose": "any",
    },
]

# Opening on the Habitation panel rather than HA's landing page. Reuses the
# dashboard generator's own constant so a panel rename can't leave this
# pointing at a dead path.
START_URL = f"/{DEFAULT_PANEL_PATH}"


def _manifest_keys() -> dict[str, Any]:
    """The keys we stamp onto HA's manifest, as a plain mapping.

    Split out from the apply so a test can assert the values without needing to
    stand up HA's frontend module.
    """
    return {
        "name": APP_NAME,
        "short_name": APP_NAME,
        "icons": ICONS,
        "start_url": START_URL,
        # `id` is what browsers key an installed app on. Changing it means this
        # installs as a distinct app from any previously-installed Home
        # Assistant, which is what we want, at the cost of a box that already
        # had HA installed showing two icons until the old one is removed.
        "id": START_URL,
        "theme_color": BRAND_COLOUR,
        "background_color": BRAND_COLOUR,
        # HA ships these pointing at the Android companion app. MDN lists
        # `prefer_related_applications` being false or absent as an install
        # requirement, so leaving it True risks the browser offering the Play
        # Store listing instead of installing the page. Whether desktop Chrome
        # actually honours it against a non-matching platform is unconfirmed;
        # clearing it costs nothing either way.
        "prefer_related_applications": False,
        "related_applications": [],
    }


@callback
def async_apply_manifest(hass: HomeAssistant) -> None:
    """Rebrand HA's web app manifest as Sensa, on an appliance only.

    Best-effort by design: a failure leaves the stock HA manifest in place,
    which is a cosmetic regression, not a broken install.
    """
    if not _is_appliance(hass):
        return

    try:
        from homeassistant.components.frontend import add_manifest_json_key

        for key, value in _manifest_keys().items():
            add_manifest_json_key(key, value)
    except Exception:
        _LOGGER.warning(
            "Could not apply Sensa branding to the web app manifest; "
            "an installed app will show Home Assistant's name and icon",
            exc_info=True,
        )
        return

    _LOGGER.debug("Applied Sensa branding to the web app manifest")
