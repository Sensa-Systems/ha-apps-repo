"""Licensing — trial / activation / phone-home, Ed25519 JWT, fingerprint, persistence.

Sync API so it can be imported from both the stdlib-HTTP-server App and,
via ``hass.async_add_executor_job``, the Home Assistant integrations.
"""

from sensa_core.licensing.client import LicensingClient
from sensa_core.licensing.deployment import (
    DEPLOYMENT_TYPES,
    is_appliance_deployment,
    normalise_deployment,
)
from sensa_core.licensing.fingerprint import compute_fingerprint
from sensa_core.licensing.jwt import LicenceJwtError, verify_and_parse
from sensa_core.licensing.limits import (
    edition_device_cap,
    normalise_tier,
    resolve_device_cap,
)
from sensa_core.licensing.models import (
    CloudUnavailableError,
    FingerprintComponents,
    LicenceClaims,
    LicenceInvalidError,
    LicenceStatus,
    PhoneHomeResult,
    RegistrationPayload,
    TrialExhaustedError,
)
from sensa_core.licensing.store import LicensingStore

__all__ = [
    "DEPLOYMENT_TYPES",
    "CloudUnavailableError",
    "FingerprintComponents",
    "LicenceClaims",
    "LicenceInvalidError",
    "LicenceJwtError",
    "LicenceStatus",
    "LicensingClient",
    "LicensingStore",
    "PhoneHomeResult",
    "RegistrationPayload",
    "TrialExhaustedError",
    "compute_fingerprint",
    "edition_device_cap",
    "is_appliance_deployment",
    "normalise_deployment",
    "normalise_tier",
    "resolve_device_cap",
    "verify_and_parse",
]
