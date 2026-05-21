"""Supabase Auth JWT helpers."""

from __future__ import annotations

import os
from typing import Any

import jwt
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import ExpiredSignatureError, InvalidAudienceError, InvalidTokenError, PyJWKClient
from jwt.exceptions import PyJWKClientError

bearer_scheme = HTTPBearer(auto_error=False)
_jwks_client: PyJWKClient | None = None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> dict[str, Any]:
    """Verify a Supabase access token and return its claims."""
    if credentials is None:
        raise _unauthorized("Missing Authorization bearer token.")
    if credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _unauthorized("Invalid Authorization header. Use Bearer token.")

    token = credentials.credentials

    try:
        claims = _decode_token(token)
    except ExpiredSignatureError as exc:
        raise _unauthorized("Authorization token has expired.") from exc
    except InvalidAudienceError as exc:
        raise _unauthorized("Authorization token has an invalid audience.") from exc
    except PyJWKClientError as exc:
        raise _unauthorized("Authorization signing key could not be found.") from exc
    except InvalidTokenError as exc:
        detail = str(exc) or "Authorization token is invalid."
        raise _unauthorized(f"Authorization token is invalid: {detail}") from exc
    except Exception as exc:
        raise _unauthorized(f"Authorization token could not be verified: {exc}") from exc

    if not claims.get("sub"):
        raise _unauthorized("Authorization token is missing a subject.")
    if not claims.get("email"):
        raise _unauthorized("Authorization token is missing an email.")
    return claims


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> dict[str, Any] | None:
    """Return verified claims when present, otherwise None."""
    if credentials is None:
        return None
    try:
        return get_current_user(credentials)
    except HTTPException:
        return None


def _decode_token(token: str) -> dict[str, Any]:
    header = jwt.get_unverified_header(token)
    algorithm = header.get("alg")

    if algorithm == "HS256":
        secret = os.getenv("SUPABASE_JWT_SECRET")
        if not secret:
            raise InvalidTokenError("SUPABASE_JWT_SECRET is required for legacy HS256 tokens.")
        return jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")

    if algorithm not in {"ES256", "RS256"}:
        raise InvalidTokenError(f"Unsupported token signing algorithm: {algorithm}")

    signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256", "RS256"],
        audience="authenticated",
        leeway=60,
    )


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(_jwks_url())
    return _jwks_client


def _jwks_url() -> str:
    override_url = os.getenv("SUPABASE_JWKS_URL")
    if override_url:
        return override_url

    supabase_url = os.getenv("SUPABASE_URL")
    if not supabase_url:
        raise InvalidTokenError("SUPABASE_URL is required for JWKS token verification.")
    return f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=message,
        headers={"WWW-Authenticate": "Bearer"},
    )
