import { ShieldCheck, ShieldX } from "lucide-react";
import { formatTimestamp } from "@/lib/format";
import type { AuditEntry, ChainStatus } from "@/lib/types";

const ACTION_LABEL: Record<string, string> = {
  received: "Invoice received",
  read: "Read by the model",
  decided: "Checked by the matching engine",
  state_changed: "State changed",
  field_corrected: "Field corrected",
  field_confirmed: "Field confirmed",
  submitted: "Submitted for approval",
  approved: "Approved",
  rejected: "Rejected",
};

function describe(e: AuditEntry): string {
  const p = e.payload as Record<string, unknown>;
  switch (e.action) {
    case "decided":
      return String(p.decision ?? "");
    case "state_changed":
      return `${p.from} → ${p.to}`;
    case "field_corrected":
      return `${p.field}: ${JSON.stringify(p.old)} → ${JSON.stringify(p.new)}${p.note ? ` (${p.note})` : ""}`;
    case "field_confirmed":
      return String(p.field ?? "");
    case "submitted":
      return String(p.justification ?? "");
    case "approved":
    case "rejected":
      return String(p.note ?? p.reason ?? "");
    default:
      return "";
  }
}

export function ChainBadge({ status }: { status: ChainStatus }) {
  return status.ok ? (
    <span className="inline-flex items-center gap-1 text-xs text-success">
      <ShieldCheck size={14} aria-hidden /> Hash chain intact · {status.events} events
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-xs font-medium text-danger">
      <ShieldX size={14} aria-hidden /> Chain broken at event {status.first_broken_seq}
    </span>
  );
}

export function AuditTimeline({ events }: { events: AuditEntry[] }) {
  return (
    <ol className="space-y-3">
      {events.map((e) => (
        <li key={e.seq} className="grid grid-cols-[auto_1fr] gap-x-3 text-sm">
          <span className="mt-1.5 size-2 rounded-full bg-border-strong" aria-hidden />
          <div className="min-w-0">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium">{ACTION_LABEL[e.action] ?? e.action}</span>
              <span className="text-xs text-text-muted">by {e.actor}</span>
              <span className="text-xs text-text-subtle">{formatTimestamp(e.at)}</span>
            </div>
            {describe(e) && <div className="mt-0.5 break-words text-xs text-text-muted">{describe(e)}</div>}
            <div className="mt-0.5 font-mono text-[10px] text-text-subtle" title={e.hash}>
              #{e.seq} · {e.hash.slice(0, 16)}…
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}
