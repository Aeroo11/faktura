import { NextResponse, type NextRequest } from "next/server";
import { APP_URL, endKeycloakSession } from "@/lib/oidc";
import { SESSION_COOKIE, type Session, unseal } from "@/lib/session";

/** POST only, so a link or image on another site cannot sign the user out. */
export async function POST(req: NextRequest) {
  const session = await unseal<Session>(req.cookies.get(SESSION_COOKIE)?.value);
  if (session) await endKeycloakSession(session.refreshToken);
  const res = NextResponse.redirect(`${APP_URL}/login?signed_out=1`, 303);
  res.cookies.delete(SESSION_COOKIE);
  return res;
}
