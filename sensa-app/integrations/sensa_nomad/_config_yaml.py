"""Shared `configuration.yaml` managed-block registration.

Three generated-YAML pipelines (`modes.py` scenes, `automations.py` curated
Instincts, `routines.py` customer Routines) each need to tell HA to load a
Sensa-managed file via a **labelled split-configuration key**
(`<domain> <label>: !include <file>`), not a bare `<domain>: !include <file>`.
A bare key duplicates HA's own default top-level key for that domain and HA's
YAML loader discards one on a collision (see `modes.py`'s
`_build_managed_scene_block` docstring for the incident this caused with
scenes). The labelled form merges into the domain instead of colliding.

This was independently duplicated near-verbatim in `modes.py` and
`automations.py` (insert/replace/strip logic, ~40 lines each) before being
extracted here — a second write-up of the same predicate, the exact pattern
CLAUDE.md calls out as this repo's recurring bug. Both now call
`register_split_key`/`remove_split_key`; a third caller (`routines.py`) adds
no further duplication.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ._io import atomic_write_text

_LOGGER = logging.getLogger(__name__)


def _build_block(begin_marker: str, end_marker: str, domain: str, label: str, file_path: str) -> str:
    return f"{begin_marker}\n{domain} {label}: !include {file_path}\n{end_marker}\n"


def register_split_key(
    config_dir: str,
    *,
    begin_marker: str,
    end_marker: str,
    domain: str,
    label: str,
    file_path: str,
    legacy_patterns: list[re.Pattern[str]] | None = None,
) -> bool:
    """Idempotently insert/refresh a managed `<domain> <label>: !include
    <file_path>` block in `{config_dir}/configuration.yaml`, replacing
    whatever currently sits between `begin_marker`/`end_marker`.

    `legacy_patterns`, if given, are stripped from the file on every call —
    for migrating away from an earlier, colliding bare-key form (e.g. scenes'
    old bare `scene: !include nomad/scenes.yaml`). Most callers pass nothing.

    Returns True if `configuration.yaml` changed (a first-time registration
    needs an HA restart to load the new include; after that, the domain's own
    `<domain>.reload` service picks up content edits live).
    """
    config_path = Path(config_dir) / "configuration.yaml"
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    raw = original

    for pattern in legacy_patterns or []:
        raw = pattern.sub("", raw)

    managed_pattern = re.compile(
        rf"{re.escape(begin_marker)}.*?{re.escape(end_marker)}\n?", re.DOTALL,
    )
    block = _build_block(begin_marker, end_marker, domain, label, file_path)
    if managed_pattern.search(raw):
        updated = managed_pattern.sub(block, raw)
    else:
        suffix = "" if raw.endswith("\n") or raw == "" else "\n"
        updated = raw + suffix + block

    if updated == original:
        return False
    atomic_write_text(config_path, updated)
    _LOGGER.debug(
        "Nomad %s: managed block for %r updated in %s", domain, label, config_path,
    )
    return True


def remove_split_key(
    config_dir: str, *, begin_marker: str, end_marker: str,
) -> bool:
    """Strip a managed block from `configuration.yaml` (uninstall path).
    Returns True if the file changed."""
    config_path = Path(config_dir) / "configuration.yaml"
    if not config_path.exists():
        return False
    raw = config_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(begin_marker)}.*?{re.escape(end_marker)}\n?", re.DOTALL,
    )
    stripped = pattern.sub("", raw)
    if stripped == raw:
        return False
    atomic_write_text(config_path, stripped)
    return True
