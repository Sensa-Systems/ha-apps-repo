"""Find a USB serial device plugged into the box, by USB vendor id.

Shared by everything that drives a dongle over a tty (the BLE sniffer, the USB
GPS), so the sysfs walk and the device-node workaround exist once.

On a Container box Home Assistant sees the host's sysfs but Docker does not
hot-plug device nodes, so a missing ``/dev`` node is created from sysfs's
major:minor. That needs the compose stack's ``device_cgroup_rules`` (166 =
ttyACM, 188 = ttyUSB); without them ``mknod`` or the later open fails.

Blocking throughout: call from an executor.
"""

from __future__ import annotations

import os
import stat

_TTY_BASE = "/sys/class/tty"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


def _usb_attr(name: str, attr: str) -> str | None:
    """A USB device attribute (idVendor, product) for tty ``name``.

    The tty's ``device`` link points at the USB INTERFACE; the attribute lives
    on its parent, the device. Some drivers link the device itself, hence the
    second read.
    """
    dev_dir = os.path.realpath(os.path.join(_TTY_BASE, name, "device"))
    return _read(os.path.join(dev_dir, "..", attr)) or _read(os.path.join(dev_dir, attr))


def matching_ttys(vendors: frozenset[str]) -> list[str]:
    """tty names (``ttyACM0``) whose USB vendor id is in ``vendors``, sorted.

    Raises OSError when sysfs has no tty class at all (not Linux, or a
    container that cannot see it), so a caller can say so rather than
    reporting "not plugged in".
    """
    names = sorted(n for n in os.listdir(_TTY_BASE) if n.startswith(("ttyACM", "ttyUSB")))
    return [n for n in names if (_usb_attr(n, "idVendor") or "").lower() in vendors]


def usb_product(name: str) -> str | None:
    """The USB product string for tty ``name`` (``u-blox 7 - GPS/GNSS Receiver``)."""
    return _usb_attr(name, "product")


def ensure_dev_node(name: str) -> tuple[str | None, str | None]:
    """(``/dev/<name>``, None), or (None, the OS error) when it cannot be made."""
    path = f"/dev/{name}"
    if os.path.exists(path):
        return path, None
    numbers = _read(os.path.join(_TTY_BASE, name, "dev"))
    try:
        major, minor = (int(x) for x in (numbers or "").split(":"))
        os.mknod(path, 0o660 | stat.S_IFCHR, os.makedev(major, minor))
    except (OSError, ValueError) as err:
        return None, str(err)
    return path, None
