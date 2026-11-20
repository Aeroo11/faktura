import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { AuditTimeline } from "@/components/audit-timeline";
import { BankChangeActions } from "@/components/bank-change-actions";
import { BankChangeBadge } from "@/components/bank-change-badge";
import { Card, CardTitle } from "@/components/ui/card";
import { DecisionBadge, StateBadge } from "@/components/ui/badge";
import { ApiError, currentUser, getBankChange } from "@/lib/api";
import { formatTimestamp, groupDigits, rupiah } from "@/lib/format";
import type { BankAccount, BankChangeDetail } from "@/lib/types";

type Props = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: `Bank change #${(await params).id}` };
}

function Account({ label, account, tone }: { label: string; account: BankAccount; tone: "old" | "new" }) {
  return (
    <div className={tone === "new" ? "rounded-control border border-danger/30 bg-danger-soft/40 p-3" : "rounded-control border border-border p-3"}>
      <div className="text-xs text-text-muted">{label}</div>
      <div className="text-sm font-medium">{account.bank_name || "none"}</div>
      <div className="font-mono text-base tracking-wide">{account.bank_account ? groupDigits(account.bank_account) : "none"}</div>
    </div>
  );
}

function Outcome({ change }: { change: BankChangeDetail }) {
  const cb = change.callback;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
      <dt className="text-text-muted">Recorded by</dt>
      <dd>
        {change.requested_by} · {formatTimestamp(change.created_at)}
      </dd>
      <dt className="text-text-muted">How it arrived</dt>
      <dd>{change.channel}</dd>
      {cb && (
        <>
          <dt className="text-text-muted">Call-back</dt>
          <dd>
            <span className={cb.outcome === "confirmed" ? "font-medium text-success" : "font-medium text-danger"}>
              {cb.outcome === "confirmed" ? "Vendor confirmed" : "Vendor denied the change"}
            </span>{" "}
            · {cb.called_by} called <span className="font-mono">{cb.phone}</span> (number on file) and spoke to{" "}
            {cb.spoke_with} · {formatTimestamp(cb.at)}
            {cb.note && <span className="block text-xs text-text-muted">“{cb.note}”</span>}
          </dd>
        </>
      )}
      {change.decided_by && change.status !== "awaiting_approval" && (
        <>
          <dt className="text-text-muted">Decided by</dt>
          <dd>
            {change.decided_by}
            {change.decision_note && <span className="text-text-muted"> · “{change.decision_note}”</span>}
          </dd>
        </>
      )}
      {change.erp_status && (
        <>
          <dt className="text-text-muted">Odoo</dt>
          <dd>
            {change.erp_status === "synced"
              ? "New account trusted in Odoo; the old one is no longer payable."
              : change.erp_status === "pending"
                ? "Waiting for faktura-erp-sync to trust the new account in Odoo."
                : `Needs attention: ${change.erp_error}`}
          </dd>
        </>
      )}
    </dl>
  );
}

export default async function BankChangePage({ params }: Props) {
  const id = Number((await params).id);
  if (!Number.isInteger(id)) notFound();
  const [change, user] = await Promise.all([
    getBankChange(id).catch((e) => {
      if (e instanceof ApiError && e.status === 404) notFound();
      throw e;
    }),
    currentUser(),
  ]);

  return (
    <div className="space-y-5">
      <Link href="/bank-changes" className="inline-flex items-center gap-1 text-sm text-text-muted hover:text-text">
        <ArrowLeft size={15} aria-hidden /> Bank changes
      </Link>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            Bank change #{change.id} · {change.vendor.name ?? change.vendor_id}
          </h1>
          <p className="text-sm text-text-muted">
            {change.vendor_id}
            {change.source_invoice_id && (
              <>
                {" "}· from invoice{" "}
                <Link href={`/invoices/${change.source_invoice_id}`} className="text-accent hover:underline">
                  {change.source_invoice_id}
                </Link>
              </>
            )}
          </p>
        </div>
        <BankChangeBadge status={change.status} />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-5">
          <Card>
            <CardTitle>Requested change</CardTitle>
            <div className="mb-4 grid items-center gap-3 sm:grid-cols-[1fr_auto_1fr]">
              <Account label="On file" account={change.old} tone="old" />
              <ArrowRight size={18} className="mx-auto hidden text-text-subtle sm:block" aria-hidden />
              <Account label="Requested" account={change.new} tone="new" />
            </div>
            <Outcome change={change} />
          </Card>

          <Card>
            <CardTitle>Next step</CardTitle>
            <BankChangeActions change={change} user={user} />
            {change.status === "approved" && (
              <p className="text-sm text-success">
                Approved. Faktura now pays this vendor only into the new account; invoices naming the old one are held.
              </p>
            )}
            {change.status === "rejected" && (
              <p className="text-sm text-danger">
                Rejected. Invoices asking to be paid into this account stay held
                {change.callback?.outcome === "denied" ? "; the open ones were rejected as likely fraud" : ""}.
              </p>
            )}
          </Card>
        </div>

        <div className="space-y-5">
          <Card padded={false}>
            <div className="px-4 pt-4">
              <CardTitle>Invoices naming the requested account</CardTitle>
            </div>
            {change.invoices.length === 0 ? (
              <p className="px-4 pb-4 text-sm text-text-muted">None.</p>
            ) : (
              <ul className="divide-y divide-border text-sm">
                {change.invoices.map((inv) => (
                  <li key={inv.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
                    <Link href={`/invoices/${inv.id}`} className="font-medium text-accent hover:underline">
                      {inv.id}
                    </Link>
                    <span className="tabular-nums">{rupiah(inv.total)}</span>
                    <span className="flex gap-1.5">
                      <DecisionBadge decision={inv.decision} />
                      <StateBadge state={inv.state} />
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card>
            <CardTitle>Audit trail</CardTitle>
            <AuditTimeline events={change.audit} />
          </Card>
        </div>
      </div>
    </div>
  );
}
