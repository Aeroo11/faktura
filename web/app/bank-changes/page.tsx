import type { Metadata } from "next";
import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { BankChangeBadge } from "@/components/bank-change-badge";
import { listBankChanges } from "@/lib/api";
import { formatTimestamp, groupDigits } from "@/lib/format";

export const metadata: Metadata = { title: "Bank changes" };

export default async function BankChangesPage() {
  const { items } = await listBankChanges();
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Vendor bank-account changes</h1>
        <p className="max-w-3xl text-sm text-text-muted">
          A new payee account is never accepted on the invoice that asks for it. Each change is recorded, confirmed
          by calling the vendor back on the number already on file, and approved by someone who did neither.
        </p>
      </div>
      <Card padded={false}>
        {items.length === 0 ? (
          <p className="px-5 py-10 text-center text-sm text-text-muted">
            No bank-change requests yet. They are opened from an invoice held for a changed bank account.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-border bg-surface-2 text-left text-xs text-text-muted">
                <tr>
                  <th className="px-4 py-2 font-medium">Request</th>
                  <th className="px-4 py-2 font-medium">Vendor</th>
                  <th className="px-4 py-2 font-medium">New account</th>
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="px-4 py-2 font-medium">Updated</th>
                  <th className="w-8" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {items.map((c) => (
                  <tr key={c.id} className="group hover:bg-surface-2">
                    <td className="px-4 py-2.5">
                      <Link href={`/bank-changes/${c.id}`} className="font-medium text-accent group-hover:underline">
                        #{c.id}
                      </Link>
                      <div className="text-xs text-text-subtle">by {c.requested_by}</div>
                    </td>
                    <td className="px-4 py-2.5">{c.vendor_id}</td>
                    <td className="px-4 py-2.5">
                      <div>{c.new.bank_name}</div>
                      <div className="font-mono text-xs text-text-muted">{groupDigits(c.new.bank_account)}</div>
                    </td>
                    <td className="px-4 py-2.5">
                      <BankChangeBadge status={c.status} />
                    </td>
                    <td className="px-4 py-2.5 text-xs whitespace-nowrap text-text-muted">
                      {formatTimestamp(c.updated_at)}
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
