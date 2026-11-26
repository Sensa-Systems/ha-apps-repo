"""JSON file-backed registry store.

The store is hit concurrently — the ESPHome subnet scanner fans out 20 worker
threads that each upsert a discovery row, and HTTP route handlers read on
request. Without a writer lock + atomic file replace, two saves can interleave
bytes and leave `<valid JSON><tail from previous write>` on disk, which
``json.load`` then rejects with "Extra data". The loader recovers from that by
parsing the valid prefix and quarantining the corrupted tail, so a botched
write never bricks the add-on.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Where the registry lives, overridable so a Container appliance can put it
# somewhere Home Assistant's backup can see.
#
# This file is the customer's whole device setup: every discovered and
# registered device with its display name, category and config. On the Container
# appliance the App's `/data` is its own Docker volume that HA Core cannot see,
# so a backup restored onto fresh hardware came back with Home Assistant's
# config and NONE of the van's devices (verified on-device 2026-08-08, see
# docs/system-design/backups.md § 3). Pointing this at a path under `/config`
# puts it inside the backup with no second artefact to manage.
#
# The default stays `/data` so a BYO/HAOS install, where Supervisor already
# captures the add-on's `/data`, is unaffected.
DEFAULT_REGISTRY_PATH = Path(
    os.environ.get("SENSA_REGISTRY_PATH", "/data/sensa/device_registry.json")
)

_registry_path: Path = DEFAULT_REGISTRY_PATH
# Serialises an individual `save_registry` write so the file is never torn.
_write_lock = threading.Lock()
# Serialises a whole load-modify-save SEQUENCE so two concurrent mutators
# can't each load the same snapshot, modify their own copy, and have the
# later save silently drop the earlier writer's change. `_write_lock` alone
# only prevents file *corruption*, not lost *updates*. This matters now that
# the add-on serves requests on multiple threads (a device register can run
# concurrently with the 20-thread subnet scanner) — without it, registering a
# device during an active scan can vanish under a last-writer-wins overwrite.
# Re-entrant so a future transaction-wrapped helper can call another safely.
_mutation_lock = threading.RLock()


@contextmanager
def registry_transaction():
    """Hold the mutation lock across a load-modify-save sequence.

    Wrap any function that reads the registry, mutates it, and writes it
    back so the read and write are atomic with respect to other mutators.
    """
    with _mutation_lock:
        yield


def set_registry_path(path: Path) -> None:
    """Override the registry file path (useful for testing or alternate installs)."""
    global _registry_path
    _registry_path = path


def get_registry_path() -> Path:
    return _registry_path


def _default_registry() -> dict:
    return {
        "discoveries": [],
        "devices": [],
    }


def _recover_from_corruption(path: Path, raw: str, err: json.JSONDecodeError) -> dict:
    """Try to salvage the valid JSON prefix when the file got an interleaved
    write. Falls back to a fresh-empty registry if even that fails.

    The corrupted file is renamed to ``<name>.corrupted-<unix_ts>`` so an
    operator can recover any orphaned tail manually if it matters; we never
    delete data silently."""
    timestamp = int(time.time())
    backup = path.with_suffix(path.suffix + f".corrupted-{timestamp}")
    try:
        path.replace(backup)
        _LOGGER.warning(
            "registry: %s was corrupted (%s); quarantined to %s",
            path, err, backup,
        )
    except OSError:
        _LOGGER.warning(
            "registry: %s was corrupted (%s); could not quarantine",
            path, err, exc_info=True,
        )

    try:
        decoder = json.JSONDecoder()
        recovered, _idx = decoder.raw_decode(raw)
        if isinstance(recovered, dict):
            recovered.setdefault("discoveries", [])
            recovered.setdefault("devices", [])
            _LOGGER.warning(
                "registry: recovered %d discoveries / %d devices from prefix",
                len(recovered.get("discoveries", [])),
                len(recovered.get("devices", [])),
            )
            save_registry(recovered)  # rewrite atomically so the next load is clean
            return recovered
    except (json.JSONDecodeError, ValueError):
        _LOGGER.warning("registry: prefix recovery failed, starting fresh", exc_info=True)

    fresh = _default_registry()
    save_registry(fresh)
    return fresh


def load_registry() -> dict:
    path = get_registry_path()
    if not path.exists():
        return _default_registry()
    with path.open("r", encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        return _recover_from_corruption(path, raw, err)


def save_registry(data: dict) -> None:
    path = get_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock + atomic rename: serialise writers within the process so two threads
    # can't open(w) the same file mid-flight, and write to a sibling temp file
    # that we then os.replace into place — a partial write never becomes a
    # readable registry file.
    with _write_lock:
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception:
            # Clean up the orphan temp file so we don't leak them on repeated
            # failures (full disk, permission flip, etc.).
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
