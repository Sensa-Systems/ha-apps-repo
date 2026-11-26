"""Dataclasses and exceptions for the licensing module."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Canonical deployment values live in one place so the device, Hub and licensing
# Function all key off the same EXACT strings (see deployment.py). Re-exported
# here for callers that import them from `models`.
from sensa_core.licensing.deployment import DEPLOYMENT_TYPES, normalise_deployment

# -- Exceptions --------------------------------------------------------------


class LicenceInvalidError(Exception):
    """Raised when a JWT fails validation (signature, expiry, fingerprint)."""


class TrialExhaustedError(Exception):
    """Raised when the cloud API refuses a trial (fingerprint already used one)."""

    def __init__(self, purchase_url: str, message: str) -> None:
        super().__init__(message)
        self.purchase_url = purchase_url


class CloudUnavailableError(Exception):
    """Raised when the cloud licensing API cannot be reached."""


# -- Models ------------------------------------------------------------------


@dataclass(frozen=True)
class FingerprintComponents:
    """Hashed hardware components that form the composite fingerprint.

    Each field holds a hex SHA-256 of the raw value. The raw values never
    leave the device — only these hashes are sent to the cloud.
    """

    mac: str
    board_serial: str
    ha_uuid: str
    cpu_count: str
    mem_bytes: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class LicenceClaims:
    """Parsed, verified JWT claims."""

    # Who OWNS the licence. Absent on every paid key: ownership is mutable (a dealer
    # sells stock licences and the buyer is attached afterwards) and a key is signed
    # once and never re-issued, so an owner in the claims would be frozen at issue
    # time. It is resolved live from the licence record instead (Hub does this for
    # Radar Premium). Still present on TRIAL keys, where `trial_<fingerprint>` is
    # the only identifier they carry -- trials have no `licence_id`.
    customer_id: str | None
    product: str
    editions: dict[str, str]
    trial: bool
    hardware_id: str | None
    issued_at: datetime
    # None = perpetual licence (no expiry claim in the key).
    expires_at: datetime | None = None
    trial_expires_at: datetime | None = None
    phone_home_required: bool = True
    phone_home_interval_hours: int = 24
    # Optional dealer ownership: {"id": <dealer slug>, "homepage_mode":
    # "additive" | "exclusive"}. Baked into the key by the licensing Function
    # for dealer-branded licences; absent (None) for normal licences.
    # The Nomad dashboard matches `id` against a catalog entry's `dealer` tag
    # to surface that dealer's private home layout(s).
    dealer: dict[str, str] | None = None
    # Deployment type the licence was sold for: "byo" | "enthusiast" |
    # "appliance". The three products ship different features at different
    # prices, so the Hub stamps which one the customer bought. ENFORCED as an
    # anti-arbitrage backstop (a cheaper product's licence must not run on a
    # dearer box) but FAIL-OPEN: the App only drops to read-only on a mismatch
    # against this box's deployment_mode, and an absent value (older keys / unset)
    # never locks — so it's recoverable, never a brick (see
    # routes/license.py handle_get_status for the gate, and the Hub for the
    # primary redeem-time refusal). Absent (None) on older keys / when unset.
    deployment_type: str | None = None
    # Maximum confirmed devices this licence may register. Stamped by the Hub
    # from the sold edition's default (lite 30 / plus 50 / vision unlimited),
    # editable per-licence for goodwill bumps. None = no explicit claim → the
    # device falls back to the edition default (see licensing/limits.py).
    max_devices: int | None = None
    # The LICENCE's own id (e.g. "lic_abc") — distinct from `customer_id`, which
    # identifies the BUYER. Both are needed and they are not interchangeable: a
    # customer can hold several licences. This is the id you look a box up by in
    # Hub, and the one Hub's Radar Premium resolve audits against. Carried as the
    # `licence_id` claim (older keys used `sub`); None when the key predates it.
    #
    # It went unparsed for a long time, and `routes/license.py` reported
    # `customer_id` under the name `licence_id` — so "the licence id" shown on a
    # box was really the customer id. Kept as separate fields now so support can
    # tell exactly which licence a box is running.
    licence_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LicenceClaims:
        dealer_raw = payload.get("dealer")
        # theme_mode governs the dealer's frontend THEME independently of
        # homepage_mode (which governs the home-page layouts): "additive" =
        # dealer + public themes, "exclusive" = only the dealer's theme is
        # offered (plus HA's `default` as an escape hatch), "locked" = the
        # dealer's theme and no way out, with the picker hidden and the public
        # theme files never installed. Lenient: absent / empty / any unknown
        # value → "additive" (the safe, non-restrictive default) so a Hub
        # spelling drift can never hide the public themes. That leniency is also
        # the forward-compat story: a box on an older Core folds a theme_mode it
        # has never heard of down to "additive" and simply does not lock, so
        # Control ships before any dealer is flipped to `locked`.
        _theme_mode = "additive"
        if isinstance(dealer_raw, dict):
            _tm = str(dealer_raw.get("theme_mode", "") or "additive").strip().lower()
            if _tm in ("additive", "exclusive", "locked"):
                _theme_mode = _tm
        dealer = (
            {
                "id": str(dealer_raw.get("id", "")),
                "homepage_mode": str(dealer_raw.get("homepage_mode", "") or "additive"),
                "theme_mode": _theme_mode,
            }
            if isinstance(dealer_raw, dict) and dealer_raw.get("id")
            else None
        )
        # The sold deployment type (byo | enthusiast | appliance). Lenient: only
        # a known value survives; anything else (absent, empty, typo) → None so
        # it never accidentally gates. A NON-EMPTY unrecognised value is a Hub
        # spelling drift that would silently disable enforcement — log it loudly.
        # Accepts the older claim name `install_type` as a fallback so a licence
        # signed before the `deployment_type` rename still parses.
        _dt_raw = payload.get("deployment_type") or payload.get("install_type")
        deployment_type = None
        if _dt_raw not in (None, ""):
            _dt = normalise_deployment(_dt_raw)
            if _dt:
                deployment_type = _dt
            else:
                _LOGGER.warning(
                    "Licence carries unrecognised deployment_type %r; treating "
                    "as unset (enforcement disabled for this key). The Hub must "
                    "emit one of %s.",
                    _dt_raw, DEPLOYMENT_TYPES,
                )
        return cls(
            # `.get`, not `[...]`: paid keys no longer carry an owner at all.
            customer_id=payload.get("customer_id") or None,
            # `sub` is the older spelling; Hub's Radar resolve reads the same pair.
            licence_id=payload.get("licence_id") or payload.get("sub") or None,
            product=payload.get("product", "sensa_control"),
            editions=dict(payload.get("editions", {})),
            trial=bool(payload.get("trial", False)),
            hardware_id=payload.get("hardware_id"),
            issued_at=_parse_iso(payload["issued_at"]),
            expires_at=(
                _parse_iso(payload["expires_at"])
                if payload.get("expires_at")
                else None
            ),
            trial_expires_at=(
                _parse_iso(payload["trial_expires_at"])
                if payload.get("trial_expires_at")
                else None
            ),
            phone_home_required=bool(payload.get("phone_home_required", True)),
            phone_home_interval_hours=int(payload.get("phone_home_interval_hours", 24)),
            dealer=dealer,
            deployment_type=deployment_type,
            max_devices=(
                int(payload["max_devices"])
                if payload.get("max_devices") is not None
                else None
            ),
        )


@dataclass
class PhoneHomeResult:
    """Outcome of a phone-home attempt."""

    ok: bool
    status: str  # "valid" | "conflict" | "revoked" | "trial_expired" | "unreachable"
    checked_at: datetime
    next_phone_home_before: datetime | None = None
    trial_days_remaining: int | None = None
    message: str | None = None
    grace_expires_at: datetime | None = None
    # Absolute URL to the dealer's logo, handed back by the cloud for dealer
    # dealer-branded licences so the device can download + cache it for the
    # panel header. None for normal licences / when the cloud sends nothing.
    dealer_logo_url: str | None = None
    # Break-glass remote-support grant the customer/operator issued in the Hub
    # (e.g. `{window_hours, requested_at, reason}`). Handed DOWN so the device
    # opens support when the customer can't reach the on-device toggle. None
    # when no grant is pending.
    remote_support_grant: dict | None = None
    # Sensa-hosted cloud backup: `{enabled, container_url, sas_token}`, handed
    # DOWN so the device can upload straight to Blob Storage without the App
    # relaying a gigabyte. Support-gated in the CLOUD -- a lapsed contract stops
    # issuing one and the destination simply disappears -- which is what makes
    # the gate hold on a box we do not control. None when the licence does not
    # include cloud backup.
    cloud_backup: dict | None = None
    # OTA container-registry pull credential for an OTA-entitled device
    # (`{registry, username, password}`), handed DOWN so HA Supervisor can pull
    # the private add-on image. None when the cloud sends nothing (feature off,
    # or the device should leave its registries untouched).
    registry_credential: dict | None = None
    # Explicit OTA revocation signal: "disabled" when the licence's support
    # entitlement has lapsed, telling the device to REMOVE the stored registry
    # credential so OTA stops. None on every other response (fail-open: removal
    # only ever happens on this explicit signal, never on mere absence).
    registry_access: str | None = None
    # Staged-rollout pin: the exact add-on version this device's rollout group is
    # assigned (e.g. "1.0.0-edge.42"), handed DOWN so a canary unit is pulled
    # ahead of the channel repo's broad version. None when the device should
    # follow its channel repo's advertised version (the "other"/default group).
    # Honoured device-side only on appliances, forward-only (never a downgrade).
    target_version: str | None = None
    # The SECOND rollout lever, and a different machine: the Venus OS version
    # this device's rollout group may move its Victron GX device to. None means
    # the group names NO ceiling, and the device falls back to the customer's own
    # "Automatic Victron updates" toggle: they cannot see this column, so a
    # switch they have left on must not be silently inert. Not appliance-gated,
    # unlike `target_version`: the Nomad integration drives the GX update on
    # every install type.
    #
    # A CEILING, not a shopping list. Venus has no "install version X" control
    # (its only lever installs whatever its own feed currently offers), so the
    # device acts on this by installing anything on offer that is AT OR BELOW
    # this version: that walks a van up to the ceiling and then holds it there.
    # Pinned at 3.2 on 2.45 it takes 3.0, then 3.12, and refuses 3.47 for good.
    cerbo_target_version: str | None = None
    # The THIRD rollout lever: the RutOS version this device's rollout group may
    # move its Teltonika ROUTER to. Routers only, and deliberately separate from
    # any switch pin, because Teltonika ships two unrelated firmware streams
    # (a router reports `RUTX_R_00.07.24.3`, a TSW switch `TSW2_R_00.01.10.2`),
    # so one value could only ever be correct for one of the two.
    #
    # Same reading of silence as the Cerbo's: None means no ceiling, which leaves
    # the customer's own toggle deciding, and the device then takes the STABLE
    # stream only. A CEILING, because RutOS can be told which STREAM to take but
    # not which version, so the device installs what it is offered at or below
    # this.
    teltonika_target_version: str | None = None
    # The same again for the ring's Teltonika SWITCHES, which run their own
    # firmware stream, and held separately because a router version aimed at a
    # switch is the mistake the split exists to make impossible.
    teltonika_switch_target_version: str | None = None
    # Absolute URL to the dealer's frontend THEME yaml, handed back by the cloud
    # for dealer-branded licences so the device can download + cache it to
    # /config/themes/ (mirrors dealer_logo_url). None for normal licences / when
    # the cloud sends nothing.
    dealer_theme_url: str | None = None
    # Absolute URL to the dealer's home-LAYOUT bundle, handed back by the cloud
    # for dealer-branded licences so the device can download + cache the layouts
    # that dealer publishes (mirrors dealer_theme_url). Layouts moved out of the
    # integration so a van carries only its own dealer's, and a layout change
    # reaches that dealer's boxes on the next phone-home instead of needing a
    # release plus a fleet-wide OTA. None for normal licences / when the cloud
    # sends nothing, which the device reads as "serve the public layouts".
    dealer_layouts_url: str | None = None
    # WHERE to fetch the customer's household for the Settings -> People tab.
    # A URL, not the people themselves, mirroring the dealer asset URLs: the
    # response stays small and the endpoint can move without a fleet release.
    #
    # The LICENCE is the credential at the far end, so the box needs no second
    # secret and the customer is never asked to sign in. None when the cloud
    # says nothing, which the device reads as "leave the cache alone" rather
    # than "this customer has nobody".
    people_url: str | None = None
    # Per-device Cloudflare tunnel handed DOWN so an appliance can publish its HA
    # at its dealer-branded hostname over an outbound-only (CGNAT-safe) tunnel. The
    # licensing Function mints it server-side (keyed to the licence, so a
    # hardware swap keeps the same hostname) because the fleet ships as a
    # dd-cloned master image — a build-time token would clone identically onto
    # every box. Shape: {enabled, tunnel_token, tunnel_name, hostname}. None when
    # the cloud sends nothing (feature off / BYO / leave the device file
    # untouched — fail-open). {"enabled": false} with no token is the explicit
    # disable signal (deleted tunnel / kill-switch) telling the device to idle
    # and drop its token. Appliance-only; a BYO box never receives one.
    cloudflare_tunnel: dict | None = None
    # What this licence may NOT register, by scope — e.g.
    # ``{"category": [], "service_class": ["obd2"], "device": []}`` on Base.
    # Decided by the CLOUD: device gating is the licensing Function's call, not the
    # box's (feature gating stays in the integration's code).
    #
    # `None` means the cloud said nothing this cycle (an unreachable or synthetic
    # phone-home) and the device keeps its cached set. A dict with EMPTY LISTS is a
    # real answer meaning "nothing blocked", which is how an upgrade clears a block.
    blocked_devices: dict | None = None
    # Is the licence's SUPPORT CONTRACT live? The support year pays for the things
    # Sensa operates on the customer's behalf (remote access, Sensa-driven
    # updates, Radar Premium); the licence itself is perpetual and the van keeps
    # working either way.
    #
    # The DECISION only — the cloud never sends the expiry date, so nothing on the
    # van can render a countdown from it. The device's only use for this is to
    # write ONE customer-facing log entry when it changes.
    #
    # `None` = the cloud said nothing (an older Function, an unreachable or
    # synthetic phone-home). That must read as "unknown" and never as "lapsed":
    # treating silence as lapsed would announce the end of support to every
    # customer the moment they lost signal.
    support_active: bool | None = None
    # WHEN support ends, ISO-8601, or None if the cloud did not say.
    #
    # Added 2026-08-12, deliberately reversing the "decision, not the date" line
    # above it, for ONE job: letting the box shut its own tunnel down when the
    # date passes. Without it a lapsed customer who goes offline and comes back
    # regains remote access from the moment cloudflared reconnects until the next
    # successful check-in, which is up to `phone_home_interval_hours`.
    #
    # It does NOT become the authority. The cloud deleting the tunnel is still
    # the real control, because a date cached on the van can be defeated by
    # changing the van's clock. This is the belt to that pair of braces, and it
    # is a CACHE, not a claim: support dates are mutable, so a renewal is only
    # picked up at the next check-in, and the cache is only ever allowed to
    # RESTRICT. It must never extend access beyond what the cloud last said.
    #
    # Still not for display. "No countdown on the van" was a product decision,
    # not an accident: a van that nags about a support plan punishes a customer
    # for a choice they made deliberately.
    support_expires_at: str | None = None
    # Which kinds of device the owner has let a service company see, as
    # `[{device_type, provider}]`. The box sends readings for those and nothing
    # else. A LIST (even empty) is the cloud's answer, and an empty one stops
    # the box sending; `None` = the cloud said nothing this cycle, and the box
    # keeps what it had.
    service_consents: list | None = None


@dataclass
class LicenceStatus:
    """Shape returned by /api/license/status on the App.

    Matches ``licensing-api.md`` §4.4 exactly. Integrations read this to
    decide whether to gate writable entities or show the trial countdown.
    """

    valid: bool
    trial: bool
    licence_id: str | None
    editions: dict[str, str]
    trial_days_remaining: int | None
    expires_at: str | None  # ISO string
    last_phone_home: str | None  # ISO string
    phone_home_ok: bool
    read_only: bool = False
    message: str | None = None
    purchase_url: str | None = None
    # Deployment type the licence was sold for ("byo"/"enthusiast"/"appliance"/
    # None) + whether it mismatched this box's deployment_mode (the read-only
    # anti-arbitrage backstop).
    deployment_type: str | None = None
    deployment_type_blocked: bool = False
    # Dealer ownership claim, passed through verbatim from the JWT for the
    # Nomad dashboard's dealer-private home layouts. None for normal licences.
    dealer: dict[str, str] | None = None
    # Local ``/local/sensa/...`` path of the cached dealer logo (downloaded by
    # the App while online), or None. The Nomad header shows it in place of the
    # Sensa logo. Only set when it belongs to the current dealer claim.
    dealer_logo: str | None = None
    # Name of the cached dealer THEME (the top-level key of the downloaded
    # yaml), or None. The Nomad theme picker offers it (per theme_mode) and the
    # set_theme gate allows it. Only set when it belongs to the current dealer
    # claim (and never for trials).
    dealer_theme: str | None = None
    # Is the support contract live? Mirrors PhoneHomeResult.support_active, and
    # `None` means the same thing here: unknown, NOT lapsed. The integration reads
    # this only to log a one-shot message when it changes -- no dashboard shows
    # it, and no gate on the box depends on it (every support gate is enforced in
    # the cloud, where it cannot be bypassed).
    support_active: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RegistrationPayload:
    """Customer registration (section 8 of the system design)."""

    customer_name: str
    contact_email: str
    customer_id: str | None
    licence_key: str | None
    editions: dict[str, str] = field(default_factory=dict)
    software_version: str = ""
    registered_at: str = ""  # ISO UTC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- Helpers -----------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    """Parse ISO-8601 including trailing Z."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)
