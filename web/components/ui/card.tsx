import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/** The one card shell (same recipe as TokenWatch). */
export function Card({
  as: As = "div",
  padded = true,
  className,
  children,
}: {
  as?: "div" | "section" | "article";
  padded?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <As className={cn("rounded-panel border border-border bg-surface shadow-panel", padded && "p-5", className)}>
      {children}
    </As>
  );
}

export function CardTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-4 flex items-center justify-between gap-3">
      <h2 className="text-sm font-semibold tracking-tight">{children}</h2>
      {aside}
    </div>
  );
}
