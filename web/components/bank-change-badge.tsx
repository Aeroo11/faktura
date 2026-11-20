import { CircleCheck, Clock, PhoneCall, ShieldX } from "lucide-react";
import { BANK_CHANGE_STATUS_LABEL } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { BankChangeStatus } from "@/lib/types";

const STYLE: Record<BankChangeStatus, string> = {
  awaiting_callback: "bg-warning-soft text-warning",
  awaiting_approval: "bg-accent-soft text-accent",
  approved: "bg-success-soft text-success",
  rejected: "bg-danger-soft text-danger",
};

const ICON = {
  awaiting_callback: PhoneCall,
  awaiting_approval: Clock,
  approved: CircleCheck,
  rejected: ShieldX,
} as const;

export function BankChangeBadge({ status }: { status: BankChangeStatus }) {
  const Icon = ICON[status];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        STYLE[status],
      )}
    >
      <Icon size={13} strokeWidth={2} aria-hidden />
      {BANK_CHANGE_STATUS_LABEL[status]}
    </span>
  );
}
