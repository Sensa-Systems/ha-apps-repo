"""Dealer-gated frontend-theme allow-list.

The instance-wide frontend theme (Settings → Supervisor → System → Theme, and
the gated ``sensa_nomad.set_theme`` service) is offered from a CURATED list, not
from every theme installed under ``/config/themes/``. The curated list is:

* the bundled **public** Sensa themes (``themes/sensa-*.yaml`` shipped in this
  integration — e.g. "Sensa Colours", "Sensa Industrial"), plus HA's built-in
  ``default`` as a guaranteed escape hatch; and
* the licence's **dealer** theme, when one has been fetched + cached by the App
  (``dealer_theme`` on the runtime licence status), gated by the dealer's
  ``theme_mode``: ``additive`` adds it to the public set, ``exclusive`` offers
  only it (+ ``default`` for safety), ``locked`` offers only it and nothing
  else.

``exclusive`` and ``locked`` differ in one thing, and it is the thing the dealer
is actually choosing: ``exclusive`` keeps HA's unbranded ``default`` in the
picker, so the customer can still walk away from the dealer's branding;
``locked`` does not, and ``theme_is_locked()`` additionally hides the picker and
stops the public theme YAMLs being installed at all. A picker the customer never
sees is only half the job -- HA's own Profile -> Theme page offers every
REGISTERED theme, so a lock is only real if the alternatives are never written
to ``/config/themes/``. See ``frontend.async_install_sensa_themes``.

This mirrors ``layout_catalog.py``'s dealer gating for home layouts and reuses
its ``current_dealer`` / ``SENSA_DEALER_ID`` so one dealer identity drives both
surfaces. Trials are excluded upstream (``current_dealer`` returns None for a
trial), so a trial only ever sees the public themes.

Why a curated list rather than "everything installed": a dealer theme is only
ever DELIVERED to the matching dealer's box (fetch-and-cache, like the logo), so
cross-dealer isolation is by file presence. This allow-list is the second layer
— it keeps the Sensa picker tidy and enforces ``exclusive`` server-side.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .layout_catalog import SENSA_DEALER_ID, current_dealer

_LOGGER = logging.getLogger(__name__)

# HA's built-in theme — always offered as a safe fallback so an exclusive dealer
# whose theme failed to fetch, or any misconfiguration, can never strand the box
# on an empty picker.
DEFAULT_THEME = "default"

# Sensa's own default branding: the theme an appliance image boots on (baked by
# `tools/build-appliance.py`), and what the active theme reverts to when a
# dealer theme goes away (dealer swapped for one with no theme, dealer's theme
# deleted from the Hub, licence back to a plain non-dealer one). NOT the same as
# DEFAULT_THEME, which is HA's unbranded built-in. Must stay equal to the
# top-level key of `themes/sensa-colours.yaml`; `test_theme_catalog.py` pins it.
SENSA_DEFAULT_THEME = "Sensa Colours"

_BUNDLED_THEMES_DIR = Path(__file__).parent / "themes"

# A top-level mapping key = a theme name (an unindented ``Name:`` line). Same
# shape the App's dealer_theme.py uses to read a downloaded theme's name.
_TOP_LEVEL_KEY = re.compile(r"^(?P<key>[^\s#][^:]*):\s*(?:#.*)?$")


def _theme_name_from_text(text: str) -> str | None:
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1] in (" ", "\t"):
            continue
        m = _TOP_LEVEL_KEY.match(raw_line)
        if not m:
            return None
        name = m.group("key").strip().strip("\"'").strip()
        return name or None
    return None


@lru_cache(maxsize=1)
def public_theme_names() -> tuple[str, ...]:
    """The bundled public Sensa theme names (top-level key of each
    ``themes/sensa-*.yaml``), in filename order, followed by HA's ``default``.
    Cached — the bundled set is fixed for the life of the process."""
    names: list[str] = []
    if _BUNDLED_THEMES_DIR.is_dir():
        for src in sorted(_BUNDLED_THEMES_DIR.glob("sensa-*.yaml")):
            try:
                name = _theme_name_from_text(src.read_text(encoding="utf-8"))
            except OSError:
                name = None
            if name and name not in names:
                names.append(name)
    if DEFAULT_THEME not in names:
        names.append(DEFAULT_THEME)
    return tuple(names)


# The dealer theme_mode values this device understands. An older Core folds an
# unknown spelling to "additive" (see `LicenceClaims.from_payload`), which is the
# safe direction: a box that predates `locked` simply does not lock.
THEME_MODES = ("additive", "exclusive", "locked")


def current_theme_mode(hass: Any) -> str:
    """The active dealer's theme_mode (``additive``/``exclusive``/``locked``),
    or ``additive`` when there is no dealer / it's unset."""
    dealer = current_dealer(hass) if hass is not None else None
    mode = (dealer or {}).get("theme_mode")
    return mode if mode in THEME_MODES else "additive"


def dealer_theme_name(hass: Any) -> str | None:
    """The cached dealer theme's registered name, read offline from the runtime
    licence status (``dealer_theme``), or None when none has been fetched. The
    App only sets it for a non-trial dealer that belongs to the current claim."""
    from .const import DOMAIN, RUNTIME_LICENCE_STATUS

    data = getattr(hass, "data", None) or {}
    for runtime in (data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        name = (runtime.get(RUNTIME_LICENCE_STATUS) or {}).get("dealer_theme")
        if name:
            return str(name)
    return None


def locked_theme_name(hass: Any) -> str | None:
    """The dealer theme this box is LOCKED to, or None when nothing is locked.

    The one predicate for the whole lock. The theme picker hides itself on it,
    the theme installer refuses to write the public YAMLs on it, and the licence
    refresh re-asserts the theme on it: all three derive from this call rather
    than re-deriving ``dealer + mode + cached name`` for themselves, because a
    rule written three times is this repo's recurring bug.

    Returns a name only when every part of the lock is actually known:

    * a real dealer (``current_dealer`` already excludes trials), and not the
      ``SENSA_DEALER_ID`` preview account, which must keep seeing everything;
    * ``theme_mode == "locked"``; and
    * the dealer's theme has been fetched and cached, so we know what to lock TO.

    That last one is "could not ask", not "told no": an offline first boot has
    no cached theme, so it is NOT locked and keeps the public picker rather than
    being stranded on an empty one. The lock arrives with the theme.
    """
    dealer = current_dealer(hass) if hass is not None else None
    if not dealer or dealer.get("id") == SENSA_DEALER_ID:
        return None
    if current_theme_mode(hass) != "locked":
        return None
    return dealer_theme_name(hass)


def allowed_theme_names(hass: Any) -> list[str]:
    """Theme names this licence may select, in picker order.

    * no dealer (incl. trials) → the public themes;
    * ``SENSA_DEALER_ID`` preview account → public + any cached dealer theme;
    * dealer + a cached theme, ``additive`` → the dealer theme leads, then the
      public set;
    * dealer + a cached theme, ``exclusive`` → the dealer theme only (+
      ``default`` as a safety escape hatch);
    * dealer + a cached theme, ``locked`` → the dealer theme and NOTHING else,
      not even ``default``; the picker is hidden entirely (see
      ``locked_theme_name``), and this list is what the ``set_theme`` gate
      enforces for a caller who goes round the picker;
    * dealer but NO theme cached yet (offline / first boot) → the public set,
      so an exclusive or locked dealer is never handed an empty picker (mirrors
      ``layout_catalog.dealer_is_exclusive_empty``).
    """
    public = list(public_theme_names())
    dealer = current_dealer(hass) if hass is not None else None
    if not dealer:
        return public

    if (locked := locked_theme_name(hass)) is not None:
        return [locked]

    name = dealer_theme_name(hass)
    if dealer.get("id") == SENSA_DEALER_ID:
        # Internal preview account — surface everything: public + the cached
        # dealer theme (whichever dealer it currently belongs to), if any.
        return public + ([name] if name and name not in public else [])

    if not name:
        # A dealer whose theme hasn't arrived yet — never empty, offer public.
        return public

    if current_theme_mode(hass) == "exclusive":
        result = [name]
        if DEFAULT_THEME not in result:
            result.append(DEFAULT_THEME)
        return result

    # Additive: the dealer theme leads, then the public themes (deduped).
    return [name] + [p for p in public if p != name]


# --- What HA actually has ---------------------------------------------------
#
# `frontend.set_theme` is FAIL-SOFT: an unknown theme name returns success and
# only logs a warning (documented in `tools/build-appliance.py`, which works
# round it by polling `frontend/get_themes` over the WebSocket). Server-side we
# have no WebSocket, and HA exposes no public getter, so we read the frontend
# component's own `hass.data` keys behind a guarded import.
#
# The guard is the point: if HA renames or restructures these, the import fails
# and every caller gets None, which means "could not ask" and NEVER "told no".
# A verify that cannot run must degrade to today's behaviour, not to a false
# failure. UNVERIFIED against a running HA in the session that wrote this --
# confirm on a box before trusting a negative.


def _frontend_data(hass: Any) -> tuple[dict | None, str | None]:
    """``(registered themes, current default theme)`` from HA's frontend, or
    ``(None, None)`` when this HA build does not expose them."""
    try:
        from homeassistant.components.frontend import (
            DATA_DEFAULT_THEME,
            DATA_THEMES,
        )
    except ImportError:
        _LOGGER.debug("HA frontend theme internals unavailable, cannot verify")
        return None, None
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        return None, None
    themes = data.get(DATA_THEMES)
    default = data.get(DATA_DEFAULT_THEME)
    return (
        themes if isinstance(themes, dict) else None,
        str(default) if isinstance(default, str) else None,
    )


def registered_theme_names(hass: Any) -> set[str] | None:
    """Theme names HA has actually REGISTERED (YAML on disk *and* covered by the
    ``frontend: themes:`` include), or None when it could not be read.

    Not the same set as :func:`allowed_theme_names`, which is built from the
    bundled dir plus the licence. A name can be allowed and not registered --
    that gap is exactly what makes a fail-soft ``set_theme`` look like success.
    """
    themes, _ = _frontend_data(hass)
    if themes is None:
        return None
    # HA's built-in theme is never a key in the themes map.
    return set(themes) | {DEFAULT_THEME}


def current_default_theme(hass: Any) -> str | None:
    """HA's instance-wide default theme, or None when it could not be read."""
    _, default = _frontend_data(hass)
    return default
