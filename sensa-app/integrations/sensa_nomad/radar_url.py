"""Resolve the Sensa Radar origin from the Sensa App.

The Radar URL is add-on configuration (`radar_url`), set per environment by the
pipeline from the `sensaRadarUrl` variable groups (dev/test/prod). The app exports
it as `SENSA_RADAR_URL` and returns it on `/health`; the integration reads it
there, the same way it reads `deployment_mode`, rather than from its own process
environment (which, under Supervisor, it can't see). No URLs are baked into the
integration.

It is an ORIGIN, not an API base: callers append their own path (the client adds
`RADAR_API_PREFIX`, the nav-map card embeds `/?embed=1`). It must therefore be
the origin serving the Radar WEB APP, which proxies /api/* to the backend. Point
it at the backend directly and the API half works while the map iframe renders a
JSON 404.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .api.client import SensaApiClient
from .backend_url import resolve_backend_url

_LOGGER = logging.getLogger(__name__)


async def async_resolve_radar_target(
    hass: HomeAssistant,
) -> tuple[str | None, bool]:
    """The Radar `(origin, insecure_tls)` from the app's /health.

    Both come off ONE read because they describe one target: an origin resolved
    without the TLS flag that goes with it is how you get a client that verifies
    against a dev server that cannot be verified, which fails at the handshake and
    reads as "Radar is down".

    A `None` origin means Radar isn't configured for this environment (or the app
    is briefly unreachable); the caller then leaves the Radar sensors as they are
    rather than querying with no target. `insecure_tls` is False unless the App
    says otherwise, including when the App is too old to have the field.
    """
    from .const import RADAR_INSECURE_TLS_KEY, RADAR_URL_KEY

    try:
        base = resolve_backend_url(hass)
        health = await SensaApiClient(hass, base).async_health_quiet()
    except Exception:
        _LOGGER.debug("Radar URL: app /health lookup failed", exc_info=True)
        return None, False

    url = (health or {}).get(RADAR_URL_KEY)
    insecure = bool((health or {}).get(RADAR_INSECURE_TLS_KEY))
    if isinstance(url, str) and url.strip():
        return url.strip().rstrip("/"), insecure
    return None, insecure


async def async_resolve_radar_url(hass: HomeAssistant) -> str | None:
    """The Radar origin alone, for callers that do not make API calls.

    The nav-map card wants an origin to embed and nothing else. Anything opening a
    connection wants `async_resolve_radar_target` instead, so that it gets the TLS
    answer that belongs with the origin.
    """
    url, _insecure = await async_resolve_radar_target(hass)
    return url


async def async_resolve_radar_session(
    hass: HomeAssistant, member_id: str = ""
) -> tuple[str | None, bool | None, str]:
    """The app's Radar session for a member: `(token, is_pro, reason)`.

    One call answers both questions the van has, because the app answers both in
    one response and it would be wasteful (and racy) to ask twice: `token` drives
    the amenity search, `is_pro` drives the tier badge on the Cab.

    `member_id` (a Hub User.id from the driver picker) asks the app for THAT
    customer member's session, so both the saved places AND the tier follow
    whoever is driving. Empty means the primary member, i.e. the pre-picker
    behaviour.

    **`is_pro` is `None` for "could not ask", never `False`.** The distinction is
    load-bearing: `False` is a real answer from Hub's entitlement ladder ("this
    member holds no seat"), while an unreachable app, a non-200 or a timeout is
    an absence of one. Collapsing the two would repaint a paying customer as Base
    the moment the app hiccuped mid-journey, which is the same fail-open rule the
    read-only enforcement follows for `control_locked`. The caller carries its
    previous verdict forward on `None`.

    A 200 is NOT on its own an answer. The app returns `is_pro: false` on every
    unhappy path, including "Radar is not configured for this environment", "No
    licence installed" and "Radar unreachable" -- none of which is a fact about the
    customer. It flags the difference with `entitlement_known`, and only a true
    flag produces a definite `False` here. A missing flag (an app older than the
    field) also reads as unknown: no badge is the safe failure, a wrong badge is
    not.
    """
    import socket
    from urllib.parse import urlencode

    import aiohttp

    try:
        base = resolve_backend_url(hass)
    except Exception:  # noqa: BLE001
        return None, None, ""
    url = f"{base.rstrip('/')}/api/v1/radar/token"
    if member_id:
        url = f"{url}?{urlencode({'member_id': member_id})}"
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session, session.get(url) as resp:
            if resp.status != 200:
                return None, None, ""
            data = await resp.json()
    except Exception:
        _LOGGER.debug("Radar token: app lookup failed", exc_info=True)
        return None, None, ""
    return _session_from_payload(data)


def _session_from_payload(data: object) -> tuple[str | None, bool | None, str]:
    """Map the app's `/api/v1/radar/token` body to `(token, is_pro, reason)`.

    Split out from the HTTP above so the rule below can be tested without mocking
    a client session: the rule is the interesting part, the transport is not.
    """
    data = data if isinstance(data, dict) else {}
    token = data.get("token")
    token = token.strip() if isinstance(token, str) and token.strip() else None
    # Prefer Hub's WIRE code ("no_seat_available", ...) over the English sentence:
    # the coordinator branches on a seat refusal to log it once for the customer,
    # and prose would break that the first time somebody improves the wording. Falls
    # back to the sentence so an app older than the field still says something
    # useful on the licence panel.
    reason = str(data.get("error_code") or data.get("error") or "")
    # Only an ENTITLEMENT answer moves the tier. A free-tier member gets no bearer
    # but is knowably Base (flag true, is_pro false); a box that could not ask is
    # indistinguishable from that on `is_pro` alone, hence the flag.
    if not data.get("entitlement_known"):
        return token, None, reason
    return token, bool(data.get("is_pro")), reason


async def async_resolve_radar_token(
    hass: HomeAssistant, member_id: str = ""
) -> str | None:
    """Return the Radar Premium bearer the app serves, or None.

    The app (which holds the licence) exchanges it for an is_pro Radar token and
    exposes the current token at /api/v1/radar/token. None => anonymous (free
    tier); the caller still works, just without Premium/Google data.

    A thin wrapper over `async_resolve_radar_session` for the callers that only
    want the bearer (the tracking coordinator), kept so adding the tier did not
    have to touch them.
    """
    token, _is_pro, _reason = await async_resolve_radar_session(hass, member_id)
    return token


async def async_resolve_radar_members(hass: HomeAssistant) -> list[dict]:
    """Return the customer's member list for the driver picker, or [].

    The app fetches it from Radar's /auth/nomad/members (masked emails only) and
    serves it at /api/v1/radar/members. Each item is
    `{"id", "display_name", "email_masked"}`. [] means Radar is not configured, the
    app is briefly unreachable, or the customer has no listed members yet; the
    picker then simply has no options and self-hides.
    """
    import socket

    import aiohttp

    try:
        base = resolve_backend_url(hass)
    except Exception:  # noqa: BLE001
        return []
    url = f"{base.rstrip('/')}/api/v1/radar/members"
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
        _LOGGER.debug("Radar members: app lookup failed", exc_info=True)
        return []
    members = (data or {}).get("members")
    return members if isinstance(members, list) else []
