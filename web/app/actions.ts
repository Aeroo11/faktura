"use server";

import { revalidatePath } from "next/cache";
import { redirect, unstable_rethrow } from "next/navigation";
import { ApiError, postAction, postBankChange } from "@/lib/api";
import { type FieldKind, parseFieldValue } from "@/lib/fields";

// A "use server" module may only export async functions, so the initial form state
// lives in components/action-form.tsx. Type-only exports are erased and allowed.
export interface ActionState {
  ok: boolean;
  message: string;
}

function str(form: FormData, key: string): string {
  const v = form.get(key);
  return typeof v === "string" ? v : "";
}

function version(form: FormData): number | undefined {
  const v = Number(str(form, "version"));
  return Number.isFinite(v) && v > 0 ? v : undefined;
}

/** Turn an API failure into a message for the form (redirects pass through). */
function failure(e: unknown): ActionState {
  unstable_rethrow(e); // a redirect to /login when the session has ended
  if (e instanceof ApiError) {
    const prefix =
      e.status === 403 ? "Not allowed" : e.status === 409 ? "Conflict" : e.status === 422 ? "Invalid" : "Error";
    return { ok: false, message: `${prefix}: ${e.message}` };
  }
  return { ok: false, message: "Could not reach the Faktura API." };
}

/** Run one API action and turn domain errors into a message for the form. */
async function run(id: string, action: string, body: unknown, done: string): Promise<ActionState> {
  try {
    await postAction(id, action, body);
  } catch (e) {
    return failure(e);
  }
  revalidatePath(`/invoices/${id}`);
  revalidatePath("/");
  return { ok: true, message: done };
}

/** A bank-change action. Invoices it touches are re-decided, so every page refreshes. */
async function runChange(id: number | null, action: string, body: unknown, done: string): Promise<ActionState> {
  let changeId: number;
  try {
    changeId = (await postBankChange(id, action, body)).id;
  } catch (e) {
    return failure(e);
  }
  revalidatePath("/", "layout");
  if (id == null) redirect(`/bank-changes/${changeId}`);
  return { ok: true, message: done };
}

export async function requestBankChange(_: ActionState, form: FormData): Promise<ActionState> {
  return runChange(
    null,
    "",
    { source_invoice_id: str(form, "invoice_id"), channel: str(form, "channel") },
    "Bank-change request recorded.",
  );
}

export async function recordCallback(_: ActionState, form: FormData): Promise<ActionState> {
  const outcome = str(form, "outcome");
  return runChange(
    Number(str(form, "id")),
    "callback",
    { spoke_with: str(form, "spoke_with"), outcome, note: str(form, "note"), version: version(form) },
    outcome === "denied"
      ? "Recorded: the vendor denied the change. The request and the invoices paying into it are rejected."
      : "Recorded: the vendor confirmed. An approver who was not involved decides next.",
  );
}

export async function decideBankChange(_: ActionState, form: FormData): Promise<ActionState> {
  const approve = str(form, "decision") === "approve";
  return runChange(
    Number(str(form, "id")),
    approve ? "approve" : "reject",
    { note: str(form, "note"), version: version(form) },
    approve ? "Approved: the new account is on file and open invoices were re-checked." : "Rejected.",
  );
}

export async function correctField(_: ActionState, form: FormData): Promise<ActionState> {
  const parsed = parseFieldValue(str(form, "kind") as FieldKind, str(form, "value"));
  if (!parsed.ok) return { ok: false, message: parsed.error };
  return run(
    str(form, "id"),
    "corrections",
    { field: str(form, "field"), value: parsed.value, note: str(form, "note"), version: version(form) },
    "Correction saved; the invoice was re-checked.",
  );
}

export async function confirmField(_: ActionState, form: FormData): Promise<ActionState> {
  return run(
    str(form, "id"),
    "confirmations",
    { field: str(form, "field"), note: "checked against the page", version: version(form) },
    "Field confirmed; the invoice was re-checked.",
  );
}

export async function submitInvoice(_: ActionState, form: FormData): Promise<ActionState> {
  const hold = str(form, "decision") === "fraud_hold";
  const verification = hold
    ? {
        contact_name: str(form, "contact_name"),
        contact_channel: str(form, "contact_channel"),
        confirmed_details: str(form, "confirmed_details"),
      }
    : null;
  return run(
    str(form, "id"),
    "submit",
    { justification: str(form, "justification"), vendor_verification: verification, version: version(form) },
    "Submitted for approval.",
  );
}

export async function approveInvoice(_: ActionState, form: FormData): Promise<ActionState> {
  return run(str(form, "id"), "approve", { note: str(form, "note"), version: version(form) }, "Approved.");
}

export async function rejectInvoice(_: ActionState, form: FormData): Promise<ActionState> {
  return run(str(form, "id"), "reject", { reason: str(form, "reason"), version: version(form) }, "Rejected.");
}
