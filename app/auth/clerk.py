"""Clerk session JWT verification (JWKS, no Clerk Python SDK)."""

from __future__ import annotations

import jwt
from jwt import PyJWKClient
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError, PyJWKClientError

from app.core.config import settings

_jwks_client: PyJWKClient | None = None
_jwks_client_url: str = ""


class InvalidClerkTokenError(ValueError):
    """Raised when a Clerk session JWT is missing, expired, or invalid."""


def clerk_jwks_url() -> str:
    if settings.clerk_jwks_url:
        return settings.clerk_jwks_url.rstrip("/")
    if not settings.clerk_issuer:
        raise InvalidClerkTokenError("Clerk issuer is not configured")
    return settings.clerk_issuer.rstrip("/") + "/.well-known/jwks.json"


def _jwks_client_for(url: str) -> PyJWKClient:
    global _jwks_client, _jwks_client_url
    if _jwks_client is None or _jwks_client_url != url:
        _jwks_client = PyJWKClient(url, cache_jwk_set=True, lifespan=300)
        _jwks_client_url = url
    return _jwks_client


def verify_clerk_session_token(token: str) -> str:
    """Verify a Clerk session JWT and return the user id (`sub` claim).

    JWKS URL: Frontend API URL + `/.well-known/jwks.json` (Clerk manual JWT
    verification docs). Signature, expiry, and issuer are checked. `azp`
    is checked against clerk_authorized_parties when that list is set.
    """
    if not token or not token.strip():
        raise InvalidClerkTokenError("Missing session token")
    token = token.strip()
    if not settings.clerk_issuer:
        raise InvalidClerkTokenError("Clerk issuer is not configured")

    try:
        signing_key = _jwks_client_for(clerk_jwks_url()).get_signing_key_from_jwt(
            token
        )
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer.rstrip("/"),
            options={"require": ["exp", "sub"], "verify_aud": False},
        )
    except ExpiredSignatureError as exc:
        raise InvalidClerkTokenError("Token expired") from exc
    except (InvalidTokenError, PyJWKClientError, OSError) as exc:
        raise InvalidClerkTokenError("Invalid session token") from exc

    allowed = settings.clerk_authorized_party_list()
    azp = claims.get("azp")
    if allowed and azp and azp not in allowed:
        raise InvalidClerkTokenError("Invalid authorized party")

    sub = claims.get("sub")
    if not sub:
        raise InvalidClerkTokenError("Token missing sub")
    return str(sub)
