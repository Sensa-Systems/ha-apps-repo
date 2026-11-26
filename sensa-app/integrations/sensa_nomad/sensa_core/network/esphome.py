"""ESPHome device detection and event stream parsing.

Vendor-agnostic: works with any ESPHome device. Callers pass a field mapping
to extract vendor-specific metadata from the SSE event stream.
"""

from __future__ import annotations

import base64
import gzip
import json
import socket
import urllib.error
import urllib.request

DEFAULT_HTTP_PORT = 80
DEFAULT_TIMEOUT = 3.0


def _build_basic_auth_header(username: str, password: str) -> str:
    credentials = f"{username}:{password}".encode()
    return f"Basic {base64.b64encode(credentials).decode('ascii')}"


def fetch_esphome_root_page(
    ip: str,
    username: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """Fetch the root page from an ESPHome device using basic auth."""
    url = f"http://{ip}/"
    headers = {"User-Agent": "Sensa/1.0"}
    if username or password:
        headers["Authorization"] = _build_basic_auth_header(username, password)

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            charset = response.headers.get_content_charset() or "utf-8"
    except (urllib.error.URLError, Exception):  # noqa: BLE001
        return None

    # ESPHome web_server v3 with `local: true` inlines the whole UI bundle and
    # serves the root page gzip-compressed — that's the only copy it holds in
    # flash, so Accept-Encoding can't opt out and urllib won't decompress it
    # for us. Without this, the web-shell check sees raw gzip bytes (no
    # `<esp-app>`) and every local:true device is dropped from discovery.
    # Fall back to the gzip magic bytes if the header is missing.
    if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            return None

    return raw.decode(charset, errors="ignore")


def is_esphome_web_shell(html: str) -> bool:
    """Return True if the HTML page looks like the ESPHome web_server v3 shell.

    The defining marker is the ``<esp-app>`` custom element. The UI JavaScript
    may be loaded from the esphome CDN (``oi.esphome.io/v3/www.js``) OR, on
    devices flashed with ``web_server: { local: true }``, embedded and served
    from the device itself — in which case the page never references the CDN
    URL. We must NOT require the CDN string, or local-only firmware (every
    Sensa device, by policy) is silently dropped from discovery. The
    subsequent ``/events`` Sensa-marker gate is the real discriminator, so
    keying on ``<esp-app>`` plus a script tag here is both sufficient and safe.
    """
    html_lower = html.lower()
    return "<esp-app>" in html_lower and "<script" in html_lower


def _markers_complete(data: bytes, markers: list[str]) -> bool:
    """True when every marker is present AND the line carrying it has ended.

    A marker matches near the START of its ``data:`` line (inside
    ``"name_id":"text_sensor/<marker>"``), so stopping the read the moment the
    bytes appear cuts that line short, its JSON fails to parse, and the field
    reads as missing. Which device that hits is down to where a 2048-byte recv
    happens to split the stream: the Autoterm Air heater (2026-10-05) lost
    ``Sensa Model`` this way and never reached Discovery.
    """
    for marker in markers:
        idx = data.find(marker.encode())
        if idx < 0 or data.find(b"\n", idx) < 0:
            return False
    return True


def fetch_esphome_events_chunk(
    ip: str,
    username: str = "",
    password: str = "",
    port: int = DEFAULT_HTTP_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = 32768,
    required_markers: list[str] | None = None,
) -> str | None:
    """Fetch a chunk from the ESPHome /events SSE stream using a raw socket.

    Reads until all ``required_markers`` are found in the response body with
    their lines complete, or ``max_bytes`` is reached, or the socket times out.
    The cap is a backstop, not the normal stop: the Sensa metadata streams
    after every regular entity, and on the Autoterm Air it starts ~8KB in, so
    the old 8192 cap left no room for one more entity.
    """
    headers = "User-Agent: Sensa/1.0\r\nAccept: text/event-stream\r\nConnection: close\r\n"
    if username or password:
        headers += f"Authorization: {_build_basic_auth_header(username, password)}\r\n"

    request = (
        f"GET /events HTTP/1.1\r\n"
        f"Host: {ip}\r\n"
        f"{headers}"
        f"\r\n"
    ).encode()

    chunks: list[bytes] = []
    total = 0

    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)

            while total < max_bytes:
                try:
                    chunk = sock.recv(min(2048, max_bytes - total))
                except TimeoutError:
                    break
                if not chunk:
                    break

                chunks.append(chunk)
                total += len(chunk)

                if required_markers and _markers_complete(
                    b"".join(chunks), required_markers,
                ):
                    break
    except Exception:  # noqa: BLE001
        return None

    if not chunks:
        return None

    raw = b"".join(chunks)
    parts = raw.split(b"\r\n\r\n", 1)
    body = parts[1] if len(parts) == 2 else raw
    return body.decode("utf-8", errors="ignore")


def parse_esphome_event_fields(
    events_text: str,
    field_mapping: dict[str, str],
    required: list[str] | None = None,
) -> dict[str, str] | None:
    """Parse fields from an ESPHome SSE event stream.

    ``field_mapping`` maps ESPHome sensor names to output keys, e.g.::

        {
            "Sensa Device Type": "device_type",
            "Sensa Model": "model",
        }

    Every mapped field present in the stream is captured. ``required`` is the
    subset of output keys that MUST be present for the parse to succeed — only
    the identifying fields (device_type + model) should gate discovery, so a
    device isn't dropped just because the firmware's metadata set drifts
    (extra fields like ``Sensa Channel``, or dropped ``Device MAC Address``).
    When ``required`` is None, all mapped keys are required (legacy behaviour).

    Returns the captured dict, or None if any required key is missing.
    """
    found: dict[str, str] = {}

    for raw_line in events_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data: "):
            continue

        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        name = payload.get("name")
        if name in field_mapping:
            value = payload.get("state") or payload.get("value")
            if value:
                found[field_mapping[name]] = str(value).strip()

    needed = required if required is not None else list(field_mapping.values())
    if all(key in found for key in needed):
        return found
    return None
