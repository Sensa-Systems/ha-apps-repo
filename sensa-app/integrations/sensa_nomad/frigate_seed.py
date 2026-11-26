"""Nominate the registered IP camera(s) into Frigate, the stack's camera service.

Frigate runs in the Container compose stack for a Pro-or-above licence (one
camera at Pro, the grey-waste dump camera). It reads ``/config/frigate/config.yml``
inside its own container, which is the ``frigate`` SUBPATH of Home Assistant's
``ha-config`` volume: the same bytes are this file, at ``/config/frigate/
config.yml`` from where the integration sits. So the integration owns that
file, exactly as it owns Zigbee2MQTT's ``configuration.yaml``: render it from
the registered cameras, write it only when the content changed, and ask the
App to restart the ``frigate`` service (the App has no Docker socket either;
it queues the action for the updater sidecar).

What Frigate is asked to do at Pro is deliberately small: one go2rtc restream
per camera and nothing else. No detector, no recording, no snapshots, no
birdseye. The restream is the point: Frigate holds the one connection to the
camera and every viewer (the Habitation Dump tab, and later the Vision PVR)
reads ``rtsp://127.0.0.1:8554/<slug>`` from it, so a camera that allows one
client still serves the whole van.

Fail-safe on the box, not on the customer: a write or a restart that fails is
logged and the camera entity falls back to the camera's own RTSP URL
(``camera.py``), so the picture still shows while the stack catches up.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

_LOGGER = logging.getLogger(__name__)

# Where Frigate's config lives from where the integration sits (the `frigate`
# subpath of ha-config). Overridable for tests and for a box laid out
# differently.
FRIGATE_CONFIG_PATH = os.environ.get(
    "SENSA_FRIGATE_CONFIG_PATH", "/config/frigate/config.yml",
)

# Frigate's go2rtc RTSP restream, reachable from Home Assistant on the host
# network because the compose service publishes 8554.
RESTREAM_HOST = os.environ.get("SENSA_FRIGATE_RESTREAM_HOST", "127.0.0.1")
RESTREAM_PORT = int(os.environ.get("SENSA_FRIGATE_RESTREAM_PORT", "8554"))

# The compose service the App restarts for us.
FRIGATE_SERVICE = "frigate"

# The config schema version Frigate 0.17 stamps after migrating a file. Stated
# up front so it never migrates ours (see render_config).
FRIGATE_CONFIG_VERSION = "0.17-0"

DEFAULT_RTSP_PORT = 554
DEFAULT_STREAM_PATH = "stream1"

# First line of every file we write, so a file that is NOT ours (Frigate's own
# first-boot default, or a hand edit) is recognisable in a log line.
_MARKER = "# Rendered by Sensa Nomad (frigate_seed.py); edits are overwritten."

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def camera_slug(device: dict[str, Any]) -> str:
    """Frigate's name for this camera: stable, unique, URL-safe.

    Derived from the device id (``rtsp_camera-<mac>``) rather than the display
    name, which the customer can rename; the restream URL must not move when
    they do.
    """
    raw = str(device.get("device_id") or "camera").lower()
    slug = _SLUG_RE.sub("_", raw).strip("_")
    if not slug or slug[0].isdigit():
        slug = f"cam_{slug}"
    return slug


def camera_rtsp_url(device: dict[str, Any]) -> str:
    """The camera's OWN RTSP URL from its config: host, port, stream path and
    an optional login. This is what Frigate connects to, and what the camera
    entity falls back to when Frigate is not there."""
    config = device.get("config") or {}
    host = str(config.get("host") or device.get("ip_address") or "").strip()
    port = int(config.get("port") or DEFAULT_RTSP_PORT)
    path = str(config.get("stream_path") or DEFAULT_STREAM_PATH).strip().lstrip("/")
    user = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    auth = ""
    if user:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    port_part = "" if port == DEFAULT_RTSP_PORT else f":{port}"
    return f"rtsp://{auth}{host}{port_part}/{path}"


def restream_url(device: dict[str, Any]) -> str:
    """Frigate's restream of this camera, the URL every viewer on the box reads."""
    return f"rtsp://{RESTREAM_HOST}:{RESTREAM_PORT}/{camera_slug(device)}"


def is_ip_camera(device: dict[str, Any]) -> bool:
    integration = (device.get("integration_type") or device.get("integration") or "")
    return integration == "sensa_rtsp_camera" or device.get("service_class") == "ip_camera"


def render_config(devices: list[dict[str, Any]]) -> str:
    """Frigate's config.yml for these cameras.

    One restream per camera and nothing else. With no camera at all the file
    still carries the marker and the `mqtt` block, so Frigate starts (it
    onboards with no cameras rather than refusing) and the next nomination
    only has to rewrite it.
    """
    import yaml

    cameras = [d for d in devices if is_ip_camera(d) and camera_rtsp_url(d).split("//", 1)[1].split("/", 1)[0].rstrip("@")]
    doc: dict[str, Any] = {
        # Frigate's config schema version. Without it Frigate "migrates" the
        # file on every boot and writes it back reformatted, so the next seed
        # saw a different file and bounced Frigate again (box, 2026-09-05).
        "version": FRIGATE_CONFIG_VERSION,
        "mqtt": {"enabled": False},
        # Nothing but the restream at Pro. Each is a whole subsystem with its
        # own CPU cost on a Pi, and the Vision PVR is the design that turns
        # them on (docs/system-design/vision.md).
        "detect": {"enabled": False},
        "record": {"enabled": False},
        "snapshots": {"enabled": False},
        "birdseye": {"enabled": False},
    }
    if cameras:
        doc["go2rtc"] = {"streams": {camera_slug(d): [camera_rtsp_url(d)] for d in cameras}}
        doc["cameras"] = {
            camera_slug(d): {
                "enabled": True,
                "ffmpeg": {
                    "inputs": [{
                        "path": restream_url(d),
                        "input_args": "preset-rtsp-restream",
                        # Frigate wants a role on every input. `detect` is
                        # the one it accepts for a plain stream; detection
                        # itself stays off above.
                        "roles": ["detect"],
                    }],
                },
                "detect": {"enabled": False},
            }
            for d in cameras
        }
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    return f"{_MARKER}\n{body}"


def _same_config(existing: str, content: str) -> bool:
    """Whether two config texts mean the same thing.

    Compared as YAML documents, not bytes: Frigate rewrites the file it reads
    (key order, quoting, a migration stamp), so a byte comparison saw a change
    on every boot and restarted Frigate for nothing. An unparseable existing
    file counts as different, so it is replaced.
    """
    if existing == content:
        return True
    try:
        import yaml

        return yaml.safe_load(existing) == yaml.safe_load(content)
    except Exception:  # noqa: BLE001 - not YAML: replace it
        return False


def _write_if_changed(path: str, content: str) -> bool:
    """Write `content` to `path` iff it differs in meaning. True when written."""
    try:
        with open(path, encoding="utf-8") as fh:
            if _same_config(fh.read(), content):
                return False
    except FileNotFoundError:
        pass
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)
    return True


async def async_ensure_config(hass: Any, devices: list[dict[str, Any]], path: str | None = None) -> bool:
    """Render and write Frigate's config for `devices`. True iff it changed."""
    target = path or FRIGATE_CONFIG_PATH
    content = render_config(devices)
    return await hass.async_add_executor_job(_write_if_changed, target, content)


async def async_apply(hass: Any, devices: list[dict[str, Any]], client: Any | None, path: str | None = None) -> bool:
    """Write the config and, only when it changed, ask the App to restart
    Frigate. Best-effort: nothing here raises into an add or a Save.

    Returns whether the config changed."""
    try:
        changed = await async_ensure_config(hass, devices, path)
    except Exception as err:  # noqa: BLE001 - never break the caller
        _LOGGER.warning("Frigate seed: could not write %s: %s", path or FRIGATE_CONFIG_PATH, err)
        return False
    if not changed:
        _LOGGER.debug("Frigate seed: config already current at %s", path or FRIGATE_CONFIG_PATH)
        return False
    _LOGGER.info(
        "Frigate seed: wrote %s for %d camera(s)",
        path or FRIGATE_CONFIG_PATH, sum(1 for d in devices if is_ip_camera(d)),
    )
    if client is None:
        _LOGGER.warning("Frigate seed: no App client to signal a restart; Frigate picks the config up on its next restart")
        return True
    try:
        await client.async_container_action(service=FRIGATE_SERVICE, action="restart")
        _LOGGER.info("Frigate seed: requested a Frigate restart")
    except Exception as err:  # noqa: BLE001 - the config landed; a restart hiccup is not fatal
        _LOGGER.warning("Frigate seed: restart signal failed (config written): %s", err)
    return True


# Whether the restream port answers, remembered briefly so a stream-source
# lookup does not probe on every call.
_RESTREAM_CACHE: dict[str, Any] = {"ok": None, "at": 0.0}
_RESTREAM_TTL_S = 30.0


async def async_restream_reachable(timeout: float = 0.5) -> bool:
    """True when Frigate's restream port answers on this box.

    A Base box, a HAOS box, or a Container box whose stack has not yet been
    updated has no Frigate, and the camera entity must then read the camera
    directly rather than a URL with nothing behind it.
    """
    now = time.monotonic()
    if _RESTREAM_CACHE["ok"] is not None and now - _RESTREAM_CACHE["at"] < _RESTREAM_TTL_S:
        return bool(_RESTREAM_CACHE["ok"])
    ok = False
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(RESTREAM_HOST, RESTREAM_PORT), timeout=timeout,
        )
        writer.close()
        ok = True
    except Exception:  # noqa: BLE001 - not there is the normal answer on many boxes
        ok = False
    _RESTREAM_CACHE["ok"] = ok
    _RESTREAM_CACHE["at"] = now
    return ok


def reset_restream_cache() -> None:
    _RESTREAM_CACHE["ok"] = None
    _RESTREAM_CACHE["at"] = 0.0
