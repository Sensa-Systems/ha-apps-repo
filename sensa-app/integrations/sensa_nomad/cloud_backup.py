"""Sensa-hosted cloud as a backup destination (backups-roadmap.md Phase 6).

The local copy dies with the card it sits on, and the USB stick lives in the
same vehicle as the box, so neither survives what people actually fear: the van
being stolen, or burning out. This is the first destination that leaves the
vehicle, and unlike the stick it needs nobody to remember to plug anything in.

**Sensa holds the bytes** (Richard, 2026-08-09), in Azure Blob Storage. That
makes cloud backup something Sensa sells rather than something the customer
configures, and it is why every decision below leans on the licence rather than
on anything the customer types.

Shape, and why:

* **The box uploads DIRECTLY to Blob Storage**, not through the Sensa App. A
  backup is around 1 GB on the test box; routing it through the add-on would
  copy it twice over the same link for no benefit.
* **The credential is a SAS handed down at phone-home**, cached with the licence
  status, exactly like the Cloudflare tunnel token and the OTA registry
  credential. So it is **support-gated in the CLOUD**: a lapsed contract stops
  issuing one, and this module then offers no agent at all. The device contains
  no support logic, which is what makes the gate hold on a box we do not own.
* **...but it is REFRESHED at the point of upload.** A user-delegation SAS cannot
  outlive seven days and a paid licence phones home every ninety, so the handed
  down copy is stale far more often than not. `_ensure_fresh_credential` re-reads
  it and forces a phone-home when it is close to expiry.
* **Uploads are STREAMED in blocks.** Home Assistant hands us an async iterator,
  and a block-by-block upload consumes it as it arrives, so memory does not
  scale with the size of the backup. A Raspberry Pi cannot hold a gigabyte in
  RAM to POST it in one go.
* **Nothing here ever logs the credential.** A SAS token IS the access, so a
  debug line containing one is a leak that outlives the log file.

**The upload has never run against Azure.** The account exists and the Function
mints credentials (proven 2026-08-10), but these REST calls are covered only by
tests driving a fake endpoint. Treat the first real upload as the test.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

from homeassistant.core import HomeAssistant

from . import egress
from .backup_config import is_container_appliance, record_agent_upload_progress
from .const import DOMAIN, RUNTIME_LICENCE_STATUS

_LOGGER = logging.getLogger(__name__)

# How much of the stream is buffered before a block is sent.
#
# 4 MiB is a compromise, not a maximum: Blob Storage allows far larger blocks,
# but every byte in flight is a byte held in the Pi's memory, and a smaller
# block also means less to re-send when a mobile link drops. At this size a 1 GB
# backup is about 250 blocks, well inside the 50,000-block limit even for a
# backup many times larger.
BLOCK_SIZE = 4 * 1024 * 1024

# Every block id in one blob must decode to the same length, so they are
# zero-padded rather than being the bare index.
_BLOCK_ID_WIDTH = 8

# The metadata Home Assistant needs in order to LIST a backup lives beside the
# tar rather than inside it, so listing does not mean downloading gigabytes to
# read a header.
_TAR_SUFFIX = ".tar"
_META_SUFFIX = ".metadata.json"

# How long a container listing is trusted before it is re-read. See
# `_ensure_loaded`: long enough that rendering the Backup card does not list the
# container on every frame, short enough that a backup removed outside this box
# stops being offered almost straight away.
_LIST_CACHE_SECONDS = 60

# Timeouts are on a STALL, never on total duration.
#
# `timeout=<number>` in aiohttp means the TOTAL time for the request, which is
# the wrong shape for a large body: a 4 MiB block on a slow uplink legitimately
# takes minutes, and capping the total kills an upload that is making perfectly
# good progress. That is what happened on the test box (2026-08-10): the first
# real cloud upload ran for around nineteen minutes and then died with
# `asyncio.TimeoutError` on a block, with HA reporting only "Unexpected error
# for sensa_nomad.cloud".
#
# A van is frequently on mobile data, so "slow" is the normal case rather than
# the edge one. `sock_read` + `sock_connect` fail a connection that has STOPPED
# moving while leaving a slow one alone, which is the behaviour actually wanted.
_STALL_SECONDS = 90
_CONNECT_SECONDS = 30

# How many times one block is re-sent before the upload gives up.
#
# Worth doing because `Put Block` is idempotent: the same block id can be sent
# again with no duplicate and no cleanup, so a retry is free of consequences. A
# gigabyte over 4 MiB blocks is ~250 requests, and on a mobile link the chance of
# all 250 succeeding first time is not high.
_BLOCK_ATTEMPTS = 3

_SMALL_REQUEST_TIMEOUT = 60


def redact(url: str) -> str:
    """A URL with its query stripped, for logging.

    The SAS token lives in the query string and IS the access to the container,
    so a log line carrying one hands the customer's backups to anyone who can
    read the log. Everything in this module logs through here.
    """
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return "<url>"


def cloud_credential(hass: HomeAssistant) -> dict[str, Any] | None:
    """The Blob Storage credential handed down at phone-home, or None.

    Read from the CACHED licence status, so it survives a restart and holds
    while the box is offline: losing signal must not quietly drop a destination
    the customer is paying for.
    """
    try:
        for runtime in (hass.data.get(DOMAIN) or {}).values():
            if not isinstance(runtime, dict):
                continue
            status = runtime.get(RUNTIME_LICENCE_STATUS) or {}
            cloud = status.get("cloud_backup") or {}
            if not isinstance(cloud, dict) or not cloud.get("enabled"):
                continue
            # Both halves are required. A container URL with no token cannot be
            # written to and a token with no container has nowhere to go, so
            # half a credential is a destination that fails on every backup.
            if cloud.get("container_url") and cloud.get("sas_token"):
                return cloud
    except Exception:
        _LOGGER.debug("cloud backup: credential unreadable", exc_info=True)
    return None


# Refresh the credential when it has less than this left.
#
# A SAS signed with a user-delegation key cannot outlive SEVEN DAYS (Azure
# refuses a longer one outright), while a paid licence phones home only every
# 2160 hours (90 days). So the credential handed down at phone-home is stale for
# most of its life, and the device MUST refresh near the point of use rather than
# rely on being told. One extra `/validate` on the night a backup needs it is a
# fair price; doing it on every upload regardless would not be.
CREDENTIAL_REFRESH_MARGIN = timedelta(hours=12)


def _expires_at(credential: dict[str, Any] | None) -> datetime | None:
    if not credential:
        return None
    raw = credential.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def credential_is_usable(
    credential: dict[str, Any] | None, now: datetime | None = None
) -> bool:
    """True when the credential will still be valid for a while yet.

    A credential with NO expiry counts as usable: an older Function that does not
    publish one is not a reason to refuse an upload, and the upload itself will
    say soon enough if the token is dead.
    """
    expiry = _expires_at(credential)
    if expiry is None:
        return credential is not None
    return expiry - (now or datetime.now(UTC)) > CREDENTIAL_REFRESH_MARGIN


async def async_refresh_cloud_credential(hass: HomeAssistant) -> dict[str, Any] | None:
    """Force a phone-home so the Function mints a fresh credential, and return it.

    Goes through the App's `/api/license/phone-home`, which does a real
    `/validate` and returns the new status, rather than adding a second
    credential endpoint: the phone-home already carries the licence, the gates
    and the support check, and a separate path would be a second thing to keep in
    step with them.

    Returns whatever is cached afterwards, which may still be None. Failing to
    refresh is not an error worth raising: the caller falls back to the credential
    it has, and an upload against a dead token fails with a real message from
    Azure rather than a guess from us.
    """
    for runtime in (hass.data.get(DOMAIN) or {}).values():
        if not isinstance(runtime, dict):
            continue
        client = runtime.get("client")
        if client is None:
            continue
        try:
            await client.async_post_license_phone_home()
        except Exception:
            _LOGGER.debug("cloud backup: credential refresh failed", exc_info=True)
        break
    return cloud_credential(hass)


def _agent_class() -> Any:
    """Build the agent class against HA's backup component, or None.

    Defined inside a function for the same reason as the USB agent's: it must
    subclass Home Assistant's `BackupAgent`, and importing that at module level
    would make the backup integration a hard dependency of a module loaded on
    every flavour. Subclassing also supplies `agent_id` (`domain.unique_id`),
    which the manager keys everything on.
    """
    try:
        from homeassistant.components.backup import (
            AgentBackup,
            BackupAgent,
            BackupAgentError,
        )
        from homeassistant.components.backup.models import BackupNotFound
    except ImportError:  # pragma: no cover - real HA always has these
        return None

    class _SensaCloudBackupAgent(BackupAgent):
        """Sensa-hosted storage, as a Home Assistant backup agent.

        Deliberately NOT a `LocalBackupAgent`: there is no path on this machine
        for Home Assistant to write to, so this agent does the transfer itself.
        """

        domain = DOMAIN
        name = "Sensa cloud"
        unique_id = "cloud"

        def __init__(self, hass: HomeAssistant, credential: dict[str, Any]) -> None:
            super().__init__()
            self._hass = hass
            self._container = str(credential["container_url"]).rstrip("/")
            self._sas = str(credential["sas_token"]).lstrip("?")
            self._backups: dict[str, Any] | None = None
            self._listed_at = 0.0

        # -- plumbing ----------------------------------------------------

        def _url(self, blob: str, *, query: str = "") -> str:
            extra = f"{query}&" if query else ""
            return f"{self._container}/{blob}?{extra}{self._sas}"

        def _session(self) -> Any:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            return async_get_clientsession(self._hass)

        async def _ensure_fresh_credential(self) -> None:
            """Re-read the credential, refreshing it if it is close to expiry.

            The agent is built when Home Assistant asks for the agent list, which
            can be days before a backup runs, and a SAS lasts at most seven days
            against a phone-home interval of ninety. So the token this object was
            constructed with is routinely the wrong one by the time it is used.

            Deliberately never raises: an upload with a stale token fails with
            Azure's own message, which is more use than refusing to try.
            """
            current = cloud_credential(self._hass)
            if not credential_is_usable(current):
                current = await async_refresh_cloud_credential(self._hass)
            if not current:
                return
            container_url = str(current.get("container_url") or "").rstrip("/")
            sas = str(current.get("sas_token") or "").lstrip("?")
            if container_url and sas:
                self._container, self._sas = container_url, sas

        # -- reading -----------------------------------------------------

        async def _load(self) -> dict[str, Any]:
            """Every backup in the container, read from its metadata blob.

            A blob that cannot be parsed is SKIPPED rather than raised on: one
            unreadable object must not make the whole destination unusable.
            """
            found: dict[str, Any] = {}
            session = self._session()
            listing = f"{self._container}?restype=container&comp=list&{self._sas}"
            async with session.get(listing, timeout=_SMALL_REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    raise BackupAgentError(
                        f"listing Sensa cloud failed with {resp.status}"
                    )
                body = await resp.text()
            egress.record("backup", 0, len(body.encode("utf-8")))
            try:
                tree = ElementTree.fromstring(body)
            except ElementTree.ParseError as err:
                raise BackupAgentError(
                    "Sensa cloud returned an unreadable listing"
                ) from err

            for element in tree.iter("Name"):
                name = element.text or ""
                if not name.endswith(_META_SUFFIX):
                    continue
                try:
                    async with session.get(
                        self._url(name), timeout=_SMALL_REQUEST_TIMEOUT
                    ) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text()
                        egress.record("backup", 0, len(text.encode("utf-8")))
                        meta = json.loads(text)
                    backup = AgentBackup.from_dict(meta)
                except Exception:
                    _LOGGER.debug("cloud backup: skipping %s", name, exc_info=True)
                    continue
                found[backup.backup_id] = backup
            return found

        async def _ensure_loaded(self) -> dict[str, Any]:
            """The container's listing, re-read at most once every minute.

            It used to be cached for the lifetime of this object and updated
            only for uploads and deletes THIS agent performed, so a backup
            removed any other way stayed on the card until Home Assistant
            restarted -- a row for a blob that no longer existed, which is the
            same lie as the un-deletable row, from the other direction (test
            box, 2026-08-11, a blob deleted with the Azure CLI).

            A minute rather than every call because the Backup card lists on
            every render, and rather than never because the container is shared
            with support tooling, the Portal and Azure itself. On a refresh that
            fails the PREVIOUS listing stands: could-not-ask must not empty a
            customer's backup list.
            """
            import time

            now = time.monotonic()
            fresh_enough = now - self._listed_at < _LIST_CACHE_SECONDS
            if self._backups is not None and fresh_enough:
                return self._backups
            try:
                self._backups = await self._load()
                self._listed_at = now
            except Exception:
                if self._backups is None:
                    raise
                _LOGGER.debug(
                    "cloud backup: listing refresh failed; keeping the last one",
                    exc_info=True,
                )
            return self._backups

        async def async_list_backups(self, **kwargs: Any) -> list[Any]:
            return list((await self._ensure_loaded()).values())

        async def async_get_backup(self, backup_id: str, **kwargs: Any) -> Any:
            backup = (await self._ensure_loaded()).get(backup_id)
            if backup is None:
                raise BackupNotFound(f"Backup {backup_id} not found")
            return backup

        async def async_download_backup(
            self, backup_id: str, **kwargs: Any
        ) -> AsyncIterator[bytes]:
            # Refreshed here too, because this is the RESTORE path and it is the
            # one moment the customer cannot be told "try again tomorrow".
            #
            # Deliberately NOT done in `_load`: listing happens every time the
            # Backup card renders, and a credential that cannot be refreshed
            # would then force a phone-home on every render. A stale token there
            # degrades to "could not ask", which the pill already renders neutral
            # rather than as a failure.
            await self._ensure_fresh_credential()
            await self.async_get_backup(backup_id)
            resp = await self._session().get(self._url(f"{backup_id}{_TAR_SUFFIX}"))
            if resp.status != 200:
                resp.close()
                raise BackupAgentError(
                    f"downloading from Sensa cloud failed with {resp.status}"
                )
            return egress.metered_chunks("backup", resp.content.iter_chunked(BLOCK_SIZE))

        # -- writing -----------------------------------------------------

        def _report(self, sent: int, total: int) -> None:
            """Publish how far this upload has got, for the Backup card.

            Held one byte short of the total until the run itself ends: a
            destination that reads as complete is dropped from the card (it
            would otherwise sit at 100% while the commit and the metadata write
            are still outstanding), and there is more to do after the last block
            goes up. Never raises: a progress line must not be able to fail a
            backup.
            """
            try:
                capped = min(sent, total - 1) if total > 0 else sent
                record_agent_upload_progress(
                    self._hass, self.agent_id, max(0, capped), total
                )
            except Exception:
                _LOGGER.debug("cloud backup: could not record progress", exc_info=True)

        async def _send_block(self, blob: str, index: int, payload: bytes) -> str:
            import asyncio

            import aiohttp

            block_id = base64.b64encode(
                str(index).zfill(_BLOCK_ID_WIDTH).encode()
            ).decode()
            url = self._url(blob, query=f"comp=block&blockid={block_id}")
            timeout = aiohttp.ClientTimeout(
                total=None, sock_connect=_CONNECT_SECONDS, sock_read=_STALL_SECONDS
            )
            last: Exception | None = None
            for attempt in range(_BLOCK_ATTEMPTS):
                status: int | None = None
                # ONLY the request is inside the try, and the decision about what
                # the status means is taken outside it. A non-retryable failure
                # raised in here would be at the mercy of how broad the except
                # happens to be, and it is broader than it looks: the test
                # harness stubs `aiohttp.ClientError` as bare `Exception`, which
                # swallowed the deliberate 4xx raise and retried a dead SAS three
                # times.
                try:
                    async with self._session().put(
                        url, data=payload, timeout=timeout
                    ) as resp:
                        status = resp.status
                    egress.record("backup", len(payload), 0)
                except (TimeoutError, aiohttp.ClientError) as err:
                    last = err
                if status in (201, 202):
                    return block_id
                if status is not None:
                    failure = BackupAgentError(
                        f"uploading to Sensa cloud failed with {status}"
                    )
                    # A 4xx is a decision (a dead SAS, a gone container) and will
                    # not come good by trying again; a 5xx might.
                    if status < 500:
                        raise failure
                    last = failure
                if attempt + 1 < _BLOCK_ATTEMPTS:
                    # Brief, and growing: a mobile link that just dropped is
                    # usually back within seconds, and a backup is not urgent.
                    await asyncio.sleep(2 * (attempt + 1))
            raise BackupAgentError(
                f"uploading to Sensa cloud failed after {_BLOCK_ATTEMPTS} attempts: {last}"
            )

        async def async_upload_backup(
            self,
            *,
            open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
            backup: Any,
            **kwargs: Any,
        ) -> None:
            """Stream the backup up in blocks, commit it, then record it.

            The tar goes up FIRST and the metadata second, deliberately: the
            metadata blob is what makes a backup appear in the list, so writing
            it last means an interrupted upload leaves an orphaned tar rather
            than a LISTED backup that cannot be restored. An orphan wastes
            space; a listed backup that turns out to be half a file is a promise
            broken at exactly the wrong moment.
            """
            await self._ensure_fresh_credential()
            blob = f"{backup.backup_id}{_TAR_SUFFIX}"
            block_ids: list[str] = []
            buffer = bytearray()
            # Home Assistant raises `UploadBackupEvent` only for uploads it
            # performs itself, and this agent does its own transfer, so the card
            # hears nothing from here unless we say so. On a 1.2 Mbps link a
            # gigabyte takes the best part of two hours, which is the longest
            # phase of the whole run by far and the one most in need of a
            # number. `backup.size` is the byte count HA has for the tar; 0 when
            # it does not know, in which case the destination is still named and
            # only the percentage is withheld.
            sent = 0
            total = max(0, int(getattr(backup, "size", 0) or 0))
            self._report(sent, total)

            stream = await open_stream()
            async for chunk in stream:
                buffer.extend(chunk)
                while len(buffer) >= BLOCK_SIZE:
                    block_ids.append(
                        await self._send_block(
                            blob, len(block_ids), bytes(buffer[:BLOCK_SIZE])
                        )
                    )
                    del buffer[:BLOCK_SIZE]
                    sent += BLOCK_SIZE
                    self._report(sent, total)
            if buffer:
                block_ids.append(
                    await self._send_block(blob, len(block_ids), bytes(buffer))
                )
                sent += len(buffer)
                self._report(sent, total)

            # Commit. Until this lands the blocks are uncommitted and the blob
            # does not exist, which is what makes a failed upload leave nothing
            # behind rather than a truncated file.
            listing = "".join(f"<Latest>{b}</Latest>" for b in block_ids)
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                f"<BlockList>{listing}</BlockList>"
            )
            async with self._session().put(
                self._url(blob, query="comp=blocklist"),
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=_SMALL_REQUEST_TIMEOUT,
            ) as resp:
                if resp.status not in (201, 202):
                    raise BackupAgentError(
                        f"committing to Sensa cloud failed with {resp.status}"
                    )

            egress.record("backup", len(body), 0)
            meta = json.dumps(backup.as_dict()).encode("utf-8")
            egress.record("backup", len(meta), 0)
            async with self._session().put(
                self._url(f"{backup.backup_id}{_META_SUFFIX}"),
                data=meta,
                headers={
                    "x-ms-blob-type": "BlockBlob",
                    "Content-Type": "application/json",
                },
                timeout=_SMALL_REQUEST_TIMEOUT,
            ) as resp:
                if resp.status not in (201, 202):
                    raise BackupAgentError(
                        "writing backup details to Sensa cloud failed with "
                        f"{resp.status}"
                    )

            if self._backups is not None:
                self._backups[backup.backup_id] = backup
            _LOGGER.info(
                "cloud backup: uploaded %s to %s",
                backup.backup_id,
                redact(self._container),
            )

        async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
            """Remove both blobs.

            The METADATA goes first, so a half-completed delete leaves a backup
            that is gone from the list rather than one that is listed and cannot
            be downloaded. The same reasoning as the upload order, in reverse.
            """
            backups = await self._ensure_loaded()
            if backup_id not in backups:
                raise BackupNotFound(f"Backup {backup_id} not found")
            for suffix in (_META_SUFFIX, _TAR_SUFFIX):
                async with self._session().delete(
                    self._url(f"{backup_id}{suffix}"), timeout=_SMALL_REQUEST_TIMEOUT
                ) as resp:
                    # 404 is the outcome that was asked for.
                    if resp.status not in (202, 404):
                        raise BackupAgentError(
                            f"deleting from Sensa cloud failed with {resp.status}"
                        )
            backups.pop(backup_id, None)

    return _SensaCloudBackupAgent


def async_cloud_agents(hass: HomeAssistant) -> list[Any]:
    """The cloud agent, when this licence includes it and a credential exists.

    Empty is the ordinary answer: no credential means the licence does not
    include cloud backup, or support has lapsed, or the box has not phoned home
    yet. In all three cases the destination is simply absent and the Cloud pill
    does not render.

    Returned to `backup.py` rather than exposing an `async_get_backup_agents` of
    its own, because Home Assistant loads exactly ONE backup platform per
    integration and a second one here would silently never be called.
    """
    if not is_container_appliance(hass):
        return []
    credential = cloud_credential(hass)
    if credential is None:
        return []
    cls = _agent_class()
    if cls is None:
        return []
    _LOGGER.info(
        "cloud backup: offering %s", redact(str(credential["container_url"]))
    )
    return [cls(hass, credential)]
