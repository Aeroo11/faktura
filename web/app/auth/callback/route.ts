import * as client from "openid-client";
import { NextResponse, type NextRequest } from "next/server";
import { APP_URL, NoFakturaRole, endKeycloakSession, oidcConfig, refreshLifetime, sessionFromTokens } from "@/lib/oidc";
import { LOGIN_COOKIE, type LoginState, SESSION_COOKIE, cookieOptions, seal, unseal } from "@/lib/session";

function toLogin(error: string) {
  const res = NextResponse.redirect(`${APP_URL}/login?error=${error}`);
  res.cookies.delete(LOGIN_COOKIE);
  return res;
}

/** Keycloak sends the browser back here with a code; swap it for tokens. */
export async function GET(req: NextRequest) {
  const login = await unseal<LoginState>(req.cookies.get(LOGIN_COOKIE)?.value);
  if (!login) return toLogin("expired");

  // Rebuild the URL on APP_URL so it matches the registered redirect_uri exactly.
  const current = new URL(`${APP_URL}/auth/callback${req.nextUrl.search}`);
  let tokens: client.TokenEndpointResponse;
  try {
    tokens = await client.authorizationCodeGrant(await oidcConfig(), current, {
      pkceCodeVerifier: login.verifier,
      expectedState: login.state,
    });
  } catch (e) {
    console.error("sign-in failed:", e instanceof Error ? e.message : e);
    return toLogin("failed");
  }

  let session;
  try {
    session = sessionFromTokens(tokens);
  } catch (e) {
    if (tokens.refresh_token) await endKeycloakSession(tokens.refresh_token);
    return toLogin(e instanceof NoFakturaRole ? "role" : "failed");
  }

  const maxAge = refreshLifetime(tokens);
  const res = NextResponse.redirect(`${APP_URL}${login.returnTo}`);
  res.cookies.set(SESSION_COOKIE, await seal(session, maxAge), cookieOptions(maxAge));
  res.cookies.delete(LOGIN_COOKIE);
  return res;
}
