"use server";

import { revalidatePath } from "next/cache";
import { cookies } from "next/headers";
import { ApiError, currentUser, postAction } from "@/lib/api";
import { type FieldKind, parseFieldValue } from "@/lib/fields";
import { USER_COOKIE, findUser } from "@/lib/users";

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

/** Run one API action and turn domain errors into a message for the form. */
async function run(id: string, action: string, body: unknown, done: string): Promise<ActionState> {
  const user = await currentUser();
  try {
    await postAction(id, action, body, user);
  } catch (e) {
    if (e instanceof ApiError) {
      const prefix =
        e.status === 403 ? "Not allowed" : e.status === 409 ? "Conflict" : e.status === 422 ? "Invalid" : "Error";
      return { ok: false, message: `${prefix}: ${e.message}` };
    }
    return { ok: false, message: "Could not reach the Faktura API." };
  }
  revalidatePath(`/invoices/${id}`);
  revalidatePath("/");
  return { ok: true, message: done };
}

export async function switchUser(form: FormData): Promise<void> {
  const user = findUser(str(form, "user"));
  (await cookies()).set(USER_COOKIE, user.name, { path: "/", sameSite: "lax", httpOnly: true });
  revalidatePath("/", "layout");
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
