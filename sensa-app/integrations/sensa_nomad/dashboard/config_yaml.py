"""Managed dashboard YAML + `configuration.yaml` + HA sidebar/panel administration.

Split out of the old monolithic `dashboard.py`. This half never touches the card
builders or the dashboard device state; it owns the on-disk and HA-storage side:
writing the managed dashboard YAML atomically, maintaining the SENSA MANAGED block
in `configuration.yaml`, and ordering / hiding / removing HA's own panels.

Kept free of `_dashboard_devices` / `_dashboard_hass` on purpose: those globals are
rebound by `set_dashboard_devices` and patched by the tests on the package module
itself, so their readers must stay in the package root.
"""
from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path
from typing import Any

import yaml
from homeassistant.core import HomeAssistant

from .._io import (
    atomic_write_text as _atomic_write_text,
)
from .._io import (
    forget_generated_checksum as _forget_generated_checksum,
)
from .._io import (
    record_generated_checksum as _record_generated_checksum,
)
from ._constants import (
    _LEGACY_DASHBOARD_FILES,
    _LEGACY_DASHBOARD_KEYS,
    DASHBOARD_DIR,
    DASHBOARDS,
    DEFAULT_PANEL_PATH,
    HA_DEFAULT_HIDDEN_PANELS,
    MANAGED_BLOCK_END,
    MANAGED_BLOCK_START,
    PANELS,
    SETTINGS_PANEL_PATH,
    TABLET_USER_DEFAULT_PANEL,
)

_LOGGER = logging.getLogger(__name__)

def _write_dashboard_yaml(config_dir: str) -> None:
    """Write all managed Sensa dashboard YAML files atomically.

    Each file is written via `_atomic_write_text`; a crash mid-write
    leaves the old YAML in place instead of producing a half-baked
    file that Lovelace would fail to parse.
    """
    dashboard_dir = Path(config_dir) / DASHBOARD_DIR
    dashboard_dir.mkdir(parents=True, exist_ok=True)

    # Sweep obsolete dashboard files before writing the current set.
    # Lovelace ignores files not registered in configuration.yaml, but
    # leaving them on disk confuses cold-start triage and clutters the
    # backup. Best-effort â€” failures here don't block the regen.
    for stale in _LEGACY_DASHBOARD_FILES:
        stale_path = dashboard_dir / stale
        # Always prune the checksum entry, even if the file is already
        # gone â€” otherwise the integrity sensor keeps reporting the
        # absent file as "missing" forever. This was the regression
        # behind "file integrity looks crippled": deleting nomad-test
        # .yaml left its checksum behind so every Run Check flagged a
        # missing generated file.
        _forget_generated_checksum(config_dir, (DASHBOARD_DIR / stale).as_posix())
        if stale_path.exists():
            try:
                stale_path.unlink()
                _LOGGER.info("Removed stale Sensa dashboard YAML: %s", stale_path)
            except OSError:
                _LOGGER.debug(
                    "Could not remove stale Sensa dashboard YAML %s",
                    stale_path, exc_info=True,
                )

    # Deferred: the registry lives in the package root (it names the dashboard
    # builders). Importing it at module scope would be a cycle, since the root
    # imports this module.
    from . import DASHBOARD_BUILDERS

    for dash in DASHBOARDS:
        builder = DASHBOARD_BUILDERS.get(dash["filename"])
        if not builder:
            continue
        dashboard_path = dashboard_dir / dash["filename"]
        # Isolate per-dashboard failures: a broken builder for one
        # dashboard must not abort the loop and leave the other
        # dashboards with stale YAML. Surface the failure as a
        # one-card dashboard so the customer / dev can see exactly
        # which file is broken and why instead of getting a silent
        # missing-dashboard or HA's generic "Configuration Error".
        try:
            dashboard_data = builder()
        except Exception as exc:
            _LOGGER.exception(
                "Sensa dashboard builder %r raised; writing diagnostic placeholder",
                dash["filename"],
            )
            dashboard_data = {
                "title": dash.get("title", dash["filename"]),
                "views": [{
                    "title": "Build failed",
                    "icon": "mdi:alert-circle-outline",
                    "cards": [{
                        "type": "markdown",
                        "content": (
                            f"## {dash['filename']} failed to build\n\n"
                            f"**{type(exc).__name__}**: {exc}\n\n"
                            "```\n"
                            f"{traceback.format_exc()}"
                            "```\n"
                        ),
                    }],
                }],
            }
        dashboard_yaml = yaml.safe_dump(
            dashboard_data,
            sort_keys=False,
            allow_unicode=True,
        )
        _atomic_write_text(dashboard_path, dashboard_yaml)
        _record_generated_checksum(config_dir, (DASHBOARD_DIR / dash["filename"]).as_posix(), dashboard_yaml)
        _LOGGER.debug("Sensa dashboard YAML written to %s", dashboard_path)


# Lovelace dashboards now superseded by the `panel_custom` panels. They
# keep generating (so the demo always has a working URL fallback) but
# drop out of the sidebar â€” the `sensa-*` panels are the visible nav.
_PANEL_REPLACED_DASHBOARD_KEYS = {
    "nomad-habitation", "nomad-cab", "nomad-mobile", "nomad-settings",
}


def _build_managed_dashboard_entries(indent: str = "    ") -> str:
    """Return YAML entries for all managed dashboards.

    Dashboards superseded by a panel are kept as URL-only fallbacks
    (`show_in_sidebar: false`); the rest stay visible.
    """
    entries = ""
    for dash in DASHBOARDS:
        filename = (DASHBOARD_DIR / dash["filename"]).as_posix()
        in_sidebar = (
            "false" if dash["key"] in _PANEL_REPLACED_DASHBOARD_KEYS else "true"
        )
        entries += (
            f"{indent}{dash['key']}:\n"
            f"{indent}  mode: yaml\n"
            f"{indent}  title: {dash['title']}\n"
            f"{indent}  icon: {dash['icon']}\n"
            f"{indent}  show_in_sidebar: {in_sidebar}\n"
            f"{indent}  require_admin: false\n"
            f"{indent}  filename: {filename}\n"
        )
    return entries


def _build_managed_block() -> str:
    """Return the managed dashboard registration block."""
    return (
        f"{MANAGED_BLOCK_START}\n"
        f"lovelace:\n"
        f"  mode: storage\n"
        f"  dashboards:\n"
        f"{_build_managed_dashboard_entries(indent='    ')}"
        f"{MANAGED_BLOCK_END}\n"
    )


def _replace_existing_managed_block(raw: str) -> str | None:
    """Replace an existing Sensa managed block if present."""
    pattern = re.compile(
        rf"{re.escape(MANAGED_BLOCK_START)}.*?{re.escape(MANAGED_BLOCK_END)}\n?",
        re.DOTALL,
    )
    if not pattern.search(raw):
        return None
    return pattern.sub(_build_managed_block(), raw)


def _remove_existing_sensa_dashboard_entry(text: str) -> str:
    """Remove all Sensa dashboard entries from a dashboards block."""
    dashboard_keys = set(_LEGACY_DASHBOARD_KEYS)
    lines = text.splitlines()
    result: list[str] = []
    skip = False
    entry_indent = 0

    for line in lines:
        if not skip:
            match = re.match(r"^(\s*)(nomad-\S+):\s*$", line)
            if match and match.group(2) in dashboard_keys:
                skip = True
                entry_indent = len(match.group(1))
                continue
            result.append(line)
            continue

        current_indent = len(line) - len(line.lstrip(" "))
        if line.strip() and current_indent <= entry_indent:
            skip = False
            result.append(line)

    return "\n".join(result) + ("\n" if text.endswith("\n") else "")


def _insert_under_dashboards(raw: str) -> str | None:
    """Insert or replace the Sensa dashboard entry under an existing dashboards block."""
    dashboards_match = re.search(r"^(?P<indent>\s*)dashboards:\s*$", raw, re.MULTILINE)
    if not dashboards_match:
        return None

    cleaned = _remove_existing_sensa_dashboard_entry(raw)

    dashboards_match = re.search(r"^(?P<indent>\s*)dashboards:\s*$", cleaned, re.MULTILINE)
    if not dashboards_match:
        return None

    dashboards_indent = dashboards_match.group("indent")
    entry_indent = dashboards_indent + "  "
    entry = _build_managed_dashboard_entries(indent=entry_indent)

    insert_pos = dashboards_match.end()
    return cleaned[:insert_pos] + "\n" + entry + cleaned[insert_pos:]


def _insert_under_lovelace(raw: str) -> str | None:
    """Insert a dashboards block under an existing lovelace block."""
    lovelace_match = re.search(r"^(?P<indent>\s*)lovelace:\s*$", raw, re.MULTILINE)
    if not lovelace_match:
        return None

    block = (
        "  dashboards:\n"
        f"{_build_managed_dashboard_entries(indent='    ')}"
    )

    insert_pos = lovelace_match.end()
    return raw[:insert_pos] + "\n" + block + raw[insert_pos:]


def _update_configuration_yaml(config_dir: str) -> bool:
    """Ensure configuration.yaml reflects the managed dashboard set.

    The frontend is fully on `panel_custom` panels now, so `DASHBOARDS`
    is empty and there's nothing to register â€” instead, strip any
    managed block a previous (Lovelace) version left behind. This is
    the one-shot migration off Lovelace; idempotent once the block is
    gone. (Block removal needs an HA restart to take effect, since
    configuration.yaml is read at boot.)

    Returns True if configuration.yaml changed.
    """
    config_path = Path(config_dir) / "configuration.yaml"
    raw = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

    if not DASHBOARDS:
        # Panels-only: remove any leftover managed block + collapse the
        # now-empty lovelace/dashboards scaffolding.
        cleaned = _strip_managed_lovelace(raw)
        if cleaned != raw:
            _atomic_write_text(config_path, cleaned)
            _LOGGER.info(
                "Removed legacy Sensa Lovelace dashboard block from %s "
                "(panels-only). Restart HA to drop the old dashboards "
                "from the sidebar.", config_path,
            )
            return True
        return False

    replaced = _replace_existing_managed_block(raw)
    if replaced is not None:
        if replaced != raw:
            _atomic_write_text(config_path, replaced)
            _LOGGER.debug("Sensa managed dashboard block replaced in %s", config_path)
            return True
        return False

    if re.search(r"^\s*dashboards:\s*$", raw, re.MULTILINE) and re.search(
        r"^\s*lovelace:\s*$", raw, re.MULTILINE
    ):
        updated = _insert_under_dashboards(raw)
        if updated is not None and updated != raw:
            _atomic_write_text(config_path, updated)
            _LOGGER.debug(
                "Sensa dashboard entry inserted under existing dashboards block in %s",
                config_path,
            )
            return True
        return False

    if re.search(r"^\s*lovelace:\s*$", raw, re.MULTILINE):
        updated = _insert_under_lovelace(raw)
        if updated is not None and updated != raw:
            _atomic_write_text(config_path, updated)
            _LOGGER.debug(
                "Sensa dashboards block inserted under existing lovelace block in %s",
                config_path,
            )
            return True
        return False

    suffix = "" if raw.endswith("\n") or raw == "" else "\n"
    updated = raw + suffix + _build_managed_block()
    if updated != raw:
        _atomic_write_text(config_path, updated)
        _LOGGER.debug("Sensa managed lovelace block appended to %s", config_path)
        return True

    return False


def _create_or_update_sensa_settings_dashboard(config_dir: str) -> bool:
    """Create or update all Sensa-managed dashboard artifacts.

    Returns True if configuration.yaml changed.
    """
    _write_dashboard_yaml(config_dir)
    return _update_configuration_yaml(config_dir)


async def async_create_or_update_sensa_settings_dashboard(
    hass: HomeAssistant,
) -> bool:
    """Reconcile the managed dashboard artifacts.

    Panels-only now: this writes no Lovelace YAML (DASHBOARDS is empty)
    and strips any leftover managed block from configuration.yaml â€” i.e.
    it's the one-shot migration off Lovelace. Kept under the same name +
    call sites (setup + regen) so the regen flow and existing tests stay
    wired; the default landing surface is the Habitation panel, set via
    `userData.default_panel` in `async_set_sidebar_order`, not the Lovelace
    `frontend.set_default_dashboard` service.
    """
    config_dir = hass.config.config_dir
    return await hass.async_add_executor_job(
        _create_or_update_sensa_settings_dashboard,
        config_dir,
    )


def _build_sidebar_userdata(
    existing_sidebar: dict[str, Any],
    panel_paths: list[str],
    legacy_ids: list[str],
) -> dict[str, Any]:
    """Build the frontend `sidebar` user-data value: Sensa panels first, HA's
    built-in dashboards hidden. Merges onto any existing sidebar dict so we
    don't drop unrelated keys."""
    existing_order = existing_sidebar.get("panelOrder", [])
    # Drop any prior Sensa panel paths and the legacy nomad-* dashboard keys
    # from the order, then prepend the panel paths.
    remove_set = set(panel_paths) | set(legacy_ids)
    filtered = [p for p in existing_order if p not in remove_set]
    return {
        **existing_sidebar,
        "panelOrder": panel_paths + filtered,
        # Hide HA's built-in dashboards. Set authoritatively (not a union) so
        # this list is the single source of truth: dropping a panel from
        # HA_DEFAULT_HIDDEN_PANELS un-hides it on the next regen. NB HA
        # force-shows the CURRENT default panel even if listed here, so Overview
        # (lovelace) only actually hides once default_panel is a Sensa panel.
        "hiddenPanels": sorted(HA_DEFAULT_HIDDEN_PANELS),
    }


async def _async_build_user_default_panels(
    hass: HomeAssistant,
) -> dict[str, str]:
    """Map user_id -> default panel path for EVERY appliance login.

    Resolves each HA user's homeassistant-provider username. The tablet users
    (hab/cab/mobile) get their own panel; EVERY other password login â€” the
    owner AND the `sensa_support` admin backdoor (and any stray account) â€” gets
    the Habitation default. This matters because the appliance lockdown
    UNREGISTERS HA's `lovelace` (Overview) panel: HA has no instance-wide
    default-panel setting (only this per-user one), so any user left without a
    Sensa default would resolve `/` to the now-missing lovelace and hit a 404.
    Routing every login onto a Sensa panel keeps `/` working for all of them
    (support can still reach `/config` etc. by URL â€” they're an HA admin).

    Users with NO homeassistant credential (HA system/token-only accounts, e.g.
    Supervisor) are left untouched â€” they never open `/` interactively.
    `async_set_sidebar_order` then writes each mapping through the frontend
    store API.
    """
    from ..const import APPLIANCE_ADMIN_USERNAME

    overrides: dict[str, str] = {}
    try:
        users = await hass.auth.async_get_users()
    except Exception:
        _LOGGER.debug("Could not enumerate users for default panels", exc_info=True)
        return overrides

    for user in users:
        username = None
        for cred in getattr(user, "credentials", []):
            if cred.auth_provider_type == "homeassistant":
                username = (cred.data or {}).get("username")
                break
        # No password login â†’ an HA system/token-only user; don't touch it.
        if username is None:
            continue
        # The `admin` login manages the box â†’ lands on Settings. Tablet users â†’
        # their own panel. Everyone else (support / unknown) â†’ the Habitation
        # default so nobody lands on the removed Overview.
        if username == APPLIANCE_ADMIN_USERNAME:
            overrides[user.id] = SETTINGS_PANEL_PATH
        else:
            overrides[user.id] = TABLET_USER_DEFAULT_PANEL.get(
                username, DEFAULT_PANEL_PATH
            )
    return overrides


async def async_set_sidebar_order(hass: HomeAssistant) -> None:
    """Set each appliance user's landing dashboard + hidden sidebar via HA's
    frontend user-data STORE API â€” NOT by hand-writing .storage.

    Hand-writing `frontend.user_data_<id>` is unreliable: HA lazy-loads that
    store and caches it in memory, and any later `set_user_data` (the frontend
    re-persists `sidebar` on a panel reorder, or HA flushes on stop) rewrites
    the file from the in-memory copy, CLOBBERING hand-written keys not in it â€”
    which is why a hand-written `core.default_panel` silently vanished while
    `sidebar` (which the frontend re-persists) survived. `async_set_item` updates
    the in-memory dict + the file + live subscribers, so it sticks and takes
    effect on the next `/` navigation. Runs appliance-only (the sole caller gates
    on deployment_mode).
    """
    from homeassistant.components.frontend import storage as frontend_storage

    user_default_panels = await _async_build_user_default_panels(hass)
    if not user_default_panels:
        # The smoking gun for "ran too early": appliance is confirmed but the
        # baked-in owner/hab/cab/mobile users don't exist yet (build script
        # creates them after the integration first set up). The licence poller
        # re-applies this, so it self-heals â€” but log it so the device log
        # distinguishes "ran, no users" from "never ran".
        _LOGGER.info(
            "Appliance: no tablet/admin users resolved yet â€” default panels "
            "NOT applied (will retry on the next poll)"
        )
        return

    panel_paths = [p["path"] for p in PANELS]
    legacy_ids = list(_LEGACY_DASHBOARD_KEYS)

    changed = 0
    for user_id, panel in user_default_panels.items():
        try:
            store = await frontend_storage.async_user_store(hass, user_id)
            existing = getattr(store, "data", None) or {}
            # Landing dashboard â€” the "core" key's `default_panel` (what
            # getDefaultPanelUrlPath reads as hass.userData.default_panel).
            core = {**(existing.get("core") or {}), "default_panel": panel}
            # Drop the stale, never-read top-level key a prior build hand-wrote.
            core.pop("defaultPanel", None)
            # Sidebar order + hidden HA dashboards.
            sidebar = _build_sidebar_userdata(
                existing.get("sidebar") or {}, panel_paths, legacy_ids
            )
            # Dirty-check: this runs on the 15-min licence poller too, so write
            # ONLY when something actually changed. Otherwise every poll would
            # rewrite each user's store to disk (flash write-amplification) AND
            # push a userData update to every live client â€” re-applying the
            # sidebar/default on kiosks every 15 min for no reason.
            wrote = False
            if core != (existing.get("core") or {}):
                await store.async_set_item("core", core)
                wrote = True
            if sidebar != (existing.get("sidebar") or {}):
                await store.async_set_item("sidebar", sidebar)
                wrote = True
            if wrote:
                changed += 1
                _LOGGER.info(
                    "Set default panel + sidebar for user %s â†’ %s", user_id, panel
                )
        except Exception:
            _LOGGER.warning(
                "Could not set default panel / sidebar for user %s",
                user_id, exc_info=True,
            )
    # Only log the summary when we actually wrote â€” keeps the recurring poll
    # quiet once settled while still marking the passes that did work.
    if changed:
        _LOGGER.info(
            "Appliance default panels applied for %d user(s): %s",
            changed, user_default_panels,
        )


async def async_remove_ha_builtin_dashboards(hass: HomeAssistant) -> None:
    """Appliance kiosk lockdown: UNREGISTER HA's built-in dashboard panels so a
    customer can't reach them by URL (``/energy``, ``/history``, â€¦) OR by sidebar.

    Hiding a panel (``sidebar.hiddenPanels``) only removes its sidebar icon â€” the
    URL still resolves. HA's ``IndexView.resolve`` rejects any top-level path
    whose panel is not in ``hass.data[DATA_PANELS]``, so removing the panel is
    what actually blocks the URL. Reuses ``HA_DEFAULT_HIDDEN_PANELS`` as the
    single source of truth so hidden == removed can never drift.

    Idempotent â€” safe to call on every panel (re)registration. Sensa panels and
    ``config``/``developer-tools`` are untouched (the latter are already
    HA-admin-gated, so the non-admin owner/kiosk users can't reach them; the
    deactivated ``sensa_support`` admin acceptably loses the built-in dashboards
    but keeps config + Supervisor). Appliance-only (the sole caller gates on
    deployment_mode).
    """
    from homeassistant.components import frontend

    removed: list[str] = []
    for path in sorted(HA_DEFAULT_HIDDEN_PANELS):
        try:
            frontend.async_remove_panel(hass, path, warn_if_unknown=False)
            removed.append(path)
        except Exception:
            _LOGGER.debug(
                "Could not remove built-in panel %s", path, exc_info=True
            )
    _LOGGER.info(
        "Appliance lockdown: removed %d HA built-in dashboards: %s",
        len(removed), removed,
    )


def _strip_managed_lovelace(raw: str) -> str:
    """Return `raw` configuration.yaml text with the Sensa managed
    Lovelace block (and any leftover legacy dashboard entries + the
    now-empty lovelace/dashboards scaffolding) removed. Pure string
    transform â€” callers decide whether to write it back."""
    # First try: remove the entire managed block if present.
    pattern = re.compile(
        rf"{re.escape(MANAGED_BLOCK_START)}.*?{re.escape(MANAGED_BLOCK_END)}\n?",
        re.DOTALL,
    )
    cleaned = pattern.sub("", raw)

    # If no managed block, remove any bare legacy dashboard entries.
    if cleaned == raw:
        cleaned = _remove_existing_sensa_dashboard_entry(raw)

    # Collapse empty lovelace/dashboards scaffolding left behind.
    cleaned = re.sub(
        r"^(\s*)dashboards:\s*\n(?=\s*\n|\s*[a-z#]|\Z)",
        "", cleaned, flags=re.MULTILINE,
    )
    cleaned = re.sub(
        r"^lovelace:\s*\n\s*mode:\s*storage\s*\n(?=\s*\n|\s*[a-z#]|\Z)",
        "", cleaned, flags=re.MULTILINE,
    )
    cleaned = re.sub(
        r"^lovelace:\s*\n(?=\s*\n|\s*[a-z#]|\Z)",
        "", cleaned, flags=re.MULTILINE,
    )
    return cleaned


def _remove_sensa_settings_dashboard(config_dir: str) -> None:
    """Remove all Sensa dashboard YAML files and configuration.yaml entries."""
    # Remove every dashboard YAML file we ever shipped so an uninstall
    # (or the panels migration) doesn't leave orphans behind.
    dashboard_dir = Path(config_dir) / DASHBOARD_DIR
    for filename in _LEGACY_DASHBOARD_FILES:
        dashboard_path = dashboard_dir / filename
        if dashboard_path.exists():
            dashboard_path.unlink()
            _LOGGER.info("Removed Sensa dashboard YAML: %s", dashboard_path)

    # Remove dir if empty
    if dashboard_dir.exists() and not any(dashboard_dir.iterdir()):
        dashboard_dir.rmdir()

    config_path = Path(config_dir) / "configuration.yaml"
    if not config_path.exists():
        return
    raw = config_path.read_text(encoding="utf-8")
    cleaned = _strip_managed_lovelace(raw)
    if cleaned != raw:
        _atomic_write_text(config_path, cleaned)
        _LOGGER.info("Removed Sensa dashboard entry from configuration.yaml")


async def async_remove_sensa_settings_dashboard(hass: HomeAssistant) -> None:
    """Remove all Nomad dashboards and their configuration.yaml entries."""
    config_dir = hass.config.config_dir
    await hass.async_add_executor_job(_remove_sensa_settings_dashboard, config_dir)

    # Restore default dashboard to HA Overview
    try:
        await hass.services.async_call(
            "frontend",
            "set_default_dashboard",
            {"dashboard_id": None},
            blocking=True,
        )
        _LOGGER.info("Restored default dashboard to Overview")
    except Exception:
        _LOGGER.debug("Could not restore default dashboard", exc_info=True)
