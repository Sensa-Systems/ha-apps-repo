"""ES256 JWT verify + parse.

Offline validation: we hold the EC P-256 **public** key only. Never a
private key on-device. The private key lives in Azure Key Vault.

The encoding format is the standard JWS compact serialisation with
``alg: "ES256"`` — ECDSA on P-256 with SHA-256, which is the most common
JWT algorithm after HS256. The signature is a 64-byte concatenation of
``r || s`` (each 32 bytes); we convert to DER for ``cryptography``'s
``verify()``.

Was Ed25519 originally, switched because Azure Key Vault's standard SKU
doesn't support OKP/Ed25519 keys — only Managed HSM, which costs thousands
a month. ES256 is equivalent security at a sane price point.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from sensa_core.licensing.models import LicenceClaims


class LicenceJwtError(Exception):
    """Raised when a JWT is malformed, signed by the wrong key, or expired."""


_ES256_SIG_BYTES = 32


@dataclass
class _ParsedJws:
    header: dict[str, Any]
    payload: dict[str, Any]
    signature: bytes
    signed_input: bytes


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _raw_to_der_ecdsa(raw: bytes) -> bytes:
    """Convert JWS r||s format back to DER for cryptography.verify()."""
    if len(raw) != 2 * _ES256_SIG_BYTES:
        raise LicenceJwtError(f"signature wrong length for ES256: {len(raw)}")
    r = int.from_bytes(raw[:_ES256_SIG_BYTES], byteorder="big")
    s = int.from_bytes(raw[_ES256_SIG_BYTES:], byteorder="big")
    return encode_dss_signature(r, s)


def _split_compact(token: str) -> _ParsedJws:
    parts = token.split(".")
    if len(parts) != 3:
        raise LicenceJwtError("malformed: expected 3 segments")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except (ValueError, json.JSONDecodeError) as exc:
        raise LicenceJwtError(f"malformed: {exc}") from exc
    signed_input = f"{header_b64}.{payload_b64}".encode("ascii")
    return _ParsedJws(header, payload, signature, signed_input)


def _load_public_key(pem_or_raw: str | bytes) -> ec.EllipticCurvePublicKey:
    if isinstance(pem_or_raw, str):
        pem_or_raw = pem_or_raw.encode("utf-8")
    key = load_pem_public_key(pem_or_raw)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise LicenceJwtError("public key is not EC")
    if not isinstance(key.curve, ec.SECP256R1):
        raise LicenceJwtError(f"public key curve is not P-256: {key.curve.name}")
    return key


def verify_and_parse(
    token: str,
    public_key_pem: str | bytes,
    *,
    now: datetime | None = None,
    expected_product: str = "sensa_control",
) -> LicenceClaims:
    """Verify signature, expiry, product claim; return parsed claims.

    Raises ``LicenceJwtError`` on any validation failure. The caller is
    responsible for enforcing fingerprint binding — we only parse claims
    here; binding is a semantic check done against local state.
    """
    parsed = _split_compact(token)

    alg = parsed.header.get("alg")
    if alg != "ES256":
        raise LicenceJwtError(f"unsupported alg: {alg!r}")

    pub = _load_public_key(public_key_pem)
    try:
        pub.verify(
            _raw_to_der_ecdsa(parsed.signature),
            parsed.signed_input,
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature as exc:
        raise LicenceJwtError("signature invalid") from exc

    product = parsed.payload.get("product")
    if product != expected_product:
        raise LicenceJwtError(f"wrong product claim: {product!r}")

    now = now or datetime.now(timezone.utc)
    try:
        claims = LicenceClaims.from_payload(parsed.payload)
    except (KeyError, ValueError) as exc:
        raise LicenceJwtError(f"claims malformed: {exc}") from exc

    if claims.expires_at is not None and claims.expires_at <= now:
        raise LicenceJwtError("expired")
    if claims.trial and claims.trial_expires_at and claims.trial_expires_at <= now:
        raise LicenceJwtError("trial expired")

    return claims
