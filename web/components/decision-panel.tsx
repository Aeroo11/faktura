"use client";

import { approveInvoice, rejectInvoice, submitInvoice } from "@/app/actions";
import { Button, FormMessage, Hidden, inputClass, useAction } from "@/components/action-form";
import { formatTimestamp } from "@/lib/format";
import type { InvoiceDetail } from "@/lib/types";
import type { DemoUser } from "@/lib/users";

function Reject({ inv }: { inv: InvoiceDetail }) {
  const [state, act, pending] = useAction(rejectInvoice);
  return (
    <form action={act} className="space-y-2 border-t border-border pt-4">
      <Hidden values={{ id: inv.id, version: inv.version }} />
      <input name="reason" required placeholder="Why reject? e.g. confirmed duplicate" className={inputClass} />
      <Button tone="danger" pending={pending}>
        Reject invoice
      </Button>
      <FormMessage state={state} />
    </form>
  );
}

function Submit({ inv }: { inv: InvoiceDetail }) {
  const [state, act, pending] = useAction(submitInvoice);
  const hold = inv.decision === "fraud_hold";
  return (
    <form action={act} className="space-y-2">
      <Hidden values={{ id: inv.id, version: inv.version, decision: inv.decision }} />
      <textarea
        name="justification"
        required
        rows={3}
        placeholder="Why should this be paid anyway? (sent to the approver)"
        className={inputClass}
      />
      {hold && (
        <fieldset className="space-y-2 rounded-control border border-danger/30 bg-danger-soft/40 p-3">
          <legend className="px-1 text-xs font-medium text-danger">Vendor verification (required for a fraud hold)</legend>
          <p className="text-xs text-text-muted">
            Call the vendor on contact details from the vendor master — never on details printed on this invoice.
          </p>
          <input name="contact_name" required placeholder="Who confirmed it" className={inputClass} />
          <input name="contact_channel" required placeholder="How, e.g. phone number from vendor master" className={inputClass} />
          <input name="confirmed_details" required placeholder="What they confirmed" className={inputClass} />
        </fieldset>
      )}
      <Button pending={pending}>Submit for approval</Button>
      <FormMessage state={state} />
    </form>
  );
}

function Approve({ inv }: { inv: InvoiceDetail }) {
  const [state, act, pending] = useAction(approveInvoice);
  return (
    <form action={act} className="space-y-2">
      <Hidden values={{ id: inv.id, version: inv.version }} />
      <input name="note" placeholder="Approval note (optional)" className={inputClass} />
      <Button pending={pending}>Approve payment</Button>
      <FormMessage state={state} />
    </form>
  );
}

/** Who closed the invoice, taken from the audit trail (survives reloads, unlike a toast). */
function ClosedNotice({ inv }: { inv: InvoiceDetail }) {
  if (inv.auto_approved) {
    return (
      <p className="rounded-control bg-success-soft px-3 py-2 text-sm text-success">
        Approved automatically: every check passed and no person changed the invoice.
      </p>
    );
  }
  const last = [...inv.audit].reverse().find((e) => e.action === "approved" || e.action === "rejected");
  const approved = inv.state === "approved";
  const note = last ? String((last.payload.note ?? last.payload.reason ?? "") as string) : "";
  return (
    <p
      className={
        approved
          ? "rounded-control bg-success-soft px-3 py-2 text-sm text-success"
          : "rounded-control bg-danger-soft px-3 py-2 text-sm text-danger"
      }
    >
      {approved ? "Approved" : "Rejected"}
      {last && (
        <>
          {" "}by <span className="font-medium">{last.actor}</span> · {formatTimestamp(last.at)}
        </>
      )}
      {note && <span className="block text-xs opacity-80">“{note}”</span>}
    </p>
  );
}

export function DecisionPanel({ inv, user }: { inv: InvoiceDetail; user: DemoUser }) {
  const open = inv.state === "needs_action" || inv.state === "awaiting_approval";
  const isMaker = inv.makers.includes(user.name);

  if (!open) return <ClosedNotice inv={inv} />;
  return (
    <div className="space-y-4">
      {inv.makers.length > 0 && (
        <p className="text-xs text-text-muted">
          Changed or submitted by: <span className="font-medium text-text">{inv.makers.join(", ")}</span>. They
          cannot approve it.
        </p>
      )}
      {inv.state === "needs_action" && user.role === "ap_staff" && <Submit inv={inv} />}
      {inv.state === "needs_action" && user.role === "approver" && (
        <p className="text-sm text-text-muted">Waiting for AP staff to check this invoice. Switch to Budi or Dewi to act.</p>
      )}
      {inv.state === "awaiting_approval" && user.role === "approver" && !isMaker && <Approve inv={inv} />}
      {inv.state === "awaiting_approval" && user.role === "approver" && isMaker && (
        <p className="rounded-control bg-warning-soft px-3 py-2 text-sm text-warning">
          You changed this invoice, so another approver must approve it (maker-checker).
        </p>
      )}
      {inv.state === "awaiting_approval" && user.role === "ap_staff" && (
        <p className="text-sm text-text-muted">Waiting for an approver. Switch to Sari or Rina to approve.</p>
      )}
      <Reject inv={inv} />
    </div>
  );
}
