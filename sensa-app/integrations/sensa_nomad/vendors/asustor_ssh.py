"""Asustor ADM (Linux) NAS client — clean shutdown over SSH + Wake-on-LAN.

The Asustor Flashstor 6 (FS6706T) runs ADM, a Linux distribution. The only
capability Sensa drives is power: a *clean* shutdown (so volumes unmount and
the box halts gracefully — not a hard power cut) and a power-on via a
Wake-on-LAN magic packet.

Design notes:

* **Shutdown = SSH.** The reliable way to trigger a graceful ADM shutdown
  remotely is to SSH in and run `poweroff` / `shutdown -h now`. The customer
  enables SSH in ADM (Services > Terminal) and supplies credentials. We use
  `asyncssh`, which is natively async (its connect path is `asyncio.open_
  connection` under the hood — the same path `config_flow._tcp_probe` uses
  reliably on LAN IPv4), so no executor-thread juggling is needed.

* **Power-on = Wake-on-LAN.** Reuse HA's built-in `wake_on_lan` integration's
  `async_send_magic_packet` helper rather than hand-rolling a magic-packet
  socket or adding a dependency. `wake_on_lan` is declared in the manifest's
  `after_dependencies` so the service is loaded.

* **State = reachability.** The switch shows on/off from a lightweight
  TCP-connect probe to the SSH port — same shape as `_tcp_probe`. We only care
  that the box answers, not what it says.

* **Host keys.** A NAS on the LAN won't have a pre-trusted host key and ADM may
  regenerate it, so we connect with `known_hosts=None` (verification disabled).
  This is the pragmatic choice for a local appliance; we never persist a
  known_hosts file.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# SSH connect/run ceiling. A clean shutdown severs the connection mid-command,
# so the run itself returns almost immediately; this just bounds a hung connect.
_SSH_TIMEOUT = 10.0
# Reachability probe timeout — short, like Teltonika's degraded poll timeout.
_PROBE_TIMEOUT = 1.5
_DEFAULT_SSH_PORT = 22

# Tried in order. ADM's SSH user usually needs root for poweroff, so try a
# non-interactive sudo first, then a plain call (in case the user IS root),
# then sudo shutdown as a last resort. The first one that doesn't raise wins.
_SHUTDOWN_COMMANDS = (
    "sudo -n poweroff",
    "poweroff",
    "sudo -n shutdown -h now",
)


class AsustorSshError(Exception):
    """Base error for the Asustor SSH client."""


class AsustorAuthError(AsustorSshError):
    """SSH authentication failed (bad username / password / key)."""


class AsustorConnectionError(AsustorSshError):
    """Could not reach the NAS over SSH (offline, wrong host/port, timeout)."""


class AsustorSshClient:
    """Talks to one Asustor NAS: reachability probe, shutdown, Wake-on-LAN."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        username: str,
        password: str = "",
        key_pem: str | None = None,
        mac: str = "",
        *,
        ssh_port: int = _DEFAULT_SSH_PORT,
    ) -> None:
        self._hass = hass
        self._host = host
        self._username = username
        self._password = password or None
        self._key_pem = key_pem or None
        self._mac = mac
        self._ssh_port = ssh_port

    @property
    def mac(self) -> str:
        return self._mac

    def _client_keys(self) -> list | None:
        """Parse the optional private-key PEM into asyncssh key objects.

        Returns None (let asyncssh fall back to password auth) when no key is
        configured or the PEM doesn't parse — a bad key shouldn't mask a
        working password.
        """
        if not self._key_pem:
            return None
        try:
            import asyncssh

            return [asyncssh.import_private_key(self._key_pem)]
        except Exception:
            _LOGGER.warning(
                "Asustor NAS %s: SSH private key did not parse — "
                "falling back to password auth",
                self._host,
                exc_info=True,
            )
            return None

    async def async_is_reachable(self, timeout: float = _PROBE_TIMEOUT) -> bool:
        """Return True if the NAS accepts a TCP connection on the SSH port.

        Mirrors `config_flow._tcp_probe`: open, close, report reachability —
        we never speak SSH here, so a powered-but-SSH-disabled box still reads
        as 'on' as long as something answers on the port.
        """
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._ssh_port),
                timeout=timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001, S110
            pass
        return True

    async def async_shutdown(self) -> None:
        """Open SSH and trigger a clean ADM shutdown.

        Raises AsustorAuthError on bad credentials, AsustorConnectionError if
        the box can't be reached. A successful shutdown severs the connection
        mid-command, which asyncssh surfaces as ConnectionLost — we treat that
        as success (the box is going down, which is exactly what we asked for).
        """
        import asyncssh

        try:
            async with asyncssh.connect(
                self._host,
                port=self._ssh_port,
                username=self._username,
                password=self._password,
                client_keys=self._client_keys(),
                known_hosts=None,  # LAN appliance — see module docstring
                connect_timeout=_SSH_TIMEOUT,
            ) as conn:
                for cmd in _SHUTDOWN_COMMANDS:
                    try:
                        await conn.run(cmd, timeout=_SSH_TIMEOUT, check=False)
                        _LOGGER.info(
                            "Asustor NAS %s: sent shutdown command %r",
                            self._host,
                            cmd,
                        )
                        return
                    except asyncssh.ConnectionLost:
                        # The box dropped the link as it halted — success.
                        _LOGGER.info(
                            "Asustor NAS %s: connection dropped during %r "
                            "(shutdown in progress)",
                            self._host,
                            cmd,
                        )
                        return
                    except asyncio.TimeoutError:
                        # This command hung; try the next form.
                        _LOGGER.debug(
                            "Asustor NAS %s: command %r timed out, trying next",
                            self._host,
                            cmd,
                        )
        except asyncssh.PermissionDenied as err:
            raise AsustorAuthError(str(err)) from err
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as err:
            raise AsustorConnectionError(str(err)) from err

    async def async_wol(self) -> None:
        """Send a Wake-on-LAN magic packet to the NAS MAC.

        Reuses HA's built-in `wake_on_lan` integration. Raises if no MAC has
        been configured.
        """
        if not self._mac:
            raise AsustorSshError("No Wake-on-LAN MAC configured for this NAS")
        try:
            from homeassistant.components.wake_on_lan import (
                async_send_magic_packet,
            )

            await async_send_magic_packet(self._hass, self._mac)
        except ImportError:
            # Older HA without the helper export — fall back to the service.
            await self._hass.services.async_call(
                "wake_on_lan",
                "send_magic_packet",
                {"mac": self._mac},
                blocking=True,
            )
        _LOGGER.info("Asustor NAS: sent Wake-on-LAN packet to %s", self._mac)
