import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

export function StatTile({
  label,
  value,
  sub,
  icon: Icon,
  tone = "default",
}: {
  label: string;
  value: string;
  sub?: string;
  icon?: LucideIcon;
  tone?: "default" | "danger" | "accent";
}) {
  return (
    <div
      className={cn(
        "min-w-0 bg-surface px-4 py-4 sm:px-5",
        tone === "danger" && "border-l-2 border-danger",
        tone === "accent" && "border-l-2 border-accent bg-accent-soft",
      )}
    >
      <span className="inline-flex items-center gap-2 text-xs font-medium text-text-muted sm:text-sm">
        {Icon && <Icon size={15} strokeWidth={1.75} aria-hidden className="shrink-0 text-text-subtle" />}
        {label}
      </span>
      <div className="mt-1.5 text-[1.5rem] leading-tight font-semibold tracking-tight tabular-nums sm:text-[1.75rem]">
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-text-muted">{sub}</div>}
    </div>
  );
}
