"use client";

import { useState } from "react";
import { CheckCheck, Pencil } from "lucide-react";
import { confirmField, correctField } from "@/app/actions";
import { Button, FormMessage, Hidden, inputClass, useAction } from "@/components/action-form";
import { FIELDS, type FieldDef } from "@/lib/fields";
import { reasonLabel, rupiah } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { Decision, InvoiceDetail, InvoiceLine, Reason } from "@/lib/types";

const FLAG_STYLE: Record<Decision, string> = {
  approve: "",
  review_reading: "border-l-accent bg-accent-soft/60",
  business_exception: "border-l-warning bg-warning-soft/60",
  fraud_hold: "border-l-danger bg-danger-soft/60",
};
const SEVERITY: Decision[] = ["approve", "review_reading", "business_exception", "fraud_hold"];

function show(def: FieldDef, v: unknown): string {
  if (v == null || v === "") return "â€”";
  if (def.kind === "money") return rupiah(v as number);
  if (def.kind === "lines") return `${(v as InvoiceLine[]).length} line(s)`;
  return String(v);
}

function editText(def: FieldDef, v: unknown): string {
  if (v == null) return "";
  if (def.kind === "lines") return JSON.stringify(v, null, 2);
  return String(v);
}

function LinesTable({ lines }: { lines: InvoiceLine[] }) {
  return (
    <table className="mt-2 w-full text-xs">
      <thead className="text-text-subtle">
        <tr>
          <th className="py-1 text-left font-medium">Item</th>
          <th className="py-1 text-right font-medium">Qty</th>
          <th className="py-1 text-right font-medium">Unit price</th>
          <th className="py-1 text-right font-medium">Amount</th>
        </tr>
      </thead>
      <tbody>
        {lines.map((l, i) => (
          <tr key={i} className="border-t border-border">
            <td className="py-1 pr-2">{l.description}</td>
            <td className="py-1 text-right tabular-nums">{l.qty}</td>
            <td className="py-1 text-right tabular-nums whitespace-nowrap">{rupiah(l.unit_price)}</td>
            <td className="py-1 text-right tabular-nums whitespace-nowrap">{rupiah(l.amount)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FieldRow({
  def,
  inv,
  reasons,
  canEdit,
}: {
  def: FieldDef;
  inv: InvoiceDetail;
  reasons: Reason[];
  canEdit: boolean;
}) {
  const [editing, setEditing] = useState(false);
  // Close the editor from inside the action on success. The row must keep its identity
  // across the re-render that follows (see the key in FieldTable), or this feedback is lost.
  const [saveState, save, saving] = useAction(async (prev, form) => {
    const result = await correctField(prev, form);
    if (result.ok) setEditing(false);
    return result;
  });
  const [confirmState, confirm, confirming] = useAction(confirmField);
  const value = inv.reading[def.key];
  const modelValue = inv.model_reading[def.key];
  const changed = JSON.stringify(value) !== JSON.stringify(modelValue);
  const verified = inv.human_verified.includes(def.key);
  const worst = reasons.reduce<Decision>(
    (w, r) => (SEVERITY.indexOf(r.status) > SEVERITY.indexOf(w) ? r.status : w),
    "approve",
  );
  const base = { id: inv.id, field: def.key, kind: def.kind, version: inv.version };

  return (
    <li className={cn("border-l-2 border-l-transparent px-4 py-3", reasons.length > 0 && FLAG_STYLE[worst])}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-xs text-text-muted">
            {def.label}
            {verified && (
              <span className="inline-flex items-center gap-0.5 text-success">
                <CheckCheck size={13} aria-hidden /> checked by a person
              </span>
            )}
          </div>
          <div className={cn("mt-0.5 break-all", def.kind !== "text" && "font-mono tabular-nums")}>
            {show(def, value)}
          </div>
          {changed && (
            <div className="mt-0.5 text-xs text-text-subtle">
              model read: <span className="font-mono line-through">{show(def, modelValue)}</span>
            </div>
          )}
          {reasons.map((r) => (
            <div key={r.code} className="mt-1 text-xs text-text-muted">
              âš‘ {reasonLabel(r.code)}
            </div>
          ))}
        </div>
        {canEdit && !editing && (
          <div className="flex shrink-0 gap-1.5">
            {reasons.length > 0 && (
              <form action={confirm}>
                <Hidden values={base} />
                <Button tone="quiet" pending={confirming}>
                  Reading is correct
                </Button>
              </form>
            )}
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="inline-flex items-center gap-1 rounded-control border border-border px-2.5 py-1.5 text-sm hover:bg-surface-2"
            >
              <Pencil size={13} aria-hidden /> Correct
            </button>
          </div>
        )}
      </div>

      {def.kind === "lines" && <LinesTable lines={value as InvoiceLine[]} />}

      {editing && (
        <form action={save} className="mt-3 space-y-2">
          <Hidden values={base} />
          {def.kind === "lines" ? (
            <textarea name="value" rows={8} defaultValue={editText(def, value)} className={cn(inputClass, "font-mono text-xs")} />
          ) : (
            <input name="value" defaultValue={editText(def, value)} className={cn(inputClass, "font-mono")} aria-label={`New ${def.label}`} />
          )}
          <input name="note" placeholder="Note (what you checked)" className={inputClass} />
          <div className="flex gap-2">
            <Button pending={saving}>Save correction</Button>
            <button type="button" onClick={() => setEditing(false)} className="text-sm text-text-muted">
              Cancel
            </button>
          </div>
        </form>
      )}
      <div className="mt-2 space-y-1">
        <FormMessage state={saveState} />
        <FormMessage state={confirmState} />
      </div>
    </li>
  );
}

export function FieldTable({ inv, canEdit }: { inv: InvoiceDetail; canEdit: boolean }) {
  return (
    <ul className="divide-y divide-border">
      {FIELDS.map((def) => (
        <FieldRow
          // Keyed by field only: a version in the key would remount the row after every
          // save and wipe the success message the user is waiting for.
          key={def.key}
          def={def}
          inv={inv}
          reasons={inv.reasons.filter((r) => r.fields?.includes(def.key))}
          canEdit={canEdit}
        />
      ))}
    </ul>
  );
}
