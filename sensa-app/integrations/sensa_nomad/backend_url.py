"""Resolve the Sensa App backend URL at runtime.

The Sensa App backend runs as a Supervisor add-on alongside HA Core.
Other add-ons / integrations reach it via its supervisor-assigned
container hostname, which is *not* a constant: HA Supervisor prefixes
the add-on's own slug (``sensa_app``) with an 8-char hash derived from
the **repository name** it was installed from. So the same backend
ends up at, e.g.:

  * ``5ec3123c-sensa-app`` — one specific dev/private repo
  * ``6472230c-sensa-app`` — `sha1("ha-apps-repo-edge")[:8]` repo
  * ``80129365-sensa-app`` — `sha1("ha-apps-repo")[:8]` (stable repo)
  * ``local-sensa-app``    — Supervisor's local-add-ons directory

Hardcoding one prefix (the original ``DEFAULT_BACKEND_URL``) broke every
install from a different repo with a misleading DNS-timeout error from
inside the config flow. The fix is to ask Supervisor for the actual
hostname of whichever ``sensa_app`` install is present.

This module is the single place that lookup lives. Use
``resolve_backend_url(hass)`` everywhere that previously imported
``DEFAULT_BACKEND_URL``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


# The add-on's own slug (see app/addons/sensa_app/config.yaml `slug:`).
# Supervisor wraps it as ``<repo-hash>_sensa_app`` for repo-installed
# add-ons and ``local_sensa_app`` for the local-dir install path.
_SENSA_APP_SLUG = "sensa_app"

# Port the add-on listens on (`ports:` block in its config.yaml).
# Fixed by the add-on, not per-install.
_SENSA_APP_PORT = 8099

# Fallback used when (a) HA Core isn't running under Supervisor at all
# (dev installs, container/Core-only), or (b) Supervisor is up but the
# `sensa_app` add-on isn't registered yet. Matches the historical
# hardcoded value so an installation that *was* working pre-resolver
# keeps working without any config change.
FALLBACK_BACKEND_URL = f"http://5ec3123c-{_SENSA_APP_SLUG.replace('_', '-')}:{_SENSA_APP_PORT}"


def resolve_backend_url(hass: HomeAssistant) -> str:
    """Return the URL to use for the Sensa App backend.

    Order of preference:

    1. ``SENSA_APP_BACKEND_URL`` env var — explicit override for
       Container / compose installs, where HA Core runs as a plain
       Docker container with no Supervisor to ask (e.g.
       ``http://localhost:8099`` when the App's port is published to the
       host).
    2. Supervisor's installed-add-on info — the only source that
       reports the actual hostname for *this* HA install. Requires the
       ``hassio`` integration to be loaded (true on any Supervised /
       HAOS install; absent on bare HA Core).
    3. ``FALLBACK_BACKEND_URL`` — the historical hardcoded value. Used
       only when (1) and (2) can't be answered. Carries a log line at
       WARNING so the operator can see what happened if connectivity
       later fails.
    """
    override = os.environ.get("SENSA_APP_BACKEND_URL")
    if override:
        return override.rstrip("/")

    hostname = _hostname_from_supervisor(hass)
    if hostname:
        return f"http://{hostname}:{_SENSA_APP_PORT}"

    _LOGGER.warning(
        "Sensa App backend hostname could not be resolved via Supervisor "
        "(hassio integration not loaded, or add-on not installed yet); "
        "falling back to %s. If DNS lookup fails, install / start the "
        "Sensa App add-on, then reload the Sensa Nomad integration.",
        FALLBACK_BACKEND_URL,
    )
    return FALLBACK_BACKEND_URL


def _hostname_from_supervisor(hass: HomeAssistant) -> str | None:
    """Look up the sensa_app add-on's container hostname from Supervisor.

    Returns ``None`` (rather than raising) on every failure mode:
    Supervisor not running, hassio integration not loaded, add-on not
    installed, hostname missing from the info blob. Callers fall back
    to the constant URL.
    """
    try:
        from homeassistant.components.hassio import get_addons_info
    except ImportError:
        # Bare HA Core install — no Supervisor in the picture at all.
        return None

    try:
        addons: dict[str, Any] | None = get_addons_info(hass)
    except Exception:  # noqa: BLE001 — Supervisor may not be ready yet
        # `HassioNotReadyError` and friends — surface as "fallback".
        return None
    if not addons:
        return None

    for slug, info in addons.items():
        if not _slug_is_sensa_app(slug) or not info:
            continue
        hostname = info.get("hostname")
        if hostname:
            return str(hostname)
        # No hostname in the info blob — derive it from the slug. Same
        # rule Supervisor itself uses (`hostname_from_addon_slug` in
        # homeassistant.components.hassio: slug → slug.replace('_','-')).
        return slug.replace("_", "-")

    return None


def _slug_is_sensa_app(slug: str) -> bool:
    """Match any Supervisor slug that represents our add-on.

    Covers:

    * ``sensa_app`` — bare slug (legacy, unlikely under Supervisor)
    * ``<repo-hash>_sensa_app`` — repo-installed via any of the three
      Sensa distribution repos (edge / beta / stable)
    * ``local_sensa_app`` — installed from Supervisor's local add-ons
      directory
    """
    return slug == _SENSA_APP_SLUG or slug.endswith(f"_{_SENSA_APP_SLUG}")
