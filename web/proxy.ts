import * as client from "openid-client";
import { NextResponse, type NextRequest } from "next/server";
import { oidcConfig, refreshLifetime, sessionFromTokens } from "@/lib/oidc";
import { SESSION_COOKIE, type Session, cookieOptions, seal, unseal } from "@/lib/session";

const REFRESH_BEFORE_S = 30;

function signedOut(req: NextRequest) {
  if (req.nextUrl.pathname.startsWith("/api/")) {
    return new NextResponse("signed out", { status: 401 });
  }
  const login = new URL("/login", req.url);
  if (req.method === "GET") login.searchParams.set("returnTo", req.nextUrl.pathname + req.nextUrl.search);
  const res = NextResponse.redirect(login, 303);
  res.cookies.delete(SESSION_COOKIE);
  return res;
}

/**
 * Every page needs a session. Access tokens live 5 minutes; one about to expire is
 * refreshed here (the only place besides route handlers that may set cookies), and the
 * new one is passed on to the page rendering this same request. If Keycloak refuses
 * the refresh (session ended, user disabled, role removed) the user is signed out.
 */
export async function proxy(req: NextRequest) {
  const session = await unseal<Session>(req.cookies.get(SESSION_COOKIE)?.value);
  if (!session) return signedOut(req);
  if (session.expiresAt - Date.now() / 1000 > REFRESH_BEFORE_S) return NextResponse.next();

  let fresh: Session;
  let maxAge: number;
  try {
    const tokens = await client.refreshTokenGrant(await oidcConfig(), session.refreshToken);
    fresh = sessionFromTokens(tokens);
    maxAge = refreshLifetime(tokens);
  } catch {
    return signedOut(req);
  }
  const sealed = await seal(fresh, maxAge);
  req.cookies.set(SESSION_COOKIE, sealed);
  const res = NextResponse.next({ request: { headers: req.headers } });
  res.cookies.set(SESSION_COOKIE, sealed, cookieOptions(maxAge));
  return res;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|login|auth/).*)"],
};
