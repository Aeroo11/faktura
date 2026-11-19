import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { SESSION_COOKIE, type Session, type SessionUser, unseal } from "./session";
import type { ChainStatus, InvoiceDetail, InvoiceSummary, SummaryGroup } from "./types";

/** The browser never talks to FastAPI; every call goes through the Next server. */
export const API_URL = process.env.FAKTURA_API_URL ?? "http://127.0.0.1:8077";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** The signed-in session. proxy.ts has already sent anyone without one to /login. */
async function session(): Promise<Session> {
  const s = await unseal<Session>((await cookies()).get(SESSION_COOKIE)?.value);
  if (!s) redirect("/login");
  return s;
}

/** The user if signed in, for chrome shown on every page including /login. */
export async function signedInUser(): Promise<SessionUser | null> {
  return (await unseal<Session>((await cookies()).get(SESSION_COOKIE)?.value))?.user ?? null;
}

export async function currentUser(): Promise<SessionUser> {
  return (await session()).user;
}

/** Every call carries the user's Keycloak access token; FastAPI verifies it. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${(await session()).accessToken}`);
  if (init.body) headers.set("Content-Type", "application/json");
  return fetch(`${API_URL}${path}`, { ...init, headers, cache: "no-store" });
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init);
  if (res.status === 401) redirect("/login?error=expired");
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export function listInvoices(params: { state?: string; decision?: string; limit?: number }) {
  const q = new URLSearchParams();
  if (params.state) q.set("state", params.state);
  if (params.decision) q.set("decision", params.decision);
  q.set("limit", String(params.limit ?? 500));
  return request<{ items: InvoiceSummary[] }>(`/invoices?${q}`);
}

export const getInvoice = (id: string) =>
  request<InvoiceDetail>(`/invoices/${encodeURIComponent(id)}`);

export const getSummary = () => request<{ groups: SummaryGroup[] }>("/summary");

export const verifyChain = () => request<ChainStatus>("/audit/verify");

export function postAction(id: string, action: string, body: unknown) {
  return request<InvoiceDetail>(`/invoices/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
