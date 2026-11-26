"""Shared IO helpers for Sensa Nomad file writes.

Both the dashboard pipeline and the modes/scenes pipeline write files
into the HA config directory. Both must be crash-safe: a half-written
`configuration.yaml` wedges HA on the next start, and a half-written
scenes YAML file produces silent mode-activation failures. Both
pipelines want the same write semantics, so the helper lives here and
they both import it.

Kept as its own tiny module so it can be unit-tested without pulling
in the whole dashboard or modes machinery.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, content: str) -> None:
    """Write `content` to `path` atomically.

    Strategy: write to a sibling temp file in the same directory (same
    filesystem, so the final rename is a single-inode atomic swap on
    POSIX and Windows alike), flush + fsync, then `os.replace` onto
    the target. A crash mid-write leaves either the old file intact or
    the complete new file — never a half-written YAML.

    The parent directory is also fsynced so the rename survives a
    kernel-level power loss. `os.replace` (not `os.rename`) because
    it's defined to overwrite existing files on Windows.

    fsync failures on tmpfs / Windows-share mounts are swallowed — the
    rename itself still gives us best-effort atomicity and the fsync
    is only a belt-and-braces durability guarantee.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    tmp_name: str | None = None
    try:
        # `delete=False` + explicit replace because NamedTemporaryFile
        # would delete the file on close, before we get a chance to
        # rename it into place.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(parent),
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            try:
                os.fsync(tmp.fileno())
            except OSError:
                pass
        os.replace(tmp_name, str(path))
        tmp_name = None
        try:
            fd = os.open(str(parent), os.O_RDONLY)
            os.fsync(fd)
        except OSError:
            pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass


def record_generated_checksum(config_dir: str, relative_path: str, content: str) -> None:
    """Record the SHA-256 hash of a just-written generated file.

    relative_path is relative to config_dir (e.g. "nomad/dashboards/nomad-habitation.yaml").
    Stored in {config_dir}/nomad/.sensa_checksums.json so SensaIntegritySensor can later
    detect manual edits by comparing on-disk hashes against these stored values.
    """
    checksums_path = Path(config_dir) / "nomad" / ".sensa_checksums.json"
    checksums_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing: dict[str, str] = (
            json.loads(checksums_path.read_text(encoding="utf-8"))
            if checksums_path.exists()
            else {}
        )
    except Exception:  # noqa: BLE001
        existing = {}
    existing[relative_path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    atomic_write_text(checksums_path, json.dumps(existing, indent=2))


def forget_generated_checksum(config_dir: str, relative_path: str) -> None:
    """Drop a generated file's checksum entry from the sidecar.

    Call this whenever a generated file is *intentionally* deleted
    (e.g. a dashboard we no longer ship). Without it the entry lingers
    in `.sensa_checksums.json` and `SensaIntegritySensor` reports the
    now-absent file as "missing" — a false integrity failure. No-op
    when the sidecar or the entry doesn't exist.
    """
    checksums_path = Path(config_dir) / "nomad" / ".sensa_checksums.json"
    if not checksums_path.exists():
        return
    try:
        existing: dict[str, str] = json.loads(
            checksums_path.read_text(encoding="utf-8")
        )
    except Exception:  # noqa: BLE001
        return
    if relative_path in existing:
        del existing[relative_path]
        atomic_write_text(checksums_path, json.dumps(existing, indent=2))
