"use client";

import { decideBankChange, recordCallback } from "@/app/actions";
import { Button, FormMessage, Hidden, inputClass, useAction } from "@/components/action-form";
import { groupDigits } from "@/lib/format";
import type { BankChangeDetail } from "@/lib/types";
import type { SessionUser } from "@/lib/session";

function CallbackForm({ change }: { change: BankChangeDetail }) {
  const [state, act, pending] = useAction(recordCallback);
  const phone = change.vendor.phone_on_file;
  if (!phone) {
    return (
      <p className="rounded-control bg-danger-soft px-3 py-2 text-sm text-danger">
        This vendor has no phone number on file. Fix the vendor master first. Never call a number from the invoice,
        an email, or a letter.
      </p>
    );
  }
  return (
    <form action={act} className="space-y-3">
      <Hidden values={{ id: change.id, version: change.version }} />
      <div className="rounded-control border border-border bg-surface-2 px-3 py-2">
        <div className="text-xs text-text-muted">Call the number on file in the vendor master</div>
        <div className="font-mono text-lg font-semibold tracking-wide">{phone}</div>
        <div className="text-xs text-text-muted">
          Not a number printed on the invoice, in the email, or in the letter: those come from whoever asked for the
          change. Ask the vendor to read their new account number to you, then compare it with{" "}
          <span className="font-mono">{groupDigits(change.new.bank_account)}</span>.
        </div>
      </div>
      <input name="spoke_with" required placeholder="Who did you speak to? Name and role" className={inputClass} />
      <fieldset className="flex flex-wrap gap-4 text-sm">
        <legend className="sr-only">Outcome</legend>
        <label className="inline-flex items-center gap-2">
          <input type="radio" name="outcome" value="confirmed" required /> Vendor confirmed this account
        </label>
        <label className="inline-flex items-center gap-2">
          <input type="radio" name="outcome" value="denied" /> Vendor did not change their account
        </label>
      </fieldset>
      <input name="note" placeholder="Note (optional)" className={inputClass} />
      <Button pending={pending}>Record call-back</Button>
      <FormMessage state={state} />
    </form>
  );
}

function DecideForm({ change }: { change: BankChangeDetail }) {
  const [state, act, pending] = useAction(decideBankChange);
  return (
    <form action={act} className="space-y-2">
      <Hidden values={{ id: change.id, version: change.version }} />
      <input name="note" placeholder="Note (required to reject)" className={inputClass} />
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          name="decision"
          value="approve"
          disabled={pending}
          className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent/90 disabled:opacity-60"
        >
          Approve change
        </button>
        <button
          type="submit"
          name="decision"
          value="reject"
          disabled={pending}
          className="rounded-control bg-danger px-3 py-1.5 text-sm font-medium text-white hover:bg-danger/90 disabled:opacity-60"
        >
          Reject change
        </button>
      </div>
      <FormMessage state={state} />
    </form>
  );
}

/** The next step for this user, or why it is someone else's. */
export function BankChangeActions({ change, user }: { change: BankChangeDetail; user: SessionUser }) {
  const involved = [change.requested_by, change.callback?.called_by].filter(Boolean);
  if (change.status === "awaiting_callback") {
    if (user.role !== "ap_staff") return <p className="text-sm text-text-muted">Waiting for AP staff to call the vendor.</p>;
    if (user.name === change.requested_by) {
      return (
        <p className="rounded-control bg-warning-soft px-3 py-2 text-sm text-warning">
          You recorded this request, so a colleague must make the call-back.
        </p>
      );
    }
    return <CallbackForm change={change} />;
  }
  if (change.status === "awaiting_approval") {
    if (user.role !== "approver") return <p className="text-sm text-text-muted">Waiting for an approver.</p>;
    if (involved.includes(user.name)) {
      return (
        <p className="rounded-control bg-warning-soft px-3 py-2 text-sm text-warning">
          You took part in this change, so another approver must decide.
        </p>
      );
    }
    return <DecideForm change={change} />;
  }
  return null;
}
