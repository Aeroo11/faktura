import "server-only";

import * as client from "openid-client";
import { decodeJwt } from "jose";
import { type Session, roleFromClaims } from "./session";

/** Keycloak realm and this app's registration there (see keycloak/faktura-realm.json). */
const ISSUER = process.env.OIDC_ISSUER ?? "http://127.0.0.1:8180/realms/faktura";
const CLIENT_ID = process.env.OIDC_CLIENT_ID ?? "faktura-web";
export const APP_URL = (process.env.APP_URL ?? "http://127.0.0.1:3077").replace(/\/$/, "");
export const REDIRECT_URI = `${APP_URL}/auth/callback`;

let configPromise: Promise<client.Configuration> | null = null;

export function oidcConfig(): Promise<client.Configuration> {
  configPromise ??= (async () => {
    const secret = process.env.OIDC_CLIENT_SECRET;
    if (!secret) throw new Error("OIDC_CLIENT_SECRET must be set");
    const issuer = new URL(ISSUER);
    return client.discovery(issuer, CLIENT_ID, secret, undefined, {
      // Local Keycloak runs on plain HTTP; anything else must be HTTPS.
      execute: issuer.protocol === "http:" && isLoopback(issuer) ? [client.allowInsecureRequests] : [],
    });
  })().catch((e) => {
    configPromise = null; // retry discovery on the next request
    throw e;
  });
  return configPromise;
}

function isLoopback(url: URL): boolean {
  return url.hostname === "127.0.0.1" || url.hostname === "localhost";
}

export class NoFakturaRole extends Error {}

/** Turn a token response into a session. The role comes from the access token. */
export function sessionFromTokens(tokens: client.TokenEndpointResponse): Session {
  if (!tokens.refresh_token) throw new Error("Keycloak returned no refresh token");
  const claims = decodeJwt(tokens.access_token); // fresh from the token endpoint; the API verifies it
  const role = roleFromClaims(claims);
  const name = String(claims.preferred_username ?? "");
  if (!role || !name) throw new NoFakturaRole(name || "unknown user");
  return {
    user: { name, display: String(claims.given_name ?? claims.name ?? name), role },
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    expiresAt: Math.floor(Date.now() / 1000) + (tokens.expires_in ?? 60),
  };
}

/** Seconds until the Keycloak session (and so the refresh token) ends. */
export function refreshLifetime(tokens: client.TokenEndpointResponse): number {
  const v = Number((tokens as Record<string, unknown>).refresh_expires_in);
  return Number.isFinite(v) && v > 0 ? v : 1800;
}

/** End the Keycloak session from the server, so the next sign-in asks for a password again. */
export async function endKeycloakSession(refreshToken: string): Promise<void> {
  const config = await oidcConfig();
  const endpoint = config.serverMetadata().end_session_endpoint;
  if (!endpoint) return;
  const body = new URLSearchParams({
    client_id: CLIENT_ID,
    client_secret: process.env.OIDC_CLIENT_SECRET ?? "",
    refresh_token: refreshToken,
  });
  await fetch(endpoint, { method: "POST", body, cache: "no-store" }).catch(() => undefined);
}
