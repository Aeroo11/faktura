"use client";

import { useActionState } from "react";
import type { ActionState } from "@/app/actions";
import { cn } from "@/lib/cn";

export const IDLE: ActionState = { ok: true, message: "" };

export function useAction(action: (s: ActionState, f: FormData) => Promise<ActionState>) {
  return useActionState(action, IDLE);
}

export function FormMessage({ state }: { state: ActionState }) {
  if (!state.message) return null;
  return (
    <p
      aria-live="polite"
      className={cn(
        "rounded-control px-2.5 py-1.5 text-xs",
        state.ok ? "bg-success-soft text-success" : "bg-danger-soft text-danger",
      )}
    >
      {state.message}
    </p>
  );
}

export function Hidden({ values }: { values: Record<string, string | number> }) {
  return (
    <>
      {Object.entries(values).map(([k, v]) => (
        <input key={k} type="hidden" name={k} value={String(v)} />
      ))}
    </>
  );
}

export const inputClass =
  "w-full rounded-control border border-border bg-surface px-2.5 py-1.5 text-sm focus:border-accent focus:outline-none";

export function Button({
  children,
  pending,
  tone = "primary",
}: {
  children: React.ReactNode;
  pending?: boolean;
  tone?: "primary" | "danger" | "quiet";
}) {
  return (
    <button
      type="submit"
      disabled={pending}
      className={cn(
        "rounded-control px-3 py-1.5 text-sm font-medium disabled:opacity-60",
        tone === "primary" && "bg-accent text-white hover:bg-accent/90",
        tone === "danger" && "bg-danger text-white hover:bg-danger/90",
        tone === "quiet" && "border border-border bg-surface text-text hover:bg-surface-2",
      )}
    >
      {pending ? "Working…" : children}
    </button>
  );
}
