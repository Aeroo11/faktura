import * as client from "openid-client";
import { NextResponse, type NextRequest } from "next/server";
import { REDIRECT_URI, oidcConfig } from "@/lib/oidc";
import { safeReturnTo } from "@/lib/return-to";
import { LOGIN_COOKIE, cookieOptions, seal } from "@/lib/session";

/** Start the authorization code flow with PKCE: redirect to Keycloak's login page. */
export async function GET(req: NextRequest) {
  const config = await oidcConfig();
  const verifier = client.randomPKCECodeVerifier();
  const state = client.randomState();
  const url = client.buildAuthorizationUrl(config, {
    redirect_uri: REDIRECT_URI,
    scope: "openid profile",
    code_challenge: await client.calculatePKCECodeChallenge(verifier),
    code_challenge_method: "S256",
    state,
  });
  const res = NextResponse.redirect(url);
  const returnTo = safeReturnTo(req.nextUrl.searchParams.get("returnTo"));
  res.cookies.set(LOGIN_COOKIE, await seal({ verifier, state, returnTo }, 600), cookieOptions(600));
  return res;
}
