import Link from "next/link";
import { ChevronRight, Clock, Inbox, ShieldAlert, Zap } from "lucide-react";
import { Card } from "@/components/ui/card";
import { DecisionBadge } from "@/components/ui/badge";
import { StatTile } from "@/components/ui/stat-tile";
import { getSummary, listInvoices } from "@/lib/api";
import { DECISION_LABEL, STATE_LABEL, reasonLabel, rupiah, rupiahCompact } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { Decision, State, SummaryGroup } from "@/lib/types";

const TABS: State[] = ["needs_action", "awaiting_approval", "approved", "rejected"];
const DECISIONS: Decision[] = ["fraud_hold", "business_exception", "review_reading", "approve"];

function sum(groups: SummaryGroup[], pick: (g: SummaryGroup) => boolean) {
  const gs = groups.filter(pick);
  return { count: gs.reduce((a, g) => a + g.count, 0), total: gs.reduce((a, g) => a + g.total, 0) };
}

export default async function QueuePage({
  searchParams,
}: {
  searchParams: Promise<{ state?: string; decision?: string }>;
}) {
  const sp = await searchParams;
  const state = (TABS as string[]).includes(sp.state ?? "") ? (sp.state as State) : "needs_action";
  const decision = (DECISIONS as string[]).includes(sp.decision ?? "") ? (sp.decision as Decision) : undefined;

  const [{ groups }, { items }] = await Promise.all([getSummary(), listInvoices({ state, decision })]);
  const all = sum(groups, () => true);
  const open = sum(groups, (g) => g.state === "needs_action");
  const waiting = sum(groups, (g) => g.state === "awaiting_approval");
  const held = sum(groups, (g) => g.state === "needs_action" && g.decision === "fraud_hold");
  const auto = sum(groups, (g) => g.state === "approved" && g.decision === "approve");
  const tabCount = (s: State) => sum(groups, (g) => g.state === s).count;

  const href = (s: State, d?: Decision) => `/?state=${s}${d ? `&decision=${d}` : ""}`;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-panel border border-border bg-border shadow-panel lg:grid-cols-4">
        <StatTile icon={Inbox} label="Needs action" value={String(open.count)} sub={`${rupiahCompact(open.total)} waiting`} tone="accent" />
        <StatTile icon={ShieldAlert} label="Payment held" value={rupiahCompact(held.total)} sub={`${held.count} invoices on fraud hold`} tone="danger" />
        <StatTile icon={Clock} label="Awaiting approval" value={String(waiting.count)} sub={rupiahCompact(waiting.total)} />
        <StatTile
          icon={Zap}
          label="Approved untouched"
          value={all.count ? `${Math.round((100 * auto.count) / all.count)}%` : "—"}
          sub={`${auto.count} of ${all.count} invoices, no human step`}
        />
      </div>

      <Card padded={false}>
        <nav className="flex flex-wrap gap-1 border-b border-border px-3 pt-3" aria-label="Queue">
          {TABS.map((t) => (
            <Link
              key={t}
              href={href(t)}
              aria-current={t === state ? "page" : undefined}
              className={cn(
                "-mb-px border-b-2 px-3 py-2 text-sm font-medium",
                t === state ? "border-accent text-accent" : "border-transparent text-text-muted hover:text-text",
              )}
            >
              {STATE_LABEL[t]} <span className="ml-1 text-xs tabular-nums text-text-subtle">{tabCount(t)}</span>
            </Link>
          ))}
        </nav>

        <div className="flex flex-wrap items-center gap-2 px-4 py-3 text-sm">
          <span className="text-text-muted">Decision:</span>
          <Link href={href(state)} className={cn("rounded-full px-2.5 py-0.5", !decision ? "bg-text text-surface" : "bg-surface-2 text-text-muted")}>
            All
          </Link>
          {DECISIONS.map((d) => (
            <Link
              key={d}
              href={href(state, d)}
              className={cn("rounded-full px-2.5 py-0.5", decision === d ? "bg-text text-surface" : "bg-surface-2 text-text-muted")}
            >
              {DECISION_LABEL[d]}
            </Link>
          ))}
        </div>

        {items.length === 0 ? (
          <p className="px-5 py-10 text-center text-sm text-text-muted">Nothing here.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-y border-border bg-surface-2 text-left text-xs text-text-muted">
                <tr>
                  <th className="px-4 py-2 font-medium">Invoice</th>
                  <th className="px-4 py-2 font-medium">Vendor</th>
                  <th className="px-4 py-2 text-right font-medium">Total</th>
                  <th className="px-4 py-2 font-medium">Decision</th>
                  <th className="px-4 py-2 font-medium">Why</th>
                  <th className="w-8" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {items.map((inv) => (
                  <tr key={inv.id} className="group hover:bg-surface-2">
                    <td className="px-4 py-2.5">
                      <Link href={`/invoices/${inv.id}`} className="font-medium text-accent group-hover:underline">
                        {inv.id}
                      </Link>
                      <div className="font-mono text-xs text-text-subtle">{inv.invoice_number ?? "—"}</div>
                    </td>
                    <td className="max-w-56 truncate px-4 py-2.5">{inv.vendor_name ?? "—"}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{rupiah(inv.total)}</td>
                    <td className="px-4 py-2.5">
                      <DecisionBadge decision={inv.decision} />
                    </td>
                    <td className="max-w-80 px-4 py-2.5 text-xs text-text-muted">
                      {inv.reason_codes.length ? inv.reason_codes.map(reasonLabel).join(" · ") : "All checks passed"}
                    </td>
                    <td className="pr-3 text-text-subtle">
                      <ChevronRight size={16} aria-hidden />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
