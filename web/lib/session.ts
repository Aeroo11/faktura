import "server-only";

import { EncryptJWT, jwtDecrypt } from "jose";
import type { Role } from "./types";

/**
 * The signed-in user's session, kept in one encrypted, httpOnly cookie. The browser
 * holds it but cannot read it; only this server can decrypt it and use the tokens.
 */
export interface SessionUser {
  name: string; // preferred_username: the audit actor
  display: string;
  role: Role;
}

export interface Session {
  user: SessionUser;
  accessToken: string;
  refreshToken: string;
  /** Access-token expiry, epoch seconds. */
  expiresAt: number;
}

/** State carried through the redirect to Keycloak and back (PKCE verifier, CSRF state). */
export interface LoginState {
  verifier: string;
  state: string;
  returnTo: string;
}

export const SESSION_COOKIE = "faktura_session";
export const LOGIN_COOKIE = "faktura_login";
export const ROLES: readonly Role[] = ["ap_staff", "approver"];

let keyPromise: Promise<Uint8Array> | null = null;

/** 256-bit key derived from FAKTURA_SESSION_SECRET. */
function key(): Promise<Uint8Array> {
  keyPromise ??= (async () => {
    const secret = process.env.FAKTURA_SESSION_SECRET;
    if (!secret || secret.length < 32) {
      throw new Error("FAKTURA_SESSION_SECRET must be set (at least 32 characters)");
    }
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret));
    return new Uint8Array(digest);
  })();
  return keyPromise;
}

export async function seal(payload: object, maxAgeS: number): Promise<string> {
  return new EncryptJWT({ ...payload })
    .setProtectedHeader({ alg: "dir", enc: "A256GCM" })
    .setIssuedAt()
    .setExpirationTime(`${Math.max(1, Math.floor(maxAgeS))}s`)
    .encrypt(await key());
}

export async function unseal<T>(value: string | undefined): Promise<T | null> {
  if (!value) return null;
  try {
    const { payload } = await jwtDecrypt(value, await key());
    return payload as T;
  } catch {
    return null; // tampered, expired, or sealed with an old secret: treat as signed out
  }
}

/**
 * The Faktura role in an access token's realm roles. Exactly one, mirroring the API:
 * an account holding both roles could prepare and approve the same payment.
 */
export function roleFromClaims(claims: Record<string, unknown>): Role | null {
  const access = claims.realm_access as { roles?: unknown } | undefined;
  const roles = Array.isArray(access?.roles) ? access.roles : [];
  const ours = ROLES.filter((r) => roles.includes(r));
  return ours.length === 1 ? ours[0] : null;
}

export const cookieOptions = (maxAgeS: number) => ({
  httpOnly: true,
  sameSite: "lax" as const,
  secure: (process.env.APP_URL ?? "").startsWith("https://"),
  path: "/",
  maxAge: Math.max(1, Math.floor(maxAgeS)),
});
