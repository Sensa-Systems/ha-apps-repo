"""Frontend asset installer for the Sensa Nomad integration.

Today this only handles HA theme bundling: copies the YAML files
under `themes/` (relative to this package) into `<config_dir>/themes/`
so HA's default frontend config

    frontend:
      themes: !include_dir_merge_named themes

picks them up. After a successful copy we call
`frontend.reload_themes` so the new themes appear in the user-profile
theme dropdown without an HA restart.

Idempotent: a theme YAML is only rewritten if the bundled content
differs from what's already on disk, so reload-storms during normal
integration setup don't churn `/config/themes/`.

Mirrors the modes/dashboard pattern:
  * Atomic writes via `_io.atomic_write_text`.
  * Generated-file checksums via `_io.record_generated_checksum`
    so SensaIntegritySensor can spot manual edits.
  * Removed in `async_remove_entry` via `async_remove_sensa_themes`.

We never modify `configuration.yaml`. Customers who have replaced the
default frontend block with one that omits the `themes:` include
will get the YAMLs on disk but nothing in the picker until they
re-add the include — that's a deliberate trade-off: stomping on a
customised `frontend:` block is far worse than a one-line README ask.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from ._io import (
    atomic_write_text,
    forget_generated_checksum,
    record_generated_checksum,
)

_LOGGER = logging.getLogger(__name__)

# Source dir inside the integration package — the pipeline ships
# everything under `custom_components/sensa_nomad/` so this folder
# lands on the customer's HA host alongside the .py modules.
_BUNDLED_THEMES_DIR = Path(__file__).parent / "themes"

# Subdir under HA's config_dir that the default `frontend:` block
# loads themes from.
_HA_THEMES_SUBDIR = "themes"

# Filename prefix — every Sensa-shipped theme YAML must start with
# this. Lets us tell our files apart from customer files in
# `/config/themes/` so uninstall only deletes what we put there.
_SENSA_THEME_PREFIX = "sensa-"


def _uninstall_public_themes_blocking(config_dir: str) -> list[str]:
    """Delete the BUNDLED public Sensa themes from `<config_dir>/themes/`.

    The enforcing half of a dealer theme lock. Hiding our own picker is not a
    lock: HA's Profile → Theme page offers every REGISTERED theme, so as long as
    "Sensa Colours" sits in `/config/themes/` the customer can pick it there and
    the dealer's branding is gone. The only way to make the lock real offline is
    for the alternatives never to be registered.

    Deletes only files this integration ships (the exact filenames under the
    bundled dir), NOT the `sensa-*.yaml` glob `_remove_themes_blocking` uses on
    uninstall, because the App caches the dealer's own theme as
    `themes/sensa-dealer-*.yaml` and deleting THAT would strand the locked box
    on a theme HA no longer knows.

    Returns the relative paths actually removed, so the caller only reloads when
    something changed.
    """
    if not _BUNDLED_THEMES_DIR.is_dir():
        return []
    target_dir = Path(config_dir) / _HA_THEMES_SUBDIR
    if not target_dir.is_dir():
        return []

    removed: list[str] = []
    for src in sorted(_BUNDLED_THEMES_DIR.glob("*.yaml")):
        if not src.name.startswith(_SENSA_THEME_PREFIX):
            continue
        dest = target_dir / src.name
        if not dest.exists():
            continue
        try:
            dest.unlink()
        except OSError:
            _LOGGER.debug("Locked theme: could not remove %s", dest, exc_info=True)
            continue
        rel = (Path(_HA_THEMES_SUBDIR) / src.name).as_posix()
        # Drop the checksum too, or SensaIntegritySensor reports a generated
        # file we deliberately deleted as tampered-with.
        forget_generated_checksum(config_dir, rel)
        removed.append(rel)
        _LOGGER.info("Dealer theme lock: removed public Sensa theme %s", rel)
    return removed


def _install_themes_blocking(config_dir: str) -> list[str]:
    """Copy bundled theme YAMLs into `<config_dir>/themes/`.

    Returns the list of relative paths (relative to `config_dir`) we
    actually wrote — files whose content already matches the bundled
    version are skipped so there's no thrash on every integration
    setup.
    """
    if not _BUNDLED_THEMES_DIR.is_dir():
        _LOGGER.debug("No bundled themes directory at %s", _BUNDLED_THEMES_DIR)
        return []

    target_dir = Path(config_dir) / _HA_THEMES_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for src in sorted(_BUNDLED_THEMES_DIR.glob("*.yaml")):
        if not src.name.startswith(_SENSA_THEME_PREFIX):
            # Anything that lands here without our prefix is a
            # packaging bug — refuse to copy so we never overwrite
            # a customer file by accident on uninstall.
            _LOGGER.warning(
                "Bundled theme %s does not start with %r — skipping",
                src.name, _SENSA_THEME_PREFIX,
            )
            continue
        content = src.read_text(encoding="utf-8")
        dest = target_dir / src.name
        if dest.exists() and dest.read_text(encoding="utf-8") == content:
            continue
        atomic_write_text(dest, content)
        rel = (Path(_HA_THEMES_SUBDIR) / src.name).as_posix()
        record_generated_checksum(config_dir, rel, content)
        written.append(rel)
        _LOGGER.info("Installed Sensa theme: %s", rel)

    return written


async def async_install_sensa_themes(hass: HomeAssistant) -> None:
    """Install bundled Sensa themes into HA's themes dir, unless this box is
    locked to a dealer theme, in which case remove them instead.

    Safe to call repeatedly, and it must be: the lock is only knowable once the
    licence has been read, so setup normally installs (nothing known yet =
    "could not ask", which must not lock) and the licence refresh calls back to
    remove. Idempotent in both directions.

    Best-effort: any failure is logged but never aborts integration
    setup — themes are cosmetic, the rest of Nomad must keep
    working.
    """
    from .theme_catalog import locked_theme_name

    locked = locked_theme_name(hass)
    try:
        if locked is not None:
            changed = await hass.async_add_executor_job(
                _uninstall_public_themes_blocking, hass.config.config_dir,
            )
        else:
            changed = await hass.async_add_executor_job(
                _install_themes_blocking, hass.config.config_dir,
            )
    except Exception:
        _LOGGER.warning(
            "Failed to install Sensa themes — Nomad will keep running, "
            "themes can be installed manually under /config/themes/",
            exc_info=True,
        )
        return

    if not changed:
        return

    try:
        await hass.services.async_call(
            "frontend", "reload_themes", {}, blocking=True,
        )
        _LOGGER.debug("Sensa themes: frontend.reload_themes called")
    except Exception:
        _LOGGER.debug(
            "Sensa themes: frontend.reload_themes failed — themes "
            "will appear after the next HA restart",
            exc_info=True,
        )


def _remove_themes_blocking(config_dir: str) -> None:
    """Delete every Sensa-prefixed YAML from `<config_dir>/themes/`."""
    target_dir = Path(config_dir) / _HA_THEMES_SUBDIR
    if not target_dir.is_dir():
        return
    for path in target_dir.glob(f"{_SENSA_THEME_PREFIX}*.yaml"):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            _LOGGER.debug(
                "Failed to remove Sensa theme %s on uninstall",
                path, exc_info=True,
            )


async def async_remove_sensa_themes(hass: HomeAssistant) -> None:
    """Remove every bundled Sensa theme on integration uninstall."""
    await hass.async_add_executor_job(
        _remove_themes_blocking, hass.config.config_dir,
    )
    try:
        await hass.services.async_call(
            "frontend", "reload_themes", {}, blocking=True,
        )
    except Exception:
        _LOGGER.debug(
            "Sensa themes: frontend.reload_themes after removal failed",
            exc_info=True,
        )


# --- Per-user theme pins ----------------------------------------------------
#
# HA's per-user Profile -> Theme selection OVERRIDES the instance-wide default,
# so `frontend.set_theme` alone does not give every login the same theme: any
# user who has ever picked one keeps it, for ever. That is what made the System
# panel look broken, and it also holes a dealer theme lock below the waterline,
# because a pinned login never sees the locked theme at all.
#
# One instance, one theme: setting the theme clears every pin.
#
# The pin lives in HA's frontend user-data store, the same store
# `dashboard/config_yaml.py` uses for `core.default_panel` and `sidebar`. HA
# writes it under `selectedTheme`, and the frontend reads a null value as
# "Backend-selected", so we null rather than delete.
#
# We do NOT trust that name on its own. It was never verified against a running
# HA, and a clear that silently matches nothing is indistinguishable from a
# theme that will not change, which is exactly the bug this is here to fix. So
# the sweep also nulls any OTHER top-level key whose value is a mapping carrying
# a `theme` member: that shape is the theme selection and nothing else in the
# store has it (`core` holds `default_panel`, `sidebar` holds panel order). A
# rename cannot outrun it.
_SELECTED_THEME_KEY = "selectedTheme"


def _pinned_theme_keys(data: dict) -> list[str]:
    """Top-level store keys that hold a theme pin, by name or by shape."""
    keys: list[str] = []
    for key, value in data.items():
        if value is None:
            continue
        if key == _SELECTED_THEME_KEY or (
            isinstance(value, dict) and "theme" in value
        ):
            keys.append(key)
    return keys


async def async_clear_pinned_user_themes(hass: HomeAssistant) -> int:
    """Clear every login's pinned profile theme so they all follow the
    instance-wide default. Returns how many users were changed.

    Best-effort per user: one unreadable store must not stop the rest, and it
    must never fail the theme change that called us.
    """
    from homeassistant.components.frontend import storage as frontend_storage

    try:
        users = await hass.auth.async_get_users()
    except Exception:
        _LOGGER.debug("Could not list users to clear pinned themes", exc_info=True)
        return 0

    cleared = 0
    seen_keys: set[str] = set()
    for user in users:
        # System-generated users (tokens, integrations) have no profile to pin.
        if getattr(user, "system_generated", False):
            continue
        try:
            store = await frontend_storage.async_user_store(hass, user.id)
            data = getattr(store, "data", None) or {}
            seen_keys.update(data)
            pinned = _pinned_theme_keys(data)
            if not pinned:
                continue
            for key in pinned:
                await store.async_set_item(key, None)
            cleared += 1
            _LOGGER.info(
                "Cleared the pinned profile theme for user %s (keys: %s)",
                user.id, pinned,
            )
        except Exception:
            _LOGGER.debug(
                "Could not clear the pinned theme for user %s", user.id, exc_info=True
            )
    if not cleared:
        # Says what the stores DO hold, so "nothing was pinned" and "we looked
        # in the wrong place" stop looking the same from the log.
        _LOGGER.info(
            "No pinned profile themes to clear (frontend user-store keys seen: %s)",
            sorted(seen_keys),
        )
    return cleared
