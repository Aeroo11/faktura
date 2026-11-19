import { CircleAlert, CircleCheck, Eye, ShieldAlert } from "lucide-react";
import { DECISION_LABEL, STATE_LABEL } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { Decision, State } from "@/lib/types";

const DECISION_STYLE: Record<Decision, string> = {
  approve: "bg-success-soft text-success",
  review_reading: "bg-accent-soft text-accent",
  business_exception: "bg-warning-soft text-warning",
  fraud_hold: "bg-danger-soft text-danger",
};

const DECISION_ICON = {
  approve: CircleCheck,
  review_reading: Eye,
  business_exception: CircleAlert,
  fraud_hold: ShieldAlert,
} as const;

/** Colour is never the only signal: every badge carries an icon and a word. */
export function DecisionBadge({ decision }: { decision: Decision }) {
  const Icon = DECISION_ICON[decision];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        DECISION_STYLE[decision],
      )}
    >
      <Icon size={13} strokeWidth={2} aria-hidden />
      {DECISION_LABEL[decision]}
    </span>
  );
}

export function StateBadge({ state }: { state: State }) {
  return (
    <span className="inline-flex items-center rounded-full border border-border-strong px-2 py-0.5 text-xs font-medium whitespace-nowrap text-text-muted">
      {STATE_LABEL[state]}
    </span>
  );
}
