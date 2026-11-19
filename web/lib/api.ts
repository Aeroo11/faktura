import "server-only";

import { cookies } from "next/headers";
import type { ChainStatus, InvoiceDetail, InvoiceSummary, SummaryGroup } from "./types";
import { type DemoUser, USER_COOKIE, findUser } from "./users";

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

export async function currentUser(): Promise<DemoUser> {
  const store = await cookies();
  return findUser(store.get(USER_COOKIE)?.value);
}

async function request<T>(path: string, init: RequestInit = {}, user?: DemoUser): Promise<T> {
  const headers = new Headers(init.headers);
  if (user) {
    headers.set("X-User", user.name);
    headers.set("X-Role", user.role);
  }
  if (init.body) headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}${path}`, { ...init, headers, cache: "no-store" });
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

export function postAction(id: string, action: string, body: unknown, user: DemoUser) {
  return request<InvoiceDetail>(
    `/invoices/${encodeURIComponent(id)}/${action}`,
    { method: "POST", body: JSON.stringify(body) },
    user,
  );
}
