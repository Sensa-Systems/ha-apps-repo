"""A USB stick as a backup destination (backups-roadmap.md Phase 5).

The local copy dies with the card it sits on, which is the one failure backups
exist for. This is the first destination that leaves the vehicle, and it is the
one that needs no account, no subscription and no internet: plug a stick in and
every backup lands on it as well.

**Home Assistant does the work, we only tell it where.** A `LocalBackupAgent`
hands HA a path and HA writes the tar there itself, so there is no second copy
step, no progress reporting of our own, and encryption stays exactly as it is:
`protected` is a per-agent setting and HA re-encrypts per agent on upload
(`EncryptedBackupStreamer`), so the stick gets an encrypted copy without this
module handling a key.

**The agent only exists while a stick is mounted.** `async_get_backup_agents` is
re-run whenever the listener fires, so plugging one in makes the destination
appear and pulling it out makes it go away, with no restart. An agent that
pointed at an absent path would be worse than no agent: HA would treat every
scheduled backup as having a destination it could not write to.

Two things this module DELIBERATELY does not do:

* It does not mount anything. Mounting is the host's job (roadmap Phase 5 step
  1) and doing it from inside a container would need privileges this
  integration has no business holding.
* It does not delete a customer's own files. It reads and writes inside its own
  directory on the stick and nothing else, because a stick is somebody's
  property and may well have holiday photos on it.

**Container appliance only**, the same gate as the rest of the backup policy: on
HAOS the customer has Supervisor's own backup UI and on BYO it is their Home
Assistant, so adding a destination under either would be taking a decision that
is not ours.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .backup_config import is_container_appliance

_LOGGER = logging.getLogger(__name__)

# Where the host's Sensa media mounts appear inside this container.
#
# `/media/sensa`, NOT `/media`. The Home Assistant image ships its own
# `/media/cdrom`, `/media/floppy` and `/media/usb` directories, and an earlier
# revision of this scanned `/media` directly: on the test box it duly created a
# `sensa-backups` directory inside each of them and would have offered three
# phantom USB destinations backed by empty directories in the container's own
# filesystem (observed 2026-08-09). A dedicated subdirectory means everything
# under the root is a stick the host mounted for us and nothing else.
#
# Fails CLOSED when the compose bind is absent (a dev box, or a host without the
# overlay): the directory does not exist, so no media is found and no
# destination is offered.
MEDIA_ROOT = Path(os.environ.get("SENSA_MEDIA_ROOT", "/media/sensa"))

# Our own directory on the stick. Everything this module reads and writes lives
# under it: a customer's stick is not ours to tidy, and a backup agent that
# listed every tar on a shared stick would offer to restore somebody's unrelated
# archive.
BACKUP_DIRNAME = "sensa-backups"

# Where HA's "the agent list may have changed" callbacks are kept.
LISTENERS_KEY = "sensa_nomad_backup_agent_listeners"

def _mounted_media(root: Path = MEDIA_ROOT) -> list[Path]:
    """Every mounted removable medium, as directories under the media root.

    A DIRECTORY under the root is the signal, not a mount check: from inside a
    container the host's mount table is not ours to read, and the bind mount
    presents each mounted stick as a directory that appears and disappears.
    """
    try:
        return sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        # No media root at all is the normal state on a box with nothing
        # plugged in, and on every flavour that has no host plumbing.
        return []


def usb_backup_dirs(root: Path = MEDIA_ROOT) -> list[Path]:
    """Sensa backup directories on mounted sticks, writable ones only.

    A stick is only offered as a destination once it is genuinely usable, so a
    write-protected or full one never becomes a destination HA then fails to
    write to on every scheduled run.
    """
    out: list[Path] = []
    for medium in _mounted_media(root):
        target = medium / BACKUP_DIRNAME
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Read-only stick, or one we have no permission on. Not an error
            # worth shouting about: a customer may plug in anything.
            _LOGGER.debug("media %s is not writable, skipping", medium)
            continue
        if os.access(target, os.W_OK):
            out.append(target)
    return out


# Filesystems that cannot hold a file larger than 4 GB.
#
# FAT32 is what a USB stick is sold formatted as, so this is the DEFAULT case,
# not an edge one. A backup under 4 GB writes to it perfectly well, which is why
# a stick like this is still offered as a destination rather than refused: on
# the test box a full backup is around 1 GB and the customer would rightly be
# annoyed at being told their working stick was unusable. What must not happen
# is the silent version, where backups work for a year, quietly cross 4 GB and
# fail from then on with nothing having changed that the customer can see.
#
# `msdos` is FAT16 and worse; `exfat`, `ntfs`, `ext4` and the rest have no such
# limit and are left alone.
SMALL_FILE_FILESYSTEMS = frozenset({"vfat", "msdos"})

# The largest file these filesystems can hold, 4 GiB minus one byte.
SMALL_FILE_LIMIT_BYTES = 4 * 1024**3 - 1

_MOUNTINFO = Path("/proc/self/mountinfo")


def _unescape_mount_field(value: str) -> str:
    """Undo the octal escaping the kernel applies to mountinfo paths.

    A stick labelled "MY STICK" mounts at a path containing a space, which the
    kernel writes as a backslash-escaped octal code. Without this the lookup
    misses and the filesystem reads as unknown, which is the one answer that
    must not come from a parsing slip: unknown means no warning, and no warning
    is exactly the silent failure this exists to prevent.
    """
    if "\\" not in value:
        return value
    out = value
    for escape, char in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        out = out.replace(escape, char)
    return out


def filesystem_types(source: Path = _MOUNTINFO) -> dict[str, str]:
    """Mount point -> filesystem type, as this container can see it.

    Read from `/proc/self/mountinfo` rather than by asking the host: the stick is
    mounted by the host and propagated in (`rslave`), and the propagated mount
    appears here complete with its real type, verified on the test box:

        /media/sensa/SUNDATA rw,... - vfat /dev/sda1 rw,...

    Returns an empty map rather than raising if the file is missing or oddly
    shaped (a non-Linux dev machine, a future kernel). Callers treat "not known"
    as "no warning", because inventing a filesystem type would be worse than
    saying nothing.
    """
    out: dict[str, str] = {}
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        # `id parent maj:min root MOUNTPOINT opts... - FSTYPE source superopts`
        # The optional fields before the separator are why this splits on " - "
        # instead of counting columns.
        head, sep, tail = line.partition(" - ")
        if not sep:
            continue
        head_fields = head.split()
        tail_fields = tail.split()
        if len(head_fields) < 5 or not tail_fields:
            continue
        out[_unescape_mount_field(head_fields[4])] = tail_fields[0]
    return out


def small_file_warning(medium: Path, types: dict[str, str] | None = None) -> str:
    """A customer-facing warning for a stick that cannot hold a large backup.

    Empty string when there is nothing to say, including when the filesystem
    cannot be determined.
    """
    # `as_posix()`, not `str()`: on Windows the latter yields backslashes and
    # can never match a mountinfo path, so the tests would pass vacuously by
    # never finding a filesystem at all.
    fs = (types if types is not None else filesystem_types()).get(medium.as_posix(), "")
    if fs.lower() not in SMALL_FILE_FILESYSTEMS:
        return ""
    return (
        "This stick is formatted as FAT32, which cannot store a file over 4 GB. "
        "Backups work until they reach that size, then stop. "
        "Reformat the stick as exFAT to be safe."
    )


# Where local backups land, for the "how full is it" reading on the Local pill.
# The DIRECTORY is measured rather than the container root because that is the
# filesystem a backup actually consumes.
LOCAL_BACKUP_DIR = Path("/config/backups")


def media_capacity(medium: Path) -> dict[str, Any]:
    """How big a filesystem is and how full, or {} if it cannot be measured.

    Read with `statvfs` against the path as this container sees it. For a stick
    that is the device itself: the host mounts it and the bind propagates, so
    the figures are the real one's rather than the container filesystem's
    (verified with `df` reporting /dev/sda1 at /media/sensa/SUNDATA).

    Uses the space a NON-ROOT process could claim (`f_bavail`) as free, because
    filesystems reserve blocks and counting those would overstate what is
    actually left to write. Blocking, so callers hand it to an executor.
    """
    try:
        st = os.statvfs(medium)
    except (OSError, AttributeError, ValueError):
        # AttributeError covers Windows, which has no statvfs at all.
        return {}
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return {}
    used = max(0, total - free)
    return {
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": used,
        "percent_used": round(used * 100 / total),
    }


def destination_capacities(root: Path = MEDIA_ROOT) -> dict[str, dict[str, Any]]:
    """Capacity per destination KIND: what the pills report.

    Local is the filesystem holding `/config/backups`; USB is the first mounted
    stick. Keyed by kind rather than by agent id so the card can read it without
    knowing anything about agents. Blocking; hand it to an executor.
    """
    out: dict[str, dict[str, Any]] = {}
    # The backups directory does not exist until the first backup is written, and
    # a box that has never taken one is exactly when somebody asks whether there
    # is room. Its parent is the same filesystem, so it answers the same
    # question.
    local = media_capacity(LOCAL_BACKUP_DIR) or media_capacity(LOCAL_BACKUP_DIR.parent)
    if local:
        out["local"] = local
    media = _mounted_media(root)
    if media:
        usb = media_capacity(media[0])
        if usb:
            out["usb"] = usb
    return out


def _agent_class() -> Any:
    """Build the agent class against HA's backup component, or None.

    The class is defined INSIDE a function rather than at module level because
    it must subclass HA's `LocalBackupAgent`, and importing that at module level
    would make the backup integration a hard dependency of a module the
    integration loads on every flavour. Returns None when it is unavailable, and the platform then
    offers no agents at all rather than raising during setup.
    """
    try:
        from homeassistant.components.backup import (
            BackupAgentError,
            LocalBackupAgent,
        )
        from homeassistant.components.backup.models import BackupNotFound
        from homeassistant.components.backup.util import read_backup, suggested_filename
    except ImportError:  # pragma: no cover - real HA always has these
        return None

    class _SensaUsbBackupAgent(LocalBackupAgent):
        """Reads and writes Sensa backups inside one directory on one stick."""

        domain = "sensa_nomad"

        def __init__(self, hass: HomeAssistant, path: Path) -> None:
            super().__init__()
            self._hass = hass
            self._dir = path
            # The stick's own directory name, so two sticks are two distinct
            # agents and HA can tell which backup is on which. Sanitised
            # because `agent_id` is `f"{domain}.{unique_id}"` and ends up in
            # config, urls and the customer's Location column.
            label = path.parent.name or "usb"
            self.unique_id = "usb_" + "".join(
                c if c.isalnum() else "_" for c in label
            ).strip("_").lower()
            # What the customer reads in the Backup list's Location column.
            self.name = f"USB stick ({path.parent.name})" if path.parent.name else "USB stick"
            self._backups: dict[str, tuple[Any, Path]] = {}
            self._loaded = False

        # -- reading ---------------------------------------------------------

        def _read(self) -> dict[str, tuple[Any, Path]]:
            found: dict[str, tuple[Any, Path]] = {}
            for tar in self._dir.glob("*.tar"):
                try:
                    backup = read_backup(tar)
                except Exception as err:  # noqa: BLE001
                    # A stick is a shared medium: an unreadable tar is far more
                    # likely to be somebody else's file than a fault of ours,
                    # so it is skipped rather than reported as a broken backup.
                    _LOGGER.debug("ignoring unreadable file %s: %s", tar, err)
                    continue
                found[backup.backup_id] = (backup, tar)
            return found

        async def _ensure_loaded(self) -> None:
            if not self._loaded:
                self._backups = await self._hass.async_add_executor_job(self._read)
                self._loaded = True

        async def async_list_backups(self, **kwargs: Any) -> list[Any]:
            await self._ensure_loaded()
            return [b for b, _ in self._backups.values()]

        async def async_get_backup(self, backup_id: str, **kwargs: Any) -> Any:
            await self._ensure_loaded()
            entry = self._backups.get(backup_id)
            if entry is None:
                raise BackupNotFound(f"Backup {backup_id} not found")
            backup, path = entry
            if not await self._hass.async_add_executor_job(path.exists):
                # The stick was pulled, or the file removed on another machine.
                self._backups.pop(backup_id, None)
                raise BackupNotFound(f"Backup {backup_id} not found")
            return backup

        # -- writing ---------------------------------------------------------

        def get_new_backup_path(self, backup: Any) -> Path:
            """Where HA should write a new backup. HA does the writing."""
            return self._dir / suggested_filename(backup)

        def get_backup_path(self, backup_id: str) -> Path:
            entry = self._backups.get(backup_id)
            if entry is None:
                raise BackupAgentError(f"Backup {backup_id} not found")
            return entry[1]

        async def async_upload_backup(
            self,
            *,
            open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
            backup: Any,
            on_progress: Any = None,
            **kwargs: Any,
        ) -> None:
            """Write the backup to the stick.

            **This agent has to do the writing itself.** An earlier version only
            recorded the path, copied from `CoreLocalBackupAgent`, on the
            assumption that Home Assistant writes the file for any local agent.
            It does not: the Core writer writes its tar to the ONE primary local
            agent it was given, and `manager._async_upload_backup` then calls
            `async_upload_backup(open_stream=...)` on every agent in the list,
            local or not (read off HA 2026.8.1). So the no-op returned without
            error, HA recorded the stick as holding the backup, and the Backup
            card faithfully reported a copy that did not exist -- the worst
            failure this feature can have, because the whole point of a second
            destination is being able to trust it. Proven on the test box: two
            manual backups reported as written to a stick holding 32 KB.

            Written to a temporary name and renamed on success, so an
            interrupted write leaves nothing that later looks like a backup: the
            listing globs `*.tar`, and a half-written tar with a readable header
            would be offered for restore.
            """
            target = self.get_new_backup_path(backup)
            partial = target.with_suffix(target.suffix + ".writing")
            written = 0

            def _open() -> Any:
                partial.parent.mkdir(parents=True, exist_ok=True)
                return partial.open("wb")

            handle = await self._hass.async_add_executor_job(_open)
            try:
                stream = await open_stream()
                async for chunk in stream:
                    await self._hass.async_add_executor_job(handle.write, chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        # Feeds HA's own upload-progress event, so a slow write
                        # to a USB 2 stick is visible rather than looking hung.
                        on_progress(bytes_uploaded=written)
                await self._hass.async_add_executor_job(handle.close)
                await self._hass.async_add_executor_job(partial.replace, target)
            except Exception:
                # Leave nothing behind. A stick is most likely to fail
                # mid-write (pulled out, full, or FAT32's 4 GB ceiling), and a
                # leftover part-file is both wasted space and a restore trap.
                def _cleanup() -> None:
                    try:
                        handle.close()
                    except Exception:  # noqa: BLE001, S110
                        pass
                    partial.unlink(missing_ok=True)

                await self._hass.async_add_executor_job(_cleanup)
                raise

            self._backups[backup.backup_id] = (backup, target)
            _LOGGER.info(
                "backup: wrote %s (%.1f MB) to %s",
                target.name,
                written / (1024 * 1024),
                self._dir.parent.name,
            )

        async def async_download_backup(
            self, backup_id: str, **kwargs: Any
        ) -> AsyncIterator[bytes]:
            # Local agents are read from disk by the manager via
            # `get_backup_path`, so this is never the route taken.
            raise NotImplementedError

        async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
            await self._ensure_loaded()
            entry = self._backups.get(backup_id)
            if entry is None:
                raise BackupNotFound(f"Backup {backup_id} not found")
            _backup, path = entry

            def _unlink() -> None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    # Already gone is the outcome that was asked for.
                    pass

            await self._hass.async_add_executor_job(_unlink)
            self._backups.pop(backup_id, None)

    return _SensaUsbBackupAgent


# Set by the config entry: what to run when the set of destinations we offer
# changes. Kept in `hass.data` rather than passed in, because Home Assistant
# calls `async_get_backup_agents` itself and there is no argument to thread it
# through.
RECONCILE_HOOK_KEY = "sensa_nomad_backup_agent_reconcile"

_OFFERED_KEY = "sensa_nomad_backup_agents_offered"


@callback
def async_set_reconcile_hook(hass: HomeAssistant, hook: Any) -> Callable[[], None]:
    """Register what to run when our destination list changes.

    Returns a remover, so the caller can hand it straight to
    `entry.async_on_unload` and a reload cannot leave a hook pointing at a dead
    entry's client.
    """
    hass.data[RECONCILE_HOOK_KEY] = hook

    @callback
    def _remove() -> None:
        if hass.data.get(RECONCILE_HOOK_KEY) is hook:
            hass.data.pop(RECONCILE_HOOK_KEY, None)

    return _remove


@callback
def _async_agents_offered(hass: HomeAssistant, offered: frozenset[str]) -> None:
    """Reconcile when the destinations we offer have changed.

    **This is the seam that makes a stick actually get used**, and it exists
    because the obvious places do not work. Which destinations a backup is
    written to lives in Home Assistant's STORED config, and Sensa reconciles
    that at entry setup and on the 15-minute backup poller. On a cold boot the
    entry sets up BEFORE Home Assistant loads this platform, so that reconcile
    honestly recorded the only destination that existed at the time (the local
    one) and nothing re-ran it when the stick's agent appeared moments later.
    The result on the test box was a stick mounted, offered, and never written
    to (2026-08-09).

    Hooking the moment we are ASKED for agents covers every ordering, because
    Home Assistant asks exactly when the answer might have changed: at platform
    load, and whenever the media watch tells it to. Only a CHANGE triggers a
    write, so the routine re-asks cost nothing.
    """
    if hass.data.get(_OFFERED_KEY) == offered:
        return
    first = _OFFERED_KEY not in hass.data
    hass.data[_OFFERED_KEY] = offered
    hook = hass.data.get(RECONCILE_HOOK_KEY)
    if hook is None:
        # The entry has not set up yet, so its own setup reconcile is still to
        # come and will see this list. Nothing to do.
        return
    if first and not offered:
        # First ask on a box with nothing plugged in: the answer is the same as
        # the assumption the setup reconcile already made.
        return

    async def _run() -> None:
        # Yield first: Home Assistant updates `manager.backup_agents` with what
        # we just returned immediately AFTER this call, so reconciling without
        # a hop would read the list we are in the middle of replacing.
        import asyncio

        await asyncio.sleep(0)
        try:
            await hook()
        except Exception:
            _LOGGER.debug("backup agents: reconcile hook failed", exc_info=True)

    hass.async_create_task(_run())


async def async_get_backup_agents(hass: HomeAssistant, **kwargs: Any) -> list[Any]:
    """One agent per writable Sensa directory on a mounted stick.

    Empty on every flavour but the Container appliance, and empty when nothing
    is plugged in. Empty is the correct answer, not a failure: HA simply has one
    destination instead of two.
    """
    # Logged because every way this can answer "none" is silent by design, and
    # working out WHICH one fired cost a run of guesses on the test box: the
    # flavour gate, an HA whose backup API we do not recognise, and an empty
    # media root all look identical from outside.
    if not is_container_appliance(hass):
        _LOGGER.debug("backup agents: not a Container appliance, offering none")
        return []
    cls = _agent_class()
    if cls is None:
        _LOGGER.debug("backup agents: HA backup API not available, offering none")
        return []
    dirs = await hass.async_add_executor_job(usb_backup_dirs)
    agents: list[Any] = [cls(hass, path) for path in dirs]
    # The cloud agent joins the SAME list, because Home Assistant loads exactly
    # one backup platform per integration: a second `async_get_backup_agents`
    # in another module would never be called at all.
    from .cloud_backup import async_cloud_agents

    agents.extend(async_cloud_agents(hass))
    _LOGGER.info(
        "backup agents: offering %d destination(s): %s",
        len(agents),
        ", ".join(a.agent_id for a in agents) or "none",
    )
    _async_agents_offered(hass, frozenset(a.agent_id for a in agents))
    return agents


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant, *, listener: Callable[[], None], **kwargs: Any
) -> Callable[[], None]:
    """Let HA know the agent list can change while it is running.

    Registering the listener is what makes "plug a stick in and it becomes a
    destination" work without a restart. What actually notices a stick appearing
    is `async_start_media_watch`, started from `async_setup_entry`; the list is
    also correct at every
    setup and whenever something else triggers a reload.
    """
    listeners: list[Callable[[], None]] = hass.data.setdefault(LISTENERS_KEY, [])
    listeners.append(listener)

    @callback
    def _remove() -> None:
        if listener in listeners:
            listeners.remove(listener)

    return _remove


@callback
def async_notify_backup_agents_changed(hass: HomeAssistant) -> None:
    """Tell HA to re-ask for the agent list."""
    for listener in list(hass.data.get(LISTENERS_KEY, [])):
        listener()


# How often we look for a stick appearing or disappearing.
#
# There is no event to subscribe to: the host mounts the stick and the mount
# propagates into this container, but nothing inside Home Assistant is told. A
# poll is the only mechanism available, and it costs one directory listing.
#
# 30 seconds rather than riding the 15-minute backup poller: somebody who has
# just pushed a stick in is standing there looking at the screen, and a quarter
# of an hour of nothing reads as "it does not work".
MEDIA_POLL_SECONDS = 30

_SEEN_KEY = "sensa_nomad_backup_media_seen"


def _our_agent_ids(hass: HomeAssistant) -> frozenset[str]:
    """The backup agents HA currently has from us, or empty if it has no manager."""
    from .backup_config import _manager

    manager = _manager(hass)
    try:
        return frozenset(
            a for a in manager.backup_agents if a.startswith("sensa_nomad.")
        )
    except AttributeError:
        return frozenset()


# How long to wait for HA to finish rebuilding its agent list.
#
# `async_notify_backup_agents_changed` only schedules the rebuild (HA's listener
# calls `async_create_task`), so a reconcile fired in the same breath would read
# the OLD agent list and write a config that omits the stick it just noticed.
# Short, because this is only ever a scheduling hop rather than real work.
AGENT_RELOAD_TIMEOUT = 5.0


async def _await_agent_reload(
    hass: HomeAssistant, before: frozenset[str], timeout: float = AGENT_RELOAD_TIMEOUT
) -> None:
    """Wait for the rebuild to land, and give up rather than hang.

    Waits for our own agent ids to CHANGE rather than for a particular id to
    appear, so it needs no second copy of how an agent id is derived from a
    path. Deriving it twice is this repo's recurring bug, and the two copies
    would only have to disagree once for the wait to time out on every plug-in.

    Failing to settle is not an error: the caller reconciles anyway and the
    backup poller reconciles again later, so the worst case is the behaviour we
    had before this existed.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
        if _our_agent_ids(hass) != before:
            return
    _LOGGER.debug("media watch: agent list did not settle, reconciling anyway")


def async_start_media_watch(
    hass: HomeAssistant,
    on_change: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> Callable[[], None]:
    """Notice sticks arriving and leaving, and re-ask HA for the agent list.

    Returns an unsubscribe, always, so the caller can register it for teardown
    without checking anything.

    `on_change` runs after the agent list is rebuilt, and is what makes a stick
    START BEING USED rather than merely being known about. Telling HA the agent
    list changed is not enough on its own: which destinations a backup is
    written to lives in HA's STORED config, and that is only reconciled on the
    15-minute backup poller. Without this, somebody who plugs a stick in sees
    the destination appear and yet the next backup still goes to the card alone,
    for up to a quarter of an hour, with nothing on screen to explain it. The
    callback is awaited inside the timer tick and its failures are swallowed,
    because a reconcile that cannot run is the next poll's problem, not a reason
    to kill the watch.

    Only notifies on a CHANGE. Firing every tick would make the backup manager
    rebuild its agent list twice a minute for ever on a box that has never had a
    stick anywhere near it.

    Gated on the SAME predicate as `async_get_backup_agents`: everywhere else
    that returns [] whatever is plugged in, so a timer there could only ever
    announce a change that cannot produce an agent.
    """
    from homeassistant.helpers.event import async_track_time_interval

    if not is_container_appliance(hass):
        return lambda: None

    async def _tick(_now: Any) -> None:
        try:
            current = {str(p) for p in await hass.async_add_executor_job(usb_backup_dirs)}
        except Exception:
            _LOGGER.debug("media watch: scan failed", exc_info=True)
            return
        if current == hass.data.get(_SEEN_KEY):
            return
        previous = hass.data.get(_SEEN_KEY)
        hass.data[_SEEN_KEY] = current
        if previous is None and not current:
            # First tick on a box with nothing plugged in. There is no agent to
            # rebuild and nothing to reconcile, so stay quiet.
            return
        _LOGGER.info(
            "backup media changed: %s", ", ".join(sorted(current)) or "none attached"
        )
        # NOT skipped on the first tick when a stick IS attached, and that is
        # deliberate. The old code assumed the agent list built at platform load
        # was already right, which is false on a cold boot: Home Assistant sets
        # its backup component up BEFORE this integration knows which flavour it
        # is on, so `async_get_backup_agents` correctly answered "none" and was
        # never asked again. On the test box that left a mounted stick offered
        # as a destination that no backup was ever written to (2026-08-09). The
        # cost of being wrong the other way is one redundant agent rebuild at
        # boot on a box with a stick in it.
        before = _our_agent_ids(hass)
        async_notify_backup_agents_changed(hass)
        await _await_agent_reload(hass, before)
        if on_change is not None:
            try:
                await on_change()
            except Exception:
                _LOGGER.debug("media watch: change hook failed", exc_info=True)

    from datetime import timedelta

    return async_track_time_interval(
        hass, _tick, timedelta(seconds=MEDIA_POLL_SECONDS)
    )
