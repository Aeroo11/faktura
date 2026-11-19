"""Who is calling the API.

`oidc` (the default): the caller sends a Keycloak access token as `Authorization: Bearer`.
The token's signature is checked against the realm's published keys, and so are its
issuer, audience (`faktura-api`), and expiry. The username becomes the audit actor; the
realm role decides what the user may do.

`dev`: the caller states who they are in X-User / X-Role headers. Only for tests and
local scripts, and only when FAKTURA_AUTH=dev is set explicitly, so a missing setting
never silently turns authentication off.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

from faktura.app import service

KeyResolver = Callable[[str], Any]  # token -> public key


class Unauthenticated(Exception):
    """No valid identity: 401."""


class NotPermitted(Exception):
    """A valid identity that may not use Faktura: 403."""


class IdpUnavailable(Exception):
    """The identity provider's keys could not be fetched: 503."""


class Authenticator(Protocol):
    mode: str

    def user(self, bearer: str | None, x_user: str | None, x_role: str | None) -> service.User: ...


def user_from_claims(claims: dict[str, Any]) -> service.User:
    """Map token claims to a Faktura user. Exactly one Faktura role, or no access.

    Someone who holds both roles could prepare an invoice and approve it under two
    sessions, so a dual-role account is refused outright: segregation of duties is
    enforced at the identity provider as well as in the workflow.
    """
    name = claims.get("preferred_username")
    if not isinstance(name, str) or not name:
        raise Unauthenticated("token has no preferred_username")
    realm_roles = (claims.get("realm_access") or {}).get("roles") or []
    roles = sorted(set(realm_roles) & set(service.ROLES))
    if not roles:
        raise NotPermitted(f"{name} has no Faktura role")
    if len(roles) > 1:
        raise NotPermitted(f"{name} holds {' and '.join(roles)}; one person may hold only one")
    return service.User(name=name, role=roles[0])


@dataclass
class OidcAuth:
    issuer: str
    audience: str
    resolve_key: KeyResolver
    leeway_s: int = 30
    mode: str = "oidc"

    @classmethod
    def from_env(cls) -> OidcAuth:
        issuer = os.environ.get("OIDC_ISSUER", "http://127.0.0.1:8180/realms/faktura").rstrip("/")
        jwks_url = os.environ.get("OIDC_JWKS_URL", f"{issuer}/protocol/openid-connect/certs")
        # Keys are cached; an unknown key id (after rotation) triggers one refetch.
        jwks = jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300, timeout=5)

        def resolve(token: str) -> Any:
            try:
                return jwks.get_signing_key_from_jwt(token).key
            except jwt.PyJWKClientConnectionError as e:
                raise IdpUnavailable(f"cannot fetch signing keys from {jwks_url}") from e
            except jwt.PyJWKClientError as e:
                raise Unauthenticated(f"token signed with an unknown key: {e}") from e

        return cls(
            issuer=issuer,
            audience=os.environ.get("OIDC_AUDIENCE", "faktura-api"),
            resolve_key=resolve,
        )

    def claims(self, token: str) -> dict[str, Any]:
        try:
            key = self.resolve_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as e:
            raise Unauthenticated(f"invalid token: {e}") from e
        if claims.get("typ", "Bearer") != "Bearer":  # Keycloak marks ID and refresh tokens
            raise Unauthenticated(f"not an access token (typ={claims.get('typ')})")
        return claims

    def user(self, bearer: str | None, x_user: str | None, x_role: str | None) -> service.User:
        if not bearer:
            raise Unauthenticated("missing bearer token")
        return user_from_claims(self.claims(bearer))


@dataclass
class DevHeaderAuth:
    """DEV ONLY: trusts the caller's X-User / X-Role headers."""

    mode: str = "dev"

    def user(self, bearer: str | None, x_user: str | None, x_role: str | None) -> service.User:
        if not x_user or not x_role:
            raise Unauthenticated("dev auth: send X-User and X-Role")
        if x_role not in service.ROLES:
            raise NotPermitted(f"unknown role {x_role!r}")
        return service.User(name=x_user, role=x_role)


def auth_from_env() -> Authenticator:
    mode = os.environ.get("FAKTURA_AUTH", "oidc")
    if mode == "oidc":
        return OidcAuth.from_env()
    if mode == "dev":
        return DevHeaderAuth()
    raise ValueError(f"unknown FAKTURA_AUTH {mode!r}; use oidc or dev")
