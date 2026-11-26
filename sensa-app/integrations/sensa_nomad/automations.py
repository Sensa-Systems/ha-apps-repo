"""Generate HA automations from the curated catalog.

Mirrors the scenes pipeline in `modes.py`: build the enabled automations from
`instincts_catalog`, atomically write them to `nomad/automations.yaml`, and
register a managed block in `configuration.yaml`. The block uses HA's
split-configuration key **`automation sensa: !include nomad/automations.yaml`**,
which merges into the `automation` domain alongside the customer's default
`automation: !include automations.yaml` (no duplicate top-level key).

Generated automations call the `sensa_nomad.log` service, so toggling one on the
Automations tab makes its messages appear in the Sensa log.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ._config_yaml import register_split_key, remove_split_key
from ._io import atomic_write_text, record_generated_checksum
from .const import (
    AUTOMATIONS_FILE,
    AUTOMATIONS_MANAGED_BEGIN,
    AUTOMATIONS_MANAGED_END,
)
from .instincts_catalog import build_enabled_automations

_LOGGER = logging.getLogger(__name__)


def _write_automations_yaml(config_dir: str, automations: list[dict[str, Any]]) -> None:
    """Serialise the automations list and atomically write it to
    `{config_dir}/nomad/automations.yaml`."""
    path = Path(config_dir) / AUTOMATIONS_FILE
    content = yaml.safe_dump(automations, sort_keys=False, allow_unicode=True)
    atomic_write_text(path, content)
    record_generated_checksum(config_dir, AUTOMATIONS_FILE, content)
    _LOGGER.debug("Nomad automations: YAML written to %s (%d entries)",
                  path, len(automations))


def _ensure_registration(config_dir: str) -> bool:
    """Make sure configuration.yaml carries our managed automations block.
    Returns True if the file changed (a first-time registration needs an HA
    restart to load the new include)."""
    return register_split_key(
        config_dir,
        begin_marker=AUTOMATIONS_MANAGED_BEGIN,
        end_marker=AUTOMATIONS_MANAGED_END,
        domain="automation",
        label="sensa",
        file_path=AUTOMATIONS_FILE,
    )


def _remove_managed_block(config_dir: str) -> bool:
    """Strip our managed block from configuration.yaml (uninstall path)."""
    return remove_split_key(
        config_dir,
        begin_marker=AUTOMATIONS_MANAGED_BEGIN,
        end_marker=AUTOMATIONS_MANAGED_END,
    )


async def async_create_or_update_sensa_automations(
    hass: HomeAssistant, entry: ConfigEntry,
) -> bool:
    """Rebuild `nomad/automations.yaml` from the enabled curated automations and
    ensure configuration.yaml is registered. Returns True if configuration.yaml
    changed (first-time registration needs a restart; afterwards
    `async_reload_automations` picks up edits live)."""
    config_dir = hass.config.config_dir
    automations = build_enabled_automations(hass, dict(entry.options))

    def _write_all() -> bool:
        _write_automations_yaml(config_dir, automations)
        return _ensure_registration(config_dir)

    return await hass.async_add_executor_job(_write_all)


async def async_reload_automations(hass: HomeAssistant) -> None:
    """Ask HA to re-read automations without a full restart."""
    try:
        await hass.services.async_call("automation", "reload", {}, blocking=True)
        _LOGGER.debug("Nomad automations: automation.reload called")
    except Exception:
        _LOGGER.debug(
            "Nomad automations: automation.reload failed — will refresh on the "
            "next HA restart", exc_info=True,
        )


async def async_remove_sensa_automations(hass: HomeAssistant) -> None:
    """Uninstall path: drop the managed block so configuration.yaml doesn't
    include an automations file we no longer generate."""
    config_dir = hass.config.config_dir
    await hass.async_add_executor_job(_remove_managed_block, config_dir)
