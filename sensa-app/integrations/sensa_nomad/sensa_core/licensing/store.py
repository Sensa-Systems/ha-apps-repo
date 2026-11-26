"""Licence state persistence.

JSON file at ``/data/licensing.json`` (override via ``SENSA_LICENCE_DATA_PATH``
or the constructor). Atomic writes via temp-file + ``os.replace`` so a crash
mid-write can never leave a partially-written licence blob.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sensa_core.licensing.models import (
    FingerprintComponents,
    LicenceClaims,
    PhoneHomeResult,
    RegistrationPayload,
)

# The licence file DELIBERATELY does not follow `SENSA_STATE_PATH` into
# `/config` with the rest of the durable state (decided 2026-08-08).
#
# It briefly did, so the licence would be captured in a Home Assistant backup.
# Changing the path does not move the FILE: the App restarted, found no licence
# at the new location, and did what it is built to do without one, which is
# provision a 14-day trial. A working Vision licence was replaced by a trial on
# the test box.
#
# The upside was never worth that. The JWT has `hardware_id` baked in, so a
# restore onto DIFFERENT hardware prompts re-registration regardless, and that
# is the case people actually restore in. A same-hardware restore simply asks
# for the key again, which rebinds last-wins.
#
# So the licence keeps exactly one home, the one it has always had. Do not add
# it to the travelling set.
_DEFAULT_PATH = Path(os.environ.get("SENSA_LICENCE_DATA_PATH", "/data")) / "licensing.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LicensingStore:
    """Synchronous persistence for licence state.

    Schema (JSON):
        {
          "jwt": "eyJ...",
          "fingerprint": {
            "hash": "sha256:...",
            "components": {"mac": "...", ...}
          },
          "phone_home": {
            "last_at": "2026-04-23T10:00:00+00:00",
            "ok": true,
            "status": "valid",
            "next_before": "2026-04-24T10:00:00+00:00",
            "trial_days_remaining": 13
          },
          "registration": {
            "status": "complete" | "pending",
            "payload": {...},        # only while pending
            "submitted_at": "..."
          },
          "watermark": {
            "licence_id": "cust_jane",
            "fingerprint_hash": "sha256:...",
            "written_at": "2026-05-29T10:00:00+00:00"
          }
        }

    The watermark is a breadcrumb persisted into ``/data/`` so it travels in HA
    backups (Layer 5 of the licence-protection model, system-design.md §7).
    When a backup is restored on different hardware, the watermark's
    ``fingerprint_hash`` no longer matches the current host's fingerprint —
    visible to support diagnostics and to the customer-facing Backup card.
    The JWT's own ``hardware_id`` already gates licence validity; the
    watermark adds an audit trail that survives even when the JWT changes.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else _DEFAULT_PATH

    # -- IO -----------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".licensing-", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- Mutators -----------------------------------------------------------

    def set_jwt(self, jwt: str, claims: LicenceClaims) -> None:
        data = self.load()
        data["jwt"] = jwt
        data["claims_cache"] = {
            "customer_id": claims.customer_id,
            "trial": claims.trial,
            "editions": claims.editions,
            "expires_at": (
                claims.expires_at.isoformat() if claims.expires_at else None
            ),
            "trial_expires_at": (
                claims.trial_expires_at.isoformat() if claims.trial_expires_at else None
            ),
            "phone_home_interval_hours": claims.phone_home_interval_hours,
        }
        self.save(data)

    def clear_jwt(self) -> None:
        data = self.load()
        data.pop("jwt", None)
        data.pop("claims_cache", None)
        self.save(data)

    def set_fingerprint(self, fingerprint_hash: str, components: FingerprintComponents) -> None:
        data = self.load()
        data["fingerprint"] = {
            "hash": fingerprint_hash,
            "components": components.to_dict(),
        }
        self.save(data)

    def get_fingerprint(self) -> tuple[str, dict[str, str]] | None:
        fp = self.load().get("fingerprint")
        if not fp:
            return None
        return fp.get("hash", ""), dict(fp.get("components", {}))

    def record_phone_home(
        self, result: PhoneHomeResult, *, update_pin: bool = True
    ) -> None:
        data = self.load()
        prev = data.get("phone_home", {}) or {}
        data["phone_home"] = {
            "last_at": result.checked_at.isoformat(),
            "ok": result.ok,
            "status": result.status,
            "next_before": (
                result.next_phone_home_before.isoformat()
                if result.next_phone_home_before
                else None
            ),
            "trial_days_remaining": result.trial_days_remaining,
            "message": result.message,
            # The cloud's last word on a pending break-glass support grant. The
            # App surfaces it via /api/license/status and the integration
            # honours it; once the device opens support the cloud clears it, so
            # a later phone-home overwrites this with None.
            "remote_support_grant": result.remote_support_grant,
            # The cloud-backup credential, CARRIED FORWARD on any cycle that
            # was not an authoritative success.
            #
            # Losing signal must not drop a destination the customer is paying
            # for, and an unreachable `/validate` carries no credential at all.
            # Storing that `None` wiped a perfectly good SAS and the Cloud pill
            # disappeared from the card after a single failed check-in (test
            # box, 2026-08-10 19:14, `status: unreachable`). Same rule and same
            # reason as `target_version` above; the module docstring on the
            # device side already promised this behaviour, which is what made
            # the omission easy to miss.
            "cloud_backup": (
                result.cloud_backup if result.ok else prev.get("cloud_backup")
            ),
            # The staged-rollout version pin (the device's rollout-group target).
            # Persisted so an offline appliance still knows its pin across reboots.
            # Only a SUCCESSFUL, AUTHORITATIVE /validate response updates it: an
            # unreachable cycle carries no version, and a *synthetic* phone-home
            # (recorded on redeem/activate/trial, update_pin=False) likewise has
            # no real target — both carry the previous pin forward rather than
            # wiping it. An authoritative ok response uses result.target_version
            # verbatim (including None, which legitimately clears a removed pin).
            "target_version": (
                result.target_version
                if (update_pin and result.ok)
                else prev.get("target_version")
            ),
            # The Venus OS target for this device's Victron GX. Same carry-
            # forward rule and the same reason as the pin above: an unreachable
            # cycle carries no answer, and dropping the target there would stop
            # a ring mid-rollout every time the van lost signal.
            "cerbo_target_version": (
                result.cerbo_target_version
                if (update_pin and result.ok)
                else prev.get("cerbo_target_version")
            ),
            # The RutOS target for this device's Teltonika router. Same carry-
            # forward rule and the same reason again: the van that loses signal
            # mid-rollout must not drop out of its ring.
            "teltonika_target_version": (
                result.teltonika_target_version
                if (update_pin and result.ok)
                else prev.get("teltonika_target_version")
            ),
            "teltonika_switch_target_version": (
                result.teltonika_switch_target_version
                if (update_pin and result.ok)
                else prev.get("teltonika_switch_target_version")
            ),
            # Cloud-decided device gating (see PhoneHomeResult). Persisted so the
            # gate survives reboots and holds while offline — the box must not
            # forget it is a Base licence just because it lost signal. Only a real
            # answer (a dict) replaces it; an unreachable cycle or a synthetic
            # phone-home carries the previous set forward rather than wiping it.
            "blocked_devices": (
                result.blocked_devices
                if result.blocked_devices is not None
                else prev.get("blocked_devices")
            ),
            # The support contract's live/lapsed state, as last decided by the
            # cloud. Same carry-forward rule as blocked_devices, for the same
            # reason: only a real answer (a bool) replaces it, so an unreachable
            # cycle or a synthetic phone-home keeps the previous verdict instead
            # of wiping it to "unknown". Without the carry-forward, a box that
            # went offline would forget its support state and re-announce the
            # lapse to the customer the next time the cloud confirmed it.
            "support_active": (
                result.support_active
                if result.support_active is not None
                else prev.get("support_active")
            ),
            # WHEN support ends. Same carry-forward rule again, and it matters
            # more here than anywhere else on this list: this is the date the box
            # counts down against to close its own tunnel, so wiping it on an
            # unreachable cycle would hand a lapsed box its remote access back
            # every time it lost signal -- exactly the hole it exists to close.
            "support_expires_at": (
                result.support_expires_at
                if result.support_expires_at is not None
                else prev.get("support_expires_at")
            ),
            # Which devices the owner lets a service company see. Same
            # carry-forward rule: only a real answer (a list) replaces it, so an
            # unreachable cycle neither starts nor stops anything.
            "service_consents": (
                result.service_consents
                if result.service_consents is not None
                else prev.get("service_consents")
            ),
            # NB: the per-device Cloudflare tunnel is deliberately NOT persisted
            # here. The FILE /data/cloudflare.json is the single source of truth
            # cloudflared reads (and the status route surfaces via
            # cloudflare.status_block); nothing reads it back off the phone-home
            # record, so persisting it only duplicated the connector token at
            # rest in licensing.json. It is applied straight from the live
            # PhoneHomeResult by cloudflare.apply_phone_home_cloudflare.
        }
        self.save(data)

    def get_phone_home(self) -> dict[str, Any]:
        return dict(self.load().get("phone_home", {}))

    def set_registration_pending(self, payload: RegistrationPayload) -> None:
        data = self.load()
        data["registration"] = {
            "status": "pending",
            "payload": payload.to_dict(),
            "queued_at": _utc_now_iso(),
        }
        self.save(data)

    def set_registration_complete(self) -> None:
        data = self.load()
        data["registration"] = {
            "status": "complete",
            "submitted_at": _utc_now_iso(),
        }
        self.save(data)

    def get_registration(self) -> dict[str, Any]:
        return dict(self.load().get("registration", {}))

    def set_watermark(self, licence_id: str | None, fingerprint_hash: str) -> None:
        """Persist a backup watermark for restore-detection + support diagnostics.

        Called from the licence routes after every successful JWT operation, so
        the watermark always reflects the latest known-good (licence, hardware)
        pair. Lives in ``/data/licensing.json`` so it travels in HA backups.
        """
        data = self.load()
        data["watermark"] = {
            "licence_id": licence_id,
            "fingerprint_hash": fingerprint_hash,
            "written_at": _utc_now_iso(),
        }
        self.save(data)

    def get_watermark(self) -> dict[str, Any]:
        return dict(self.load().get("watermark", {}))

    # -- Accessors for consumers -------------------------------------------

    def get_jwt(self) -> str | None:
        return self.load().get("jwt")

    def get_claims_cache(self) -> dict[str, Any]:
        return dict(self.load().get("claims_cache", {}))
