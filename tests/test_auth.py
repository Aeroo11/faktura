"""Bearer-token checks against a locally generated RSA key (no Keycloak needed)."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from faktura.app import auth
from faktura.app.api import app
from faktura.app.db import Base, make_engine, make_sessionmaker

ISSUER = "http://idp.test/realms/faktura"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key=KEY, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": ["faktura-api", "account"],
        "sub": "u-1",
        "typ": "Bearer",
        "iat": now,
        "exp": now + 300,
        "preferred_username": "sari",
        "realm_access": {"roles": ["approver", "offline_access", "default-roles-faktura"]},
    } | overrides
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg)


@pytest.fixture()
def client():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    app.state.sessions = make_sessionmaker(engine)
    app.state.ctx = None  # these routes never decide anything
    app.state.auth = auth.OidcAuth(ISSUER, "faktura-api", resolve_key=lambda t: KEY.public_key())
    with TestClient(app) as c:
        yield c
    engine.dispose()


def get(client, tok):
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    return client.get("/summary", headers=headers)


def test_valid_token_is_accepted(client):
    assert get(client, token()).status_code == 200


@pytest.mark.parametrize(
    ("why", "tok"),
    [
        ("no token", None),
        ("signed by someone else", token(key=OTHER_KEY)),
        ("expired", token(iat=int(time.time()) - 900, exp=int(time.time()) - 600)),
        ("issued for another API", token(aud="other-api")),
        ("another realm", token(iss="http://idp.test/realms/other")),
        ("an ID token", token(typ="ID")),
        ("no expiry", token(exp=None)),
        ("alg none", jwt.encode({"iss": ISSUER, "aud": "faktura-api"}, None, algorithm="none")),
        ("not a JWT", "x.y.z"),
    ],
)
def test_bad_tokens_get_401(client, why, tok):
    r = get(client, tok)
    assert r.status_code == 401, why
    assert r.headers["www-authenticate"] == "Bearer"


def test_headers_are_ignored_in_oidc_mode(client):
    r = client.get("/summary", headers={"X-User": "sari", "X-Role": "approver"})
    assert r.status_code == 401


def test_dual_role_or_no_role_is_refused(client):
    both = token(realm_access={"roles": ["approver", "ap_staff"]})
    assert get(client, both).status_code == 403
    assert "only one" in get(client, both).json()["detail"]
    assert get(client, token(realm_access={"roles": ["offline_access"]})).status_code == 403


def test_claims_map_to_user():
    user = auth.user_from_claims(
        {"preferred_username": "budi", "realm_access": {"roles": ["ap_staff", "uma_authorization"]}}
    )
    assert (user.name, user.role) == ("budi", "ap_staff")


def test_idp_outage_is_503_not_401(client):
    def down(_):
        raise auth.IdpUnavailable("keycloak down")

    app.state.auth.resolve_key = down
    assert get(client, token()).status_code == 503


def test_auth_mode_defaults_to_oidc(monkeypatch):
    monkeypatch.delenv("FAKTURA_AUTH", raising=False)
    assert auth.auth_from_env().mode == "oidc"
    monkeypatch.setenv("FAKTURA_AUTH", "dev")
    assert auth.auth_from_env().mode == "dev"
    monkeypatch.setenv("FAKTURA_AUTH", "off")
    with pytest.raises(ValueError):
        auth.auth_from_env()
