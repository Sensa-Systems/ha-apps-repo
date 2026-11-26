"""Fixed Nomad Modes catalog.

A Mode is the customer-facing wrapper over an HA scene. Tapping a mode
tile on the Habitation dashboard calls `sensa_nomad.activate_mode`,
which fires `scene.turn_on scene.nomad_<slug>`. Modes are latched —
they stay active until the customer picks another one.

Why a Python module instead of a JSON file: the catalog holds typed
icons + optional per-mode policy (future things like
`requires_location: true` for Sentry), and a vendor module that
contributes mode defaults should be able to `from
.modes_catalog import MODES` and iterate. JSON would force string-
keyed dict access everywhere.

Customer rationale for the 8 chosen modes (Richard's set):
  * Service   — maintenance / technician access. Everything unlocked
                and visible so a service visit doesn't need the owner
                present.
  * Sentry    — unattended-vehicle watch. Security lights armed,
                motion cameras active.
  * Drive     — vehicle is moving (slug `driving`). Cab concerns over
                habitation: distraction-free lighting, GPS active,
                non-essentials off.
  * Parked    — just arrived / setting up (slug `parking`). Awnings,
                levellers, habitation systems coming online.
  * Standby   — low-power hold. Everything non-critical off, BMS
                allowed to balance, fridge keeps cold.
  * Work      — daytime workspace. Bright cool lighting, comfortable
                HVAC, network prioritised.
  * Movie     — entertainment. Dim warm lighting, heating at comfort
                setpoint, networking unthrottled.
  * Sleep     — overnight. Lights off, quiet systems, security on,
                charging disabled until dawn.
  * Startup   - the state the van boots into; the default startup
                scene on a fresh install.
  * SOS       - danger to life: calling for help. Sensa emails the
                emergency contacts entered in MySensa. Shown only while
                Hub's `emergency` feature gate is on (GATED_MODES).
  * Panic     - someone is trying to break in: lights, siren, locks.

Order in `MODES` = display order in the Modes view tile grid.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ModeDef:
    """Fixed-catalog definition of a single Mode.

    Attributes:
      slug:
        Stable lowercase identifier. Used verbatim in
        `scene.nomad_<slug>` entity_ids, the activate-mode service
        payload, and the `device_catalog.json` per-device `modes`
        blocks. MUST NOT CHANGE once customers have added devices —
        renaming a slug would orphan every existing scene.
      name:
        Display label shown on the Modes tile, the editor card and the
        current-mode sensor state. Human-facing; safe to change — the
        scene entity_id is derived from `slug` (via `_build_scenes_dict`),
        NOT from this name, so renaming a label never moves a scene.
      icon:
        MDI icon rendered on the tile. Safe to change.
    """

    slug: str
    name: str
    icon: str


MODES: tuple[ModeDef, ...] = (
    ModeDef("service",  "Service",  "mdi:wrench"),
    ModeDef("sentinel", "Sentry",   "mdi:shield-home"),
    ModeDef("driving",  "Drive",   "mdi:steering"),
    ModeDef("parking",  "Parked",  "mdi:parking"),
    ModeDef("standby",  "Standby",  "mdi:power-standby"),
    ModeDef("work",     "Work",     "mdi:briefcase"),
    ModeDef("movie",    "Movie",    "mdi:movie-open"),
    ModeDef("sleep",    "Sleep",    "mdi:sleep"),
    ModeDef("startup",  "Startup",  "mdi:power"),
    ModeDef("sos",      "SOS",      "mdi:alarm-light"),
    ModeDef("panic",    "Panic",    "mdi:shield-alert"),
)

MODE_SLUGS: tuple[str, ...] = tuple(m.slug for m in MODES)

# Modes the instincts switch to on their own (Drive when the van sets off,
# Parked when it stops). The customer can hide any OTHER preset from the
# tiles / pills / tabs, but not these: hiding one would leave the van
# sitting in a scene the customer can't see or pick.
LOCKED_MODE_SLUGS: tuple[str, ...] = ("driving", "parking")

# Modes a tile must be HELD to fire, never tapped: a pocket or an elbow must
# not call for help. The tiles draw these red. Published per scene as `hold`
# on the current-mode sensor's `modes`, which is the only place the cards read.
HOLD_MODE_SLUGS: tuple[str, ...] = ("sos", "panic")

# Presets that exist only while a Hub feature gate is on for this customer:
# slug -> gate key. SOS and the emergency contacts it emails are one feature,
# released together, so the van never offers an SOS that alerts nobody. A
# gated-off preset is left out of the scene list every card renders from and
# refused by `activate_mode`; it still exists, so turning the gate on needs no
# scene rebuild.
GATED_MODES: dict[str, str] = {"sos": "emergency"}

# Customer-created scenes get a slug under this prefix so they can never
# collide with a preset (present or future) and are recognisable in
# entity_ids (`scene.nomad_custom_<x>`, `binary_sensor.sensa_nomad_mode_custom_<x>_active`).
CUSTOM_SLUG_PREFIX = "custom_"
_CUSTOM_SLUG_MAX = 40


def custom_mode_slug(name: str, taken: Iterable[str]) -> str:
    """Derive a stable, unique slug for a customer-created scene from its
    name. Derived ONCE at creation and never changed by a rename (the slug
    is baked into the scene entity_id + the active sensor entity_id)."""
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    base = base[:_CUSTOM_SLUG_MAX] or "scene"
    existing = set(taken)
    slug = f"{CUSTOM_SLUG_PREFIX}{base}"
    n = 2
    while slug in existing:
        slug = f"{CUSTOM_SLUG_PREFIX}{base}_{n}"
        n += 1
    return slug


def is_custom_slug(slug: str) -> bool:
    return slug.startswith(CUSTOM_SLUG_PREFIX)

# Sentinel value for the "Default scene on startup" choice that restores
# whatever mode was active before the last HA restart (resolved to the real
# slug at startup via `ModeOverrides.get_last_active_mode`). Not a real mode,
# so it's accepted alongside MODE_SLUGS wherever the startup choice is set.
STARTUP_MODE_PREVIOUS = "previous"


def scene_entity_id(mode_slug: str) -> str:
    """HA entity_id convention for the generated scene of a mode.

    Used both when writing scenes.yaml (this is the declared id) and
    when building the activate-mode service call. Keep in sync with
    the `id:` field emitted by `modes._build_scenes_dict`.
    """
    return f"scene.nomad_{mode_slug}"


def mode_active_entity_id(mode_slug: str) -> str:
    """HA entity_id of the per-mode "is this mode current" binary sensor.

    `binary_sensor.SensaModeActiveBinarySensor` declares its entity_id from
    this, and anything asking "is the van in Drive right now?" reads it back
    through this same function -- the Parked instinct is the first such caller.
    Written once because the producer and the reader drifting apart would give
    a condition that is silently never true.
    """
    return f"binary_sensor.sensa_nomad_mode_{mode_slug}_active"


def get_mode(slug: str) -> ModeDef | None:
    """Look up a mode by slug. Returns `None` for unknown slugs so
    callers can no-op gracefully instead of crashing the service."""
    for mode in MODES:
        if mode.slug == slug:
            return mode
    return None
