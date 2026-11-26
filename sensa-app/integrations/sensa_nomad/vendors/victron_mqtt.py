"""Victron Cerbo GX MQTT client — direct connection to the Cerbo's broker.

The Cerbo runs its own Mosquitto instance, exposed on the LAN when
"MQTT on LAN" is enabled in Venus OS (Settings → Services). Sensa
connects directly to that broker rather than assuming the Cerbo has
been bridged into HA's broker. Keeps the integration self-contained —
no customer-side broker plumbing, no reliance on HA's MQTT integration
being installed.

Two responsibilities:

    1. Maintain a long-lived aiomqtt connection to `<host>:<port>` with
       optional username/password + TLS. Subscribe to
       `N/+/<service>/<instance>/<path>` topics, parse the Venus OS JSON
       payload (`{"value": 12.34}`), and pass `(vrm, path, value)` to a
       callback. The Cerbo's VRM portal id (the `+` wildcard) gets
       captured from the first matching message so subsequent
       diagnostics and the keepalive publish know which id to use.

    2. Drive the keepalive. Venus OS stops publishing live values about a
       minute after the last `R/<vrm>/keepalive` request, so this client
       runs a per-connection `_keepalive_loop` that publishes one every
       KEEPALIVE_INTERVAL_SECS for the life of the connection (and recycles
       the connection once the keepalive starts failing, i.e. the link has
       died). It is the single keepalive driver and is independent of the
       coordinator's poll, so the stream stays open even when no HA entity
       is subscribed and the customer never has to touch anything.

Why aiomqtt and not HA's MQTT integration: the customer's Cerbo has
its own broker — HA's integration is for the broker HA uses (which may
be a different Mosquitto or none at all). Connecting directly avoids
customer-side bridging configuration and keeps the "MQTT on LAN" tick-
box in Venus OS as the only setup step.

Why not paho-mqtt directly: aiomqtt is a thin async wrapper around
paho, and HA is async-first. Using aiomqtt keeps the client's lifecycle
(connect, subscribe, disconnect) in async idioms instead of threading
paho's synchronous callbacks across an executor boundary.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl as ssl_module
import time
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Subset of Venus OS system-level paths Sensa exposes for the demo. Each
# entry is the trailing path after `N/<vrm>/system/0/` — the wildcard
# `+` for the VRM id is added at subscribe time. Per-device topics
# (battery/<id>, solarcharger/<id>, etc.) are left out for now; once the
# system view is solid we'll fan out to those.
SYSTEM_TOPICS: tuple[str, ...] = (
    "Dc/Battery/Voltage",
    "Dc/Battery/Current",
    "Dc/Battery/Power",
    "Dc/Battery/Soc",
    "Dc/Pv/Power",
    "Dc/Pv/Current",
    "Ac/ConsumptionOnInput/L1/Power",
    "Ac/ConsumptionOnOutput/L1/Power",
    "Ac/Grid/L1/Power",
    "Dc/System/Power",
    "SystemState/State",
)

# Venus OS expects a keepalive request at least once a minute, or it stops
# streaming live values. The cadence is set by KEEPALIVE_INTERVAL_SECS below.
KEEPALIVE_TOPIC_TEMPLATE = "R/{vrm}/keepalive"

# Cadence for the per-connection keepalive loop (see `_keepalive_loop`). Each
# keepalive prompts Venus to re-broadcast its topics, so this ALSO sets the
# worst-case full-refresh latency. 5 s keeps the power-flow / DC-load display
# feeling live (the cadence the old coordinator poll was tuned to before the
# keepalive moved here), well inside Venus's ~60 s keepalive window. This loop
# is the SINGLE keepalive driver, running for the life of the MQTT connection
# and independent of the coordinator's listener-gated poll.
KEEPALIVE_INTERVAL_SECS = 5.0

# A single keepalive publish failure is tolerated (it may be a transient hiccup
# or a race with another publish on the shared client); only after this many
# CONSECUTIVE failures (about KEEPALIVE_INTERVAL_SECS * N of no good keepalive)
# is the link treated as genuinely dead and the connection recycled. Stops a
# one-off error tearing down a healthy link, while still detecting a dead link
# within ~10 s (2 x 5 s).
KEEPALIVE_MAX_FAILURES = 2

# A connection must stay up at least this long to count as "stable". The
# reconnect backoff is only reset to its floor after a stable session, so a
# genuinely flapping link (drops within seconds) escalates toward the cap
# instead of hammering reconnects, while a link that streams for a while before
# dropping still recovers quickly. Kept modest so a marginal-but-usable Cerbo
# link isn't punished with a long backoff after a brief drop.
STABLE_CONNECTION_SECS = 20.0

# Default ports for Venus "MQTT on LAN". Plaintext broker runs on 1883;
# TLS (Venus OS "MQTT on LAN (SSL only)") runs on 8883.
DEFAULT_PORT_PLAIN = 1883
DEFAULT_PORT_TLS = 8883


def _parse_venus_payload(raw: bytes | str) -> Any:
    """Decode a Venus OS MQTT payload to its Python value, or `None`.

    Venus publishes every value as `{"value": <x>}`. `<x>` may be a
    number, string, bool, or `null` (the topic exists but the underlying
    D-Bus item has no current value — common for Ac/Grid power on a site
    with no AC input). We also tolerate raw scalars (some bridges
    transcode them) so a future broker change doesn't silently drop
    every reading.
    """
    if not raw:
        return None
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    text = text.strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        # Non-JSON payload — most likely a raw number from a bridge.
        # Best-effort cast; fall back to the raw text so the caller still
        # sees something rather than dropping the message silently.
        try:
            return float(text)
        except ValueError:
            return text
    if isinstance(decoded, dict) and "value" in decoded:
        return decoded["value"]
    return decoded


class VictronMqttClient:
    """Subscribe + keepalive helpers for a Cerbo GX over MQTT.

    Owns an aiomqtt `Client` for the lifetime of the coordinator. The
    background task stays in the `async with client:` context while
    messages flow; `async_disconnect()` cancels the task and lets the
    context manager close the TCP socket cleanly.

    The client is stateless across messages — it just routes them to
    the callback. The coordinator owns the value cache so test stubs
    can drop this client and feed the cache directly.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        on_value: Callable[[str, str, str, str, Any], None],
        device_id: str,
        *,
        host: str,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool | None = False,
    ) -> None:
        self._hass = hass
        self._on_value = on_value
        self._device_id = device_id
        self._host = host
        self._username = username or None
        self._password = password or None
        # Transport candidates the connect loop walks. `use_tls=None` means
        # "auto": try plaintext (1883) first, then TLS (8883), and lock onto
        # whichever connects — so a Cerbo left in Venus OS's more-secure
        # SSL-only "MQTT on LAN" mode works without the customer pinning
        # ssl=True. An explicit True/False fixes the transport (honoured when
        # the customer has set the SSL switch via the picker). A custom `port`
        # overrides the standard port for whichever transport(s) apply.
        if use_tls is None:
            self._transports: list[tuple[int, bool]] = [
                (port or DEFAULT_PORT_PLAIN, False),
                (port or DEFAULT_PORT_TLS, True),
            ]
        else:
            self._transports = [
                (port or (DEFAULT_PORT_TLS if use_tls else DEFAULT_PORT_PLAIN), use_tls),
            ]
        self._transport_idx = 0
        # Set True once a connect succeeds so reconnects don't thrash between
        # ports — a Cerbo doesn't change transport across a reboot.
        self._transport_locked = False
        # Current attempt's port/TLS, surfaced for the connect log line.
        self._port, self._use_tls = self._transports[0]
        # The Cerbo's VRM portal id — captured from the first message that
        # matches our subscription wildcard. Used to address keepalive
        # publishes back at the right device.
        self.vrm_id: str | None = None
        self._client: Any = None  # aiomqtt.Client once connected
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # Per-instance client identifier suffix so two coordinators for
        # the same device (e.g. during a races reload where the old task
        # hasn't fully cancelled yet) don't collide on the broker — a
        # shared client ID would cause Venus OS's Mosquitto to kick the
        # older connection on every new one, resulting in a 2-second
        # reconnect loop. The token is short so logs stay readable.
        self._client_token = secrets.token_hex(3)

    @property
    def host(self) -> str | None:
        """The Cerbo's LAN host/IP this client connects to. Exposed so the
        dashboard can address the Remote Console (`http://<host>/`) even
        when the backend device row didn't echo `ip_address` back."""
        return self._host

    async def async_subscribe(self) -> None:
        """Connect to the Cerbo's broker and subscribe to system topics.

        Launches a background task that holds the aiomqtt context open
        for the coordinator's lifetime. Messages are dispatched to
        `_handle_message` as they arrive. The task exits cleanly on
        `async_disconnect`; transient network drops are retried in an
        inner loop so the stream re-establishes without customer
        intervention.
        """
        if self._task is not None:
            return
        self._stopping = False
        self._task = self._hass.async_create_background_task(
            self._run(),
            name=f"victron_cerbo_mqtt_{self._device_id}",
        )
        _LOGGER.info(
            "Cerbo MQTT connecting: %s:%d tls=%s user=%s device=%s",
            self._host, self._port, self._use_tls,
            self._username or "<none>", self._device_id,
        )

    async def async_disconnect(self) -> None:
        """Stop the background task + let aiomqtt close the socket."""
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                # Cancellation is the happy path; any other exception
                # from inside the task is logged by `_run` already, so
                # we swallow on the way out to avoid double-reporting.
                pass

    async def async_publish_keepalive(self) -> None:
        """Send one keepalive to keep the Cerbo broadcasting. Single source of
        the keepalive publish; `_keepalive_loop` calls this on a timer.

        No-op until we have a live client AND a captured VRM id. Before VRM
        capture there's nothing useful to send: MQTT 3.1.1 forbids wildcards in
        publish topic names and mosquitto 2.x rejects `R/+/keepalive` outright,
        so the wildcard "wake every Cerbo" trick we used to rely on was a silent
        no-op on modern brokers.

        That's harmless because the cold-start path doesn't actually need a
        keepalive: Venus OS publishes Serial + CustomName as retained topics, so
        the `N/+/#` subscription gets the first message within milliseconds of
        connect, the handler captures the VRM id from it, and the next keepalive
        lands at the addressed `R/<vrm>/keepalive` topic, which the broker
        accepts.

        A publish failure is NOT swallowed here: it propagates so the caller
        (`_keepalive_loop`) can count consecutive failures and recycle a dead
        connection. A no-op (no client / no VRM id yet) is not a failure.
        """
        client = self._client
        if client is None or not self.vrm_id:
            return
        topic = KEEPALIVE_TOPIC_TEMPLATE.format(vrm=self.vrm_id)
        await client.publish(topic, payload="", qos=0, retain=False)

    async def async_publish_value(
        self, service: str, instance: str, tail: str, value: Any,
    ) -> bool:
        """Write a value back to a Venus OS D-Bus path over MQTT.

        Venus accepts writes on the `W/<vrm>/<service>/<instance>/<tail>`
        topic with the same `{"value": <x>}` envelope it publishes reads
        on (`N/...`). Used to drive controls like the Multiplus inverter
        Mode (`W/<vrm>/vebus/<instance>/Mode`).

        Returns True only when the packet was handed to the broker. A
        no-op (returns False) until we have a live client AND a captured
        VRM id — there's no addressable topic before the first inbound
        message gives us the portal id, same constraint the keepalive
        publish lives under.
        """
        client = self._client
        if client is None or not self.vrm_id:
            return False
        topic = f"W/{self.vrm_id}/{service}/{instance}/{tail}"
        try:
            await client.publish(
                topic, payload=json.dumps({"value": value}), qos=0, retain=False,
            )
            return True
        except Exception:
            _LOGGER.warning(
                "Cerbo MQTT write %s failed for %s",
                topic, self._device_id, exc_info=True,
            )
            return False

    # -- internals ------------------------------------------------------------

    async def _run(self) -> None:
        """Background task: connect, subscribe, dispatch messages.

        Outer loop handles reconnect on transient failures (network
        blips, Cerbo reboot, etc.) with a short backoff. Inner `async
        with client:` owns one TCP socket and the subscription set for
        its lifetime.
        """
        try:
            import aiomqtt
        except ImportError:
            _LOGGER.error(
                "Cerbo MQTT %s: aiomqtt not installed — the Sensa Nomad "
                "integration's manifest pins it; if you're seeing this "
                "during development, run `pip install aiomqtt` in the HA "
                "venv and restart HA.",
                self._device_id,
            )
            return

        backoff = 2.0
        while not self._stopping:
            port, use_tls = self._transports[self._transport_idx]
            self._port, self._use_tls = port, use_tls
            # Set once the session is fully connected; stays None if the connect
            # attempt itself fails, so the backoff reset below only fires after a
            # genuinely-established session (not a failed connect).
            connect_monotonic: float | None = None
            try:
                # Only pass TLS kwargs when TLS is actually enabled.
                # aiomqtt hands `tls_insecure` to paho's
                # `tls_insecure_set()`, which requires an SSL context
                # to be configured FIRST — so passing `tls_insecure=False`
                # on a plaintext connection raises "Must configure SSL
                # context before using tls_insecure_set" every connect
                # cycle. Building the kwargs dict conditionally keeps
                # the plaintext path clean.
                client_kwargs: dict[str, Any] = {
                    "hostname": self._host,
                    "port": port,
                    "username": self._username,
                    "password": self._password,
                    "identifier": f"sensa-nomad-{self._device_id}-{self._client_token}",
                    "timeout": 10.0,
                }
                if use_tls:
                    # Venus OS's "MQTT on LAN (SSL only)" uses a self-
                    # signed cert. `verify_mode=NONE` keeps the
                    # connection working without the customer having
                    # to export and install the broker's CA cert —
                    # same stance requests takes for the Teltonika
                    # REST client (`_session.verify = False`).
                    client_kwargs["tls_params"] = aiomqtt.TLSParameters(
                        cert_reqs=ssl_module.CERT_NONE,
                    )
                    client_kwargs["tls_insecure"] = True
                async with aiomqtt.Client(**client_kwargs) as client:
                    self._client = client
                    # Subscribe to every topic Venus publishes under a
                    # VRM id so we see both the system-level aggregates
                    # (`system/0/…`) AND the per-device branches
                    # (`battery/256/…`, `solarcharger/288/…`, etc.).
                    # The coordinator owns the routing: each message
                    # is parsed into (service, instance, tail) and
                    # checked against the per-service templates in
                    # `victron_mqtt_services.py`. Anything outside the
                    # templates is silently dropped so unknown or
                    # purely-internal Venus topics don't spawn stray
                    # entities.
                    await client.subscribe("N/+/#", qos=0)
                    # No priming publish: Venus OS publishes Serial +
                    # CustomName as retained topics under
                    # `N/<vrm>/system/0/`, so subscribing to `N/+/#`
                    # triggers an immediate replay of those retained
                    # payloads from the broker. `_dispatch` captures
                    # the VRM id from the first message; from that
                    # tick onward `async_publish_keepalive` lands at
                    # the addressed `R/<vrm>/keepalive` topic. We
                    # used to also publish `R/+/keepalive` here as a
                    # belt-and-braces nudge but MQTT 3.1.1 forbids
                    # wildcards in publish topic names — mosquitto
                    # 2.x rejects the packet, so the line was a
                    # silent no-op anyway. Removing it makes the
                    # cold-start contract honest about what's
                    # actually doing the work (retained replay).
                    _LOGGER.info(
                        "Cerbo MQTT connected: subscribed N/+/system/0/# "
                        "on %s via %s:%d (%s) (routing %d known tails into "
                        "value cache)",
                        self._device_id, self._host, port,
                        "TLS" if use_tls else "plaintext",
                        len(SYSTEM_TOPICS),
                    )
                    # Lock onto this transport so a later reconnect doesn't
                    # flip ports: once we've seen a Venus broker answer here,
                    # that's the one.
                    self._transport_locked = True
                    connect_monotonic = time.monotonic()
                    # Pump inbound messages AND drive the keepalive on separate
                    # tasks, racing them so that EITHER a message-loop error OR a
                    # persistently-failing keepalive tears the connection down
                    # and lets the outer loop reconnect. The keepalive is the
                    # canary: Venus stops streaming about 60 s after the last
                    # keepalive, and a half-open socket makes client.publish
                    # raise paho code 4 ("not connected"), but
                    # `async for message in client.messages` can block
                    # indefinitely on that same half-open socket without ever
                    # raising, so the message loop alone never notices the drop.
                    # Racing the two surfaces the dead connection and reconnects,
                    # instead of the keepalive failing silently forever and every
                    # value freezing until an HA restart (the "power flow stuck
                    # until I restart HA" bug).
                    msg_task = asyncio.ensure_future(self._dispatch_loop(client))
                    ka_task = asyncio.ensure_future(self._keepalive_loop())
                    try:
                        done, _pending = await asyncio.wait(
                            {msg_task, ka_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        # Surface the first task's error (if any) so the outer
                        # handler runs the reconnect/backoff path.
                        for task in done:
                            if not task.cancelled() and task.exception() is not None:
                                raise task.exception()
                    finally:
                        for task in (msg_task, ka_task):
                            task.cancel()
                        for task in (msg_task, ka_task):
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                            except Exception:
                                # The losing race-task's real error (e.g. the
                                # MqttError describing why the stream actually
                                # dropped) would otherwise be discarded; log it
                                # at debug so a drop's root cause survives.
                                _LOGGER.debug(
                                    "Cerbo MQTT %s: secondary task ended with "
                                    "an error", self._device_id, exc_info=True,
                                )
                # Connection ended without raising (broker closed the stream
                # cleanly, or the message iterator simply ended). Null the stale
                # client and fall through to the shared reconnect delay below
                # rather than spinning the while-loop with no pause.
                self._client = None
                if not self._stopping:
                    _LOGGER.info(
                        "Cerbo MQTT %s stream ended, reconnecting",
                        self._device_id,
                    )
            except asyncio.CancelledError:
                self._client = None
                raise
            except Exception as err:  # noqa: BLE001
                self._client = None
                # Auto-detect: if the customer hasn't pinned a transport and
                # we haven't yet connected, flip plaintext vs TLS immediately
                # (no backoff) so an SSL-only Cerbo is found on the next attempt
                # instead of waiting out a backoff between every flip.
                if not self._transport_locked and len(self._transports) > 1:
                    self._transport_idx = (
                        self._transport_idx + 1
                    ) % len(self._transports)
                    next_tls = self._transports[self._transport_idx][1]
                    _LOGGER.debug(
                        "Cerbo MQTT %s connect failed on %s:%d (%s), "
                        "trying %s next",
                        self._device_id, self._host, port, err,
                        "TLS" if next_tls else "plaintext",
                    )
                    if self._transport_idx != 0:
                        # More transports left to try this round: retry now,
                        # skipping the shared backoff delay below.
                        continue
                    # Cycled through every transport: fall through to back off
                    # before the next full round so we don't DoS our retry loop.
                else:
                    _LOGGER.warning(
                        "Cerbo MQTT %s connection lost (%s), reconnecting",
                        self._device_id, err,
                    )
            # Shared reconnect delay, reached on a clean stream end AND on a
            # handled error (but NOT the immediate transport-flip `continue`).
            if self._stopping:
                break
            # Reset the backoff floor only after a session that stayed up long
            # enough to count as stable, so a fast-flapping link keeps escalating
            # toward the cap instead of resetting to 2 s on every brief connect.
            if connect_monotonic is not None and (
                time.monotonic() - connect_monotonic >= STABLE_CONNECTION_SECS
            ):
                backoff = 2.0
            await asyncio.sleep(backoff)
            # Exponential backoff, capped at 60 s, so a Cerbo offline for hours
            # doesn't DoS our retry loop but a quick recovery isn't made to wait.
            backoff = min(backoff * 2, 60.0)
        self._client = None

    async def _dispatch_loop(self, client: Any) -> None:
        """Pump inbound MQTT messages to the handler for the life of THIS
        connection. Returns when the message iterator ends (a clean broker
        close) and raises when it errors — `_run` treats either as a
        disconnect and reconnects."""
        async for message in client.messages:
            self._handle_message(message)

    async def _keepalive_loop(self) -> None:
        """Publish the Venus keepalive on a fixed cadence for the life of THIS
        connection, via `async_publish_keepalive` (the single publish point).

        This is the sole keepalive driver: it runs unconditionally for the life
        of the connection (the coordinator no longer publishes a keepalive at
        all). A SINGLE publish failure is tolerated (a transient
        hiccup, or a race with another publish on the shared client); after
        KEEPALIVE_MAX_FAILURES consecutive failures the link is treated as
        genuinely dead (paho "not connected"; Venus stops streaming anyway), so
        we raise to make `_run` recycle the connection rather than sit on a dead
        socket until an HA restart. A no-op publish (no VRM id captured yet) is
        not counted as a failure.
        """
        failures = 0
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECS)
            try:
                await self.async_publish_keepalive()
                failures = 0
            except Exception:
                failures += 1
                _LOGGER.debug(
                    "Cerbo MQTT %s: keepalive publish failed (%d/%d)",
                    self._device_id, failures, KEEPALIVE_MAX_FAILURES,
                    exc_info=True,
                )
                if failures >= KEEPALIVE_MAX_FAILURES:
                    raise

    def _handle_message(self, msg: Any) -> None:
        """MQTT callback — split topic, parse payload, hand to coordinator.

        Topic shape is `N/<vrm>/<service>/<instance>/<tail>`. We extract
        all four and pass them along so the coordinator can group
        values per `(service, instance)` and create one HA device per
        physical thing (each battery, each MPPT, each Multi, each
        tank sensor, etc.) rather than flattening everything into one.
        """
        topic = str(getattr(msg, "topic", "") or "")
        # aiomqtt wraps the topic in a `Topic` object whose str() yields
        # the raw topic path — same interface HA's mqtt component's msg
        # object exposed, so the parser stays identical.
        parts = topic.split("/")
        # Expected: ["N", vrm, service, instance, tail0, tail1, ...]
        if len(parts) < 5 or parts[0] != "N":
            return
        vrm = parts[1]
        service = parts[2]
        instance = parts[3]
        tail = "/".join(parts[4:])
        if not vrm or not service or not instance or not tail:
            return
        if self.vrm_id is None:
            self.vrm_id = vrm
            _LOGGER.info(
                "Cerbo MQTT captured VRM id %s for device %s",
                vrm,
                self._device_id,
            )
        value = _parse_venus_payload(getattr(msg, "payload", b""))
        try:
            self._on_value(vrm, service, instance, tail, value)
        except Exception:
            _LOGGER.warning(
                "Cerbo MQTT value handler failed for %s/%s/%s: %s",
                service, instance, tail, value,
                exc_info=True,
            )
