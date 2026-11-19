import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { AuditTimeline, ChainBadge } from "@/components/audit-timeline";
import { DecisionPanel } from "@/components/decision-panel";
import { FieldTable } from "@/components/field-table";
import { Card, CardTitle } from "@/components/ui/card";
import { DecisionBadge, StateBadge } from "@/components/ui/badge";
import { ApiError, currentUser, getInvoice, verifyChain } from "@/lib/api";
import { reasonLabel, rupiah } from "@/lib/format";
import { cn } from "@/lib/cn";

type Props = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: (await params).id };
}

const REASON_DOT = {
  approve: "bg-success",
  review_reading: "bg-accent",
  business_exception: "bg-warning",
  fraud_hold: "bg-danger",
} as const;

export default async function InvoicePage({ params }: Props) {
  const { id } = await params;
  const [inv, chain, user] = await Promise.all([
    getInvoice(id).catch((e) => {
      if (e instanceof ApiError && e.status === 404) notFound();
      throw e;
    }),
    verifyChain(),
    currentUser(),
  ]);
  const canEdit = inv.state === "needs_action" && user.role === "ap_staff";

  return (
    <div className="space-y-5">
      <Link href="/" className="inline-flex items-center gap-1 text-sm text-text-muted hover:text-text">
        <ArrowLeft size={15} aria-hidden /> Queue
      </Link>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">{inv.vendor_name ?? inv.id}</h1>
          <p className="text-sm text-text-muted">
            {inv.id} · {inv.invoice_number ?? "no invoice number"} ·{" "}
            <span className="tabular-nums">{rupiah(inv.total)}</span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <DecisionBadge decision={inv.decision} />
          <StateBadge state={inv.state} />
        </div>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <Card padded={false} className="self-start overflow-hidden lg:sticky lg:top-4">
          {/* eslint-disable-next-line @next/next/no-img-element -- dynamic proxied render, not a static asset */}
          <img src={`/api/invoices/${inv.id}/image`} alt={`Scanned invoice ${inv.id}`} className="w-full" />
        </Card>

        <div className="space-y-5">
          <Card>
            <CardTitle>Why it is here</CardTitle>
            {inv.reasons.length === 0 ? (
              <p className="text-sm text-success">All checks passed against PO, goods receipt, and vendor master.</p>
            ) : (
              <ul className="space-y-2 text-sm">
                {inv.reasons.map((r, i) => (
                  <li key={`${r.code}-${i}`} className="flex gap-2">
                    <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", REASON_DOT[r.status])} aria-hidden />
                    <span>
                      <span className="font-medium">{reasonLabel(r.code)}</span>
                      <span className="block text-xs text-text-muted">{r.message}</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card padded={false}>
            <div className="px-4 pt-4">
              <CardTitle
                aside={
                  canEdit ? <span className="text-xs text-text-muted">Check flagged fields against the page</span> : null
                }
              >
                What the invoice says
              </CardTitle>
            </div>
            <FieldTable inv={inv} canEdit={canEdit} />
          </Card>

          <Card>
            <CardTitle>Decision</CardTitle>
            <DecisionPanel inv={inv} user={user} />
          </Card>

          <Card>
            <CardTitle aside={<ChainBadge status={chain} />}>Audit trail</CardTitle>
            <AuditTimeline events={inv.audit} />
          </Card>
        </div>
      </div>
    </div>
  );
}
